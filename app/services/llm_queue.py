from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import GenerationTask
from app.services.llm_errors import classify_exception
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


@dataclass(frozen=True)
class QueueFailureSummary:
    task_id: int
    task_type: str
    chapter_number: int | None
    attempt: int
    max_attempts: int
    error_category: str
    error: str
    retryable: bool


@dataclass(frozen=True)
class RunningTaskSummary:
    task_id: int
    task_type: str
    chapter_number: int | None
    attempt: int
    max_attempts: int
    running_age_seconds: int
    timeout_seconds: int
    stale: bool
    recoverable: bool


@dataclass(frozen=True)
class StaleTaskRecovery:
    task_id: int
    previous_status: str
    new_status: str
    chapter_number: int | None
    attempt: int
    max_attempts: int
    age_seconds: int
    error_category: str


@dataclass(frozen=True)
class QueueHealthReport:
    total: int
    counts: dict[str, int]
    oldest_pending_id: int | None
    oldest_pending_chapter: int | None
    latest_failures: list[QueueFailureSummary]
    running_tasks: list[RunningTaskSummary]
    running_count: int = 0
    stale_running_count: int = 0


def enqueue_draft_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
    max_attempts: int = 3,
    timeout_seconds: int = 3600,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_DRAFT)
    return _enqueue(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        dry_run=dry_run,
        queue_type=QUEUE_DRAFT,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )


def enqueue_revise_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
    max_attempts: int = 3,
    timeout_seconds: int = 3600,
) -> GenerationTask:
    _guard_active_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_REVISE)
    return _enqueue(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        dry_run=dry_run,
        queue_type=QUEUE_REVISE,
        max_attempts=max_attempts,
        timeout_seconds=timeout_seconds,
    )


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


def build_generation_queue_health(session: Session, *, failure_limit: int = 5, stale_after_seconds: int = 3600) -> QueueHealthReport:
    if failure_limit < 1:
        raise ValueError("failure_limit must be >= 1")
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.task_type.in_(QUEUE_TYPES))
            .order_by(GenerationTask.id)
        )
    )
    counts = Counter(task.status for task in tasks)
    oldest_pending = next((task for task in tasks if task.status == "pending"), None)
    oldest_pending_input = _loads_json(oldest_pending.input_json) if oldest_pending else {}
    failed_tasks = [task for task in reversed(tasks) if task.status == "failed"][:failure_limit]
    running_tasks = [_running_summary(task, fallback_timeout_seconds=stale_after_seconds) for task in tasks if task.status == "running"]
    stale_running = [task for task in running_tasks if task.stale]
    return QueueHealthReport(
        total=len(tasks),
        counts=dict(sorted(counts.items())),
        oldest_pending_id=oldest_pending.id if oldest_pending else None,
        oldest_pending_chapter=oldest_pending_input.get("chapter_number") if oldest_pending else None,
        latest_failures=[_failure_summary(task) for task in failed_tasks],
        running_tasks=running_tasks,
        running_count=counts.get("running", 0),
        stale_running_count=len(stale_running),
    )


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
    timeout_seconds = _task_timeout_seconds(input_data, fallback=3600)
    input_data["attempt"] = attempt
    input_data["running_started_at"] = _utc_now_iso()
    input_data["task_timeout_seconds"] = timeout_seconds
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
        classification = classify_exception(exc)
        retryable = classification.retryable and attempt < max_attempts
        task.status = "pending" if retryable else "failed"
        task.output_json = _dumps_json(
            {
                "error_category": classification.category,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "task_timeout_seconds": timeout_seconds,
                "running_age_seconds": _running_age_seconds(task),
                "retryable": retryable,
            }
        )
        session.flush()
        return QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    child_task = _latest_child_generation_task(session, after_id=before_task_id, version_id=version.id)
    task.status = "completed"
    input_data = _loads_json(task.input_json)
    input_data.pop("running_started_at", None)
    task.input_json = _dumps_json(input_data)
    task.output_json = _dumps_json(
        {
            "version_id": version.id,
            "child_generation_task_id": child_task.id if child_task else None,
            "dry_run": dry_run,
            "attempt": attempt,
            "max_attempts": max_attempts,
            "task_timeout_seconds": timeout_seconds,
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


def recover_stale_generation_tasks(
    session: Session,
    *,
    timeout_seconds: int = 3600,
    limit: int = 20,
) -> list[StaleTaskRecovery]:
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be >= 1")
    if limit < 1:
        raise ValueError("limit must be >= 1")
    recovered: list[StaleTaskRecovery] = []
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.task_type.in_(QUEUE_TYPES), GenerationTask.status == "running")
            .order_by(GenerationTask.id)
            .limit(limit)
        )
    )
    for task in tasks:
        input_data = _loads_json(task.input_json)
        task_timeout_seconds = _task_timeout_seconds(input_data, fallback=timeout_seconds)
        age = _running_age_seconds(task)
        if age < task_timeout_seconds:
            continue
        output_data = _loads_json(task.output_json)
        attempt = int(input_data.get("attempt") or output_data.get("attempt") or 0)
        max_attempts = int(input_data.get("max_attempts") or output_data.get("max_attempts") or 3)
        new_status = "pending" if attempt < max_attempts else "failed"
        input_data.pop("running_started_at", None)
        task.input_json = _dumps_json(input_data)
        output_data.update(
            {
                "error_category": "timeout",
                "error_type": "StaleRunningTask",
                "error": f"running task exceeded timeout_seconds={task_timeout_seconds}",
                "attempt": attempt,
                "max_attempts": max_attempts,
                "task_timeout_seconds": task_timeout_seconds,
                "retryable": new_status == "pending",
                "recovered_from_status": "running",
                "stale_age_seconds": age,
            }
        )
        task.output_json = _dumps_json(output_data)
        task.status = new_status
        recovered.append(
            StaleTaskRecovery(
                task_id=task.id,
                previous_status="running",
                new_status=new_status,
                chapter_number=input_data.get("chapter_number"),
                attempt=attempt,
                max_attempts=max_attempts,
                age_seconds=age,
                error_category="timeout",
            )
        )
    session.flush()
    return recovered


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
    timeout_seconds: int,
) -> GenerationTask:
    if timeout_seconds < 1:
        raise ValueError("timeout_seconds must be >= 1")
    task = GenerationTask(
        book_id=book_id,
        task_type=queue_type,
        status="pending",
        input_json=_dumps_json(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "attempt": 0,
                "max_attempts": max_attempts,
                "task_timeout_seconds": timeout_seconds,
            }
        ),
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


