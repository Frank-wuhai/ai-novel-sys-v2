"""Long-running background daemon that continuously drains the generation queue.

Design goals (see conversation notes 2026-07-01):
- Multi-worker parallel task execution to unblock daily-word-count and
  multi-book concurrent authoring.
- Automatic recovery of stale/running tasks each tick so operators don't
  have to click "recover stale tasks" manually.
- Structured stdout/stderr logging so the daemon is observable when run
  via nohup / systemd / tmux.
- Optimistic claim: SELECT pending + UPDATE running inside a session
  scope so multiple worker threads never grab the same task.

The daemon exits cleanly on SIGINT/SIGTERM. Use --max-cycles for tests.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import GenerationTask
from app.services.llm_queue import (
    QUEUE_TYPES,
    claim_next_pending_task,
    recover_stale_generation_tasks,
    run_generation_queue_task,
)


LOGGER = logging.getLogger("generation_queue_daemon")


@dataclass
class DaemonMetrics:
    """In-memory counters used for structured logging and observability."""

    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    tasks_completed: int = 0
    tasks_failed: int = 0
    stale_recovered: int = 0
    cycles: int = 0
    last_tick_at: Optional[datetime] = None

    def snapshot(self) -> dict:
        now = datetime.now(UTC)
        uptime = max((now - self.started_at).total_seconds(), 1.0)
        return {
            "uptime_seconds": int(uptime),
            "cycles": self.cycles,
            "tasks_completed": self.tasks_completed,
            "tasks_failed": self.tasks_failed,
            "stale_recovered": self.stale_recovered,
            "tasks_per_minute": round(self.tasks_completed / uptime * 60, 2),
            "stale_recovered_per_hour": round(self.stale_recovered / uptime * 3600, 2),
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
        }


class ShutdownFlag:
    """Thread-safe shutdown signal shared across worker threads."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def request(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def wait(self, seconds: float) -> None:
        self._event.wait(timeout=seconds)


def _claim_next_pending_task(
    claimed_ids: set[int], *, worker_id: str, lease_seconds: int
) -> Optional[int]:
    """Atomically pick the next pending task via DB-level UPDATE.

    Delegates the actual atomic claim to
    :func:`app.services.llm_queue.claim_next_pending_task`, which uses an
    ``UPDATE ... WHERE status='pending'`` guarded by row-level atomicity so
    multi-process daemons can't grab the same task twice. The in-memory
    ``claimed_ids`` set is a second-line defence: it prevents worker threads
    inside *this* daemon process from re-picking a task whose row-flip is
    still committing when the next tick fires.
    """

    with session_scope() as session:
        task = claim_next_pending_task(
            session,
            worker_id=worker_id,
            lease_seconds=lease_seconds,
            exclude_task_ids=claimed_ids,
        )
        if task is None:
            return None
        return task.id


def _run_task(task_id: int, metrics: DaemonMetrics) -> None:
    """Worker-thread entrypoint: process a single queue task.

    The task must already have been atomically claimed (status='running',
    lease_owner set) via ``claim_next_pending_task`` before this is invoked.
    We call ``run_generation_queue_task(pre_claimed=True)`` so the runner
    knows to skip re-claim / re-lease and just execute the body.
    """

    started = time.monotonic()
    try:
        with session_scope() as session:
            result = run_generation_queue_task(
                session, task_id=task_id, pre_claimed=True
            )
            # Read every attribute we need while the ORM instance is still
            # bound to the session; the session closes when we leave the
            # ``with`` block and we don't want DetachedInstanceError.
            final_status = result.task.status
            final_type = result.task.task_type
            version_id = result.version_id
            child_task_id = result.child_generation_task_id
        elapsed = time.monotonic() - started
        LOGGER.info(
            "task=%s type=%s status=%s elapsed=%.1fs version_id=%s child_task=%s",
            task_id,
            final_type,
            final_status,
            elapsed,
            version_id,
            child_task_id,
        )
        if final_status == "completed":
            metrics.tasks_completed += 1
        else:
            metrics.tasks_failed += 1
    except Exception as exc:  # noqa: BLE001 — daemon must never die on a task error
        elapsed = time.monotonic() - started
        LOGGER.exception("task=%s crashed after %.1fs: %s", task_id, elapsed, exc)
        metrics.tasks_failed += 1


