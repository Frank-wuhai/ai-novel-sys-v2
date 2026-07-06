"""Regression for bug #4: _execute_generation_task_body flushes but never commits.

Symptom (production, 2026-07-06 20:19 E.4 Book2 Ch6):
    worker log: executed generation_task_id=1447 status=completed version_id=1209 child=1448
    DB after SIGTERM: task 1447 still 'running', v1209 never existed.

Root cause hypothesis:
    - cli.py runs `with session_scope() as session:` and reuses that ONE session
      across 2000 loops.
    - run_generation_queue_task() commits when it flips a task to 'running' (line 425).
    - _execute_generation_task_body() success tail (line 522) only calls
      session.flush() -- no commit.
    - If the outer session_scope exits via exception (SIGTERM, crash), the
      accumulated `task.status='completed'` and any new rows (version, child task)
      get rollback'd. Meanwhile the earlier 'running' commit persists as an
      orphaned lease.

This regression simulates that: run the handler for a pending task with a fake
task-body, close the session WITHOUT calling session_scope's implicit commit,
and assert the completed state persisted. It should FAIL before the fix and
PASS after.

Uses QUEUE_REBUILD_CANDIDATES because the commit-boundary bug lives in the
shared _execute_generation_task_body -- task type is irrelevant.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import session as db_session  # noqa: E402
from app.db.session import session_scope  # noqa: E402
from app.models.entities import Book, Chapter, ChapterVersion, GenerationTask  # noqa: E402
from app.services import llm_queue  # noqa: E402
from regression_db import isolated_database  # noqa: E402


def _seed(session, *, chapter_number: int) -> tuple[int, int, int]:
    """Seed a book+chapter+pending rebuild task, return the task id."""
    book = Book(id=2, title="commit-boundary-regression", genre="武侠", target_platform="manual", status="draft")
    session.add(book)
    session.flush()
    chapter = Chapter(book_id=book.id, chapter_number=chapter_number, title=f"Ch{chapter_number}", status="draft")
    session.add(chapter)
    session.flush()
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=1,
        status="needs_revision",
        content="prior draft body",
    )
    session.add(version)
    session.flush()
    task = GenerationTask(
        book_id=book.id,
        task_type=llm_queue.QUEUE_REBUILD_CANDIDATES,
        status="pending",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "attempt": 0,
                "max_attempts": 3,
                "task_timeout_seconds": 3600,
                "candidate_count": 1,
            }
        ),
        output_json="{}",
    )
    session.add(task)
    session.flush()
    return task.id, chapter.id, version.id


def main() -> int:
    isolated_database("generation-queue-commit-boundary-regression")
    failures: list[str] = []

    # --- Seed ---
    with session_scope() as session:
        task_id, chapter_id, source_version_id = _seed(session, chapter_number=6)

    # --- Fake generate_rebuild_candidates so we don't hit LLM ---
    # It must add a new ChapterVersion + enqueue a child task, mimicking the
    # real production path (that's what caused the SIGTERM-rollback in prod).
    def fake_rebuild(session, *, book_id, chapter_number, candidate_count, dry_run, existing_task_id):
        new_version = ChapterVersion(
            chapter_id=chapter_id,
            version_number=2,
            status="candidate",
            content="fake rebuilt candidate body",
        )
        session.add(new_version)
        session.flush()
        # Enqueue a child task (mimic real handler's downstream chain)
        child = GenerationTask(
            book_id=book_id,
            task_type="llm_review_chapter",
            status="pending",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "attempt": 0,
                    "max_attempts": 3,
                    "task_timeout_seconds": 3600,
                }
            ),
            output_json="{}",
        )
        session.add(child)
        session.flush()

        # Return an object compatible with what real generate_rebuild_candidates returns
        class _R:
            selected_version_id = new_version.id
        return _R()

    original_rebuild = llm_queue.generate_rebuild_candidates
    llm_queue.generate_rebuild_candidates = fake_rebuild

    # --- Simulate the worker loop: pipe the pending task through the handler
    # via a SessionLocal (NOT session_scope), then close WITHOUT commit ---
    # This models cli.py's outer session_scope tripping (SIGTERM/exception) after
    # the handler returned. session_scope's finally calls session.close(); on the
    # exception path it also rolls back. We simulate exception path.
    result = None
    handler_snapshot: dict = {}
    session = db_session.SessionLocal()
    try:
        result = llm_queue.run_generation_queue_task(session, task_id=task_id)
        # snapshot handler's in-memory success state BEFORE rollback detaches it
        handler_snapshot = {
            "task_status": result.task.status,
            "version_id": result.version_id,
            "child_generation_task_id": result.child_generation_task_id,
        }
        # Handler returned. Now the OUTER session_scope explodes:
        raise RuntimeError("simulated SIGTERM / outer session_scope failure")
    except RuntimeError:
        session.rollback()  # exactly what session_scope does on exception
    finally:
        session.close()
        llm_queue.generate_rebuild_candidates = original_rebuild

    # Sanity: handler reported success in-memory before the outer crash
    if not handler_snapshot:
        failures.append("handler did not return a result before simulated crash")
    else:
        if handler_snapshot["task_status"] != "completed":
            failures.append(
                f"handler should mark task completed in-memory, got status={handler_snapshot['task_status']}"
            )
        if handler_snapshot["version_id"] is None:
            failures.append(
                f"handler should return a version_id, got {handler_snapshot['version_id']}"
            )

    # --- Verify DB state on a fresh session ---
    with session_scope() as fresh:
        db_task = fresh.get(GenerationTask, task_id)
        db_task_status = db_task.status if db_task else None
        db_versions = list(
            fresh.scalars(
                select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id)
            )
        )
        version_numbers = sorted(v.version_number for v in db_versions)
        db_child_tasks = list(
            fresh.scalars(
                select(GenerationTask).where(
                    GenerationTask.book_id == 2,
                    GenerationTask.id != task_id,
                )
            )
        )
        child_ids = [t.id for t in db_child_tasks]

    if db_task is None:
        failures.append("task disappeared from DB entirely")
    else:
        if db_task_status != "completed":
            failures.append(
                f"BUG #4 reproduced: task.status should be 'completed' after handler success, "
                f"got '{db_task_status}' -- worker log said completed but rollback erased it"
            )

    if 2 not in version_numbers:
        failures.append(
            f"BUG #4 reproduced: rebuilt ChapterVersion (v2) missing after rollback, "
            f"got version_numbers={version_numbers}"
        )

    if not child_ids:
        failures.append(
            "BUG #4 reproduced: child (review) task never landed in DB after rollback"
        )

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