def _failure_summary(task: GenerationTask) -> QueueFailureSummary:
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    return QueueFailureSummary(
        task_id=task.id,
        task_type=task.task_type,
        chapter_number=input_data.get("chapter_number"),
        attempt=int(output_data.get("attempt") or input_data.get("attempt") or 0),
        max_attempts=int(output_data.get("max_attempts") or input_data.get("max_attempts") or 0),
        error_category=str(output_data.get("error_category") or ""),
        error=str(output_data.get("error") or ""),
        retryable=bool(output_data.get("retryable", False)),
    )


def _running_summary(task: GenerationTask, *, fallback_timeout_seconds: int) -> RunningTaskSummary:
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    attempt = int(input_data.get("attempt") or output_data.get("attempt") or 0)
    max_attempts = int(input_data.get("max_attempts") or output_data.get("max_attempts") or 3)
    timeout_seconds = _task_timeout_seconds(input_data, fallback=fallback_timeout_seconds)
    age = _running_age_seconds(task)
    stale = age >= timeout_seconds
    return RunningTaskSummary(
        task_id=task.id,
        task_type=task.task_type,
        chapter_number=input_data.get("chapter_number"),
        attempt=attempt,
        max_attempts=max_attempts,
        running_age_seconds=age,
        timeout_seconds=timeout_seconds,
        stale=stale,
        recoverable=stale,
    )


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}


def _dumps_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _running_age_seconds(task: GenerationTask) -> int:
    input_data = _loads_json(task.input_json)
    raw = input_data.get("running_started_at")
    started = _parse_datetime(raw) if isinstance(raw, str) and raw else task.created_at
    return max(0, int((datetime.utcnow() - started).total_seconds()))


def _task_timeout_seconds(input_data: dict, *, fallback: int) -> int:
    return max(1, int(input_data.get("task_timeout_seconds") or input_data.get("timeout_seconds") or fallback))


def _parse_datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return datetime.utcnow() - timedelta(days=365)
