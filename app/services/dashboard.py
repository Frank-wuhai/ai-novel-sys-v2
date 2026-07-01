from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, GenerationTask
from app.services.author_command_center import build_author_command_center
from app.services.llm_queue import VISIBLE_QUEUE_TYPES
from app.services.dashboard_explain import explain_chapter_state, explain_queue_task
from app.services.planning import build_human_decision_package, build_team_decision_package, plan_chapters
from app.services.production_decision import decide_chapter_production
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
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
    decisions = build_team_decision_package(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
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
                    f"heartbeat_at={input_data.get('heartbeat_at', '')}",
                    f"lease_expires_at={input_data.get('lease_expires_at', '')}",
                    f"last_progress={input_data.get('last_progress', '')}",
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
                "team_decisions",
                f"continuity={decisions.continuity_count}",
                f"adoption={decisions.approval_count}",
                f"publish={decisions.publish_count}",
                f"inspect={decisions.inspect_count}",
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
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
    decisions = build_team_decision_package(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
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
            _chapter_snapshot(item, queue_tasks=queue_tasks)
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
        "team_decisions": _team_decisions_payload(decisions),
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


def _team_decisions_payload(decisions) -> dict:
    return {
        "continuity": decisions.continuity_count,
        "adoption": decisions.approval_count,
        "publish": decisions.publish_count,
        "inspect": decisions.inspect_count,
        "items": [
            {
                "type": _team_decision_type(item.decision_type),
                "legacy_type": item.decision_type,
                "role": _team_decision_role(item.decision_type),
                "chapter": item.chapter_number,
                "chapter_id": item.chapter_id,
                "version_id": item.version_id,
                "publish_job_id": item.publish_job_id,
                "reason": item.reason,
                "display_reason": _team_decision_reason(item.decision_type, item.reason),
                "command_hint": item.command_hint,
            }
            for item in decisions.items
        ],
    }


def _team_decision_type(value: str) -> str:
    return {
        "human_approval": "adoption_confirmation",
        "final_publish_confirmation": "publish_confirmation",
        "manual_inspection": "flow_inspection",
        "continuity_writeback": "continuity_writeback",
    }.get(value, value)


def _team_decision_role(value: str) -> str:
    if value == "human_approval":
        return "主编"
    if value == "final_publish_confirmation":
        return "流程官"
    if value == "manual_inspection":
        return "流程官"
    return "主笔"


def _team_decision_reason(decision_type: str, reason: str) -> str:
    if decision_type == "human_approval":
        return "主编已给出准定稿判断，等待你确认是否采用。"
    if decision_type == "final_publish_confirmation":
        return "流程官已完成发布准备，等待最终发布确认。"
    if decision_type == "manual_inspection":
        return "流程官发现状态不一致，需要先排查。"
    return reason


def _chapter_snapshot(item, *, queue_tasks: list[GenerationTask] | None = None) -> dict:
    decision = decide_chapter_production(item)
    queue = _chapter_queue_snapshot(item.chapter_number, queue_tasks or [])
    snapshot = {
        "number": item.chapter_number,
        "chapter_id": item.chapter_id,
        "brief_id": item.brief_id,
        "version_id": item.latest_version_id,
        "version_status": item.latest_version_status,
        "team_status": decision.status,
        "team_status_label": decision.label,
        "team_next_step": decision.next_step,
        "author_status": decision.status,
        "author_status_label": decision.label,
        "author_next_step": decision.next_step,
        "production_decision": decision.to_dict(),
        "quality_passed": item.latest_quality_passed,
        "publish_job_id": item.publish_job_id,
        "publish_status": item.publish_job_status,
        "next_action": item.next_action,
        "reason": item.reason,
        "queue": queue,
    }
    snapshot["explain"] = explain_chapter_state(snapshot)
    return snapshot


def _chapter_queue_snapshot(chapter_number: int, queue_tasks: list[GenerationTask]) -> dict:
    for task in queue_tasks:
        data = _loads_json(task.input_json)
        if data.get("chapter_number") != chapter_number:
            continue
        return _task_snapshot(None, task)
    return {}


def _author_chapter_state(item) -> dict[str, str]:
    decision = decide_chapter_production(item)
    return {"status": decision.status, "label": decision.label, "next_step": decision.next_step}


def _recommend_next(*, book_id: int, plan_items, queue_tasks: list[GenerationTask]) -> str:
    pending_queue = [task for task in queue_tasks if task.status == "pending"]
    failed_queue = [task for task in queue_tasks if task.status == "failed"]
    if pending_queue:
        return f"python -m app.cli run-generation-queue --max-tasks {min(3, len(pending_queue))}"
    if failed_queue:
        return f"python -m app.cli show-generation-task --task-id {failed_queue[0].id}"
    auto = next((item for item in plan_items if decide_chapter_production(item).can_continue), None)
    if auto:
        return f"python -m app.cli run-next-action --book-id {book_id} --chapter-number {auto.chapter_number} --dry-run"
    waiting = next((item for item in plan_items if item.next_action == "wait_generation_task"), None)
    if waiting:
        return "wait for queued generation, or run list-generation-queue --status pending"
    manual = next((item for item in plan_items if item.next_action in {"approve_chapter", "mark_publish_job"}), None)
    if manual:
        return f"python -m app.cli human-decision-package --book-id {book_id} --start {manual.chapter_number} --count 1"
    return "no immediate action in selected range"


def _queue_tasks(session: Session, *, book_id: int) -> list[GenerationTask]:
    return list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type.in_(VISIBLE_QUEUE_TYPES))
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


def _task_snapshot(session: Session | None, task: GenerationTask) -> dict:
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    llm_parameters = input_data.get("llm_parameters") if isinstance(input_data.get("llm_parameters"), dict) else {}
    actual = _actual_model_snapshot(session, output_data) if session is not None else {}
    snapshot = {
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
        "lease_owner": input_data.get("lease_owner", ""),
        "lease_expires_at": input_data.get("lease_expires_at", ""),
        "heartbeat_at": input_data.get("heartbeat_at", ""),
        "last_progress": input_data.get("last_progress", ""),
        "error_category": output_data.get("error_category", ""),
    }
    snapshot["explain"] = explain_queue_task(snapshot)
    return snapshot


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
