from __future__ import annotations

import argparse
import difflib
import json
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    Character,
    GenerationTask,
    KnowledgeEmbedding,
    PlotThread,
    PlatformFeedback,
    PublishingTarget,
    QualityReport,
    PowerSystem,
    StoryBible,
    StoryFoundation,
    StoryArc,
    VisualAsset,
    Volume,
    WorldRule,
)
from app.services.agent_plan_intelligence import (
    create_market_research_pack,
    create_visual_asset,
    ensure_market_research_evidence,
    index_book_knowledge,
    ingest_market_research_results,
    list_visual_assets,
    run_agent_plan_enhancement_cycle,
)
from app.services.aesthetic_profile import apply_revision_idea_to_repair_payload, apply_revision_idea_to_skeleton, build_aesthetic_profile_block, merge_style_with_aesthetic_profile, profile_from_story_text, story_bible_display_fields, strip_aesthetic_profile_blocks
from app.services.web_search import run_market_web_search, web_search_status
from app.services.canon import add_character, add_plot_thread, add_power_system, add_world_rule, format_canon_context
from app.services.dashboard import build_project_snapshot
from app.services.dashboard_bootstrap import bootstrap_book_production
from app.services.db_ops import (
    check_database_health,
    check_schema_version,
    create_database_backup,
    list_database_backups,
    restore_database_from_backup,
)
from app.services.evidence import add_evidence_source, add_market_signal, audit_market_evidence, format_market_evidence_context
from app.services.feedback import (
    apply_feedback_adjustment_to_brief,
    create_feedback_adjustment,
    format_author_preference_context,
    list_feedback_adjustments,
    list_platform_feedback,
    record_author_preference,
    record_platform_feedback,
    submit_revision_suggestion,
    summarize_platform_feedback,
)
from app.services.llm_audit import llm_failure_suggestion, list_llm_request_logs, summarize_llm_failures, summarize_llm_usage
from app.services.llm_costs import summarize_llm_cost
from app.services.live_llm import run_live_llm_smoke
from app.services.bias import evaluate_generation_bias
from app.services.author_workbench import build_author_workbench_report
from app.services.book_development import clean_generated_text, develop_new_book_from_inspiration
from app.services.chapter_samples import (
    adopt_chapter_sample,
    build_chapter_sample_learning,
    generate_chapter_samples,
    latest_chapter_samples,
    sync_chapter_sample_learning,
)
from app.services.dashboard_production_actions import repair_chapter_brief, restart_production_from_chapter
from app.services.author_runner import author_background_timeout_seconds, author_terminal_status, run_author_mode
from app.services.failure_attribution import attribute_generation_failure
from app.services.intent_acceptance import evaluate_author_intent
from app.services.model_strategy import build_model_strategy
from app.services.production_packet import build_chapter_production_packet
from app.services.production_run_review import build_production_pattern_memory, latest_production_run_review, production_run_review_payload
from app.services.production_router import prepare_production
from app.services.production_scaffold import repair_production_scaffold
from app.services.publish_preflight import build_publish_preflight
from app.services.readiness import check_production_readiness
from app.services.revision_intent import extract_revision_decision
from app.services.story_dna import (
    build_story_dna_from_development,
    build_story_dna_from_skeleton,
    story_dna_display_fields,
    story_dna_for_book,
    strip_story_dna_blocks,
)
from app.services.skeleton_governance import (
    audit_skeleton_sources,
    audit_story_skeleton_with_agent_evidence,
    repair_skeleton_until_pass,
    repair_story_skeleton_with_market_evidence,
)
from app.services.skeleton_normalizer import normalize_story_skeleton_payload
from app.services.skeleton_context_reset import reset_context_after_skeleton_approval
from app.services.skeleton_sync import (
    current_skeleton_values as canonical_current_skeleton_values,
    list_skeleton_versions,
    propagate_core_term_changes,
    record_skeleton_version,
    synchronize_skeleton_derivatives,
)
from app.services.writer_loop import build_writer_loop_plan
from app.llm.providers import ArkOpenAIProvider
from app.services.llm_queue import (
    QUEUE_TYPES,
    build_generation_queue_health,
    cancel_generation_queue_task,
    pause_generation_queue_task,
    recover_stale_generation_tasks,
    resume_generation_queue_task,
    retry_generation_queue_task,
    run_generation_queue,
    run_generation_queue_task,
)
from app.services.planning import AUTO_ACTIONS, create_chapter_plan, plan_chapters, run_next_action, upgrade_chapter_briefs_production_standards
from app.services.production import (
    approve_chapter,
    auto_prepare_publish_job,
    create_book,
    create_foundation,
    execute_publish_job,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    seed_prompts,
    upsert_publishing_target,
)
from app.services.continuity import default_chapter_continuity_summary, latest_version_for_chapter, record_chapter_continuity
from app.services.story import create_story_arc, create_volume, format_story_control_context, get_story_bible, upsert_story_bible
from app.services.story_alignment import build_story_alignment_audit
from app.dashboard_assets import DASHBOARD_HTML as HTML
from app.dashboard_payloads import (
    _approval_revision_label,
    _approval_revision_mode_from_level,
    _approval_revision_text,
    _brief_payload,
    _database_payload,
    _failed_tasks_payload,
    _generation_tasks_for_chapter,
    _llm_usage_payload,
    _loads_json,
    _parse_feedback_ids,
    _publishing_payload,
    _quality_payload,
    _version_diff_payload,
)


_BACKGROUND_LOCK = threading.Lock()
_DB_WRITE_LOCK = threading.RLock()
_BACKGROUND_RUNS: dict[str, dict] = {}


def _start_background_queue_run(*, max_tasks: int = 1, book_id: int = 0, chapter_number: int = 0) -> dict:
    if max_tasks < 1 or max_tasks > 3:
        raise ValueError("max_tasks must be between 1 and 3")
    selected_task_id = None
    if book_id or chapter_number:
        with session_scope() as session:
            selected_task_id = _pending_generation_task_id(session, book_id=book_id, chapter_number=chapter_number)
        if not selected_task_id:
            return {"status": "noop", "message": "当前作品/章节没有待启动的生成任务。"}
    with _BACKGROUND_LOCK:
        active = next((run for run in _BACKGROUND_RUNS.values() if run.get("status") == "running"), None)
        if active:
            return {"status": "running", "run_id": active["run_id"], "message": "模型生成任务已经在运行，系统会自动刷新状态。"}
        run_id = str(int(time.time() * 1000))
        _BACKGROUND_RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "executed_count": 0,
            "error": "",
        }

    def worker() -> None:
        try:
            with _DB_WRITE_LOCK, session_scope() as session:
                if selected_task_id:
                    result = run_generation_queue_task(session, task_id=selected_task_id)
                    executed_count = 1 if result.task.status in {"completed", "failed", "pending"} else 0
                else:
                    batch = run_generation_queue(session, max_tasks=max_tasks)
                    executed_count = len(batch.results)
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "completed",
                        "finished_at": time.time(),
                        "executed_count": executed_count,
                    }
                )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "error": str(exc),
                    }
                )

    thread = threading.Thread(target=worker, name=f"queue-worker-{run_id}", daemon=True)
    thread.start()
    return {"status": "running", "run_id": run_id, "message": "模型生成任务已开始，系统会自动刷新状态。"}


def _start_background_review_run(
    *,
    book_id: int,
    chapter_number: int,
    platform: str = "manual",
    auto_revise_until_pass: bool = False,
    max_revision_cycles: int = 3,
) -> dict:
    if not book_id or not chapter_number:
        raise ValueError("book_id and chapter_number are required")
    max_revision_cycles = max(1, min(5, int(max_revision_cycles or 3)))
    with _BACKGROUND_LOCK:
        active = next((run for run in _BACKGROUND_RUNS.values() if run.get("status") == "running"), None)
        if active:
            return {"status": "running", "run_id": active["run_id"], "message": "后台任务已经在运行，系统会自动刷新状态。"}
        run_id = str(int(time.time() * 1000))
        _BACKGROUND_RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "executed_count": 0,
            "error": "",
            "kind": "review",
            "auto_revise_until_pass": auto_revise_until_pass,
        }

    def worker() -> None:
        try:
            executed = []
            revision_count = 0
            max_actions = max_revision_cycles * 3 + 4
            for _ in range(max_actions):
                try:
                    with _DB_WRITE_LOCK, session_scope() as session:
                        result = run_next_action(
                            session,
                            book_id=book_id,
                            chapter_number=chapter_number,
                            dry_run=False,
                            queue_generation=False,
                            platform=platform,
                        )
                except Exception as exc:
                    with _BACKGROUND_LOCK:
                        _BACKGROUND_RUNS[run_id].update(
                            {
                                "status": "failed",
                                "finished_at": time.time(),
                                "executed_count": len(executed),
                                "executed": executed,
                                "error": str(exc),
                            }
                        )
                    return
                executed.append(
                    {
                        "action": result.action,
                        "status": result.status,
                        "message": result.message,
                        "object_id": result.object_id,
                    }
                )
                if not auto_revise_until_pass or result.status != "executed":
                    break
                if result.action == "review_chapter":
                    if revision_count >= max_revision_cycles and "passed=False" in result.message:
                        break
                    continue
                if result.action == "create_revision_brief":
                    if revision_count >= max_revision_cycles:
                        break
                    continue
                if result.action == "revise_chapter":
                    revision_count += 1
                    continue
                if result.action in {"record_chapter_continuity", "approve_chapter", "mark_publish_job"}:
                    break
                break
            with _BACKGROUND_LOCK:
                terminal = author_terminal_status(executed)
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "completed",
                        "finished_at": time.time(),
                        "executed_count": len(executed),
                        "result": executed[-1] if executed else {},
                        "executed": executed,
                        "terminal_status": terminal["status"],
                        "terminal_message": terminal["message"],
                    }
                )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "error": str(exc),
                    }
                )

    thread = threading.Thread(target=worker, name=f"review-worker-{run_id}", daemon=True)
    thread.start()
    message = "质检和主编审稿已在后台开始，系统会自动刷新状态。"
    if auto_revise_until_pass:
        message = f"自动质检-修订闭环已开始，最多修订 {max_revision_cycles} 轮，并会质检最后一版。"
    return {"status": "running", "run_id": run_id, "message": message}


def _start_background_sample_run(
    *,
    book_id: int,
    chapter_number: int,
    sample_count: int = 3,
    focus: str = "opening",
    dry_run: bool = False,
    max_attempts: int = 3,
) -> dict:
    if not book_id or not chapter_number:
        raise ValueError("book_id and chapter_number are required")
    sample_count = max(1, min(5, int(sample_count or 3)))
    max_attempts = max(1, min(5, int(max_attempts or 3)))
    with _BACKGROUND_LOCK:
        active = next((run for run in _BACKGROUND_RUNS.values() if run.get("status") == "running"), None)
        if active:
            return {"status": "running", "run_id": active["run_id"], "message": "后台任务已经在运行，系统会自动刷新状态。"}
        run_id = str(int(time.time() * 1000))
        _BACKGROUND_RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "executed_count": 0,
            "error": "",
            "kind": "sample",
            "chapter_number": chapter_number,
            "timeout_seconds": 420,
        }

    def worker() -> None:
        try:
            with _DB_WRITE_LOCK, session_scope() as session:
                task = generate_chapter_samples(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                    sample_count=sample_count,
                    focus=focus,
                    dry_run=dry_run,
                    max_attempts=max_attempts,
                )
                output_data = _loads_json(task.output_json)
                status = task.status
                error = output_data.get("error", "")
                sample_total = len(output_data.get("samples") or [])
                gate_passed = bool(output_data.get("gate_passed"))
                attempts = output_data.get("attempts") or []
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "completed" if status == "completed" else "failed",
                        "finished_at": time.time(),
                        "executed_count": 1,
                        "result": {
                            "generation_task_id": task.id,
                            "status": status,
                            "sample_count": sample_total,
                            "gate_passed": gate_passed,
                            "attempts": len(attempts),
                        },
                        "error": error,
                        "terminal_message": (
                            "章节小样已生成并通过门禁。"
                            if status == "completed" and gate_passed
                            else "章节小样已生成，但多样性门禁未通过。"
                            if status == "completed"
                            else "章节小样生成失败。"
                        ),
                    }
                )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "executed_count": 0,
                        "error": str(exc),
                        "terminal_message": "章节小样生成失败，请查看后台诊断。",
                    }
                )

    thread = threading.Thread(target=worker, name=f"sample-worker-{run_id}", daemon=True)
    thread.start()
    return {"status": "running", "run_id": run_id, "message": "章节小样已开始后台生成，完成后会自动显示在作者工作台。"}


def _start_background_author_run(
    *,
    book_id: int,
    chapter_number: int,
    platform: str = "manual",
    max_revision_cycles: int = 3,
) -> dict:
    if not book_id or not chapter_number:
        raise ValueError("book_id and chapter_number are required")
    max_revision_cycles = max(1, min(5, int(max_revision_cycles or 3)))
    with _BACKGROUND_LOCK:
        active = next((run for run in _BACKGROUND_RUNS.values() if run.get("status") == "running"), None)
        if active:
            return {"status": "running", "run_id": active["run_id"], "message": "后台主笔已经在运行，系统会自动刷新状态。"}
        run_id = str(int(time.time() * 1000))
        _BACKGROUND_RUNS[run_id] = {
            "run_id": run_id,
            "status": "running",
            "started_at": time.time(),
            "finished_at": None,
            "executed_count": 0,
            "error": "",
            "kind": "author",
            "max_revision_cycles": max_revision_cycles,
            "timeout_seconds": author_background_timeout_seconds(max_revision_cycles),
        }

    def worker() -> None:
        executed: list[dict] = []
        try:
            with _DB_WRITE_LOCK:
                run = run_author_mode(
                    book_id=book_id,
                    chapter_number=chapter_number,
                    platform=platform,
                    max_revision_cycles=max_revision_cycles,
                )
            executed = run.executed
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "completed",
                        "finished_at": time.time(),
                        "executed_count": len(executed),
                        "result": run.latest_result,
                        "executed": executed,
                        "terminal_status": run.terminal_status,
                        "terminal_message": run.terminal_message,
                    }
                )
        except Exception as exc:
            with _BACKGROUND_LOCK:
                _BACKGROUND_RUNS[run_id].update(
                    {
                        "status": "failed",
                        "finished_at": time.time(),
                        "executed_count": len(executed),
                        "executed": executed,
                        "error": str(exc),
                        "terminal_status": "system_failed",
                        "terminal_message": "系统执行失败，请查看后台诊断。",
                    }
                )

    thread = threading.Thread(target=worker, name=f"author-worker-{run_id}", daemon=True)
    thread.start()
    return {"status": "running", "run_id": run_id, "message": "主笔模式已开始：系统会自动跑到可读稿或明确失败。"}


