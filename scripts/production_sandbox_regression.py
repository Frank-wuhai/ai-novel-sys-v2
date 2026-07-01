from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport, StoryFoundation
from app.services.production_sandbox import production_sandbox_run
from regression_db import isolated_database, sqlite_path


def main() -> int:
    isolated_database("production-sandbox-source")
    failures: list[str] = []
    source_path = sqlite_path("production-sandbox-source")
    with session_scope() as session:
        book = Book(title="sandbox", genre="test", target_platform="manual", status="draft")
        session.add(book)
        session.flush()
        session.add(StoryFoundation(book_id=book.id, premise="premise", reader_promise="promise", status="active"))
        chapter1 = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="briefing")
        chapter2 = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add_all([chapter1, chapter2])
        session.flush()
        session.add_all(
            [
                ChapterBrief(chapter_id=chapter1.id, goal="第1章", required_beats="承接", constraints="", status="ready"),
                ChapterBrief(chapter_id=chapter2.id, goal="第2章", required_beats="承接", constraints="", status="ready"),
                GenerationTask(book_id=book.id, task_type="queue_draft_chapter", status="pending", input_json=json.dumps({"chapter_number": 1}), output_json="{}"),
            ]
        )
        session.flush()
        book_id = book.id

    result = production_sandbox_run(book_id=book_id, start_chapter=1, end_chapter=2, from_live=True, max_steps_per_chapter=2)
    if not Path(result.sandbox_db).exists():
        failures.append("sandbox_db_not_created")
    if not Path(result.artifact_path).exists():
        failures.append("sandbox_artifact_not_created")
    if result.queued_tasks_cleared != 1:
        failures.append(f"sandbox_did_not_clear_queue_interference:{result.queued_tasks_cleared}")
    if result.pending_tasks_after != 0:
        failures.append(f"sandbox_left_pending_tasks:{result.pending_tasks_after}")
    if [step.chapter_number for step in result.steps[:2]] != [1, 2]:
        failures.append(f"sandbox_range_not_executed:{result.to_dict()}")

    with session_scope() as session:
        live_pending = session.scalar(select(GenerationTask).where(GenerationTask.book_id == book_id, GenerationTask.status == "pending"))
        if not live_pending:
            failures.append("live_queue_mutated_by_sandbox")
        live_versions = list(session.scalars(select(ChapterVersion).join(Chapter).where(Chapter.book_id == book_id)))
        live_quality = list(session.scalars(select(QualityReport).join(ChapterVersion).join(Chapter).where(Chapter.book_id == book_id)))
        if live_versions or live_quality:
            failures.append(f"sandbox_wrote_live_generation_state:v{len(live_versions)}:q{len(live_quality)}")
    if not source_path.exists():
        failures.append("source_db_missing_after_sandbox")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("production-sandbox-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
