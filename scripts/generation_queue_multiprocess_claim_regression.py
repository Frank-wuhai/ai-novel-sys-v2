"""Regression: DB-level atomic claim safe under multi-process contention.

Before ``claim_next_pending_task`` existed, the daemon relied on an in-process
``claimed_ids`` set + a SELECT-then-UPDATE flow. Two daemon *processes* would
happily grab the same pending task, run it twice, and cause duplicated LLM
spend + version divergence. This regression starts multiple worker processes
that all race for the same pool of pending tasks and asserts:

- every seeded task is claimed exactly once (no duplicate claims)
- every task ends up 'running' with a lease_owner from exactly one worker
- unclaimed tasks stay 'pending' (no lost tasks)

We do NOT run the tasks — we only exercise the claim path. Running would drag
in the full LLM stack; the invariant we care about is claim atomicity.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from regression_db import isolated_database, sqlite_path  # noqa: E402


REGRESSION_DB_NAME = "generation-queue-multiprocess-claim-regression"
NUM_TASKS = 20
NUM_WORKERS = 4
LEASE_SECONDS = 60


def _worker_body(db_url: str, worker_idx: int, result_queue: mp.Queue) -> None:
    """Child-process entrypoint. Configures its own DB URL then claims until dry."""

    # Each subprocess reconfigures the SQLAlchemy engine to point at the same
    # SQLite file the parent seeded. This mirrors production where multiple
    # daemon processes independently open the same DB.
    os.environ["AI_NOVEL_DATABASE_URL"] = db_url

    # Reimport lazily so the child gets a fresh engine.
    from app.db.session import configure_database, session_scope  # noqa: WPS433
    from app.services.llm_queue import claim_next_pending_task  # noqa: WPS433

    configure_database(db_url)

    claimed: list[dict] = []
    # Cap the loop to NUM_TASKS+1 so a runaway process never spins forever.
    for _ in range(NUM_TASKS + 1):
        with session_scope() as session:
            task = claim_next_pending_task(
                session,
                worker_id=f"proc{worker_idx}",
                lease_seconds=LEASE_SECONDS,
            )
            if task is None:
                break
            claimed.append(
                {
                    "task_id": task.id,
                    "lease_owner": json.loads(task.input_json).get("lease_owner"),
                    "status": task.status,
                }
            )
    result_queue.put({"worker": worker_idx, "claimed": claimed})


def _seed_pending_tasks(count: int) -> list[int]:
    from app.db.session import session_scope  # noqa: WPS433
    from app.models.entities import Book, GenerationTask  # noqa: WPS433
    from app.services import llm_queue  # noqa: WPS433

    ids: list[int] = []
    with session_scope() as session:
        session.add(
            Book(
                id=2,
                title="multiprocess claim regression",
                genre="武侠",
                target_platform="manual",
                status="draft",
            )
        )
        for chapter_number in range(1, count + 1):
            task = GenerationTask(
                book_id=2,
                task_type=llm_queue.QUEUE_REBUILD_CANDIDATES,
                status="pending",
                input_json=json.dumps(
                    {
                        "chapter_number": chapter_number,
                        "attempt": 0,
                        "max_attempts": 3,
                        "task_timeout_seconds": LEASE_SECONDS,
                    }
                ),
                output_json="{}",
            )
            session.add(task)
            session.flush()
            ids.append(task.id)
    return ids


def main() -> int:
    db_url = isolated_database(REGRESSION_DB_NAME)
    # ``isolated_database`` returns a relative sqlite path; expand to absolute
    # so subprocesses (which cwd-chdir is unpredictable) can locate the file.
    abs_path = sqlite_path(REGRESSION_DB_NAME).resolve()
    absolute_url = f"sqlite:///{abs_path}"

    seeded_ids = _seed_pending_tasks(NUM_TASKS)
    if len(seeded_ids) != NUM_TASKS:
        print("generation_queue_multiprocess_claim_regression=FAIL")
        print(f"- seeded {len(seeded_ids)} tasks, expected {NUM_TASKS}")
        return 1

    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    workers: list[mp.Process] = []
    for i in range(NUM_WORKERS):
        p = ctx.Process(target=_worker_body, args=(absolute_url, i, result_queue))
        p.start()
        workers.append(p)

    for p in workers:
        p.join(timeout=60)
        if p.is_alive():
            p.terminate()
            print("generation_queue_multiprocess_claim_regression=FAIL")
            print(f"- worker {p.pid} timed out and had to be terminated")
            return 1

    per_worker_results: list[dict] = []
    while not result_queue.empty():
        per_worker_results.append(result_queue.get_nowait())

    # Reconfigure parent DB back to the isolated file for verification.
    from app.db.session import configure_database, session_scope  # noqa: WPS433
    from app.models.entities import GenerationTask  # noqa: WPS433
    from sqlalchemy import select  # noqa: WPS433

    configure_database(absolute_url)

    failures: list[str] = []
    all_claimed_ids: list[int] = []
    per_worker_counts: dict[int, int] = {}
    for item in per_worker_results:
        worker_idx = item["worker"]
        worker_claims = item["claimed"]
        per_worker_counts[worker_idx] = len(worker_claims)
        for entry in worker_claims:
            all_claimed_ids.append(entry["task_id"])

    # Invariant 1: every task claimed exactly once (no duplicates).
    dup_counter = Counter(all_claimed_ids)
    duplicates = {tid: n for tid, n in dup_counter.items() if n > 1}
    if duplicates:
        failures.append(
            f"multi-process claim leaked duplicates: {duplicates}. Per-worker={per_worker_counts}"
        )

    # Invariant 2: total claimed == total seeded (no lost tasks).
    if len(all_claimed_ids) != NUM_TASKS:
        failures.append(
            f"claimed_total={len(all_claimed_ids)} but seeded {NUM_TASKS}. Per-worker={per_worker_counts}"
        )

    # Invariant 3: each seeded task ended up running with a lease_owner and
    #              the owner belongs to exactly one worker index (proc0..procN).
    with session_scope() as session:
        rows = list(
            session.scalars(
                select(GenerationTask).where(GenerationTask.id.in_(seeded_ids))
            )
        )
        status_counts = Counter(t.status for t in rows)
        pending_left = [t.id for t in rows if t.status == "pending"]
        missing_lease: list[int] = []
        lease_owners: list[str] = []
        for task in rows:
            data = json.loads(task.input_json or "{}")
            owner = data.get("lease_owner")
            if task.status == "running":
                if not owner:
                    missing_lease.append(task.id)
                else:
                    lease_owners.append(owner)

    if pending_left:
        failures.append(f"tasks left pending after multi-process claim: {pending_left}")
    if missing_lease:
        failures.append(f"running tasks missing lease_owner: {missing_lease}")
    unique_owners = {owner for owner in lease_owners}
    # Invariant 3 (concurrency proof): at least two workers must have won
    # a claim. Requiring *every* worker to win is brittle under SQLite
    # BEGIN IMMEDIATE contention — a straggler process may lose every race.
    # What we're really guarding against is silent serialization (only one
    # worker ever winning) which would prove the claim path isn't racing at
    # all.
    if len(unique_owners) < 2:
        failures.append(
            f"expected multi-process contention; only one worker won tasks. owners={sorted(unique_owners)}"
        )

    # Invariant 4: workers should share the load; single worker sweeping all
    # 20 tasks would still be correct-but-boring — flag it as a warning-level
    # failure so we notice if concurrency degrades to serialization.
    max_per_worker = max(per_worker_counts.values()) if per_worker_counts else 0
    if max_per_worker >= NUM_TASKS:
        failures.append(
            f"expected multi-process concurrency; one worker claimed all {NUM_TASKS} tasks: {per_worker_counts}"
        )

    if failures:
        print("generation_queue_multiprocess_claim_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        print(f"per_worker_counts={per_worker_counts}")
        print(f"status_counts={dict(status_counts)}")
        return 1

    print("generation_queue_multiprocess_claim_regression=PASS")
    print(
        f"summary={{'workers': {NUM_WORKERS}, 'tasks': {NUM_TASKS}, 'per_worker': {per_worker_counts}, "
        f"'unique_lease_owners': {sorted(unique_owners)}, 'status': {dict(status_counts)}}}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