def _background_runs_payload() -> list[dict]:
    now = time.time()
    with _BACKGROUND_LOCK:
        for run in _BACKGROUND_RUNS.values():
            if (
                run.get("status") == "running"
                and int(run.get("executed_count") or 0) == 0
                and now - float(run.get("started_at") or now) > int(run.get("timeout_seconds") or 180)
            ):
                kind = str(run.get("kind") or "")
                timeout_message = "后台任务启动超时，请重试；若反复出现，查看模型连接或数据库锁。"
                if kind == "sample":
                    timeout_message = "章节小样生成超时，请重试；若反复出现，查看模型连接。"
                elif kind == "author":
                    timeout_message = "后台主笔启动超时，请重试；若反复出现，查看模型连接或数据库锁。"
                run.update(
                    {
                        "status": "failed",
                        "finished_at": now,
                        "error": f"后台任务启动后 {int(run.get('timeout_seconds') or 180)} 秒内没有完成任何动作，已自动标记为失败。",
                        "terminal_status": "system_failed",
                        "terminal_message": timeout_message,
                    }
                )
        runs = list(_BACKGROUND_RUNS.values())[-10:]
    payload = []
    for run in reversed(runs):
        started_at = float(run.get("started_at") or now)
        finished_at = run.get("finished_at")
        payload.append(
            {
                "run_id": run.get("run_id", ""),
                "kind": run.get("kind", "queue"),
                "status": run.get("status", ""),
                "running_age_seconds": int((float(finished_at) if finished_at else now) - started_at),
                "executed_count": run.get("executed_count", 0),
                "error": run.get("error", ""),
                "result": run.get("result", {}),
                "terminal_status": run.get("terminal_status", ""),
                "terminal_message": run.get("terminal_message", ""),
                "timeout_seconds": int(run.get("timeout_seconds") or 180),
            }
        )
    return payload


def _pending_generation_task_id(session, *, book_id: int = 0, chapter_number: int = 0) -> int | None:
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.task_type.in_(QUEUE_TYPES), GenerationTask.status == "pending")
        .order_by(GenerationTask.id)
    )
    if book_id:
        stmt = stmt.where(GenerationTask.book_id == book_id)
    tasks = list(session.scalars(stmt))
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if chapter_number and int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        return task.id
    return None


