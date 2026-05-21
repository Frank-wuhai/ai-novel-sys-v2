from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import GenerationTask
from app.services.production import draft_chapter, revise_chapter


QUEUE_DRAFT = "queue_draft_chapter"
QUEUE_REVISE = "queue_revise_chapter"
QUEUE_TYPES = {QUEUE_DRAFT, QUEUE_REVISE}
ACTIVE_STATUSES = {"pending", "running", "paused"}


@dataclass(frozen=True)
class QueueRunResult:
    task: GenerationTask
    version_id: int | None
    child_generation_task_id: int | None


@dataclass(frozen=True)
class QueueBatchResult:
    results: list[QueueRunResult]


def enqueue_draft_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
    max_attempts: int = 3,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_DRAFT)
    return _enqueue(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run, queue_type=QUEUE_DRAFT, max_attempts=max_attempts)


def enqueue_revise_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
    max_attempts: int = 3,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_REVISE)
    return _enqueue(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run, queue_type=QUEUE_REVISE, max_attempts=max_attempts)


def list_generation_queue(
    session: Session,
    *,
    status: str = "",
    limit: int = 20,
) -> list[GenerationTask]:
    stmt = select(GenerationTask).where(GenerationTask.task_type.in_(QUEUE_TYPES)).order_by(GenerationTask.id.desc()).limit(limit)
    if status:
        stmt = stmt.where(GenerationTask.status == status)
    return list(session.scalars(stmt))


def run_generation_queue_task(session: Session, *, task_id: int | None = None) -> QueueRunResult:
    task = session.get(GenerationTask, task_id) if task_id else _next_pending_task(session)
    if not task:
        raise ValueError("no pending generation queue task")
    if task.task_type not in QUEUE_TYPES:
        raise ValueError(f"not a generation queue task: {task.task_type}")
    if task.status != "pending":
        raise ValueError(f"generation queue task must be pending, got {task.status}")

    input_data = _loads_json(task.input_json)
    chapter_number = int(input_data.get("chapter_number") or 0)
    dry_run = bool(input_data.get("dry_run", True))
    attempt = int(input_data.get("attempt") or 0) + 1
    max_attempts = int(input_data.get("max_attempts") or 3)
    input_data["attempt"] = attempt
    if chapter_number < 1:
        task.status = "failed"
        task.input_json = _dumps_json(input_data)
        task.output_json = _dumps_json({"error_category": "validation", "error": "chapter_number is required", "attempt": attempt})
        session.flush()
        return QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    before_task_id = session.scalar(select(func.max(GenerationTask.id))) or 0
    task.status = "running"
    task.input_json = _dumps_json(input_data)
    session.flush()
    try:
        if task.task_type == QUEUE_DRAFT:
            version = draft_chapter(session, book_id=task.book_id, chapter_number=chapter_number, dry_run=dry_run)
        elif task.task_type == QUEUE_REVISE:
            version = revise_chapter(session, book_id=task.book_id, chapter_number=chapter_number, dry_run=dry_run)
        else:
            raise ValueError(f"unsupported queue type: {task.task_type}")
    except Exception as exc:
        task.status = "failed" if attempt >= max_attempts else "pending"
        task.output_json = _dumps_json(
            {
                "error_category": _error_category(exc),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "retryable": attempt < max_attempts,
            }
        )
        session.flush()
        return QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    child_task = _latest_child_generation_task(session, after_id=before_task_id, version_id=version.id)
    task.status = "completed"
    task.output_json = _dumps_json(
        {
            "version_id": version.id,
            "child_generation_task_id": child_task.id if child_task else None,
            "dry_run": dry_run,
            "attempt": attempt,
            "max_attempts": max_attempts,
        },
    )
    session.flush()
    return QueueRunResult(
        task=task,
        version_id=version.id,
        child_generation_task_id=child_task.id if child_task else None,
    )


def run_generation_queue(session: Session, *, max_tasks: int = 1) -> QueueBatchResult:
    if max_tasks < 1:
        raise ValueError("max_tasks must be >= 1")
    results: list[QueueRunResult] = []
    seen_task_ids: set[int] = set()
    for _ in range(max_tasks):
        task = _next_pending_task(exclude_task_ids=seen_task_ids, session=session)
        if not task:
            break
        seen_task_ids.add(task.id)
        results.append(run_generation_queue_task(session, task_id=task.id))
    return QueueBatchResult(results=results)


