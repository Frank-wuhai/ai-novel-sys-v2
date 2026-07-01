from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from time import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import ROOT_DIR
from app.db import session as db_session
from app.db.init import current_sqlite_path, init_db
from app.models.entities import GenerationTask
from app.services.execution_mode import ExecutionMode
from app.services.production_kernel import ProductionKernel


@dataclass(frozen=True)
class SandboxStep:
    chapter_number: int
    action: str
    status: str
    message: str
    object_id: int | None


@dataclass(frozen=True)
class SandboxRunResult:
    source_db: str
    sandbox_db: str
    artifact_path: str
    book_id: int
    start_chapter: int
    end_chapter: int
    steps: list[SandboxStep]
    queued_tasks_cleared: int
    pending_tasks_after: int

    def to_dict(self) -> dict:
        return {
            "source_db": self.source_db,
            "sandbox_db": self.sandbox_db,
            "artifact_path": self.artifact_path,
            "book_id": self.book_id,
            "start_chapter": self.start_chapter,
            "end_chapter": self.end_chapter,
            "queued_tasks_cleared": self.queued_tasks_cleared,
            "pending_tasks_after": self.pending_tasks_after,
            "steps": [step.__dict__ for step in self.steps],
        }


def production_sandbox_run(
    *,
    book_id: int,
    start_chapter: int,
    end_chapter: int,
    from_live: bool = True,
    max_steps_per_chapter: int = 10,
    artifact_dir: Path | None = None,
) -> SandboxRunResult:
    if book_id < 1:
        raise ValueError("book_id must be >= 1")
    if start_chapter < 1 or end_chapter < start_chapter:
        raise ValueError("chapter range is invalid")
    if max_steps_per_chapter < 1:
        raise ValueError("max_steps_per_chapter must be >= 1")

    source = current_sqlite_path()
    if from_live and not source:
        raise ValueError("--from-live requires a sqlite DATABASE_URL")
    if from_live and source and not source.exists():
        raise ValueError(f"source database not found: {source}")

    run_id = int(time())
    output_dir = artifact_dir or (ROOT_DIR / "data" / "dry_run_reports")
    output_dir.mkdir(parents=True, exist_ok=True)
    sandbox_db = output_dir / f"production_sandbox_book{book_id}_{start_chapter}_{end_chapter}_{run_id}.db"
    if from_live and source:
        _copy_sqlite_database(source, sandbox_db)
    else:
        db_session.configure_database(f"sqlite:///{sandbox_db}")
        init_db()

    original_url = str(db_session.settings.database_url)
    steps: list[SandboxStep] = []
    queued_tasks_cleared = 0
    pending_tasks_after = 0
    try:
        db_session.configure_database(f"sqlite:///{sandbox_db}")
        with db_session.session_scope() as session:
            queued_tasks_cleared = _clear_sandbox_generation_interference(session, book_id=book_id)
            for chapter_number in range(start_chapter, end_chapter + 1):
                run = ProductionKernel(session, book_id=book_id, chapter_number=chapter_number).run_until_terminal(
                    mode=ExecutionMode.SANDBOX,
                    max_steps=max_steps_per_chapter,
                )
                for event in run.executed:
                    steps.append(
                        SandboxStep(
                            chapter_number=chapter_number,
                            action=str(event.get("action", "")),
                            status=str(event.get("status", "")),
                            message=str(event.get("message", "")),
                            object_id=event.get("object_id"),
                        )
                    )
            pending_tasks_after = len(
                list(
                    session.scalars(
                        select(GenerationTask).where(
                            GenerationTask.book_id == book_id,
                            GenerationTask.status.in_(["pending", "running"]),
                        )
                    )
                )
            )
    finally:
        db_session.configure_database(original_url)

    artifact = output_dir / f"production_sandbox_book{book_id}_{start_chapter}_{end_chapter}_{run_id}.json"
    result = SandboxRunResult(
        source_db=str(source or ""),
        sandbox_db=str(sandbox_db),
        artifact_path=str(artifact),
        book_id=book_id,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        steps=steps,
        queued_tasks_cleared=queued_tasks_cleared,
        pending_tasks_after=pending_tasks_after,
    )
    artifact.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(destination) + suffix)
        if path.exists():
            path.unlink()
    with sqlite3.connect(source) as source_conn, sqlite3.connect(destination) as dest_conn:
        source_conn.backup(dest_conn)


def _clear_sandbox_generation_interference(session: Session, *, book_id: int) -> int:
    tasks = list(
        session.scalars(
            select(GenerationTask).where(
                GenerationTask.book_id == book_id,
                GenerationTask.status.in_(["pending", "running"]),
            )
        )
    )
    for task in tasks:
        task.status = "cancelled"
        task.output_json = json.dumps(
            {"sandbox_cancelled": True, "reason": "production_sandbox_run clears live queue interference"},
            ensure_ascii=False,
        )
    session.flush()
    return len(tasks)
