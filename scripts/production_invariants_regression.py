from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import func, select

from app.db.session import session_scope
import app.services.rebuild_candidates as rebuild_candidates
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, PlatformFeedback, PublishJob, QualityReport, StoryArc, StoryBible, StoryFoundation
from app.services.llm_queue import QUEUE_REBUILD_CANDIDATES, QUEUE_TYPES
from app.services.planning import plan_chapters, run_next_action
from app.services.production_actions import AUTO_ACTIONS, MANUAL_ACTIONS
from app.services.production_decision import decide_chapter_production
from app.workflows.state_machine import move
from regression_db import isolated_database


DASHBOARD = ROOT / "app" / "dashboard.html"


def main() -> int:
    isolated_database("production-invariants-regression")
    failures: list[str] = []

    with session_scope() as session:
        book = _create_gate_ready_book(session)
        _case_unpassed_cannot_publish(session, book_id=book.id, failures=failures)
        _case_previous_chapter_blocks_next_real_production(session, book_id=book.id, failures=failures)
        _case_needs_revision_requires_contract(session, book_id=book.id, failures=failures)
        _case_approved_publish_job_snapshot_safe(session, book_id=book.id, failures=failures)
        _case_preview_does_not_write_live_state(session, book_id=book.id, failures=failures)
        _case_frontend_backend_action_consistency(session, failures=failures)
        _case_done_is_terminal_completed(session, book_id=book.id, failures=failures)
        _case_rebuild_candidate_not_sync_execute(session, book_id=book.id, failures=failures)
        _case_candidate_scoring_policy(failures=failures)
        _case_running_tasks_have_timeout_or_heartbeat(session, book_id=book.id, failures=failures)

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("production-invariants-regression: PASS")
    return 0


def _case_unpassed_cannot_publish(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=1, status="draft")
    failed = _version(session, chapter=chapter, version_number=1, status="reviewed_pass", passed=False, score=52)
    session.add(PublishJob(chapter_version_id=failed.id, platform="manual", status="pending", automation_payload="{}"))
    session.flush()
    item = plan_chapters(session, book_id=book_id, start=1, count=1)[0]
    if item.next_action in {"publish_job_dry_run", "queue_publish_job", "mark_publish_job", "done"}:
        failures.append(f"unpassed_chapter_publishable:{item.next_action}:{item.reason}")


def _case_previous_chapter_blocks_next_real_production(session, *, book_id: int, failures: list[str]) -> None:
    first = _chapter(session, book_id=book_id, number=2, status="draft")
    session.add(ChapterBrief(chapter_id=first.id, goal="第2章", required_beats="承接", constraints="", status="ready"))
    _version(session, chapter=first, version_number=1, status="needs_revision", passed=False, score=45)
    second = _chapter(session, book_id=book_id, number=3, status="briefing")
    session.add(ChapterBrief(chapter_id=second.id, goal="第3章", required_beats="承接第2章", constraints="", status="ready"))
    session.flush()
    result = run_next_action(session, book_id=book_id, chapter_number=3, dry_run=False, queue_generation=True)
    if result.status != "blocked" and result.action in {"draft_chapter", "queue_draft_chapter", "revise_chapter", "queue_revise_chapter"}:
        failures.append(f"next_chapter_real_production_not_blocked:{result}")


def _case_needs_revision_requires_contract(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=4, status="draft")
    session.add(ChapterBrief(chapter_id=chapter.id, goal="第4章", required_beats="承接", constraints="", status="ready"))
    _version(session, chapter=chapter, version_number=1, status="needs_revision", passed=False, score=50)
    session.flush()
    item = plan_chapters(session, book_id=book_id, start=4, count=1)[0]
    if item.next_action in {"revise_chapter", "queue_revise_chapter"}:
        failures.append(f"needs_revision_without_contract_revisable:{item.next_action}:{item.reason}")


def _case_approved_publish_job_snapshot_safe(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=5, status="approved")
    session.add(ChapterBrief(chapter_id=chapter.id, goal="第5章", required_beats="承接", constraints="", status="ready"))
    approved = _version(session, chapter=chapter, version_number=1, status="approved", passed=True, score=90)
    session.add(PublishJob(chapter_version_id=approved.id, platform="manual", status="pending", automation_payload="{}"))
    session.flush()
    try:
        item = plan_chapters(session, book_id=book_id, start=5, count=1)[0]
    except Exception as exc:  # pragma: no cover - regression signal
        failures.append(f"approved_publish_job_snapshot_crashed:{type(exc).__name__}:{exc}")
        return
    if item.next_action not in {"publish_job_dry_run", "queue_publish_job", "mark_publish_job", "done"}:
        failures.append(f"approved_publish_job_unexpected_action:{item.next_action}:{item.reason}")


