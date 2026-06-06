from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, GenerationTask
from app.services.author_command_center import build_author_command_center
from app.services.llm_queue import QUEUE_TYPES
from app.services.planning import AUTO_ACTIONS, build_human_decision_package, plan_chapters
from app.services.production_control import build_production_control_report
from app.services.readiness import check_production_readiness


@dataclass(frozen=True)
class DashboardReport:
    lines: list[str]


def build_project_dashboard(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 20,
    recent_tasks: int = 10,
) -> DashboardReport:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    readiness = check_production_readiness(session, book_id=book_id, start=start, count=count, live_llm=False)
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count)
    decisions = build_human_decision_package(session, book_id=book_id, start=start, count=count)
    queue_tasks = _queue_tasks(session, book_id=book_id)
    tasks = _recent_tasks(session, book_id=book_id, limit=recent_tasks)
    task_stats = _task_stats(tasks)
    production_control = build_production_control_report(session, book_id=book_id, start=start, count=count)

    lines = [
        f"book\tid={book.id}\ttitle={book.title}\tgenre={book.genre}\tplatform={book.target_platform}\tstatus={book.status}",
        f"range\tstart={start}\tcount={count}\tend={start + count - 1}",
        f"readiness\tpassed={readiness.passed}",
    ]
    lines.extend(f"check\t{check.name}\tpassed={check.passed}\tdetail={check.detail}" for check in readiness.checks)

    next_counts = Counter(item.next_action for item in plan_items)
    lines.append("chapter_actions\t" + "\t".join(f"{name}={next_counts[name]}" for name in sorted(next_counts)))
    for item in plan_items:
        author_state = _author_chapter_state(item)
        lines.append(
            "\t".join(
                [
                    "chapter",
                    f"number={item.chapter_number}",
                    f"chapter_id={item.chapter_id or ''}",
                    f"version_id={item.latest_version_id or ''}",
                    f"version_status={item.latest_version_status}",
                    f"author_status={author_state['status']}",
                    f"author_status_label={author_state['label']}",
                    f"quality_passed={item.latest_quality_passed}",
                    f"publish_job_id={item.publish_job_id or ''}",
                    f"publish_status={item.publish_job_status}",
                    f"next_action={item.next_action}",
                    f"author_next_step={author_state['next_step']}",
                    f"reason={item.reason}",
                ]
            )
        )

    queue_counts = Counter(task.status for task in queue_tasks)
    lines.append("generation_queue\t" + "\t".join(f"{name}={queue_counts[name]}" for name in sorted(queue_counts)))
    for task in queue_tasks[:10]:
        input_data = _loads_json(task.input_json)
        output_data = _loads_json(task.output_json)
        lines.append(
            "\t".join(
                [
                    "queue_task",
                    f"id={task.id}",
                    f"type={task.task_type}",
                    f"status={task.status}",
                    f"chapter={input_data.get('chapter_number', '')}",
                    f"attempt={input_data.get('attempt', '')}",
                    f"max_attempts={input_data.get('max_attempts', '')}",
                    f"timeout_seconds={_task_timeout_seconds(input_data)}",
                    f"running_age_seconds={_running_age_seconds(task) if task.status == 'running' else ''}",
                    f"error_category={output_data.get('error_category', '')}",
                ]
            )
        )

    lines.append(
        "\t".join(
            [
                "generation_recent",
                f"count={len(tasks)}",
                f"completed={task_stats['completed']}",
                f"failed={task_stats['failed']}",
                f"estimated_tokens={task_stats['estimated_tokens']}",
                f"elapsed_ms={task_stats['elapsed_ms']}",
            ]
        )
    )
    lines.append(
        "\t".join(
            [
                "human_decisions",
                f"continuity={decisions.continuity_count}",
                f"approval={decisions.approval_count}",
                f"publish={decisions.publish_count}",
                f"inspect={decisions.inspect_count}",
            ]
        )
    )
    recommendation = production_control.next_actions[0] if production_control.next_actions else _recommend_next(book_id=book_id, plan_items=plan_items, queue_tasks=queue_tasks)
    lines.append(f"recommendation\t{recommendation}")
    return DashboardReport(lines=lines)