def main() -> int:
    parser = argparse.ArgumentParser(prog="run_local_dashboard.py")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.database_url:
        configure_database(args.database_url)
    if args.self_test:
        with session_scope() as session:
            books = list(session.scalars(select(Book).order_by(Book.id)))
            queue = build_generation_queue_health(session)
            if books:
                build_project_snapshot(session, book_id=books[0].id, start=1, count=1)
                _chapter_detail(session, book_id=books[0].id, chapter_number=1)
                _feedback_payload(session, book_id=books[0].id)
                _knowledge_payload(session, book_id=books[0].id, chapter_number=1)
                _llm_usage_payload(session, book_id=books[0].id)
                _failed_tasks_payload(session, book_id=books[0].id)
                _publishing_payload(session, book_id=books[0].id)
                _database_payload(session)
            action_result = _perform_action(session, {"action": "queue_health"})
            print("dashboard_self_test=PASS")
            print(f"book_count={len(books)}")
            print(f"queue_total={queue.total}")
            print(f"action_status={action_result['status']}")
        return 0
    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    host, port = server.server_address
    print(f"dashboard_url=http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    return 0


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self._send_html(HTML)
                return
            if parsed.path == "/api/books":
                with session_scope() as session:
                    books = list(session.scalars(select(Book).order_by(Book.id)))
                    self._send_json(
                        [
                            {
                                "id": book.id,
                                "title": book.title,
                                "genre": book.genre,
                                "platform": book.target_platform,
                                "status": book.status,
                            }
                            for book in books
                        ]
                    )
                return
            if parsed.path == "/api/book-options":
                with session_scope() as session:
                    books = list(session.scalars(select(Book).order_by(Book.id)))
                    self._send_json([_book_option(book) for book in books])
                return
            if parsed.path == "/api/snapshot":
                query = parse_qs(parsed.query)
                book_id = _int_query(query, "book_id", 0)
                if not book_id:
                    raise ValueError("book_id is required")
                with session_scope() as session:
                    self._send_json(
                        build_project_snapshot(
                            session,
                            book_id=book_id,
                            chapter_number=_int_query(query, "chapter_number", 1),
                            start=_int_query(query, "start", 1),
                            count=_int_query(query, "count", 20),
                        )
                    )
                return
            if parsed.path == "/api/queue-health":
                with session_scope() as session:
                    report = build_generation_queue_health(session)
                    self._send_json(
                        {
                            "total": report.total,
                            "counts": report.counts,
                            "oldest_pending_id": report.oldest_pending_id,
                            "oldest_pending_chapter": report.oldest_pending_chapter,
                            "running_count": report.running_count,
                            "stale_running_count": report.stale_running_count,
                            "background_runs": _background_runs_payload(),
                            "running_tasks": [
                                {
                                    "task_id": item.task_id,
                                    "task_type": item.task_type,
                                    "chapter_number": item.chapter_number,
                                    "attempt": item.attempt,
                                    "max_attempts": item.max_attempts,
                                    "running_age_seconds": item.running_age_seconds,
                                    "timeout_seconds": item.timeout_seconds,
                                    "stale": item.stale,
                                    "recoverable": item.recoverable,
                                }
                                for item in report.running_tasks
                            ],
                            "latest_failures": [
                                {
                                    "task_id": item.task_id,
                                    "task_type": item.task_type,
                                    "chapter_number": item.chapter_number,
                                    "attempt": item.attempt,
                                    "max_attempts": item.max_attempts,
                                    "error_category": item.error_category,
                                    "error": item.error,
                                    "retryable": item.retryable,
                                }
                                for item in report.latest_failures
                            ],
                        }
                    )
                return
            if parsed.path == "/api/chapter-detail":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(
                        _chapter_detail(
                            session,
                            book_id=_int_query(query, "book_id", 0),
                            chapter_number=_int_query(query, "chapter_number", 1),
                        )
                    )
                return
            if parsed.path == "/api/feedback":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_feedback_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/knowledge":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(
                        _knowledge_payload(
                            session,
                            book_id=_int_query(query, "book_id", 0),
                            chapter_number=_int_query(query, "chapter_number", 1),
                        )
                    )
                return
            if parsed.path == "/api/llm-usage":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_llm_usage_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/failed-tasks":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_failed_tasks_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/publishing":
                query = parse_qs(parsed.query)
                with session_scope() as session:
                    self._send_json(_publishing_payload(session, book_id=_int_query(query, "book_id", 0)))
                return
            if parsed.path == "/api/database":
                with session_scope() as session:
                    self._send_json(_database_payload(session))
                return
            self._send_text("not found", status=HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self._send_text(f"ERROR: {exc}", status=HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/action":
                self._send_text("not found", status=HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            payload = json.loads(raw or "{}")
            if not isinstance(payload, dict):
                raise ValueError("JSON object is required")
            if payload.get("action") == "restore_database":
                self._send_json(_perform_restore_action(payload))
                return
            self._send_json(_perform_action_with_retry(payload))
        except Exception as exc:
            self._send_text(f"ERROR: {exc}", status=HTTPStatus.BAD_REQUEST)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_json(self, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, value: str) -> None:
        body = value.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, value: str, *, status: HTTPStatus) -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _int_query(query: dict[str, list[str]], name: str, default: int) -> int:
    values = query.get(name)
    if not values:
        return default
    return int(values[0])


def _book_option(book: Book) -> dict:
    return {
        "id": book.id,
        "title": book.title,
        "genre": book.genre,
        "platform": book.target_platform,
        "status": book.status,
    }


def _perform_action(session, payload: dict) -> dict:
    action = str(payload.get("action") or "")
    if action == "queue_health":
        report = build_generation_queue_health(session)
        return {"status": "ok", "total": report.total, "counts": report.counts, "background_runs": _background_runs_payload()}
    if action == "update_book_settings":
        book = session.get(Book, int(payload.get("book_id") or 0))
        if not book:
            raise ValueError("book not found")
        platform = str(payload.get("platform") or "").strip()
        if platform:
            book.target_platform = platform
        genre = str(payload.get("genre") or "").strip()
        if genre:
            book.genre = genre
        session.flush()
        return {
            "status": "saved",
            "message": "作品目标已保存；后续调研、设定修复和章节生产会默认使用这组目标。",
            "book": _book_option(book),
        }
    if action == "start_queue_background":
        return _start_background_queue_run(
            max_tasks=int(payload.get("max_tasks") or 1),
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
        )
    if action == "start_review_background":
        return _start_background_review_run(
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
            platform=str(payload.get("platform") or "manual"),
            auto_revise_until_pass=bool(payload.get("auto_revise_until_pass")),
            max_revision_cycles=int(payload.get("max_revision_cycles") or 3),
        )
    if action == "start_author_background":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        preflight = _production_preflight_payload(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
        )
        preflight = _auto_repair_preflight_if_needed(session, book_id=book_id, chapter_number=chapter_number, preflight=preflight)
        if not preflight["passed"]:
            if _preflight_only_model_drift(preflight):
                _create_model_drift_revision_brief(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                    blockers=preflight["blockers"],
                )
                session.commit()
            else:
                raise ValueError("生产前体检未通过：" + "；".join(preflight["blockers"]))
        return _start_background_author_run(
            book_id=book_id,
            chapter_number=chapter_number,
            platform=str(payload.get("platform") or "manual"),
            max_revision_cycles=int(payload.get("max_revision_cycles") or 3),
        )
    if action == "update_chapter_brief":
        return _update_chapter_brief_action(session, payload)
    if action == "repair_current_chapter_brief":
        return _repair_current_chapter_brief_action(session, payload)
    if action == "restart_production_from_chapter":
        return _restart_production_from_chapter_action(session, payload)
    if action == "prepare_production":
        return prepare_production(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
            platform=str(payload.get("platform") or "manual"),
        ).to_dict()
    if action == "run_queue":
        max_tasks = int(payload.get("max_tasks") or 1)
        if max_tasks < 1 or max_tasks > 3:
            raise ValueError("max_tasks must be between 1 and 3")
        batch = run_generation_queue(session, max_tasks=max_tasks)
        return {
            "status": "executed",
            "executed_count": len(batch.results),
            "tasks": [
                {
                    "generation_task_id": result.task.id,
                    "status": result.task.status,
                    "version_id": result.version_id,
                    "child_generation_task_id": result.child_generation_task_id,
                }
                for result in batch.results
            ],
        }
    if action == "pause_queue_task":
        task = pause_generation_queue_task(session, task_id=int(payload.get("task_id") or 0), reason="dashboard")
        return {"status": task.status, "generation_task_id": task.id}
    if action == "resume_queue_task":
        task = resume_generation_queue_task(session, task_id=int(payload.get("task_id") or 0))
        return {"status": task.status, "generation_task_id": task.id}
    if action == "cancel_queue_task":
        task = cancel_generation_queue_task(session, task_id=int(payload.get("task_id") or 0), reason="dashboard")
        return {"status": task.status, "generation_task_id": task.id}
    if action == "retry_queue_task":
        task = retry_generation_queue_task(session, task_id=int(payload.get("task_id") or 0))
        return {"status": task.status, "generation_task_id": task.id}
    if action == "backup_database":
        backup = create_database_backup(session, label=str(payload.get("label") or "dashboard"))
        return {
            "status": backup.status,
            "database_backup_id": backup.id,
            "backup_path": backup.backup_path,
            "size_bytes": backup.size_bytes,
        }
    if action == "check_live_llm":
        result = run_live_llm_smoke(session)
        return {
            "status": "completed" if result.passed else "failed",
            "passed": result.passed,
            "provider": result.provider,
            "model": result.model,
            "request_id": result.request_id,
            "estimated_total_tokens": result.estimated_total_tokens,
            "elapsed_ms": result.elapsed_ms,
            "error_category": result.error_category,
            "error": result.error,
        }
    if action == "repair_readiness_gate":
        return _repair_readiness_gate_action(session, payload)
    if action == "auto_resolve_author_blocker":
        return _auto_resolve_author_blocker_action(session, payload)
    if action == "index_book_knowledge":
        result = index_book_knowledge(
            session,
            book_id=int(payload.get("book_id") or 0),
            dry_run=not bool(payload.get("live_embedding")),
            reset=True,
            limit_chapters=int(payload.get("limit_chapters") or 80),
        )
        return {"status": "indexed", "message": f"已更新语义记忆 {result['indexed_count']} 条。", **result}
    if action == "create_market_research_pack":
        book = session.get(Book, int(payload.get("book_id") or 0))
        if not book:
            raise ValueError("book not found")
        query = str(payload.get("market_query") or "").strip()
        platform = str(payload.get("platform") or "").strip() or book.target_platform or "番茄小说"
        result = create_market_research_pack(
            session,
            genre=book.genre or "未分类",
            query=query or f"{platform} {book.genre or '网文'} 最新爆款 趋势 开篇 卖点 避雷",
            platform=platform,
        )
        return {
            "status": "created",
            "message": f"联网搜索任务包已生成：{len(result.get('queries') or [])} 个查询。",
            **result,
        }
    if action == "ingest_market_research_results":
        book = session.get(Book, int(payload.get("book_id") or 0))
        if not book:
            raise ValueError("book not found")
        raw_result = str(payload.get("result_json") or "").strip()
        if not raw_result:
            raise ValueError("请先粘贴 Agent Plan 联网搜索返回的 JSON")
        result = ingest_market_research_results(
            session,
            genre=book.genre or "未分类",
            result_json=raw_result,
            source_prefix=str(payload.get("source_prefix") or "agent-search"),
        )
        signal_count = len(result.get("market_signal_ids") or [])
        return {
            "status": "ingested",
            "message": f"已导入市场证据：{signal_count} 条信号。",
            **result,
        }
    if action == "run_market_web_search":
        book = session.get(Book, int(payload.get("book_id") or 0))
        if not book:
            raise ValueError("book not found")
        query = str(payload.get("market_query") or "").strip()
        platform = str(payload.get("platform") or "").strip() or book.target_platform or "番茄小说"
        search = run_market_web_search(
            query=query or f"{platform} {book.genre or '网文'} 最新爆款 趋势 开篇 卖点 避雷",
            provider=str(payload.get("provider") or "auto"),
            max_results=int(payload.get("max_results") or 5),
            search_depth=str(payload.get("search_depth") or "basic"),
        )
        result = ingest_market_research_results(
            session,
            genre=book.genre or "未分类",
            result_json=search.result_json,
            source_prefix=f"{search.provider}-search",
        )
        signal_count = len(result.get("market_signal_ids") or [])
        return {
            "status": "completed",
            "message": f"{search.provider} 搜索并导入 {signal_count} 条市场信号，消耗 {search.used_credits} credit。",
            "search": search.to_dict(),
            **result,
        }
    if action == "agent_plan_cycle":
        result = run_agent_plan_enhancement_cycle(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0) or None,
            market_query=str(payload.get("market_query") or ""),
            platform=str(payload.get("platform") or ""),
            dry_run=not bool(payload.get("live_embedding")),
            rebuild_memory=True,
            create_visuals=True,
            auto_market_search=True,
        )
        indexed_count = result["semantic_memory_after"].get("indexed_count", 0)
        asset_count = len(result.get("visual_asset_ids") or [])
        market_status = result.get("market_research", {}).get("step", {}).get("status", "")
        market_provider = result.get("market_research", {}).get("step", {}).get("provider", "")
        return {
            "status": "completed",
            "message": f"增强循环完成：市场证据 {market_status}/{market_provider}，语义记忆 {indexed_count} 条，视觉方案 {asset_count} 个。",
            **result,
        }
    if action == "create_cover_asset":
        asset = create_visual_asset(
            session,
            book_id=int(payload.get("book_id") or 0),
            asset_type="cover",
            style=str(payload.get("style") or ""),
            dry_run=True,
        )
        return {"status": "created", "message": "封面视觉方案已生成。", "visual_asset_id": asset.id, "artifact_path": asset.artifact_path}
    if action == "create_chapter_illustration_asset":
        asset = create_visual_asset(
            session,
            book_id=int(payload.get("book_id") or 0),
            asset_type="chapter_illustration",
            chapter_number=int(payload.get("chapter_number") or 0) or None,
            style=str(payload.get("style") or ""),
            dry_run=True,
        )
        return {"status": "created", "message": "章节插图方案已生成。", "visual_asset_id": asset.id, "artifact_path": asset.artifact_path}
    if action == "create_new_book":
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("书名不能为空")
        genre = str(payload.get("genre") or "玄幻脑洞").strip()
        platform = str(payload.get("platform") or "番茄小说").strip()
        promise = str(payload.get("reader_promise") or "").strip() or "升级快、冲突强、每章都有明确钩子"
        premise = str(payload.get("premise") or "").strip() or f"{title}：主角在{genre}世界获得改变命运的核心能力，一边变强一边被更高层的势力盯上。"
        world_engine = str(payload.get("world_engine") or "").strip() or f"{genre}世界分层清晰，资源、身份和力量等级决定生存空间；核心能力必须有代价和限制。"
        protagonist_engine = str(payload.get("protagonist_engine") or "").strip() or "主角开局被压制，但目标明确：活下去、变强、夺回主动权。"
        conflict_engine = str(payload.get("conflict_engine") or "").strip() or "长期冲突来自身边危机、同阶竞争者，以及隐藏在世界规则背后的高位势力。"
        aesthetic_profile = build_aesthetic_profile_block(
            prose_style=str(payload.get("prose_style") or "").strip(),
            atmosphere=str(payload.get("atmosphere") or "").strip(),
            story_route=str(payload.get("story_route") or "").strip(),
            must_have=str(payload.get("style_must_have") or "").strip(),
            must_not=str(payload.get("style_must_not") or "").strip(),
        )
        story_dna = str(payload.get("story_dna") or "").strip() or build_story_dna_from_development(
            {
                "title": title,
                "genre": genre,
                "reader_promise": promise,
                "premise": premise,
                "prose_style": str(payload.get("prose_style") or "").strip(),
                "atmosphere": str(payload.get("atmosphere") or "").strip(),
                "story_route": str(payload.get("story_route") or "").strip(),
                "style_must_not": str(payload.get("style_must_not") or "").strip(),
                "protagonist_engine": protagonist_engine,
                "world_engine": world_engine,
                "conflict_engine": conflict_engine,
                "volume_summary": str(payload.get("volume_summary") or "").strip(),
                "arc_goal": str(payload.get("arc_goal") or "").strip(),
                "arc_climax": str(payload.get("arc_climax") or "").strip(),
                "arc_turn": str(payload.get("arc_turn") or "").strip(),
                "creative_candidates": payload.get("creative_candidates") if isinstance(payload.get("creative_candidates"), list) else [],
                "chosen_creative_engine": str(payload.get("chosen_creative_engine") or "").strip(),
            }
        )
        book = create_book(session, title=title, genre=genre, platform=platform)
        foundation = create_foundation(
            session,
            book_id=book.id,
            premise=premise,
            reader_promise=promise,
            world_engine=world_engine,
            protagonist_engine=protagonist_engine,
            conflict_engine=conflict_engine,
        )
        bible = upsert_story_bible(
            session,
            book_id=book.id,
            positioning=premise,
            reader_promise=promise,
            main_plot=conflict_engine or premise,
            protagonist_arc=protagonist_engine,
            power_curve=world_engine,
            forbidden_rules="避免系统提示词、作者说明、元叙事泄露到正文。",
            style_guide="\n\n".join(["番茄小说节奏：开篇快，冲突明确，章末留钩子。", aesthetic_profile, story_dna]),
            status="draft",
        )
        bootstrap = bootstrap_book_production(
            session,
            book_id=book.id,
            title=title,
            genre=genre,
            premise=premise,
            reader_promise=promise,
            world_engine=world_engine,
            protagonist_engine=protagonist_engine,
            conflict_engine=conflict_engine,
        )
        create_volume(session, book_id=book.id, volume_number=1, title="第一卷", summary=str(payload.get("volume_summary") or "").strip())
        create_story_arc(session, book_id=book.id, arc_number=1, title="开局破局", start_chapter=1, end_chapter=5, goal=str(payload.get("arc_goal") or "").strip(), climax=str(payload.get("arc_climax") or "").strip(), turn=str(payload.get("arc_turn") or "").strip(), volume_number=1)
        return {
            "status": "created",
            "book": _book_option(book),
            "foundation_id": foundation.id,
            "story_bible_id": bible.id,
            "bootstrap": bootstrap,
        }
    if action == "brainstorm_new_book":
        return {
            "status": "completed",
            "ideas": _brainstorm_new_book_ideas(
                idea_prompt=str(payload.get("idea_prompt") or ""),
                feedback=str(payload.get("feedback") or ""),
                seed_title=str(payload.get("seed_title") or ""),
                genre=str(payload.get("genre") or "玄幻脑洞"),
                reader_promise=str(payload.get("reader_promise") or ""),
                current_ideas=payload.get("current_ideas") if isinstance(payload.get("current_ideas"), list) else [],
            ),
        }
    if action == "develop_new_book":
        return {"status": "completed", "draft": develop_new_book_from_inspiration(idea_prompt=str(payload.get("idea_prompt") or ""), feedback=str(payload.get("feedback") or ""), title=str(payload.get("title") or ""), genre=str(payload.get("genre") or "玄幻脑洞"), platform=str(payload.get("platform") or "番茄小说"))}
    if action == "bootstrap_book_production":
        book_id = int(payload.get("book_id") or 0)
        result = repair_production_scaffold(
            session,
            book_id=book_id,
            only_missing=True,
            approve_skeleton=True,
            chapter_count=5,
            apply=bool(payload.get("apply")),
        )
        if result.get("mode") == "preview":
            return {
                "status": "preview",
                "book_id": book_id,
                "message": f"生产骨架补全预览：预计变更 {result.get('planned_count', 0)} 项。确认后才会写入数据库。",
                "bootstrap": result,
                "planned_count": result.get("planned_count", 0),
            }
        created_count = int(result.get("created_count") or 0)
        upgraded_count = int(result.get("upgraded_count") or 0)
        existing_count = sum(1 for item in (result.get("items") or {}).values() if isinstance(item, dict) and item.get("status") == "existing")
        return {
            "status": "completed",
            "book_id": book_id,
            "created_count": created_count,
            "existing_count": existing_count,
            "upgraded_count": upgraded_count,
            "bootstrap": result,
            "message": f"生产骨架补全完成，新增 {created_count} 项，升级 {upgraded_count} 项，已有 {existing_count} 项。",
        }
    if action == "update_story_skeleton":
        book_id = int(payload.get("book_id") or 0)
        book = session.get(Book, book_id)
        if not book:
            raise ValueError(f"book not found: {book_id}")
        result = _update_story_skeleton(session, book=book, payload=payload)
        approved_count = 0
        approve_key = str(payload.get("approve_after_save_key") or "").strip()
        context_reset = None
        if approve_key:
            _assert_skeleton_can_be_approved(_skeleton_payload_from_update_payload(payload))
            sync_result = synchronize_skeleton_derivatives(
                session,
                book_id=book.id,
                approve_keys=[approve_key],
                chapter_count=5,
                reason=f"approve_field:{approve_key}",
            )
            approved_count = sync_result.approved_count
        elif bool(payload.get("approve_after_save")):
            _assert_skeleton_can_be_approved(_skeleton_payload_from_update_payload(payload))
            context_reset = reset_context_after_skeleton_approval(
                session,
                book_id=book.id,
                skeleton=_skeleton_payload_from_update_payload(payload),
                start_chapter=1,
                plan_count=5,
            )
            sync_result = synchronize_skeleton_derivatives(
                session,
                book_id=book.id,
                approve_keys="all",
                chapter_count=5,
                reason="save_and_enable",
            )
            approved_count = sync_result.approved_count
        else:
            record_skeleton_version(
                session,
                book_id=book.id,
                reason="save_draft",
                values=_skeleton_payload_from_update_payload(payload),
            )
        message = (
            f"生产骨架已保存，并确认 {approved_count} 个当前项。后续章节会使用新的设定。"
            if approve_key
            else (
                f"生产骨架已保存并确认 {approved_count} 项；已清理旧生产上下文并从第 1 章重建生产说明。"
                if bool(payload.get("approve_after_save"))
                else "生产骨架已保存为草稿。后续章节会使用新的设定。"
            )
        )
        return {
            "status": "saved",
            "message": message,
            "approved_count": approved_count,
            "context_reset": context_reset.to_dict() if context_reset else {},
            **result,
        }
    if action == "suggest_story_skeleton":
        book_id = int(payload.get("book_id") or 0)
        book = session.get(Book, book_id)
        if not book:
            raise ValueError(f"book not found: {book_id}")
        skeleton = _suggest_story_skeleton(
            book=book,
            revision_idea=str(payload.get("revision_idea") or ""),
            current_skeleton=payload.get("current_skeleton") if isinstance(payload.get("current_skeleton"), dict) else {},
        )
        return {"status": "completed", "message": "AI 已生成骨架草案，请检查后保存。", "skeleton": skeleton}
    if action == "audit_story_skeleton_draft":
        current_skeleton = payload.get("current_skeleton") if isinstance(payload.get("current_skeleton"), dict) else {}
        report = audit_skeleton_sources({f"draft.{key}": str(value or "") for key, value in current_skeleton.items()})
        return {
            "status": "completed",
            "message": "已完成当前表单生产体检；未写入数据库。",
            "governance": report.to_dict(),
        }
    if action == "repair_story_skeleton_draft":
        book_id = int(payload.get("book_id") or 0)
        current_skeleton = payload.get("current_skeleton") if isinstance(payload.get("current_skeleton"), dict) else {}
        revision_idea = str(payload.get("revision_idea") or "").strip()
        ai_error = ""
        try:
            repair_payload = _repair_story_skeleton_with_ai(
                session,
                book_id=book_id,
                current_skeleton=current_skeleton,
                revision_idea=revision_idea,
            )
            repair_payload["generation_source"] = "live_model"
        except Exception as exc:
            ai_error = f"{type(exc).__name__}: {exc}"
            repair_payload = (
                repair_story_skeleton_with_market_evidence(session, book_id=book_id, skeleton=current_skeleton)
                if book_id
                else repair_skeleton_until_pass(current_skeleton).to_dict()
            )
            repair_payload["generation_source"] = "rule_fallback"
            repair_payload["ai_error"] = ai_error
        repair_payload = apply_revision_idea_to_repair_payload(repair_payload, revision_idea=revision_idea)
        repair_payload = _preserve_skeleton_identity_fields_in_payload(repair_payload, current_skeleton)
        repair_payload = _sanitize_skeleton_repair_payload(repair_payload)
        market_count = int((repair_payload.get("market_context") or {}).get("signal_count") or 0)
        source_text = "AI 模型" if repair_payload.get("generation_source") == "live_model" else "规则兜底"
        return {
            "status": "completed",
            "message": (
                f"已用{source_text}生成骨架修复草案，并参考 {market_count} 条市场信号，请检查后保存确认。"
                if repair_payload.get("passed")
                else f"已用{source_text}生成骨架修复草案，并参考 {market_count} 条市场信号，但仍有未解风险。"
            ),
            **repair_payload,
        }
    if action == "apply_story_skeleton_repair":
        book_id = int(payload.get("book_id") or 0)
        book = session.get(Book, book_id)
        if not book:
            raise ValueError(f"book not found: {book_id}")
        current_skeleton = payload.get("current_skeleton") if isinstance(payload.get("current_skeleton"), dict) else {}
        revision_idea = str(payload.get("revision_idea") or "").strip()
        preview_skeleton = payload.get("repaired_skeleton") if isinstance(payload.get("repaired_skeleton"), dict) else {}
        if preview_skeleton:
            repaired_skeleton = _sanitize_story_skeleton_payload({key: str(preview_skeleton.get(key) or "").strip() for key, _ in SKELETON_APPROVAL_FIELDS})
            after = audit_skeleton_sources({f"preview.{key}": value for key, value in repaired_skeleton.items()})
            repair_payload = {
                "status": "completed" if after.passed else "needs_human_review",
                "passed": after.passed,
                "skeleton": repaired_skeleton,
                "repaired_skeleton": repaired_skeleton,
                "after": after.to_dict(),
                "applied_strategy": "apply_current_preview",
                "next_actions": [
                    "当前预览仍有未解 blocker，请先在页面中人工调整后再确认。"
                ] if not after.passed else [],
            }
        else:
            repair_payload = repair_story_skeleton_with_market_evidence(session, book_id=book_id, skeleton=current_skeleton)
            repair_payload = apply_revision_idea_to_repair_payload(repair_payload, revision_idea=revision_idea)
            repair_payload = _preserve_skeleton_identity_fields_in_payload(repair_payload, current_skeleton)
            repair_payload = _sanitize_skeleton_repair_payload(repair_payload)
        if not repair_payload.get("passed"):
            return {
                "status": "needs_human_review",
                "message": "自动修复草案仍未通过骨架审计；页面已填入草案，请先处理未解 blocker。",
                **repair_payload,
            }
        repaired_skeleton = repair_payload.get("skeleton") or {}
        result = _update_story_skeleton(session, book=book, payload=repaired_skeleton)
        context_reset = reset_context_after_skeleton_approval(
            session,
            book_id=book_id,
            skeleton=repaired_skeleton,
            start_chapter=1,
            plan_count=5,
        )
        sync_result = synchronize_skeleton_derivatives(
            session,
            book_id=book_id,
            approve_keys="all",
            chapter_count=5,
            reason="apply_repair",
        )
        approved_count = sync_result.approved_count
        return {
            **repair_payload,
            **result,
            "status": "applied",
            "message": f"已应用骨架修复草案并确认 {approved_count} 项；旧生产上下文已清理，已从第 1 章重建清洁生产说明。",
            "approved_count": approved_count,
            "context_reset": context_reset.to_dict(),
        }
    if action == "approve_skeleton_item":
        book_id = int(payload.get("book_id") or 0)
        book = session.get(Book, book_id)
        if not book:
            raise ValueError(f"book not found: {book_id}")
        current_skeleton = payload.get("current_skeleton") if isinstance(payload.get("current_skeleton"), dict) else {}
        _assert_skeleton_can_be_approved(current_skeleton)
        _update_story_skeleton(session, book=book, payload=current_skeleton)
        key = str(payload.get("key") or "")
        approve_keys = "all" if key == "all" else [key]
        sync_result = synchronize_skeleton_derivatives(
            session,
            book_id=book_id,
            approve_keys=approve_keys,
            chapter_count=5,
            reason=f"manual_approve:{key}",
        )
        approved = sync_result.approved_count
        return {"status": "approved", "message": f"已确认 {approved} 个生产骨架项。", "approved_count": approved}
    if action == "publish_dry_run":
        job = publish_job_dry_run(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "one_click_publish_prepare":
        result = auto_prepare_publish_job(
            session,
            version_id=int(payload.get("task_id") or 0),
            platform=str(payload.get("platform") or "番茄小说"),
            confirm_real_platform=False,
        )
        return result
    if action == "queue_publish_job":
        job = queue_publish_job(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "retry_publish_job":
        job = retry_publish_job(session, job_id=int(payload.get("task_id") or 0))
        return {"status": job.status, "publish_job_id": job.id}
    if action == "execute_publish_job_blocked":
        job, execution = execute_publish_job(session, job_id=int(payload.get("task_id") or 0), confirm=False)
        return {"status": execution.status, "publish_job_id": job.id, "publish_execution_id": execution.id}
    if action == "execute_publish_job_confirm":
        job, execution = execute_publish_job(session, job_id=int(payload.get("task_id") or 0), confirm=True)
        return {"status": execution.status, "publish_job_id": job.id, "publish_execution_id": execution.id}
    if action == "upsert_publishing_target":
        target = upsert_publishing_target(
            session,
            platform=str(payload.get("platform") or ""),
            account_label=str(payload.get("account_label") or ""),
            work_identifier=str(payload.get("work_identifier") or ""),
            automation_mode=str(payload.get("automation_mode") or "manual"),
            config_json=str(payload.get("config_json") or "{}"),
        )
        return {"status": "saved", "publishing_target_id": target.id}
    if action == "record_feedback":
        feedback = record_platform_feedback(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0) or None,
            platform=str(payload.get("platform") or "manual"),
            metric_name=str(payload.get("metric_name") or "comment"),
            metric_value=str(payload.get("metric_value") or ""),
            raw_text=str(payload.get("raw_text") or ""),
        )
        return {"status": "recorded", "feedback_id": feedback.id}
    if action == "record_author_preference":
        feedback = record_author_preference(
            session,
            book_id=int(payload.get("book_id") or 0),
            category=str(payload.get("category") or "general"),
            preference_text=str(payload.get("preference_text") or ""),
        )
        return {"status": "recorded", "message": "作者口味已记录。", "feedback_id": feedback.id}
    if action == "create_feedback_adjustment":
        adjustment = create_feedback_adjustment(
            session,
            book_id=int(payload.get("book_id") or 0),
            target_chapter_number=int(payload.get("target_chapter_number") or 0),
            feedback_ids=_parse_feedback_ids(payload.get("feedback_ids")),
            adjustment_text=str(payload.get("adjustment_text") or ""),
        )
        brief_id = None
        if bool(payload.get("apply_to_brief", True)):
            brief = apply_feedback_adjustment_to_brief(session, adjustment_id=adjustment.id, brief_status="revision_ready")
            brief_id = brief.id
        return {
            "status": "created",
            "feedback_adjustment_id": adjustment.id,
            "brief_id": brief_id,
            "revision_decision": extract_revision_decision(adjustment.adjustment_text),
        }
    if action == "submit_revision_suggestion":
        feedback, adjustment, brief, version = submit_revision_suggestion(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
            platform=str(payload.get("platform") or "manual"),
            suggestion_text=str(payload.get("suggestion_text") or ""),
            revision_mode=str(payload.get("revision_mode") or "auto"),
        )
        message = "建议已写入修订要求"
        if version and version.status == "needs_revision":
            message = "建议已写入修订要求，当前章已退回修订"
        return {
            "status": "created",
            "message": message,
            "feedback_id": feedback.id,
            "feedback_adjustment_id": adjustment.id,
            "brief_id": brief.id,
            "chapter_version_id": version.id if version else None,
            "chapter_version_status": version.status if version else "",
            "revision_decision": extract_revision_decision(adjustment.adjustment_text),
        }
    if action == "generate_chapter_samples":
        return _start_background_sample_run(
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
            sample_count=int(payload.get("sample_count") or 3),
            focus=str(payload.get("focus") or "opening"),
            dry_run=bool(payload.get("dry_run")),
            max_attempts=int(payload.get("max_attempts") or 3),
        )
    if action == "adopt_chapter_sample":
        adopted = adopt_chapter_sample(
            session,
            task_id=int(payload.get("task_id") or 0),
            sample_index=int(payload.get("sample_index") or 0),
            revision_mode=str(payload.get("revision_mode") or "targeted"),
        )
        return {
            "status": "created",
            "message": "已采用小样方向，并写入当前章修订要求。",
            "feedback_id": adopted.feedback_id,
            "feedback_adjustment_id": adopted.feedback_adjustment_id,
            "brief_id": adopted.brief_id,
            "chapter_version_id": adopted.chapter_version_id,
            "chapter_version_status": adopted.chapter_version_status,
        }
    if action == "submit_approval_revision":
        level = str(payload.get("revision_level") or "")
        mode = str(payload.get("revision_mode") or _approval_revision_mode_from_level(level) or "targeted")
        note = str(payload.get("note") or "")
        suggestion = _approval_revision_text(mode=mode, note=note)
        feedback, adjustment, brief, version = submit_revision_suggestion(
            session,
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 0),
            platform="manual_approval",
            suggestion_text=suggestion,
            revision_mode=mode,
        )
        return {
            "status": "created",
            "message": f"{_approval_revision_label(mode)}已写入修订要求，当前章已退回修订。",
            "feedback_id": feedback.id,
            "feedback_adjustment_id": adjustment.id,
            "brief_id": brief.id,
            "chapter_version_id": version.id if version else None,
            "chapter_version_status": version.status if version else "",
            "revision_decision": extract_revision_decision(adjustment.adjustment_text),
        }
    if action == "run_next_action":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        if not book_id or not chapter_number:
            raise ValueError("book_id and chapter_number are required")
        if not bool(payload.get("dry_run", True)):
            preflight = _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)
            preflight = _auto_repair_preflight_if_needed(session, book_id=book_id, chapter_number=chapter_number, preflight=preflight)
            if not preflight["passed"] and not _preflight_only_model_drift(preflight):
                raise ValueError("生产前体检未通过：" + "；".join(preflight["blockers"]))
        result = run_next_action(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            dry_run=bool(payload.get("dry_run", True)),
            queue_generation=not bool(payload.get("dry_run", True)),
            platform=str(payload.get("platform") or "manual"),
            preview_only=bool(payload.get("preview_only")),
        )
        if result.status == "preview":
            return {
                "status": "preview",
                "action": result.action,
                "chapter_number": result.chapter_number,
                "message": result.message,
                "object_id": result.object_id,
            }
        if result.action not in AUTO_ACTIONS or result.status != "executed":
            raise ValueError(f"action is not safe or executable: {result.action} {result.status}")
        return {
            "status": result.status,
            "action": result.action,
            "chapter_number": result.chapter_number,
            "message": result.message,
            "object_id": result.object_id,
        }
    if action == "run_current_until_blocked":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        max_steps = int(payload.get("max_steps") or 5)
        if max_steps < 1 or max_steps > 10:
            raise ValueError("max_steps must be between 1 and 10")
        if not bool(payload.get("dry_run", True)):
            preflight = _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)
            preflight = _auto_repair_preflight_if_needed(session, book_id=book_id, chapter_number=chapter_number, preflight=preflight)
            if not preflight["passed"]:
                if _preflight_only_model_drift(preflight):
                    _create_model_drift_revision_brief(session, book_id=book_id, chapter_number=chapter_number, blockers=preflight["blockers"])
                    session.commit()
                else:
                    raise ValueError("生产前体检未通过：" + "；".join(preflight["blockers"]))
        executed = []
        for _ in range(max_steps):
            result = run_next_action(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                dry_run=bool(payload.get("dry_run", True)),
                queue_generation=not bool(payload.get("dry_run", True)),
                platform=str(payload.get("platform") or "manual"),
                preview_only=bool(payload.get("preview_only")),
            )
            if result.status == "preview":
                return {
                    "status": "preview",
                    "blocked_action": result.action,
                    "message": result.message,
                    "executed": executed,
                }
            if result.action not in AUTO_ACTIONS or result.status != "executed":
                return {"status": "blocked", "blocked_action": result.action, "message": result.message, "executed": executed}
            executed.append(
                {
                    "action": result.action,
                    "status": result.status,
                    "message": result.message,
                    "object_id": result.object_id,
                }
            )
            if result.action in {"enqueue_draft_chapter", "enqueue_revise_chapter"}:
                return {
                    "status": "queued",
                    "message": "真实生成任务已创建，系统将启动后台生成。",
                    "executed": executed,
                }
        return {"status": "executed", "executed": executed}
    if action == "record_continuity_dashboard":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        summary = str(payload.get("summary") or "").strip() or default_chapter_continuity_summary(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
        )
        result = record_chapter_continuity(session, book_id=book_id, chapter_number=chapter_number, summary=summary)
        return {"status": "recorded", "chapter_id": result.chapter_id}
    if action == "approve_current_chapter":
        book_id = int(payload.get("book_id") or 0)
        chapter_number = int(payload.get("chapter_number") or 0)
        preflight = _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)
        preflight = _auto_repair_preflight_if_needed(session, book_id=book_id, chapter_number=chapter_number, preflight=preflight)
        if preflight.get("blockers"):
            raise ValueError("当前章仍有体检阻断项，不能审批：" + "；".join(preflight["blockers"]))
        version = latest_version_for_chapter(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
        )
        chapter = session.get(Chapter, version.chapter_id)
        continuity_recorded = False
        if version.status == "reviewed_pass" and chapter and chapter.status != "continuity_recorded":
            summary = default_chapter_continuity_summary(session, book_id=book_id, chapter_number=chapter_number)
            record_chapter_continuity(session, book_id=book_id, chapter_number=chapter_number, summary=summary)
            continuity_recorded = True
        approved = approve_chapter(session, version_id=version.id, reviewer=str(payload.get("reviewer") or "dashboard"))
        sample_learning = sync_chapter_sample_learning(session, book_id=book_id, chapter_number=chapter_number)
        return {
            "status": approved.status,
            "version_id": approved.id,
            "continuity_recorded": continuity_recorded,
            "sample_learning_recorded_count": sample_learning.recorded_count,
        }
    raise ValueError(f"unsupported action: {action}")