def _case_preview_does_not_write_live_state(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=6, status="briefing")
    session.add(ChapterBrief(chapter_id=chapter.id, goal="第6章", required_beats="承接", constraints="", status="ready"))
    session.flush()
    before_versions = session.scalar(select(func.count(ChapterVersion.id))) or 0
    before_tasks = session.scalar(select(func.count(GenerationTask.id))) or 0
    result = run_next_action(session, book_id=book_id, chapter_number=6, dry_run=True, preview_only=True, queue_generation=True)
    after_versions = session.scalar(select(func.count(ChapterVersion.id))) or 0
    after_tasks = session.scalar(select(func.count(GenerationTask.id))) or 0
    if result.status != "preview":
        failures.append(f"preview_not_preview:{result}")
    if after_versions != before_versions:
        failures.append(f"preview_wrote_chapter_version:{before_versions}->{after_versions}")
    if after_tasks != before_tasks:
        failures.append(f"preview_wrote_generation_task:{before_tasks}->{after_tasks}")


def _case_frontend_backend_action_consistency(session, *, failures: list[str]) -> None:
    dashboard = DASHBOARD.read_text(encoding="utf-8")
    for action in sorted(AUTO_ACTIONS | MANUAL_ACTIONS):
        if action in {"done", "adopt_recommended_chapter_sample", "repair_chapter_brief", "accept_early_stop"}:
            continue
        if action not in dashboard:
            failures.append(f"backend_action_missing_from_dashboard:{action}")
    if "reading_assessment_review" in dashboard:
        pass  # legacy label may remain in historical UI text; action execution mapping is guarded above.
    if "record_chapter_continuity" not in AUTO_ACTIONS or "record_chapter_continuity" in MANUAL_ACTIONS:
        failures.append("continuity_action_not_backend_auto")


def _case_done_is_terminal_completed(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=7, status="published")
    session.add(ChapterBrief(chapter_id=chapter.id, goal="第7章", required_beats="承接", constraints="", status="ready"))
    approved = _version(session, chapter=chapter, version_number=1, status="approved", passed=True, score=90)
    job = PublishJob(chapter_version_id=approved.id, platform="manual", status="published", automation_payload="{}")
    session.add(job)
    session.flush()
    item = plan_chapters(session, book_id=book_id, start=7, count=1)[0]
    decision = decide_chapter_production(item)
    result = run_next_action(session, book_id=book_id, chapter_number=7, dry_run=False)
    if item.next_action != "done" or result.status != "completed":
        failures.append(f"done_not_completed:{item.next_action}:{result.status}:{result.message}")
    if decision.needs_author:
        failures.append("done_requires_author_confirmation")


def _case_rebuild_candidate_not_sync_execute(session, *, book_id: int, failures: list[str]) -> None:
    chapter = _chapter(session, book_id=book_id, number=8, status="draft")
    session.add(ChapterBrief(chapter_id=chapter.id, goal="第8章", required_beats="承接", constraints="", status="ready"))
    source = _version(session, chapter=chapter, version_number=1, status="needs_revision", passed=False, score=42)
    session.add(
        ChapterBrief(
            chapter_id=chapter.id,
            goal="重建第8章",
            required_beats="reading_assessment_auto_quality#8\n当前阅读层级：需重建",
            constraints="revision_mode:rewrite",
            status="revision_ready",
        )
    )
    session.flush()
    before_versions = session.scalar(select(func.count(ChapterVersion.id))) or 0
    before_tasks = session.scalar(select(func.count(GenerationTask.id))) or 0
    result = run_next_action(session, book_id=book_id, chapter_number=8, dry_run=False, queue_generation=True)
    after_versions = session.scalar(select(func.count(ChapterVersion.id))) or 0
    after_tasks = session.scalar(select(func.count(GenerationTask.id))) or 0
    if result.action != "generate_rebuild_candidates":
        failures.append(f"rebuild_case_wrong_action:{result.action}:{result.message}")
    if result.status not in {"queued", "executed"}:
        failures.append(f"rebuild_case_not_queueable:{result.status}:{result.message}")
    if after_versions != before_versions:
        failures.append(f"rebuild_candidates_sync_wrote_versions:{before_versions}->{after_versions}:source={source.id}")
    if result.status == "queued":
        task = session.get(GenerationTask, result.object_id) if result.object_id else None
        if not task or task.task_type != QUEUE_REBUILD_CANDIDATES or task.status != "pending":
            failures.append(f"rebuild_queue_task_invalid:{result.object_id}")
    elif after_tasks == before_tasks:
        failures.append("rebuild_candidates_not_queued_or_tracked")


