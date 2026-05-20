from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, GenerationTask
from app.services.llm_queue import QUEUE_TYPES
from app.services.planning import AUTO_ACTIONS, build_human_decision_package, plan_chapters
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

    lines = [
        f"book\tid={book.id}\ttitle={book.title}\tgenre={book.genre}\tplatform={book.target_platform}\tstatus={book.status}",
        f"range\tstart={start}\tcount={count}\tend={start + count - 1}",
        f"readiness\tpassed={readiness.passed}",
    ]
    lines.extend(f"check\t{check.name}\tpassed={check.passed}\tdetail={check.detail}" for check in readiness.checks)

    next_counts = Counter(item.next_action for item in plan_items)
    lines.append("chapter_actions\t" + "\t".join(f"{name}={next_counts[name]}" for name in sorted(next_counts)))
    for item in plan_items:
        lines.append(
            "\t".join(
                [
                    "chapter",
                    f"number={item.chapter_number}",
                    f"chapter_id={item.chapter_id or ''}",
                    f"version_id={item.latest_version_id or ''}",
                    f"version_status={item.latest_version_status}",
                    f"quality_passed={item.latest_quality_passed}",
                    f"publish_job_id={item.publish_job_id or ''}",
                    f"publish_status={item.publish_job_status}",
                    f"next_action={item.next_action}",
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
    recommendation = _recommend_next(book_id=book_id, plan_items=plan_items, queue_tasks=queue_tasks)
    lines.append(f"recommendation\t{recommendation}")
    return DashboardReport(lines=lines)


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


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}