def _perform_restore_action(payload: dict) -> dict:
    result = restore_database_from_backup(
        backup_path=str(payload.get("backup_path") or ""),
        confirm=bool(payload.get("confirm")),
    )
    return {
        "status": "restored",
        "database_path": result.database_path,
        "source_backup_path": result.source_backup_path,
        "pre_restore_backup_path": result.pre_restore_backup_path,
        "restored_size_bytes": result.restored_size_bytes,
    }


def _perform_action_with_retry(payload: dict) -> dict:
    last_error: Exception | None = None
    if not _DB_WRITE_LOCK.acquire(timeout=2):
        return {"status": "busy", "message": "后台任务正在写入数据库，请等待当前任务结束后再继续操作。"}
    try:
        for attempt in range(12):
            try:
                with session_scope() as session:
                    return _perform_action(session, payload)
            except OperationalError as exc:
                if "database is locked" not in str(exc).lower():
                    raise
                last_error = exc
                time.sleep(0.5 * (attempt + 1))
    finally:
        _DB_WRITE_LOCK.release()
    raise RuntimeError("数据库正在被后台任务写入，请稍后重试。原始错误：database is locked") from last_error


def _update_story_skeleton(session, *, book: Book, payload: dict) -> dict:
    previous_values = canonical_current_skeleton_values(session, book_id=book.id)
    payload = _sanitize_story_skeleton_payload(payload)
    payload = propagate_core_term_changes(previous_values, payload)
    premise = str(payload.get("premise") or "").strip()
    if not premise:
        raise ValueError("一句话核心设定不能为空")
    reader_promise = str(payload.get("reader_promise") or "").strip()
    world_engine = str(payload.get("world_engine") or "").strip()
    protagonist_engine = str(payload.get("protagonist_engine") or "").strip()
    conflict_engine = str(payload.get("conflict_engine") or "").strip()
    foundation = _latest_foundation_for_dashboard(session, book_id=book.id)
    if not foundation:
        foundation = StoryFoundation(book_id=book.id, premise=premise)
        session.add(foundation)
    foundation.premise = premise
    foundation.reader_promise = reader_promise
    foundation.world_engine = world_engine
    foundation.protagonist_engine = protagonist_engine
    foundation.conflict_engine = conflict_engine
    foundation.status = "draft"

    existing_bible = get_story_bible(session, book_id=book.id)
    forbidden_rules = strip_story_dna_blocks(str(payload.get("forbidden_rules") or "").strip())
    style_guide = strip_story_dna_blocks(str(payload.get("style_guide") or "").strip())
    existing_dna_display = story_dna_display_fields(style_guide=existing_bible.style_guide if existing_bible else "", forbidden_rules=existing_bible.forbidden_rules if existing_bible else "")
    existing_profile_display = story_bible_display_fields(
        style_guide=existing_dna_display["style_guide"],
        forbidden_rules=existing_dna_display["forbidden_rules"],
    )
    forbidden_rules = existing_dna_display["forbidden_rules"] if forbidden_rules == (existing_bible.forbidden_rules if existing_bible else "") else forbidden_rules
    style_guide = existing_dna_display["style_guide"] if style_guide == (existing_bible.style_guide if existing_bible else "") else style_guide
    existing_display = story_bible_display_fields(style_guide=style_guide if style_guide else (existing_bible.style_guide if existing_bible else ""), forbidden_rules=forbidden_rules if forbidden_rules else (existing_bible.forbidden_rules if existing_bible else ""))
    aesthetic_profile = (
        str(payload.get("aesthetic_profile") or "").strip()
        or profile_from_story_text(style_guide=style_guide, forbidden_rules=forbidden_rules)
        or existing_profile_display["aesthetic_profile"]
        or existing_display["aesthetic_profile"]
    )
    story_dna = strip_aesthetic_profile_blocks(str(payload.get("story_dna") or "").strip()) or existing_dna_display["story_dna"] or build_story_dna_from_skeleton(
        {
            "premise": premise,
            "reader_promise": reader_promise,
            "world_engine": world_engine,
            "protagonist_engine": protagonist_engine,
            "conflict_engine": conflict_engine,
            "forbidden_rules": forbidden_rules,
            "style_guide": style_guide,
            "volume_summary": str(payload.get("volume_summary") or "").strip(),
            "arc_goal": str(payload.get("arc_goal") or "").strip(),
            "arc_climax": str(payload.get("arc_climax") or "").strip(),
            "arc_turn": str(payload.get("arc_turn") or "").strip(),
        },
        genre=book.genre,
    )
    bible = upsert_story_bible(
        session,
        book_id=book.id,
        positioning=premise,
        reader_promise=reader_promise,
        main_plot=conflict_engine or premise,
        protagonist_arc=protagonist_engine,
        power_curve=world_engine,
        forbidden_rules=strip_aesthetic_profile_blocks(strip_story_dna_blocks(forbidden_rules)),
        style_guide="\n\n".join(
            item
            for item in [merge_style_with_aesthetic_profile(strip_story_dna_blocks(style_guide), aesthetic_profile), story_dna]
            if item
        ),
        status="draft",
    )
    volume = create_volume(
        session,
        book_id=book.id,
        volume_number=1,
        title=str(payload.get("volume_title") or "第一卷").strip(),
        summary=str(payload.get("volume_summary") or "").strip(),
    )
    arc = create_story_arc(
        session,
        book_id=book.id,
        arc_number=1,
        title=str(payload.get("arc_title") or "开局破局").strip(),
        start_chapter=1,
        end_chapter=5,
        goal=str(payload.get("arc_goal") or "").strip(),
        climax=str(payload.get("arc_climax") or "").strip(),
        turn=str(payload.get("arc_turn") or "").strip(),
        volume_number=1,
    )
    _sync_skeleton_canon(
        session,
        book_id=book.id,
        premise=premise,
        world_engine=world_engine,
        protagonist_engine=protagonist_engine,
        conflict_engine=conflict_engine,
    )
    session.flush()
    return {
        "foundation_id": foundation.id,
        "story_bible_id": bible.id,
        "volume_id": volume.id,
        "story_arc_id": arc.id,
    }


