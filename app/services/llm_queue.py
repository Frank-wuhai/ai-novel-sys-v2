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
ACTIVE_STATUSES = {"pending", "running"}


@dataclass(frozen=True)
class QueueRunResult:
    task: GenerationTask
    version_id: int | None
    child_generation_task_id: int | None


def enqueue_draft_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_DRAFT)
    return _enqueue(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run, queue_type=QUEUE_DRAFT)


def enqueue_revise_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_REVISE)
    return _enqueue(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run, queue_type=QUEUE_REVISE)


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
    if chapter_number < 1:
        task.status = "failed"
        task.output_json = json.dumps({"error": "chapter_number is required"}, ensure_ascii=False)
        session.flush()
        return QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    before_task_id = session.scalar(select(func.max(GenerationTask.id))) or 0
    task.status = "running"
    session.flush()
    try:
        if task.task_type == QUEUE_DRAFT:
            version = draft_chapter(session, book_id=task.book_id, chapter_number=chapter_number, dry_run=dry_run)
        elif task.task_type == QUEUE_REVISE:
            version = revise_chapter(session, book_id=task.book_id, chapter_number=chapter_number, dry_run=dry_run)
        else:
            raise ValueError(f"unsupported queue type: {task.task_type}")
    except Exception as exc:
        task.status = "failed"
        task.output_json = json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False)
        session.flush()
        return QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    child_task = _latest_child_generation_task(session, after_id=before_task_id, version_id=version.id)
    task.status = "completed"
    task.output_json = json.dumps(
        {
            "version_id": version.id,
            "child_generation_task_id": child_task.id if child_task else None,
            "dry_run": dry_run,
        },
        ensure_ascii=False,
    )
    session.flush()
    return QueueRunResult(
        task=task,
        version_id=version.id,
        child_generation_task_id=child_task.id if child_task else None,
    )


def retry_generation_queue_task(session: Session, *, task_id: int) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if not task:
        raise ValueError(f"generation queue task not found: {task_id}")
    if task.task_type not in QUEUE_TYPES:
        raise ValueError(f"not a generation queue task: {task.task_type}")
    if task.status != "failed":
        raise ValueError(f"only failed generation queue tasks can retry, got {task.status}")
    task.status = "pending"
    task.output_json = "{}"
    session.flush()
    return task


def _enqueue(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool,
    queue_type: str,
) -> GenerationTask:
    task = GenerationTask(
        book_id=book_id,
        task_type=queue_type,
        status="pending",
        input_json=json.dumps({"chapter_number": chapter_number, "dry_run": dry_run}, ensure_ascii=False),
        output_json="{}",
    )
    session.add(task)
    session.flush()
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


def _next_pending_task(session: Session) -> GenerationTask | None:
    return session.scalar(
        select(GenerationTask)
        .where(GenerationTask.task_type.in_(QUEUE_TYPES), GenerationTask.status == "pending")
        .order_by(GenerationTask.id)
    )


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