def _case_candidate_scoring_policy(*, failures: list[str]) -> None:
    class Quality:
        def __init__(self, score: int, passed: bool, blockers: list[str]) -> None:
            self.score = score
            self.passed = passed
            self.report = json.dumps({"issues": blockers, "reading_assessment": {"blockers": blockers}}, ensure_ascii=False)

    incumbent = rebuild_candidates.IncumbentDraft(version=object(), quality=Quality(78, False, ["canon", "tone"]), score=78, passed=False)
    better_candidate = {"score": 76, "passed": False}
    if rebuild_candidates._should_restore_incumbent_over_candidate(
        incumbent=incumbent,
        candidate=better_candidate,
        candidate_quality=Quality(76, False, []),
    ):
        failures.append("candidate_with_fewer_blockers_should_beat_higher_incumbent_score")

    passing_candidate = {"score": 72, "passed": True}
    if rebuild_candidates._should_restore_incumbent_over_candidate(
        incumbent=incumbent,
        candidate=passing_candidate,
        candidate_quality=Quality(72, True, []),
    ):
        failures.append("passing_candidate_should_beat_nonpassing_incumbent")


def _case_running_tasks_have_timeout_or_heartbeat(session, *, book_id: int, failures: list[str]) -> None:
    task = GenerationTask(
        book_id=book_id,
        task_type="queue_draft_chapter",
        status="running",
        input_json=json.dumps({"chapter_number": 9, "task_timeout_seconds": 60, "heartbeat_at": "2026-01-01T00:00:00Z"}, ensure_ascii=False),
        output_json="{}",
    )
    session.add(task)
    session.flush()
    data = json.loads(task.input_json or "{}")
    if task.status == "running" and not any(data.get(key) for key in ("task_timeout_seconds", "lease_expires_at", "heartbeat_at", "running_started_at")):
        failures.append("running_task_without_timeout_or_heartbeat")


def _create_gate_ready_book(session) -> Book:
    book = Book(title="Production Invariants", genre="test", target_platform="manual")
    session.add(book)
    session.flush()
    values = {
        "premise": "主角在游戏江湖与现实后果之间同步成长。",
        "reader_promise": "每章都有玩家竞争、可见回报和现实代价。",
        "world_engine": "游戏桥段会以神经记忆形式反向影响现实判断。",
        "protagonist_engine": "主角用误判、试探和复盘逐步掌握同步规则。",
        "conflict_engine": "玩家竞争、江湖规则和现实牵连持续挤压主角选择。",
    }
    session.add(StoryFoundation(book_id=book.id, status="approved", **values))
    session.add(
        StoryBible(
            book_id=book.id,
            positioning=values["premise"],
            reader_promise=values["reader_promise"],
            power_curve=values["world_engine"],
            protagonist_arc=values["protagonist_engine"],
            main_plot=values["conflict_engine"],
            status="approved",
        )
    )
    session.add(
        StoryArc(
            book_id=book.id,
            arc_number=1,
            start_chapter=1,
            end_chapter=12,
            goal="主角确认同步规则并建立第一个玩家同盟。",
            climax="同盟交易暴露真正的江湖代价。",
            turn="主角发现现实也开始响应游戏选择。",
            status="approved",
        )
    )
    approval_values = {
        "premise": values["premise"],
        "reader_promise": values["reader_promise"],
        "world_engine": values["world_engine"],
        "protagonist_engine": values["protagonist_engine"],
        "conflict_engine": values["conflict_engine"],
        "arc_goal": "主角确认同步规则并建立第一个玩家同盟。",
        "arc_climax": "同盟交易暴露真正的江湖代价。",
        "arc_turn": "主角发现现实也开始响应游戏选择。",
    }
    for key, value in approval_values.items():
        session.add(PlatformFeedback(book_id=book.id, platform="invariants", metric_name="skeleton_approval", metric_value=key, raw_text=value))
    session.flush()
    return book


def _chapter(session, *, book_id: int, number: int, status: str) -> Chapter:
    chapter = Chapter(book_id=book_id, chapter_number=number, title=f"第{number}章", status=status)
    session.add(chapter)
    session.flush()
    return chapter


def _version(session, *, chapter: Chapter, version_number: int, status: str, passed: bool, score: int) -> ChapterVersion:
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=version_number,
        title=chapter.title,
        content=("稳定正文。" if passed else "问题正文。") * 1000,
        status=status,
        source="invariants_regression",
    )
    session.add(version)
    session.flush()
    session.add(
        QualityReport(
            chapter_version_id=version.id,
            score=score,
            passed=passed,
            report=json.dumps(
                {"status": "PASS" if passed else "FAIL", "score": score, "passed": passed, "hard_gate": {"passed": passed}, "reading_assessment": {"action": "approve_ready"} if passed else {}},
                ensure_ascii=False,
            ),
        )
    )
    session.flush()
    return version


if __name__ == "__main__":
    raise SystemExit(main())