def _sanitize_story_skeleton_payload(payload: dict) -> dict:
    return normalize_story_skeleton_payload(payload)


def _sanitize_skeleton_repair_payload(payload: dict) -> dict:
    skeleton = payload.get("skeleton") or payload.get("repaired_skeleton") or {}
    if not isinstance(skeleton, dict):
        return payload
    cleaned = _sanitize_story_skeleton_payload(skeleton)
    after = audit_skeleton_sources({f"repair.{key}": str(value or "") for key, value in cleaned.items()})
    updated = dict(payload)
    updated["skeleton"] = cleaned
    updated["repaired_skeleton"] = cleaned
    updated["passed"] = after.passed
    updated["score"] = after.score
    updated["after"] = after.to_dict()
    return updated


def _preserve_skeleton_identity_fields(repaired: dict, current_skeleton: dict) -> dict:
    updated = dict(repaired or {})
    current = current_skeleton or {}
    for key in ("aesthetic_profile", "story_dna"):
        if not str(updated.get(key) or "").strip() and str(current.get(key) or "").strip():
            updated[key] = str(current.get(key) or "").strip()
    return updated


def _preserve_skeleton_identity_fields_in_payload(payload: dict, current_skeleton: dict) -> dict:
    skeleton = payload.get("skeleton") or payload.get("repaired_skeleton") or {}
    if not isinstance(skeleton, dict):
        return payload
    repaired = _preserve_skeleton_identity_fields(skeleton, current_skeleton)
    updated = dict(payload)
    updated["skeleton"] = repaired
    updated["repaired_skeleton"] = repaired
    return updated


def _sync_skeleton_canon(
    session,
    *,
    book_id: int,
    premise: str,
    world_engine: str,
    protagonist_engine: str,
    conflict_engine: str,
) -> None:
    character = session.scalar(select(Character).where(Character.book_id == book_id, Character.name == "主角"))
    if character:
        character.personality = protagonist_engine or character.personality
        character.background = premise or character.background
        character.ability = "按最新生产骨架执行；能力收益、限制和代价必须在正文中可见。"

    world_rule = session.scalar(select(WorldRule).where(WorldRule.book_id == book_id, WorldRule.category == "生产底线"))
    if world_rule:
        world_rule.rule_text = world_engine or world_rule.rule_text

    power = session.scalar(select(PowerSystem).where(PowerSystem.book_id == book_id, PowerSystem.name == "核心能力"))
    if power:
        power.rules = world_engine or power.rules
        power.costs = "能力使用必须对应最新生产骨架中的代价，不得沿用已废弃旧设定。"
        power.limits = "以最新 Story Bible 为准；旧章节或旧质检中的名词只作历史参考，不能强制保留。"

    thread = session.scalar(select(PlotThread).where(PlotThread.book_id == book_id, PlotThread.name == "主线压力"))
    if thread:
        thread.description = conflict_engine or thread.description


def _suggest_story_skeleton(*, book: Book, revision_idea: str, current_skeleton: dict) -> dict:
    idea = revision_idea.strip()
    if not idea:
        raise ValueError("请先写一点你的修改想法或不满意点")
    current = json.dumps(current_skeleton, ensure_ascii=False, indent=2)
    prompt = f"""
你是番茄小说男频主编兼新书策划。用户不擅长填写专业设定表，你要把他的自然语言修改想法，整理成一份可直接用于生产系统的故事骨架草案。

作品：{book.title}
类型：{book.genre}
平台：{book.target_platform}

用户修改想法：
{idea}

当前骨架 JSON：
{current}

请只输出 JSON 对象，不要解释。字段必须是：
premise, reader_promise, world_engine, protagonist_engine, conflict_engine,
forbidden_rules, style_guide, aesthetic_profile, story_dna,
volume_title, volume_summary, arc_title, arc_goal, arc_climax, arc_turn

要求：
- 不要照抄用户原话，要整理成可执行的生产骨架。
- premise 用一句话说清主角、能力/核心钩子、主要冲突。
- reader_promise 要是读者能持续期待的爽点/情绪/钩子。
- world_engine 写规则和限制，不写百科。
- protagonist_engine 写主角欲望、缺陷、主动性和成长方向。
- conflict_engine 写长期压力来源和升级方式。
- forbidden_rules 写必须避开的套路、违和点、设定破坏。
- style_guide 写正文风格、节奏、信息呈现方式。
- aesthetic_profile 单独写本书的审美画像、题材主味、氛围边界；不要塞进 style_guide。
- story_dna 单独写本书每章都要兑现的核心发动机；不要塞进 style_guide。
- 先消解骨架内部矛盾：不要把某个职业、能力或单一桥段写成万能解法；如果世界规则强调真实人物和真实因果，就不能又要求主角骗取/刷取/让本地人配合表演。
- 主角能力必须有边界、失败条件和代价；卖点要能产生多种场景，不要把后续章节锁死在同一桥段。
- 第一卷和剧情段要能支撑前 5 章生产，开局压力要具体。
- 每个字段控制在 120 个汉字以内。
""".strip()
    try:
        provider = ArkOpenAIProvider()
        response = provider.generate(
            prompt,
            max_tokens=2200,
            temperature=settings.llm_planning_temperature,
            model=settings.llm_planning_model,
            response_format={"type": "json_object"},
        )
        data = json.loads(_json_object_text(response.text))
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        if "Connection error" in message or type(exc).__name__ == "APIConnectionError":
            raise ValueError("模型连接失败：请检查网络、代理或 ARK 配置后再试。") from exc
        raise ValueError(f"AI 骨架建议失败：{type(exc).__name__}: {message}") from exc
    if not isinstance(data, dict):
        raise ValueError("AI 骨架建议没有返回 JSON 对象")
    return apply_revision_idea_to_skeleton(
        _clean_story_skeleton(data, current_skeleton=current_skeleton),
        revision_idea=idea,
    )


