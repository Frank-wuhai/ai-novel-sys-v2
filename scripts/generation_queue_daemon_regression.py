"""Regression coverage for the multi-worker generation-queue daemon.

Verifies the daemon:
- consumes multiple pending queue tasks in parallel (concurrency>1),
- automatically recovers stale/running tasks each tick,
- shuts down cleanly after ``max_cycles`` even under load,
- reports honest per-tick metrics.

To keep the test fast and deterministic we patch ``run_generation_queue_task``
so worker threads don't invoke the real LLM stack. The daemon's own concurrency
plumbing, stale recovery, and shutdown loop are still exercised end-to-end.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, GenerationTask
from app.services import llm_queue

import sys as _sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in _sys.path:
    _sys.path.insert(0, str(ROOT / "scripts"))

import generation_queue_daemon as daemon  # noqa: E402
from regression_db import isolated_database  # noqa: E402


def _seed_pending(session, *, chapter_number: int) -> GenerationTask:
    task = GenerationTask(
        book_id=2,
        task_type=llm_queue.QUEUE_REBUILD_CANDIDATES,
        status="pending",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "attempt": 0,
                "max_attempts": 3,
                "task_timeout_seconds": 60,
            }
        ),
        output_json="{}",
    )
    session.add(task)
    return task


def _seed_stale_running(session, *, chapter_number: int, started_at: str) -> GenerationTask:
    task = GenerationTask(
        book_id=2,
        task_type=llm_queue.QUEUE_REBUILD_CANDIDATES,
        status="running",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "attempt": 1,
                "max_attempts": 3,
                "task_timeout_seconds": 5,
                "running_started_at": started_at,
                "lease_owner": "regression-worker",
                "lease_acquired_at": started_at,
                "lease_expires_at": started_at,
                "heartbeat_at": started_at,
            }
        ),
        output_json="{}",
    )
    session.add(task)
    return task


def _loads(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _make_fake_runner(observed_threads: set[str], lock: threading.Lock):
    """Return a stand-in for run_generation_queue_task that avoids real LLM calls."""

    def fake_runner(session, *, task_id, pre_claimed: bool = False):
        # ``claim_next_pending_task`` already flipped the row to running and
        # wrote lease metadata; the daemon always passes pre_claimed=True in
        # production. Preserve that contract in the fake so a regression
        # can't silently drift back to the racy 'pending' path.
        with lock:
            observed_threads.add(threading.current_thread().name)
        task = session.get(GenerationTask, task_id)
        if not task:
            raise ValueError(f"task {task_id} vanished")
        if pre_claimed and task.status != "running":
            raise AssertionError(
                f"pre_claimed task must already be running, got {task.status}"
            )
        input_data = _loads(task.input_json)
        input_data["running_started_at"] = datetime.now(UTC).isoformat()
        task.input_json = json.dumps(input_data)
        # emulate a short "running" window so a second worker can overlap
        time.sleep(0.1)
        task.status = "completed"
        task.output_json = json.dumps({"version_id": None, "attempt": 1, "fake": True})
        session.flush()
        return llm_queue.QueueRunResult(task=task, version_id=None, child_generation_task_id=None)

    return fake_runner


def main() -> int:
    isolated_database("generation-queue-daemon-regression")
    failures: list[str] = []

    # Seed 4 pending + 1 stale-running task.
    with session_scope() as session:
        session.add(Book(id=2, title="daemon 回归测试书", genre="武侠", target_platform="manual", status="draft"))
        for chapter_number in (1, 2, 3, 4):
            _seed_pending(session, chapter_number=chapter_number)
        stale_started_at = (datetime.now(UTC) - timedelta(seconds=600)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        stale = _seed_stale_running(session, chapter_number=99, started_at=stale_started_at)
        session.flush()
        stale_id = stale.id

    observed_threads: set[str] = set()
    lock = threading.Lock()
    fake_runner = _make_fake_runner(observed_threads, lock)

    original_runner = daemon.run_generation_queue_task
    daemon.run_generation_queue_task = fake_runner
    try:
        metrics = daemon.run_daemon(
            concurrency=3,
            stale_timeout_seconds=30,
            poll_interval_seconds=0.2,
            max_cycles=10,
        )
    finally:
        daemon.run_generation_queue_task = original_runner

    # Metric sanity.
    if metrics.tasks_completed < 4:
        failures.append(
            f"daemon should complete all 4 pending tasks, got tasks_completed={metrics.tasks_completed}"
        )
    if metrics.tasks_failed:
        failures.append(f"daemon should not report task failures on happy path: tasks_failed={metrics.tasks_failed}")
    if metrics.stale_recovered < 1:
        failures.append(f"daemon should auto-recover the seeded stale task, got stale_recovered={metrics.stale_recovered}")
    if metrics.cycles < 1:
        failures.append(f"daemon must record at least one tick, got cycles={metrics.cycles}")

    # Verify concurrency actually happened.
    if len(observed_threads) < 2:
        failures.append(
            f"daemon should dispatch across multiple worker threads, saw {sorted(observed_threads)}"
        )

    # Verify DB end state.
    with session_scope() as session:
        pending_left = list(
            session.scalars(
                select(GenerationTask).where(
                    GenerationTask.task_type.in_(llm_queue.QUEUE_TYPES),
                    GenerationTask.status == "pending",
                )
            )
        )
        completed = list(
            session.scalars(
                select(GenerationTask).where(
                    GenerationTask.task_type.in_(llm_queue.QUEUE_TYPES),
                    GenerationTask.status == "completed",
                )
            )
        )
        stale_task = session.get(GenerationTask, stale_id)
        stale_snapshot: dict | None = None
        if stale_task:
            stale_snapshot = {
                "status": stale_task.status,
                "input": _loads(stale_task.input_json),
                "output": _loads(stale_task.output_json),
            }
        pending_ids = [t.id for t in pending_left]
        completed_count = len(completed)

    if pending_ids:
        failures.append(f"daemon left pending tasks behind: {pending_ids}")
    if completed_count < 4:
        failures.append(f"daemon should mark all 4 seeded tasks completed, got {completed_count}")
    if not stale_snapshot:
        failures.append("stale task disappeared unexpectedly")
    else:
        stale_status = stale_snapshot["status"]
        stale_input = stale_snapshot["input"]
        stale_output = stale_snapshot["output"]
        if stale_status not in {"pending", "completed"}:
            failures.append(
                f"stale task should have been recovered (pending) or reprocessed (completed), got {stale_status}"
            )
        if stale_status == "pending":
            leftover_keys = {"lease_owner", "lease_acquired_at", "heartbeat_at"} & set(stale_input)
            if leftover_keys:
                failures.append(f"recovered stale task should clear lease keys, still has {sorted(leftover_keys)}")
            if not stale_output.get("recovered_from_status"):
                failures.append(f"recovered stale task should note recovered_from_status: {stale_output}")

    # Snapshot payload sanity.
    snapshot = metrics.snapshot()
    for key in ("uptime_seconds", "tasks_per_minute", "stale_recovered_per_hour", "cycles"):
        if key not in snapshot:
            failures.append(f"metrics.snapshot() missing key {key}: {snapshot}")

    if failures:
        print("generation_queue_daemon_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("generation_queue_daemon_regression=PASS")
    print(f"metrics={json.dumps(snapshot, ensure_ascii=False)}")
    print(f"worker_threads_observed={sorted(observed_threads)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
