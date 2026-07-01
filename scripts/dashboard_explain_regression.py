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

    # Second scenario: historical completed / canceled queue tasks must NOT leak
    # onto the chapter snapshot's queue field. Regression for fix_history_task_leak.
    isolated_database("dashboard-explain-history-leak")
    with session_scope() as session:
        book = Book(title="history leak", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="continuity_recorded")
        session.add(chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=chapter.id,
                goal="第1章",
                required_beats="承接",
                constraints="",
                status="ready",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id=chapter.id,
                version_number=1,
                title="第1章",
                content="正文",
                status="approved",
                source="draft:test",
            )
        )
        # Historical completed rebuild — must be filtered out from chapter.queue.
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="rebuild_chapter_candidates",
                status="completed",
                input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
                output_json="{}",
            )
        )
        # Historical canceled draft — must also be filtered out.
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="queue_draft_chapter",
                status="canceled",
                input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
                output_json="{}",
            )
        )
        session.flush()
        snapshot = build_project_snapshot(session, book_id=book.id, start=1, count=1)
        chapter_payload = snapshot["chapters"][0]
        queue_payload = chapter_payload.get("queue") or {}
        if queue_payload:
            failures.append(
                f"chapter_queue_should_be_empty_when_only_historical_tasks_exist: {queue_payload.get('status')} id={queue_payload.get('id')}"
            )
        why = (chapter_payload.get("explain") or {}).get("why") or ""
        if "关联队列任务" in why:
            failures.append(f"chapter_explain_should_not_mention_historical_queue_task: {why!r}")

    # Third scenario: an active (pending) queue task on the same chapter still surfaces.
    isolated_database("dashboard-explain-history-vs-active")
    with session_scope() as session:
        book = Book(title="history vs active", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=chapter.id,
                goal="第1章",
                required_beats="承接",
                constraints="",
                status="ready",
            )
        )
        # Older completed task — should be ignored.
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="rebuild_chapter_candidates",
                status="completed",
                input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
                output_json="{}",
            )
        )
        # Active pending task — should surface.
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="queue_revise_chapter",
                status="pending",
                input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
                output_json="{}",
            )
        )
        session.flush()
        snapshot = build_project_snapshot(session, book_id=book.id, start=1, count=1)
        chapter_payload = snapshot["chapters"][0]
        queue_payload = chapter_payload.get("queue") or {}
        if queue_payload.get("status") != "pending":
            failures.append(
                f"active_pending_task_should_surface_over_historical_completed: got status={queue_payload.get('status')}"
            )
        if queue_payload.get("type") != "queue_revise_chapter":
            failures.append(
                f"active_pending_task_type_wrong: got type={queue_payload.get('type')}"
            )

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("dashboard-explain-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