def build_project_snapshot(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
    start: int = 1,
    count: int = 20,
    recent_tasks: int = 10,
) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    readiness = check_production_readiness(session, book_id=book_id, start=start, count=count, live_llm=False)
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count)
    decisions = build_human_decision_package(session, book_id=book_id, start=start, count=count)
    queue_tasks = _queue_tasks(session, book_id=book_id)
    tasks = _recent_tasks(session, book_id=book_id, limit=recent_tasks)
    task_stats = _task_stats(tasks)
    next_counts = Counter(item.next_action for item in plan_items)
    queue_counts = Counter(task.status for task in queue_tasks)
    production_control = build_production_control_report(session, book_id=book_id, start=start, count=count)

    return {
        "book": {
            "id": book.id,
            "title": book.title,
            "genre": book.genre,
            "platform": book.target_platform,
            "status": book.status,
        },
        "range": {"start": start, "count": count, "end": start + count - 1},
        "readiness": {
            "passed": readiness.passed,
            "blocker_count": len(readiness.blockers),
            "warning_count": len(readiness.warnings),
            "checks": [
                {
                    "name": check.name,
                    "passed": check.passed,
                    "detail": check.detail,
                    "severity": check.severity,
                    "action": check.action,
                }
                for check in readiness.checks
            ],
        },
        "chapter_actions": dict(sorted(next_counts.items())),
        "chapters": [
            _chapter_snapshot(item)
            for item in plan_items
        ],
        "generation_queue": {
            "counts": dict(sorted(queue_counts.items())),
            "tasks": [_task_snapshot(session, task) for task in queue_tasks[:10]],
        },
        "generation_recent": {
            "count": len(tasks),
            "completed": task_stats["completed"],
            "failed": task_stats["failed"],
            "estimated_tokens": task_stats["estimated_tokens"],
            "elapsed_ms": task_stats["elapsed_ms"],
        },
        "human_decisions": {
            "continuity": decisions.continuity_count,
            "approval": decisions.approval_count,
            "publish": decisions.publish_count,
            "inspect": decisions.inspect_count,
            "items": [
                {
                    "type": item.decision_type,
                    "chapter": item.chapter_number,
                    "chapter_id": item.chapter_id,
                    "version_id": item.version_id,
                    "publish_job_id": item.publish_job_id,
                    "reason": item.reason,
                    "command_hint": item.command_hint,
                }
                for item in decisions.items
            ],
        },
        "production_control": production_control.to_dict(),
        "command_center": build_author_command_center(
            session,
            book_id=book_id,
            chapter_number=chapter_number or start,
            start=start,
            count=count,
        ),
        "recommendation": production_control.next_actions[0] if production_control.next_actions else _recommend_next(book_id=book_id, plan_items=plan_items, queue_tasks=queue_tasks),
    }


def _chapter_snapshot(item) -> dict:
    author_state = _author_chapter_state(item)
    return {
        "number": item.chapter_number,
        "chapter_id": item.chapter_id,
        "brief_id": item.brief_id,
        "version_id": item.latest_version_id,
        "version_status": item.latest_version_status,
        "author_status": author_state["status"],
        "author_status_label": author_state["label"],
        "author_next_step": author_state["next_step"],
        "quality_passed": item.latest_quality_passed,
        "publish_job_id": item.publish_job_id,
        "publish_status": item.publish_job_status,
        "next_action": item.next_action,
        "reason": item.reason,
    }


def _author_chapter_state(item) -> dict[str, str]:
    status = item.latest_version_status or "missing"
    action = item.next_action or ""
    quality_passed = item.latest_quality_passed is True

    if action == "wait_generation_task":
        return {
            "status": "background_working",
            "label": "后台处理中",
            "next_step": "等待后台生成或点击继续生产启动队列。",
        }
    if quality_passed and status == "needs_revision":
        return {
            "status": "needs_status_review",
            "label": "质检已过，状态待核对",
            "next_step": "正文已过质检，但版本仍标记为需修订；先人工检查当前稿，再决定审批或继续修订。",
        }
    if action in {"create_chapter_brief", "draft_chapter", "review_chapter", "create_revision_brief", "revise_chapter"}:
        return {
            "status": "can_continue",
            "label": "可自动推进",
            "next_step": "点击继续生产，让系统推进到可读稿或新的判断点。",
        }
    if action == "record_chapter_continuity":
        return {
            "status": "quality_passed",
            "label": "质检通过，待回写",
            "next_step": "点击继续生产记录连续性，然后进入人工审批。",
        }
    if action == "approve_chapter":
        return {
            "status": "needs_author",
            "label": "待你审批",
            "next_step": "阅读当前章，满意就通过，不满意就写修改意见。",
        }
    if action == "mark_publish_job":
        return {
            "status": "ready_to_publish",
            "label": "待发布确认",
            "next_step": "确认发布信息后执行发布。",
        }
    if action in {"create_publish_job", "publish_job_dry_run", "queue_publish_job", "retry_publish_job"}:
        return {
            "status": "publish_prepare",
            "label": "待发布准备",
            "next_step": "点击继续生产，系统会创建或推进发布准备。",
        }
    if action == "done":
        return {
            "status": "done",
            "label": "已完成",
            "next_step": "可以切换到下一章。",
        }
    if status in {"missing", "no_version"}:
        return {
            "status": "not_started",
            "label": "未开始",
            "next_step": "点击继续生产创建本章内容。",
        }
    if action.startswith("inspect"):
        return {
            "status": "needs_inspection",
            "label": "需要检查",
            "next_step": "查看后台状态和章节内容后再决定下一步。",
        }
    return {
        "status": "in_progress",
        "label": "处理中",
        "next_step": "按当前下一步动作继续。",
    }


