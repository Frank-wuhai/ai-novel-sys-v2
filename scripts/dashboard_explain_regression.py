from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.dashboard import build_project_snapshot
from regression_db import isolated_database


def main() -> int:
    isolated_database("dashboard-explain-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="dashboard explain", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第1章", required_beats="承接", constraints="", status="ready"))
        version = ChapterVersion(chapter_id=chapter.id, version_number=1, title="第1章", content="正文", status="needs_revision", source="revision:test")
        session.add(version)
        session.flush()
        session.add(QualityReport(chapter_version_id=version.id, score=60, passed=False, report=json.dumps({"issues": ["brief_coverage"]}, ensure_ascii=False)))
        session.add(GenerationTask(book_id=book.id, task_type="queue_revise_chapter", status="running", input_json=json.dumps({"chapter_number": 1, "running_started_at": "2000-01-01T00:00:00", "task_timeout_seconds": 1, "heartbeat_at": "2000-01-01T00:00:00", "lease_expires_at": "2000-01-01T00:00:01"}, ensure_ascii=False), output_json="{}"))
        session.flush()
        snapshot = build_project_snapshot(session, book_id=book.id, start=1, count=1)
        chapter_payload = snapshot["chapters"][0]
        task_payload = snapshot["generation_queue"]["tasks"][0]
        if "explain" not in chapter_payload:
            failures.append("chapter_explain_missing")
        if "summary" not in chapter_payload.get("explain", {}) or "next" not in chapter_payload.get("explain", {}):
            failures.append(f"chapter_explain_incomplete:{chapter_payload.get('explain')}")
        if "queue" not in chapter_payload or not chapter_payload["queue"]:
            failures.append("chapter_queue_context_missing")
        if task_payload.get("explain", {}).get("next") != "recover-stale-generation-tasks":
            failures.append(f"stale_queue_explain_wrong:{task_payload.get('explain')}")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("dashboard-explain-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