def _repair_story_skeleton_with_ai(session, *, book_id: int, current_skeleton: dict, revision_idea: str = "") -> dict:
    if not book_id:
        raise ValueError("book_id is required for AI skeleton repair")
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    rule_preview = repair_story_skeleton_with_market_evidence(session, book_id=book_id, skeleton=current_skeleton)
    market_context = rule_preview.get("market_context") or {}
    before = rule_preview.get("before") or {}
    issues = before.get("issues") or []
    current = json.dumps(current_skeleton, ensure_ascii=False, indent=2)
    issue_text = json.dumps(issues[:12], ensure_ascii=False, indent=2)
    market_text = json.dumps(market_context, ensure_ascii=False, indent=2)
    prompt = f"""
你是男频网文主编兼生产系统架构师。请把当前作品骨架修成一份可直接用于后续章节生产的执行骨架。

作品：{book.title}
类型：{book.genre}
平台：{book.target_platform}

当前骨架 JSON：
{current}

作者修改意见：
{revision_idea.strip() or "无"}

骨架审计问题：
{issue_text}

可用市场/读者信号：
{market_text}

请只输出 JSON 对象，不要解释。字段必须是：
premise, reader_promise, world_engine, protagonist_engine, conflict_engine,
forbidden_rules, style_guide, aesthetic_profile, story_dna,
volume_title, volume_summary, arc_title, arc_goal, arc_climax, arc_turn

修复要求：
- 必须针对审计问题改，不要输出通用模板。
- 保留作者原始核心卖点；只修复矛盾、过强外挂、主角被动、长篇续航不足、平台可读性不足。
- 市场信号只能帮助调整开篇压力、读者承诺、章末钩子和避雷，不得替代本书核心方向。
- 每个字段都要能指导系统执行，不要写后台术语、审计术语或空泛口号。
- 主角能力、职业、系统、重生、预知、推演等卖点若存在，必须写出边界、失败条件和代价。
- 第一卷必须能支撑至少 5 章不同场景发动机。
- forbidden_rules 要写清绝对不能写什么。
- aesthetic_profile 必须保留或修正本书审美画像、题材主味和氛围边界；如果当前已有，不要删空。
- story_dna 必须保留或修正本书长期章节发动机；如果当前已有，不要删空。
- 每个字段控制在 160 个汉字以内。
""".strip()
    provider = ArkOpenAIProvider()
    response = provider.generate(
        prompt,
        max_tokens=2600,
        temperature=settings.llm_planning_temperature,
        model=settings.llm_planning_model,
        response_format={"type": "json_object"},
    )
    data = json.loads(_json_object_text(response.text))
    if not isinstance(data, dict):
        raise ValueError("AI 修复没有返回 JSON 对象")
    repaired = apply_revision_idea_to_skeleton(
        _clean_story_skeleton(data, current_skeleton=current_skeleton),
        revision_idea=revision_idea,
    )
    repaired = _preserve_skeleton_identity_fields(repaired, current_skeleton)
    repaired = _apply_market_context_to_ai_skeleton(repaired, market_context)
    after = audit_skeleton_sources({f"ai.{key}": str(value or "") for key, value in repaired.items()})
    guarded_by_rules = False
    model_attempt = {
        "skeleton": repaired,
        "passed": after.passed,
        "score": after.score,
        "after": after.to_dict(),
    }
    if not after.passed:
        repaired = dict(rule_preview.get("skeleton") or rule_preview.get("repaired_skeleton") or {})
        repaired = _apply_market_context_to_ai_skeleton(repaired, market_context)
        repaired = apply_revision_idea_to_skeleton(repaired, revision_idea=revision_idea)
        repaired = _preserve_skeleton_identity_fields(repaired, current_skeleton)
        after = audit_skeleton_sources({f"guarded.{key}": str(value or "") for key, value in repaired.items()})
        guarded_by_rules = True
    payload = {
        **rule_preview,
        "skeleton": repaired,
        "passed": after.passed,
        "score": after.score,
        "after": after.to_dict(),
        "applied_strategy": "live_model_with_rule_guard" if guarded_by_rules else "live_model_repair",
        "llm": {
            "provider": response.provider,
            "model": response.model,
            "request_id": response.request_id,
            "elapsed_ms": response.elapsed_ms,
        },
        "model_attempt": model_attempt if guarded_by_rules else {},
    }
    return payload


def _apply_market_context_to_ai_skeleton(skeleton: dict, market_context: dict) -> dict:
    signal_count = int((market_context or {}).get("signal_count") or 0)
    if signal_count < 1:
        return skeleton
    repaired = dict(skeleton)
    expectations = [str(item).strip() for item in (market_context.get("expectations") or []) if str(item).strip()]
    avoid_rules = [str(item).strip() for item in (market_context.get("avoid_rules") or []) if str(item).strip()]
    if expectations:
        repaired["reader_promise"] = _append_text(
            repaired.get("reader_promise", ""),
            "平台读者预期：" + "；".join(expectations[:3]),
        )
        repaired["style_guide"] = _append_text(
            repaired.get("style_guide", ""),
            "市场执行要求：开篇压力、爽点回报和章末钩子必须在场景行动中可见。",
        )
        repaired["arc_goal"] = _append_text(
            repaired.get("arc_goal", ""),
            "前五章每章都要完成一个可见读者回报，并留下章末钩子。",
        )
    if avoid_rules:
        repaired["forbidden_rules"] = _append_text(
            repaired.get("forbidden_rules", ""),
            "市场避雷：" + "；".join(avoid_rules[:3]),
        )
    return repaired


def _append_text(value: str, addition: str) -> str:
    base = str(value or "").strip()
    extra = str(addition or "").strip()
    if not extra or extra in base:
        return base
    return f"{base} {extra}".strip()


def _clean_story_skeleton(data: dict, *, current_skeleton: dict) -> dict:
    fields = [
        "premise",
        "reader_promise",
        "world_engine",
        "protagonist_engine",
        "conflict_engine",
        "forbidden_rules",
        "style_guide",
        "aesthetic_profile",
        "story_dna",
        "volume_title",
        "volume_summary",
        "arc_title",
        "arc_goal",
        "arc_climax",
        "arc_turn",
    ]
    cleaned: dict[str, str] = {}
    for field in fields:
        value = clean_generated_text(data.get(field) or current_skeleton.get(field) or "")
        cleaned[field] = value
    if not cleaned["premise"]:
        raise ValueError("AI 骨架建议缺少核心设定")
    if not cleaned["volume_title"]:
        cleaned["volume_title"] = "第一卷"
    if not cleaned["arc_title"]:
        cleaned["arc_title"] = "开局破局"
    return cleaned


SKELETON_APPROVAL_FIELDS = [
    ("premise", "一句话核心设定"), ("reader_promise", "读者承诺"), ("world_engine", "世界规则 / 能力曲线"),
    ("protagonist_engine", "主角动力 / 成长弧"), ("conflict_engine", "长期冲突 / 主线"), ("forbidden_rules", "禁忌规则"),
    ("style_guide", "文风指南"), ("aesthetic_profile", "审美画像 / 题材主味"), ("story_dna", "作品 DNA / 章节发动机库"), ("volume_summary", "第一卷摘要"),
    ("arc_goal", "剧情段目标"), ("arc_climax", "剧情段高潮"), ("arc_turn", "剧情段转折"),
]


def _skeleton_payload_from_update_payload(payload: dict) -> dict[str, str]:
    return _sanitize_story_skeleton_payload({key: str(payload.get(key) or "").strip() for key, _ in SKELETON_APPROVAL_FIELDS})


def _assert_skeleton_can_be_approved(skeleton: dict[str, str]) -> None:
    report = audit_skeleton_sources({f"form.{key}": str(value or "") for key, value in skeleton.items()})
    blockers = [issue for issue in report.issues if issue.severity == "blocker"]
    if blockers:
        messages = "；".join(f"{issue.code}: {issue.message}" for issue in blockers[:3])
        raise ValueError(f"骨架逻辑审计未通过，不能确认：{messages}")


def _approve_skeleton_items(session, *, book_id: int, key: str, current_skeleton: dict) -> int:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    allowed = {field for field, _ in SKELETON_APPROVAL_FIELDS}
    keys = sorted(allowed) if key == "all" else [key]
    approved = 0
    for item_key in keys:
        if item_key not in allowed:
            raise ValueError(f"unknown skeleton item: {item_key}")
        value = str(current_skeleton.get(item_key) or "").strip()
        if not value:
            continue
        record_platform_feedback(
            session,
            book_id=book_id,
            platform="dashboard",
            metric_name="skeleton_approval",
            metric_value=item_key,
            raw_text=value,
        )
        approved += 1
    session.flush()
    return approved


def _skeleton_approval_payload(session, *, book_id: int, skeleton: dict) -> list[dict]:
    latest: dict[str, str] = {}
    rows = session.scalars(
        select(PlatformFeedback)
        .where(
            PlatformFeedback.book_id == book_id,
            PlatformFeedback.metric_name == "skeleton_approval",
        )
        .order_by(PlatformFeedback.id.desc())
    )
    for item in rows:
        latest.setdefault(item.metric_value, item.raw_text)
    return [
        {
            "key": key,
            "label": label,
            "value": str(skeleton.get(key) or "").strip(),
            "approved": bool(str(skeleton.get(key) or "").strip()) and latest.get(key) == str(skeleton.get(key) or "").strip(),
        }
        for key, label in SKELETON_APPROVAL_FIELDS
    ]


def _latest_foundation_for_dashboard(session, *, book_id: int):
    from app.models.entities import StoryFoundation

    return session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))


def _brainstorm_new_book_ideas(
    *,
    idea_prompt: str,
    feedback: str,
    seed_title: str,
    genre: str,
    reader_promise: str,
    current_ideas: list,
) -> list[dict]:
    current_text = json.dumps(current_ideas[:3], ensure_ascii=False, indent=2) if current_ideas else "[]"
    prompt = f"""
你是番茄小说男频新书策划。请基于用户的一段自然语言想法，生成 3 个彼此差异明显的新书方向。

用户输入：
- 自然语言想法：{idea_prompt or "未填写"}
- 暂定书名：{seed_title or "未定"}
- 类型：{genre or "玄幻脑洞"}
- 读者承诺：{reader_promise or "未定"}
- 对上一版方向的补充意见：{feedback or "无"}
- 上一版方向 JSON：{current_text}

要求：
- 只输出 JSON，不要解释。
- JSON 顶层为对象，字段 ideas 是数组，恰好 3 个元素。
- 每个元素包含这些字符串字段：
  title, genre, reader_promise, premise, world_engine, protagonist_engine, conflict_engine
- 方向要有番茄节奏：开局压力明确，能力/金手指有爽点但有代价，每章可留钩子。
- 如果用户写了补充意见，必须明显吸收补充意见，并避开用户不想要的方向。
- 3 个方向要区分：一个偏强爽点，一个偏悬疑反转，一个偏人物成长或情绪张力。
- 不要抄袭已知作品，不要出现“模板”“AI”“系统提示”等元叙事说明。
""".strip()
    try:
        provider = ArkOpenAIProvider()
        response = provider.generate(
            prompt,
            max_tokens=3200,
            temperature=max(0.65, settings.llm_planning_temperature),
            model=settings.llm_planning_model,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        message = str(exc) or type(exc).__name__
        if "Connection error" in message or type(exc).__name__ == "APIConnectionError":
            raise ValueError("模型连接失败：当前无法连接到真实模型服务。请检查网络、代理或 ARK 配置后再试。") from exc
        raise ValueError(f"AI 构思调用失败：{type(exc).__name__}: {message}") from exc
    try:
        data = _load_brainstorm_json(provider, response.text)
    except ValueError:
        return _brainstorm_new_book_ideas_individually(
            provider,
            idea_prompt=idea_prompt,
            feedback=feedback,
            seed_title=seed_title,
            genre=genre,
            reader_promise=reader_promise,
            current_text=current_text,
        )
    ideas = data.get("ideas") if isinstance(data, dict) else None
    if not isinstance(ideas, list):
        raise ValueError("AI 构思结果缺少 ideas 数组")
    cleaned = []
    for item in ideas[:3]:
        if not isinstance(item, dict):
            continue
        cleaned.append(_clean_idea(item, seed_title=seed_title, genre=genre))
    if not cleaned:
        raise ValueError("AI 构思没有返回可用方向")
    return cleaned


def _brainstorm_new_book_ideas_individually(
    provider: ArkOpenAIProvider,
    *,
    idea_prompt: str,
    feedback: str,
    seed_title: str,
    genre: str,
    reader_promise: str,
    current_text: str,
) -> list[dict]:
    angles = [
        "强爽点：开局压力强，能力带来明确爽感，但每次使用都有代价。",
        "悬疑反转：核心能力背后有秘密，开局事件能牵出更大的真相。",
        "人物成长：主角的欲望、缺陷和关系压力更突出，爽点服务于成长弧线。",
    ]
    ideas: list[dict] = []
    for angle in angles:
        prompt = f"""
你是番茄小说男频新书策划。请只生成 1 个新书方向。

用户自然语言想法：{idea_prompt or "未填写"}
暂定书名：{seed_title or "未定"}
类型：{genre or "玄幻脑洞"}
读者承诺：{reader_promise or "未定"}
补充意见：{feedback or "无"}
上一版方向 JSON：{current_text}
本次角度：{angle}

只输出一个 JSON 对象，不要解释。字段必须是：
title, genre, reader_promise, premise, world_engine, protagonist_engine, conflict_engine

每个字段控制在 80 个汉字以内，避免长段落，避免抄袭已知作品，避免出现“模板”“AI”“系统提示”等元叙事说明。
""".strip()
        try:
            response = provider.generate(
                prompt,
                max_tokens=900,
                temperature=max(0.65, settings.llm_planning_temperature),
                model=settings.llm_planning_model,
                response_format={"type": "json_object"},
            )
            data = json.loads(_json_object_text(response.text))
        except Exception as exc:
            raise ValueError(f"AI 构思逐条生成失败：{type(exc).__name__}: {exc}") from exc
        ideas.append(_clean_idea(data, seed_title=seed_title, genre=genre))
    return ideas


def _clean_idea(item: dict, *, seed_title: str, genre: str) -> dict:
    if not isinstance(item, dict):
        item = {}
    return {
        "title": str(item.get("title") or seed_title or ""),
        "genre": str(item.get("genre") or genre or "玄幻脑洞"),
        "reader_promise": str(item.get("reader_promise") or ""),
        "premise": str(item.get("premise") or ""),
        "world_engine": str(item.get("world_engine") or ""),
        "protagonist_engine": str(item.get("protagonist_engine") or ""),
        "conflict_engine": str(item.get("conflict_engine") or ""),
    }


def _json_object_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _load_brainstorm_json(provider: ArkOpenAIProvider, text: str) -> dict:
    try:
        return json.loads(_json_object_text(text))
    except json.JSONDecodeError as first_exc:
        repair_prompt = f"""
下面是一段模型输出，目标是番茄小说新书构思 JSON，但它不是合法 JSON。
请修复成合法 JSON。只输出 JSON 对象，不要解释。
格式必须是：
{{"ideas":[{{"title":"","genre":"","reader_promise":"","premise":"","world_engine":"","protagonist_engine":"","conflict_engine":""}}]}}
ideas 必须恰好 3 个元素。

待修复内容：
{text}
""".strip()
        try:
            repaired = provider.generate(
                repair_prompt,
                max_tokens=2600,
                temperature=0,
                model=settings.llm_planning_model,
                response_format={"type": "json_object"},
            )
            return json.loads(_json_object_text(repaired.text))
        except Exception as repair_exc:
            raise ValueError(
                f"AI 构思返回内容不是合法 JSON，自动修复也失败：{first_exc}"
            ) from repair_exc


def _chapter_detail(session, *, book_id: int, chapter_number: int) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return {"chapter": None, "latest_brief": None, "latest_quality": None, "versions": [], "generation_tasks": []}
    latest_brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    versions = list(
        session.scalars(
            select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()).limit(8)
        )
    )
    latest_version = versions[0] if versions else None
    latest_quality = (
        session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == latest_version.id)
            .order_by(QualityReport.id.desc())
        )
        if latest_version
        else None
    )
    tasks = _generation_tasks_for_chapter(session, book_id=book_id, chapter_number=chapter_number, limit=8)
    production_review = production_run_review_payload(latest_production_run_review(session, chapter_id=chapter.id))
    production_pattern_memory = build_production_pattern_memory(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        limit=8,
    )
    bias_audit = _chapter_bias_payload(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        brief=latest_brief,
        version=latest_version,
    )
    acceptance_audit = _acceptance_payload(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        brief=latest_brief,
        version=latest_version,
    )
    preflight = _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)
    quality_data = _loads_json(latest_quality.report) if latest_quality else None
    if latest_quality and quality_data is not None:
        quality_data = {**quality_data, "score": latest_quality.score, "passed": latest_quality.passed}
    failure_attribution = attribute_generation_failure(
        quality=quality_data,
        bias=bias_audit,
        intent=acceptance_audit,
        preflight=preflight,
    )
    director_sheet = _director_sheet_payload(
        session,
        book_id=book_id,
        chapter=chapter,
        brief=latest_brief,
    )
    author_workbench = build_author_workbench_report(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
    ).to_dict()
    chapter_samples = latest_chapter_samples(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        limit=3,
    )
    writer_loop = build_writer_loop_plan(
        chapter_number=chapter_number,
        goal=latest_brief.goal if latest_brief else "",
        required_beats=latest_brief.required_beats if latest_brief else "",
        constraints=latest_brief.constraints if latest_brief else "",
        quality_report=quality_data,
        sample_report=(chapter_samples.get("diversity_report") or chapter_samples.get("fallback_diversity_report")),
        previous_content=latest_version.content if latest_version else "",
        mode="dashboard",
    ).to_dict()
    return {
        "chapter": {
            "id": chapter.id,
            "number": chapter.chapter_number,
            "title": chapter.title,
            "status": chapter.status,
            "summary": chapter.summary,
        },
        "latest_brief": _brief_payload(latest_brief),
        "latest_quality": _quality_payload(latest_quality),
        "latest_version": _version_payload(latest_version),
        "production_preflight": preflight,
        "bias_audit": bias_audit,
        "acceptance_audit": acceptance_audit,
        "failure_attribution": failure_attribution,
        "director_sheet": director_sheet,
        "author_workbench": author_workbench,
        "writer_loop": writer_loop,
        "production_review": production_review,
        "production_pattern_memory": production_pattern_memory,
        "chapter_samples": chapter_samples,
        "chapter_sample_learning": build_chapter_sample_learning(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            limit=8,
        ),
        "model_strategy": _model_strategy_payload(),
        "version_diff": _version_diff_payload(versions),
        "versions": [
            _version_payload(version, include_content=False)
            for version in versions
        ],
        "generation_tasks": tasks,
    }