def _recommend_next(*, book_id: int, plan_items, queue_tasks: list[GenerationTask]) -> str:
    pending_queue = [task for task in queue_tasks if task.status == "pending"]
    failed_queue = [task for task in queue_tasks if task.status == "failed"]
    if pending_queue:
        return f"python -m app.cli run-generation-queue --max-tasks {min(3, len(pending_queue))}"
    if failed_queue:
        return f"python -m app.cli show-generation-task --task-id {failed_queue[0].id}"
    auto = next((item for item in plan_items if item.next_action in AUTO_ACTIONS), None)
    if auto:
        return f"python -m app.cli run-next-action --book-id {book_id} --chapter-number {auto.chapter_number} --dry-run"
    waiting = next((item for item in plan_items if item.next_action == "wait_generation_task"), None)
    if waiting:
        return "wait for queued generation, or run list-generation-queue --status pending"
    manual = next((item for item in plan_items if item.next_action in {"record_chapter_continuity", "approve_chapter", "mark_publish_job"}), None)
    if manual:
        return f"python -m app.cli human-decision-package --book-id {book_id} --start {manual.chapter_number} --count 1"
    return "no immediate action in selected range"


def _queue_tasks(session: Session, *, book_id: int) -> list[GenerationTask]:
    return list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type.in_(QUEUE_TYPES))
            .order_by(GenerationTask.id.desc())
        )
    )


def _recent_tasks(session: Session, *, book_id: int, limit: int) -> list[GenerationTask]:
    return list(
        session.scalars(
            select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()).limit(limit)
        )
    )


def _task_stats(tasks: list[GenerationTask]) -> dict[str, int]:
    completed = sum(1 for task in tasks if task.status == "completed")
    failed = sum(1 for task in tasks if task.status == "failed")
    estimated_tokens = 0
    elapsed_ms = 0
    for task in tasks:
        output_data = _loads_json(task.output_json)
        estimated_tokens += int(output_data.get("estimated_total_tokens") or 0)
        elapsed_ms += int(output_data.get("elapsed_ms") or 0)
    return {
        "completed": completed,
        "failed": failed,
        "estimated_tokens": estimated_tokens,
        "elapsed_ms": elapsed_ms,
    }


def _task_snapshot(session: Session, task: GenerationTask) -> dict:
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    llm_parameters = input_data.get("llm_parameters") if isinstance(input_data.get("llm_parameters"), dict) else {}
    actual = _actual_model_snapshot(session, output_data)
    return {
        "id": task.id,
        "type": task.task_type,
        "status": task.status,
        "chapter": input_data.get("chapter_number"),
        "attempt": input_data.get("attempt"),
        "max_attempts": input_data.get("max_attempts"),
        "timeout_seconds": _task_timeout_seconds(input_data),
        "llm_parameters": llm_parameters,
        "actual_model": actual,
        "running_age_seconds": _running_age_seconds(task) if task.status == "running" else 0,
        "stale": task.status == "running" and _running_age_seconds(task) >= _task_timeout_seconds(input_data),
        "error_category": output_data.get("error_category", ""),
    }


def _actual_model_snapshot(session: Session, output_data: dict) -> dict:
    child_task_id = output_data.get("child_generation_task_id")
    child_output: dict = {}
    if child_task_id:
        child = session.get(GenerationTask, int(child_task_id))
        child_output = _loads_json(child.output_json) if child else {}
    source = child_output or output_data
    return {
        "provider": source.get("provider", ""),
        "model": source.get("model", ""),
        "request_id": source.get("request_id", ""),
        "elapsed_ms": source.get("elapsed_ms", ""),
        "actual_total_tokens": source.get("actual_total_tokens", ""),
    }


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


def _running_age_seconds(task: GenerationTask) -> int:
    from datetime import datetime

    input_data = _loads_json(task.input_json)
    raw = input_data.get("running_started_at")
    started = task.created_at
    if isinstance(raw, str) and raw:
        try:
            started = datetime.fromisoformat(raw)
        except ValueError:
            started = task.created_at
    return max(0, int((datetime.utcnow() - started).total_seconds()))


def _task_timeout_seconds(input_data: dict) -> int:
    return max(1, int(input_data.get("task_timeout_seconds") or input_data.get("timeout_seconds") or 3600))
