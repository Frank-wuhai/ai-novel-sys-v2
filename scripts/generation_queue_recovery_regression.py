from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

import app.services.llm_queue as llm_queue
from app.db.session import session_scope
from app.models.entities import Book, GenerationTask
from app.services.dashboard import build_project_snapshot
from app.services.dashboard_background import background_runs_payload
from app.services.llm_queue import (
    build_generation_queue_health,
    cancel_generation_queue_task,
    recover_stale_generation_tasks,
    run_generation_queue_task,
)
from regression_db import isolated_database
from run_local_dashboard import _perform_action

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    isolated_database("generation-queue-recovery-regression")
    failures: list[str] = []

    with session_scope() as session:
        session.add(Book(id=2, title="回归测试书", genre="武侠", target_platform="manual", status="draft"))
        retryable = _running_task(
            session,
            chapter_number=2,
            attempt=1,
            max_attempts=3,
            started_at="2026-07-01T01:51:35Z",
        )
        exhausted = _running_task(
            session,
            chapter_number=3,
            attempt=3,
            max_attempts=3,
            started_at="2026-07-01T01:51:35+00:00",
        )
        force_target = _running_task(
            session,
            chapter_number=4,
            attempt=1,
            max_attempts=3,
            started_at="2026-07-01T01:51:35Z",
        )
        session.flush()
        retryable_id = retryable.id
        exhausted_id = exhausted.id
        force_target_id = force_target.id

    with session_scope() as session:
        try:
            health = build_generation_queue_health(session, stale_after_seconds=60)
            running_ids = {item.task_id for item in health.running_tasks}
            if {retryable_id, exhausted_id, force_target_id} - running_ids:
                failures.append(f"health missing running tasks: {running_ids}")
            if health.stale_running_count != 3:
                failures.append(f"expected 3 stale running tasks, got {health.stale_running_count}")
            for item in health.running_tasks:
                if item.running_age_seconds <= 0:
                    failures.append(f"running age should be positive for task {item.task_id}: {item.running_age_seconds}")
                if item.stale is not True or item.recoverable is not True:
                    failures.append(f"task {item.task_id} should be stale/recoverable: {item}")
        except Exception as exc:
            failures.append(f"build_generation_queue_health should tolerate Z/+00:00 timestamps: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        try:
            snapshot = build_project_snapshot(session, book_id=2, start=2, count=3)
            queue_items = snapshot.get("generation_queue", {}).get("tasks", [])
            if len(queue_items) != 3:
                failures.append(f"dashboard snapshot should include 3 queue tasks, got {len(queue_items)}")
            for item in queue_items:
                if item.get("running_age_seconds", 0) <= 0 or item.get("stale") is not True:
                    failures.append(f"dashboard queue item should report stale positive age: {item}")
        except Exception as exc:
            failures.append(f"build_project_snapshot should tolerate Z/+00:00 timestamps: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        try:
            recovered = recover_stale_generation_tasks(session, timeout_seconds=60, limit=2)
            recovered_by_id = {item.task_id: item for item in recovered}
            retryable_recovery = recovered_by_id.get(retryable_id)
            exhausted_recovery = recovered_by_id.get(exhausted_id)
            if not retryable_recovery or retryable_recovery.new_status != "pending":
                failures.append(f"retryable stale task should recover to pending: {retryable_recovery}")
            if not exhausted_recovery or exhausted_recovery.new_status != "failed":
                failures.append(f"exhausted stale task should recover to failed: {exhausted_recovery}")
            recovery_dict = retryable_recovery.to_dict() if retryable_recovery else {}
            if recovery_dict.get("task_id") != retryable_id or recovery_dict.get("new_status") != "pending":
                failures.append(f"stale recovery should serialize to dashboard payload: {recovery_dict}")
        except Exception as exc:
            failures.append(f"recover_stale_generation_tasks should recover aware timestamps: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        dashboard_target = _running_task(
            session,
            chapter_number=6,
            attempt=1,
            max_attempts=3,
            started_at="2026-07-01T01:51:35Z",
        )
        session.flush()
        dashboard_target_id = dashboard_target.id

    with session_scope() as session:
        try:
            result = _perform_action(session, {"action": "recover_stale_generation_tasks", "timeout_seconds": 60, "limit": 10})
            tasks = result.get("tasks") or []
            recovered_ids = {item.get("task_id") for item in tasks}
            if result.get("status") != "recovered" or dashboard_target_id not in recovered_ids:
                failures.append(f"dashboard stale recovery action should return serialized recovered tasks: {result}")
        except Exception as exc:
            failures.append(f"dashboard stale recovery action should not raise: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        retryable_task = session.get(GenerationTask, retryable_id)
        exhausted_task = session.get(GenerationTask, exhausted_id)
        _assert_recovered_task(failures, retryable_task, expected_status="pending", expected_retryable=True)
        _assert_recovered_task(failures, exhausted_task, expected_status="failed", expected_retryable=False)

    with session_scope() as session:
        try:
            canceled = cancel_generation_queue_task(session, task_id=force_target_id, reason="operator unblock", force=True)
            if canceled.status != "canceled":
                failures.append(f"force cancel should mark running task canceled, got {canceled.status}")
            output = _loads(canceled.output_json)
            if output.get("forced") is not True:
                failures.append(f"force cancel should record forced=true: {output}")
            if output.get("cancel_reason") != "operator unblock":
                failures.append(f"force cancel should keep operator reason: {output}")
        except Exception as exc:
            failures.append(f"cancel_generation_queue_task(force=True) should cancel running tasks: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        failing = _running_task(
            session,
            chapter_number=5,
            attempt=0,
            max_attempts=3,
            started_at="2026-07-01T01:51:35Z",
        )
        failing.status = "pending"
        failing.output_json = json.dumps({"error_category": "old_timeout", "error_type": "StaleRunningTask"})
        input_data = _loads(failing.input_json)
        input_data["chapter_number"] = 0
        failing.input_json = json.dumps(input_data)
        session.flush()
        failing_id = failing.id

    with session_scope() as session:
        try:
            run_generation_queue_task(session, task_id=failing_id)
        except Exception as exc:
            failures.append(f"run_generation_queue_task validation failures should be captured, not raised: {type(exc).__name__}: {exc}")

    with session_scope() as session:
        failed_task = session.get(GenerationTask, failing_id)
        if failed_task.status != "failed":
            failures.append(f"invalid chapter task should fail, got {failed_task.status}")
        failed_input = _loads(failed_task.input_json)
        lease_keys = {"running_started_at", "lease_owner", "lease_acquired_at", "lease_expires_at", "heartbeat_at"}
        remaining = lease_keys & set(failed_input)
        if remaining:
            failures.append(f"failed queue task should clear lease keys: {sorted(remaining)}")
        failed_output = _loads(failed_task.output_json)
        if failed_output.get("error_category") == "old_timeout" or failed_output.get("error_type") == "StaleRunningTask":
            failures.append(f"rerun queue task should replace stale output metadata: {failed_output}")

    with session_scope() as session:
        exception_task = _running_task(
            session,
            chapter_number=7,
            attempt=1,
            max_attempts=1,
            started_at="2026-07-01T01:51:35Z",
        )
        exception_task.status = "pending"
        session.flush()
        exception_task_id = exception_task.id
        original_generate = llm_queue.generate_rebuild_candidates
        try:
            def fail_generate(*args, **kwargs):
                raise RuntimeError("synthetic queue failure")

            llm_queue.generate_rebuild_candidates = fail_generate
            run_generation_queue_task(session, task_id=exception_task_id)
        finally:
            llm_queue.generate_rebuild_candidates = original_generate

    with session_scope() as session:
        exception_task = session.get(GenerationTask, exception_task_id)
        output = _loads(exception_task.output_json)
        if exception_task.status != "failed":
            failures.append(f"synthetic exception task should fail after max attempts, got {exception_task.status}")
        if int(output.get("running_age_seconds") or 0) < 0:
            failures.append(f"exception metadata should never record negative running age: {output}")

    dashboard = (ROOT / "app/dashboard.html").read_text(encoding="utf-8")
    for needle in [
        "const initialDashboardParams = new URLSearchParams(window.location.search);",
        "applyInitialDashboardParams();",
        "renderBookOptions(books, initialDashboardParams.get('book_id') || '')",
        "recover_stale_generation_tasks",
        "强制取消",
    ]:
        if needle not in dashboard:
            failures.append(f"dashboard should initialize controls from URL query; missing marker: {needle}")

    run_local_dashboard = (ROOT / "scripts/run_local_dashboard.py").read_text(encoding="utf-8")
    for needle in [
        '"kind": "queue"',
        '"idle_timeout_seconds": 3600',
    ]:
        if needle not in run_local_dashboard:
            failures.append(f"dashboard queue runs should allow long live LLM calls; missing marker: {needle}")

    payload = background_runs_payload(
        {
            "queue": {
                "run_id": "queue",
                "status": "running",
                "kind": "queue",
                "started_at": 1000.0,
                "last_progress_at": 1000.0,
                "idle_timeout_seconds": 3600,
            },
            "sample": {
                "run_id": "sample",
                "status": "running",
                "kind": "sample",
                "started_at": 1000.0,
                "last_progress_at": 1000.0,
            },
        },
        _DummyLock(),
        now=1200.0,
    )
    by_id = {item["run_id"]: item for item in payload}
    if by_id["queue"]["status"] != "running":
        failures.append(f"queue background run should not idle-timeout at 180s: {by_id['queue']}")
    if by_id["queue"].get("timeout_seconds") != 3600:
        failures.append(f"queue background payload should expose effective 3600s timeout: {by_id['queue']}")
    if by_id["sample"]["status"] != "failed":
        failures.append(f"default non-queue background run should still idle-timeout at 180s: {by_id['sample']}")

    if failures:
        print("generation_queue_recovery_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("generation_queue_recovery_regression=PASS")
    return 0


def _running_task(session, *, chapter_number: int, attempt: int, max_attempts: int, started_at: str) -> GenerationTask:
    task = GenerationTask(
        book_id=2,
        task_type="rebuild_chapter_candidates",
        status="running",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "attempt": attempt,
                "max_attempts": max_attempts,
                "task_timeout_seconds": 60,
                "running_started_at": started_at,
                "lease_owner": "regression-worker",
                "lease_acquired_at": started_at,
                "lease_expires_at": "2026-07-01T02:51:35Z",
                "heartbeat_at": started_at,
            }
        ),
        output_json=json.dumps(
            {
                "error_category": "timeout",
                "error_type": "StaleRunningTask",
                "error": "running task exceeded timeout_seconds=60",
                "retryable": True,
            }
        ),
    )
    session.add(task)
    return task


def _assert_recovered_task(failures: list[str], task: GenerationTask | None, *, expected_status: str, expected_retryable: bool) -> None:
    if not task:
        failures.append("expected task to exist after recovery")
        return
    input_data = _loads(task.input_json)
    output_data = _loads(task.output_json)
    if task.status != expected_status:
        failures.append(f"task {task.id} expected status {expected_status}, got {task.status}")
    lease_keys = {"running_started_at", "lease_owner", "lease_acquired_at", "lease_expires_at", "heartbeat_at"}
    remaining = lease_keys & set(input_data)
    if remaining:
        failures.append(f"task {task.id} should clear lease keys after recovery: {sorted(remaining)}")
    if output_data.get("retryable") is not expected_retryable:
        failures.append(f"task {task.id} retryable mismatch: {output_data}")
    if output_data.get("recovered_from_status") != "running":
        failures.append(f"task {task.id} should record recovered_from_status: {output_data}")


def _loads(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class _DummyLock:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


if __name__ == "__main__":
    raise SystemExit(main())