def _model_strategy_payload() -> dict:
    strategy = build_model_strategy()
    return {
        "planning_model": settings.llm_planning_model,
        "draft_model": settings.llm_draft_model,
        "revision_model": settings.llm_revision_model,
        "review_model": settings.llm_review_model,
        "draft_temperature": settings.llm_draft_temperature,
        "revision_temperature": settings.llm_revision_temperature,
        "review_temperature": settings.llm_review_temperature,
        "roles": strategy["roles"],
        "warnings": strategy["warnings"],
        "recommendations": strategy["recommendations"],
        "suggestion": "正文/整章重写优先看 draft/revision 模型；若方向对但文笔不稳，先调正文模型或温度；若能写但审稿不准，调 review 模型。",
    }


def _production_preflight_payload(session, *, book_id: int, chapter_number: int) -> dict:
    blockers: list[str] = []
    recommendations: list[str] = []
    if not book_id or not chapter_number:
        return {"passed": False, "blockers": ["缺少作品或章节"], "recommendations": ["先选择作品和当前章。"], "alignment": None}
    readiness = check_production_readiness(session, book_id=book_id, start=chapter_number, count=5, live_llm=False)
    evidence_gate = {
        "passed": readiness.passed,
        "blockers": [
            {"name": item.name, "detail": item.detail, "action": item.action}
            for item in readiness.blockers
        ],
        "warnings": [
            {"name": item.name, "detail": item.detail, "action": item.action}
            for item in readiness.warnings
        ],
    }
    for item in readiness.blockers:
        blockers.append(f"{item.name}: {item.detail}")
        if item.action:
            recommendations.append(item.action)
    for item in readiness.warnings:
        if item.action:
            recommendations.append(item.action)
    try:
        alignment = build_story_alignment_audit(session, book_id=book_id, chapter_limit=max(5, chapter_number))
    except Exception as exc:
        return {
            "passed": False,
            "blockers": [*blockers, f"方向审计失败：{exc}"],
            "recommendations": [*recommendations, "先查看作品设定和数据库状态。"],
            "alignment": None,
            "evidence_gate": evidence_gate,
        }
    if alignment.blockers:
        blockers.extend(alignment.blockers)
    recommendations.extend(alignment.recommendations)
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return {
            "passed": not blockers,
            "blockers": blockers,
            "recommendations": recommendations,
            "alignment": {"status": alignment.status, "score": alignment.score},
            "evidence_gate": evidence_gate,
        }
    brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    bias = _chapter_bias_payload(session, book_id=book_id, chapter_number=chapter_number, brief=brief, version=version)
    if bias.get("blockers"):
        blockers.extend(bias["blockers"])
    if not brief:
        recommendations.append("当前章没有 brief，继续生产会先自动创建。")
    return {
        "passed": not blockers,
        "blockers": blockers,
        "recommendations": _dedupe(recommendations),
        "alignment": {"status": alignment.status, "score": alignment.score},
        "evidence_gate": evidence_gate,
    }


def _repair_readiness_gate_action(session, payload: dict) -> dict:
    book_id = int(payload.get("book_id") or 0)
    chapter_number = int(payload.get("chapter_number") or 1)
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    platform = str(payload.get("platform") or "").strip() or book.target_platform or "番茄小说"
    query = str(payload.get("market_query") or "").strip() or f"{platform} {book.genre or '网文'} 最新爆款 趋势 开篇 卖点 避雷"
    before = check_production_readiness(session, book_id=book_id, start=chapter_number, count=5, live_llm=False)
    steps: list[dict] = []

    needs_market = any(check.name == "evidence" and (not check.passed or check.severity == "warning") for check in before.checks)
    if needs_market:
        market = ensure_market_research_evidence(
            session,
            genre=book.genre or "未分类",
            query=query,
            platform=platform,
            auto_search=True,
        )
        steps.append({"name": "market_evidence", **market.get("step", {})})

    needs_memory = any(check.name == "semantic_memory" and check.severity == "warning" for check in before.checks)
    if needs_memory:
        memory = index_book_knowledge(session, book_id=book_id, dry_run=True, reset=True)
        steps.append({"name": "semantic_memory", "status": "rebuilt", "indexed_count": memory.get("indexed_count", 0)})

    needs_canon = any(check.name == "canon" and not check.passed for check in before.checks)
    if needs_canon:
        scaffold = repair_production_scaffold(
            session,
            book_id=book_id,
            only_missing=True,
            approve_skeleton=False,
            chapter_count=5,
            apply=True,
        )
        steps.append(
            {
                "name": "canon_scaffold",
                "status": "repaired",
                "created_count": scaffold.get("created_count", 0),
                "upgraded_count": scaffold.get("upgraded_count", 0),
            }
        )

    skeleton_preview = None
    needs_skeleton = any(
        check.name in {"skeleton_governance", "skeleton_approval", "foundation", "story_bible"}
        and (not check.passed or check.severity == "warning")
        for check in before.checks
    )
    if needs_skeleton:
        current_skeleton = _current_story_skeleton_values(session, book_id=book_id)
        approval_only = any(check.name == "skeleton_approval" and not check.passed for check in before.checks) and not any(
            check.name in {"foundation", "story_bible", "skeleton_governance"} and not check.passed
            for check in before.checks
        )
        if approval_only:
            _assert_skeleton_can_be_approved(current_skeleton)
            approved_count = _approve_skeleton_items(session, book_id=book_id, key="all", current_skeleton=current_skeleton)
            steps.append({"name": "skeleton_approval", "status": "synced", "approved_count": approved_count})
        else:
            skeleton_preview = repair_story_skeleton_with_market_evidence(session, book_id=book_id, skeleton=current_skeleton)
            market_count = int((skeleton_preview.get("market_context") or {}).get("signal_count") or 0)
            steps.append(
                {
                    "name": "skeleton_repair_preview",
                    "status": "created",
                    "passed": bool(skeleton_preview.get("passed")),
                    "market_signal_count": market_count,
                    "score_before": (skeleton_preview.get("before") or {}).get("score"),
                    "score_after": (skeleton_preview.get("after") or {}).get("score"),
                }
            )

    after = check_production_readiness(session, book_id=book_id, start=chapter_number, count=5, live_llm=False)
    return {
        "status": "completed",
        "message": _readiness_repair_message(steps=steps, after=after),
        "steps": steps,
        "before": _readiness_payload(before),
        "after": _readiness_payload(after),
        "skeleton_preview": skeleton_preview,
    }


def _auto_resolve_author_blocker_action(session, payload: dict) -> dict:
    book_id = int(payload.get("book_id") or 0)
    chapter_number = int(payload.get("chapter_number") or 1)
    platform = str(payload.get("platform") or "").strip() or "番茄小说"
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")

    steps: list[dict] = []
    recovered = recover_stale_generation_tasks(session, timeout_seconds=3600, limit=20)
    if recovered:
        steps.append(
            {
                "name": "recover_stale_tasks",
                "status": "completed",
                "count": len(recovered),
                "message": f"已恢复 {len(recovered)} 个卡住的后台任务。",
            }
        )

    retried = 0
    failed_payload = _failed_tasks_payload(session, book_id=book_id)
    for item in failed_payload.get("items", []):
        if not item.get("is_queue_task"):
            continue
        try:
            retry_generation_queue_task(session, task_id=int(item.get("id") or 0))
            retried += 1
        except ValueError:
            continue
    if retried:
        steps.append(
            {
                "name": "retry_failed_queue_tasks",
                "status": "completed",
                "count": retried,
                "message": f"已把 {retried} 个失败生成任务放回待处理队列。",
            }
        )

    readiness_result = _repair_readiness_gate_action(
        session,
        {
            "book_id": book_id,
            "chapter_number": chapter_number,
            "platform": platform,
        },
    )
    readiness_steps = readiness_result.get("steps") or []
    if readiness_steps:
        steps.append(
            {
                "name": "repair_readiness",
                "status": readiness_result.get("status", "completed"),
                "count": len(readiness_steps),
                "message": readiness_result.get("message", "已整理生产准备。"),
            }
        )

    route = prepare_production(session, book_id=book_id, chapter_number=chapter_number, platform=platform).to_dict()
    message = _auto_resolve_message(steps=steps, route=route, readiness_result=readiness_result)
    return {
        "status": "resolved" if route.get("can_continue") or route.get("primary_intent") in {"continue", "wait", "approve"} else "needs_author",
        "message": message,
        "steps": steps,
        "router": route,
        "skeleton_preview": readiness_result.get("skeleton_preview"),
    }


def _auto_resolve_message(*, steps: list[dict], route: dict, readiness_result: dict) -> str:
    skeleton_preview = readiness_result.get("skeleton_preview")
    if skeleton_preview:
        return "系统已生成设定修复草案；需要你确认是否启用新版设定。"
    if route.get("can_continue"):
        return "系统已处理当前打断项，可以继续写作。"
    if route.get("primary_intent") == "wait":
        return "系统已处理当前打断项，后台正在运行，等待自动刷新。"
    if route.get("primary_intent") == "approve":
        return "系统已处理当前打断项，当前章需要阅读确认。"
    if steps:
        return "系统已处理可自动解决的打断项；仍有一项需要确认。"
    return "当前没有可自动处理的打断项；请查看系统给出的唯一下一步。"


def _readiness_repair_message(*, steps: list[dict], after) -> str:
    if not steps:
        return "生产前准备已满足当前门禁，无需自动补齐。"
    blockers = len(after.blockers)
    warnings = len(after.warnings)
    return f"生产前准备补齐完成：执行 {len(steps)} 步，剩余硬阻断 {blockers} 项，建议补齐 {warnings} 项。"


def _readiness_payload(report) -> dict:
    return {
        "passed": report.passed,
        "blocker_count": len(report.blockers),
        "warning_count": len(report.warnings),
        "checks": [
            {
                "name": item.name,
                "passed": item.passed,
                "detail": item.detail,
                "severity": item.severity,
                "action": item.action,
            }
            for item in report.checks
        ],
    }