def _recover_stale(stale_timeout_seconds: int, metrics: DaemonMetrics) -> None:
    """Best-effort stale-task recovery, called once per daemon tick."""

    try:
        with session_scope() as session:
            recovered = recover_stale_generation_tasks(
                session, timeout_seconds=stale_timeout_seconds, limit=50
            )
        if recovered:
            metrics.stale_recovered += len(recovered)
            for item in recovered:
                LOGGER.warning(
                    "stale_recovered task=%s previous=%s new=%s chapter=%s attempt=%s/%s age_seconds=%s",
                    item.task_id,
                    item.previous_status,
                    item.new_status,
                    item.chapter_number,
                    item.attempt,
                    item.max_attempts,
                    item.age_seconds,
                )
    except Exception as exc:  # noqa: BLE001 — recovery failure must not stop the daemon
        LOGGER.exception("stale recovery failed: %s", exc)


def _default_daemon_id() -> str:
    # Combines PID and a short random suffix so lease_owner is unique even
    # when several daemon processes on the same host share a PID space
    # (containers, forked launchers) or when the same PID is reused after
    # a crash. Format: "<hostname>-<pid>-<random>".
    host = os.uname().nodename.split(".")[0]
    return f"{host}-{os.getpid()}-{uuid4().hex[:6]}"


def run_daemon(
    *,
    concurrency: int,
    stale_timeout_seconds: int,
    poll_interval_seconds: float,
    max_cycles: Optional[int] = None,
    daemon_id: Optional[str] = None,
) -> DaemonMetrics:
    """Top-level daemon loop. Blocks until shutdown or max_cycles reached."""

    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    if poll_interval_seconds <= 0:
        raise ValueError("poll_interval_seconds must be > 0")

    metrics = DaemonMetrics()
    shutdown = ShutdownFlag()
    daemon_id = daemon_id or _default_daemon_id()

    def _handle_signal(signum, _frame):
        LOGGER.info("received signal %s — starting graceful shutdown", signum)
        shutdown.request()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    LOGGER.info(
        "daemon starting id=%s concurrency=%s stale_timeout=%ss poll_interval=%ss max_cycles=%s",
        daemon_id,
        concurrency,
        stale_timeout_seconds,
        poll_interval_seconds,
        max_cycles,
    )

    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="queue-worker") as pool:
        active: set[Future] = set()
        claimed_ids: set[int] = set()
        active_lock = threading.Lock()

        def _worker_done(fut: Future, task_id: int) -> None:
            with active_lock:
                active.discard(fut)
                claimed_ids.discard(task_id)

        while not shutdown.is_set():
            metrics.cycles += 1
            metrics.last_tick_at = datetime.now(UTC)

            _recover_stale(stale_timeout_seconds, metrics)

            # Fill worker slots.
            with active_lock:
                slots_free = concurrency - len(active)
            dispatched = 0
            for slot_index in range(max(slots_free, 0)):
                if shutdown.is_set():
                    break
                # Each slot gets a unique lease owner so if a daemon crashes
                # between claim and heartbeat, stale-recovery can tell which
                # worker was responsible.
                worker_id = f"{daemon_id}#w{slot_index}"
                with active_lock:
                    task_id = _claim_next_pending_task(
                        claimed_ids,
                        worker_id=worker_id,
                        lease_seconds=stale_timeout_seconds,
                    )
                    if task_id is None:
                        break
                    claimed_ids.add(task_id)
                fut = pool.submit(_run_task, task_id, metrics)
                with active_lock:
                    active.add(fut)
                fut.add_done_callback(lambda f, tid=task_id: _worker_done(f, tid))
                dispatched += 1

            if metrics.cycles % 5 == 0 or dispatched:
                LOGGER.info("tick metrics=%s dispatched=%s", json.dumps(metrics.snapshot()), dispatched)

            if max_cycles is not None and metrics.cycles >= max_cycles:
                LOGGER.info("reached max_cycles=%s, initiating shutdown", max_cycles)
                shutdown.request()
                break

            shutdown.wait(poll_interval_seconds)

        # Drain inflight tasks after shutdown request.
        LOGGER.info("draining %s inflight task(s)", len(active))
        for fut in list(active):
            fut.result()

    LOGGER.info("daemon stopped id=%s metrics=%s", daemon_id, json.dumps(metrics.snapshot()))
    return metrics


def _parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument(
        "--concurrency",
        type=int,
        default=2,
        help="Number of parallel worker threads (default 2). Bound by LLM rate limits.",
    )
    parser.add_argument(
        "--stale-timeout",
        type=int,
        default=3600,
        help="Seconds after which a running task is considered stale (default 3600).",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds to wait between empty-queue polls (default 5).",
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=None,
        help="Optional cap on tick count for tests / smoke runs.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level (default INFO).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run_daemon(
        concurrency=args.concurrency,
        stale_timeout_seconds=args.stale_timeout,
        poll_interval_seconds=args.poll_interval,
        max_cycles=args.max_cycles,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
