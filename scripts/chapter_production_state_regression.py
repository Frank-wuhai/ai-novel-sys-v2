from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from regression_db import isolated_database
from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, PublishJob, QualityReport
from app.services.chapter_production_state import get_chapter_production_state


def main() -> int:
    isolated_database("chapter-production-state-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="状态回归", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        missing = get_chapter_production_state(session, book_id=book.id, chapter_number=1)
        if missing.status != "not_started":
            failures.append(f"missing_status:{missing.to_dict()}")
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="drafting")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(chapter_id=chapter.id, version_number=1, title="第1章", content="正文", status="draft")
        session.add(version)
        task = GenerationTask(
            book_id=book.id,
            task_type="queue_revise_chapter",
            status="pending",
            input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
        )
        other = GenerationTask(
            book_id=book.id,
            task_type="queue_revise_chapter",
            status="pending",
            input_json=json.dumps({"chapter_number": 2}, ensure_ascii=False),
        )
        session.add_all([task, other])
        session.flush()
        queued = get_chapter_production_state(session, book_id=book.id, chapter_number=1)
        if queued.status != "queued" or queued.active_task_id != task.id:
            failures.append(f"queued_state_wrong:{queued.to_dict()}")
        task.status = "completed"
        version.status = "needs_revision"
        quality = QualityReport(chapter_version_id=version.id, score=45, passed=False, report="{}")
        brief = ChapterBrief(chapter_id=chapter.id, goal="修订", required_beats="", constraints="revision_mode:rewrite", status="revision_ready")
        session.add_all([quality, brief])
        session.flush()
        state = get_chapter_production_state(session, book_id=book.id, chapter_number=1)
        if state.status != "needs_revision" or state.active_revision_brief_id != brief.id:
            failures.append(f"needs_revision_state_wrong:{state.to_dict()}")
        brief.status = "used"
        version.status = "approved"
        quality.passed = True
        job = PublishJob(chapter_version_id=version.id, platform="manual", status="queued")
        session.add(job)
        session.flush()
        publish_ready = get_chapter_production_state(session, book_id=book.id, chapter_number=1)
        if publish_ready.status != "publish_ready" or publish_ready.publish_job_id != job.id:
            failures.append(f"publish_ready_state_wrong:{publish_ready.to_dict()}")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("chapter-production-state-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