def _current_story_skeleton_values(session, *, book_id: int) -> dict[str, str]:
    return canonical_current_skeleton_values(session, book_id=book_id)


def _preflight_only_model_drift(preflight: dict) -> bool:
    blockers = [str(item) for item in preflight.get("blockers", [])]
    return bool(blockers) and all(item.startswith("model_default_drift:") for item in blockers)


def _auto_repair_preflight_if_needed(session, *, book_id: int, chapter_number: int, preflight: dict) -> dict:
    blockers = [str(item) for item in preflight.get("blockers", [])]
    if not blockers or not _preflight_only_repairable_brief_blockers(blockers):
        return preflight
    repair_chapters = _repairable_brief_chapters(blockers, fallback=chapter_number)
    repaired = []
    for number in repair_chapters:
        if number < 1:
            continue
        repair_chapter_brief(session, book_id=book_id, chapter_number=number)
        repaired.append(number)
    if repaired:
        session.commit()
    return _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)


def _preflight_only_repairable_brief_blockers(blockers: list[str]) -> bool:
    return all(
        "最新章节 brief 仍含旧质检/旧修订合同残留" in item
        or "章节 brief 未显式承接核心作者意图" in item
        or "brief 未承接当前骨架锚点" in item
        for item in blockers
    )


def _repairable_brief_chapters(blockers: list[str], *, fallback: int) -> list[int]:
    numbers: list[int] = []
    for blocker in blockers:
        if ":" not in blocker:
            continue
        tail = blocker.rsplit(":", 1)[1]
        for part in tail.replace("，", ",").split(","):
            part = part.strip()
            if part.isdigit():
                numbers.append(int(part))
    if not numbers and fallback:
        numbers.append(fallback)
    return sorted(set(numbers))


def _create_model_drift_revision_brief(session, *, book_id: int, chapter_number: int, blockers: list[str]) -> None:
    drift = "，".join(item.split(":", 1)[1] if ":" in item else item for item in blockers)
    note = "\n".join(
        [
            "自动偏差修复：当前可读稿触发模型默认套路偏差。",
            f"需要删除或替换这些表达/写法：{drift}",
            "修订要求：保留现有可读场景、人物关系、追逃压力和章末危机，只把偏向网游系统文、刷经验、任务流的表达改成真实江湖语境。",
            "验收方式：下一版不能出现上述偏差词；人物行动必须仍然由江湖因果、门派恩怨、修炼代价和现场选择推动。",
        ]
    )
    submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        suggestion_text=note,
        platform="dashboard-auto-bias",
        revision_mode="local_patch",
    )


def _chapter_bias_payload(
    session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief | None,
    version: ChapterVersion | None,
) -> dict:
    canon_context, _ = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    report = evaluate_generation_bias(
        content=version.content if version else "",
        goal=brief.goal if brief else "",
        required_beats=brief.required_beats if brief else "",
        constraints=brief.constraints if brief else "",
        canon_context=canon_context,
    )
    return report.to_dict()


def _acceptance_payload(
    session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief | None,
    version: ChapterVersion | None,
) -> dict | None:
    if not brief or not version:
        return None
    canon_context, _ = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    author_preferences = format_author_preference_context(session, book_id=book_id)
    report = evaluate_author_intent(
        content=version.content or "",
        goal=brief.goal or "",
        required_beats=brief.required_beats or "",
        constraints=brief.constraints or "",
        canon_context=canon_context,
        author_preferences=author_preferences,
    )
    data = report.to_dict()
    data.setdefault("total_points", len(data.get("covered_points", [])) + len(data.get("missing_points", [])))
    data.setdefault("covered_count", len(data.get("covered_points", [])))
    return data


def _director_sheet_payload(session, *, book_id: int, chapter: Chapter, brief: ChapterBrief | None) -> str:
    if not brief:
        return ""
    book = session.get(Book, book_id)
    if not book:
        return ""
    revision_mode = _brief_revision_mode(brief)
    fresh_rewrite = revision_mode == "fresh"
    rewrite_mode = revision_mode in {"rewrite", "fresh"}
    packet = build_chapter_production_packet(
        session,
        book=book,
        chapter_number=chapter.chapter_number,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
        mode="fresh" if fresh_rewrite else ("revision" if brief.status == "revision_ready" else "draft"),
        revision_goal=brief.goal if brief.status == "revision_ready" else "",
        revision_required_beats=brief.required_beats if brief.status == "revision_ready" else "",
        revision_constraints=brief.constraints if brief.status == "revision_ready" else "",
        revision_context_mode=revision_mode if brief.status == "revision_ready" else "draft",
        fresh_rewrite=fresh_rewrite,
        rewrite_mode=rewrite_mode,
    )
    return packet.director_sheet


def _brief_revision_mode(brief: ChapterBrief) -> str:
    if brief.status != "revision_ready":
        return "draft"
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]).replace("：", ":")
    marker = "修订模式:"
    if marker not in text:
        return "targeted"
    tail = text.split(marker, 1)[1].strip()
    value = []
    for ch in tail:
        if ch.isascii() and (ch.isalpha() or ch == "_"):
            value.append(ch)
            continue
        break
    mode = "".join(value)
    return mode if mode in {"polish", "local_patch", "targeted", "rewrite", "fresh"} else "targeted"


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _update_chapter_brief_action(session, payload: dict) -> dict:
    book_id = int(payload.get("book_id") or 0)
    chapter_number = int(payload.get("chapter_number") or 0)
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    if not brief:
        raise ValueError("chapter brief not found")
    brief.goal = str(payload.get("goal") or "").strip()
    brief.required_beats = str(payload.get("required_beats") or "").strip()
    brief.constraints = str(payload.get("constraints") or "").strip()
    if brief.status not in {"revision_ready", "ready"}:
        brief.status = "ready"
    session.flush()
    return {"status": "saved", "brief_id": brief.id}


def _repair_current_chapter_brief_action(session, payload: dict) -> dict:
    book_id = int(payload.get("book_id") or 0)
    chapter_number = int(payload.get("chapter_number") or 0)
    brief = repair_chapter_brief(session, book_id=book_id, chapter_number=chapter_number)
    after = _production_preflight_payload(session, book_id=book_id, chapter_number=chapter_number)
    return {
        "status": "repaired" if after.get("passed") else "needs_attention",
        "brief_id": brief.id,
        "message": "已清理当前章生产说明；可以继续生产。" if after.get("passed") else "已生成干净生产说明，但仍有阻断项需要查看。",
        "blockers": after.get("blockers", []),
    }


def _restart_production_from_chapter_action(session, payload: dict) -> dict:
    book_id = int(payload.get("book_id") or 0)
    start_chapter = max(1, int(payload.get("start_chapter") or 1))
    return restart_production_from_chapter(session, book_id=book_id, start_chapter=start_chapter)


def _version_payload(version: ChapterVersion | None, *, include_content: bool = True) -> dict | None:
    if not version:
        return None
    payload = {
        "id": version.id,
        "version_number": version.version_number,
        "title": version.title,
        "status": version.status,
        "source": version.source,
        "content_chars": len(version.content),
        "created_at": version.created_at.isoformat() if version.created_at else None,
    }
    if include_content:
        payload["content"] = version.content
    return payload


def _feedback_payload(session, *, book_id: int) -> dict:
    summary = summarize_platform_feedback(session, book_id=book_id)
    feedback_items = list_platform_feedback(session, book_id=book_id, limit=20)
    adjustments = list_feedback_adjustments(session, book_id=book_id, limit=20)
    chapter_ids = {item.chapter_id for item in feedback_items if item.chapter_id}
    chapter_numbers = {}
    if chapter_ids:
        chapters = session.scalars(select(Chapter).where(Chapter.id.in_(chapter_ids)))
        chapter_numbers = {chapter.id: chapter.chapter_number for chapter in chapters}
    return {
        "summary": {
            "total": summary.total,
            "by_metric": summary.by_metric,
            "by_platform": summary.by_platform,
        },
        "items": [
            {
                "id": item.id,
                "chapter_id": item.chapter_id,
                "chapter_number": chapter_numbers.get(item.chapter_id),
                "platform": item.platform,
                "metric_name": item.metric_name,
                "metric_value": item.metric_value,
                "raw_text": item.raw_text,
            }
            for item in feedback_items
        ],
        "adjustments": [
            {
                "id": item.id,
                "target_chapter_number": item.target_chapter_number,
                "feedback_ids": item.feedback_ids,
                "status": item.status,
                "adjustment_text": item.adjustment_text,
            }
            for item in adjustments
        ],
    }


def _knowledge_payload(session, *, book_id: int, chapter_number: int) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    story_context, story_refs = format_story_control_context(session, book_id=book_id, chapter_number=chapter_number)
    canon_context, canon_refs = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    evidence_context, signal_ids = format_market_evidence_context(session, genre=book.genre)
    bible = get_story_bible(session, book_id=book_id)
    embedding_rows = []
    embedding_count = 0
    visual_assets = []
    try:
        embedding_rows = list(
            session.scalars(
                select(KnowledgeEmbedding)
                .where(KnowledgeEmbedding.book_id == book_id)
                .order_by(KnowledgeEmbedding.id.desc())
                .limit(5)
            )
        )
        embedding_count = session.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.book_id == book_id).count()
        visual_assets = list_visual_assets(session, book_id=book_id, limit=8)
    except OperationalError:
        session.rollback()
    return {
        "story_bible": {"id": bible.id, "status": bible.status} if bible else None,
        "skeleton": _story_skeleton_payload(session, book_id=book_id),
        "story_refs": story_refs,
        "canon_refs": canon_refs,
        "story_context": story_context,
        "canon_context": canon_context,
        "evidence_context": evidence_context,
        "market_signal_ids": signal_ids,
        "evidence_audit": [
            {
                "signal_id": item.signal_id,
                "usable": item.usable,
                "reasons": item.reasons,
                "source": item.source_key,
                "signal": item.signal_text,
            }
            for item in audit_market_evidence(session, genre=book.genre)
        ],
        "semantic_memory": {
            "count": embedding_count,
            "recent": [
                {
                    "id": item.id,
                    "source_type": item.source_type,
                    "source_label": item.source_label,
                    "model": item.model,
                    "dimensions": item.dimensions,
                }
                for item in embedding_rows
            ],
        },
        "visual_assets": [
            {
                "id": item.id,
                "asset_type": item.asset_type,
                "chapter_id": item.chapter_id,
                "status": item.status,
                "model": item.model,
                "artifact_path": item.artifact_path,
            }
            for item in visual_assets
        ],
        "web_search": web_search_status(),
    }


def _story_skeleton_payload(session, *, book_id: int) -> dict:
    foundation = _latest_foundation_for_dashboard(session, book_id=book_id)
    bible = get_story_bible(session, book_id=book_id)
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    dna_display = story_dna_display_fields(style_guide=bible.style_guide if bible else "", forbidden_rules=bible.forbidden_rules if bible else "")
    bible_display = story_bible_display_fields(style_guide=dna_display["style_guide"], forbidden_rules=dna_display["forbidden_rules"])
    story_dna = dna_display["story_dna"] or story_dna_for_book(session, book_id=book_id)
    skeleton_values = {
        "premise": foundation.premise if foundation else (bible.positioning if bible else ""),
        "reader_promise": foundation.reader_promise if foundation else (bible.reader_promise if bible else ""),
        "world_engine": foundation.world_engine if foundation else (bible.power_curve if bible else ""),
        "protagonist_engine": foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else ""),
        "conflict_engine": foundation.conflict_engine if foundation else (bible.main_plot if bible else ""),
        "forbidden_rules": bible_display["forbidden_rules"],
        "style_guide": bible_display["style_guide"],
        "aesthetic_profile": bible_display["aesthetic_profile"],
        "story_dna": story_dna,
        "volume_summary": volume.summary if volume else "",
        "arc_goal": arc.goal if arc else "",
        "arc_climax": arc.climax if arc else "",
        "arc_turn": arc.turn if arc else "",
    }
    payload = {
        "foundation": {
            "id": foundation.id,
            "premise": foundation.premise,
            "reader_promise": foundation.reader_promise,
            "world_engine": foundation.world_engine,
            "protagonist_engine": foundation.protagonist_engine,
            "conflict_engine": foundation.conflict_engine,
            "status": foundation.status,
        } if foundation else None,
        "story_bible": {
            "id": bible.id,
            "positioning": bible.positioning,
            "reader_promise": bible.reader_promise,
            "main_plot": bible.main_plot,
            "protagonist_arc": bible.protagonist_arc,
            "relationship_arc": bible.relationship_arc,
            "power_curve": bible.power_curve,
            "forbidden_rules": bible_display["forbidden_rules"],
            "style_guide": bible_display["style_guide"],
            "aesthetic_profile": bible_display["aesthetic_profile"],
            "story_dna": story_dna,
            "status": bible.status,
        } if bible else None,
        "volume": {
            "id": volume.id,
            "title": volume.title,
            "summary": volume.summary,
            "status": volume.status,
        } if volume else None,
        "story_arc": {
            "id": arc.id,
            "title": arc.title,
            "start_chapter": arc.start_chapter,
            "end_chapter": arc.end_chapter,
            "goal": arc.goal,
            "climax": arc.climax,
            "turn": arc.turn,
            "status": arc.status,
        } if arc else None,
    }
    payload["approvals"] = _skeleton_approval_payload(session, book_id=book_id, skeleton=skeleton_values)
    payload["versions"] = list_skeleton_versions(session, book_id=book_id)
    payload["governance"] = audit_story_skeleton_with_agent_evidence(session, book_id=book_id).to_dict()
    return payload

if __name__ == "__main__":
    raise SystemExit(main())