def retry_generation_queue_task(session: Session, *, task_id: int) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if not task:
        raise ValueError(f"generation queue task not found: {task_id}")
    if task.task_type not in QUEUE_TYPES:
        raise ValueError(f"not a generation queue task: {task.task_type}")
    if task.status != "failed":
        raise ValueError(f"only failed generation queue tasks can retry, got {task.status}")
    input_data = _loads_json(task.input_json)
    input_data["attempt"] = 0
    task.input_json = _dumps_json(input_data)
    task.status = "pending"
    task.output_json = "{}"
    session.flush()
    return task


def pause_generation_queue_task(session: Session, *, task_id: int, reason: str = "") -> GenerationTask:
    task = _get_queue_task(session, task_id=task_id)
    if task.status != "pending":
        raise ValueError(f"only pending generation queue tasks can pause, got {task.status}")
    output_data = _loads_json(task.output_json)
    output_data["pause_reason"] = reason
    task.output_json = _dumps_json(output_data)
    task.status = "paused"
    session.flush()
    return task


def resume_generation_queue_task(session: Session, *, task_id: int) -> GenerationTask:
    task = _get_queue_task(session, task_id=task_id)
    if task.status != "paused":
        raise ValueError(f"only paused generation queue tasks can resume, got {task.status}")
    output_data = _loads_json(task.output_json)
    output_data.pop("pause_reason", None)
    task.output_json = _dumps_json(output_data)
    task.status = "pending"
    session.flush()
    return task


def cancel_generation_queue_task(session: Session, *, task_id: int, reason: str = "") -> GenerationTask:
    task = _get_queue_task(session, task_id=task_id)
    if task.status not in {"pending", "paused", "failed"}:
        raise ValueError(f"only pending, paused, or failed generation queue tasks can cancel, got {task.status}")
    output_data = _loads_json(task.output_json)
    output_data["cancel_reason"] = reason
    task.output_json = _dumps_json(output_data)
    task.status = "canceled"
    session.flush()
    return task


def _enqueue(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool,
    queue_type: str,
    max_attempts: int,
) -> GenerationTask:
    task = GenerationTask(
        book_id=book_id,
        task_type=queue_type,
        status="pending",
        input_json=_dumps_json({"chapter_number": chapter_number, "dry_run": dry_run, "attempt": 0, "max_attempts": max_attempts}),
        output_json="{}",
    )
    session.add(task)
    session.flush()
    return task


def _get_queue_task(session: Session, *, task_id: int) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if not task:
        raise ValueError(f"generation queue task not found: {task_id}")
    if task.task_type not in QUEUE_TYPES:
        raise ValueError(f"not a generation queue task: {task.task_type}")
    return task


def _guard_active_queue_task(session: Session, *, book_id: int, chapter_number: int, queue_type: str) -> None:
    for task in session.scalars(
        select(GenerationTask).where(
            GenerationTask.book_id == book_id,
            GenerationTask.task_type == queue_type,
            GenerationTask.status.in_(ACTIVE_STATUSES),
        )
    ):
        input_data = _loads_json(task.input_json)
        if input_data.get("chapter_number") == chapter_number:
            raise ValueError(f"active generation queue task already exists: {task.id} ({task.status})")


def _next_pending_task(session: Session, *, exclude_task_ids: set[int] | None = None) -> GenerationTask | None:
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.task_type.in_(QUEUE_TYPES), GenerationTask.status == "pending")
        .order_by(GenerationTask.id)
    )
    if exclude_task_ids:
        stmt = stmt.where(GenerationTask.id.not_in(exclude_task_ids))
    return session.scalar(stmt)


def _latest_child_generation_task(session: Session, *, after_id: int, version_id: int) -> GenerationTask | None:
    tasks = list(session.scalars(select(GenerationTask).where(GenerationTask.id > after_id).order_by(GenerationTask.id.desc())))
    for task in tasks:
        output_data = _loads_json(task.output_json)
        if output_data.get("version_id") == version_id:
            return task
    return None


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


def _dumps_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _error_category(exc: Exception) -> str:
    text = str(exc).lower()
    if isinstance(exc, ValueError):
        return "validation"
    if "api" in text or "timeout" in text or "connection" in text or "rate" in text:
        return "provider"
    return "execution"
