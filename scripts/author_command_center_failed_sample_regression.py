from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, GenerationTask
from app.services.author_command_center import _active_failed_tasks
from regression_db import isolated_database


def main() -> int:
    isolated_database("author-command-center-failed-sample-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="failed sample regression", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        failed_sample = GenerationTask(
            book_id=book.id,
            task_type="chapter_sample_lab",
            status="failed",
            input_json=json.dumps({"chapter_number": 2}, ensure_ascii=False),
            output_json=json.dumps(
                {
                    "error_category": "json_repair_failed",
                    "error": "Expecting ',' delimiter",
                    "retryable": True,
                },
                ensure_ascii=False,
            ),
        )
        session.add(failed_sample)
        session.flush()
        session.add(
            GenerationTask(
                book_id=book.id,
                task_type="revise_chapter",
                status="completed",
                input_json=json.dumps({"chapter_number": 2}, ensure_ascii=False),
                output_json=json.dumps({"version_id": 100}, ensure_ascii=False),
            )
        )
        session.flush()
        active = _active_failed_tasks(session, book_id=book.id, limit=5)
        if active:
            failures.append(f"obsolete_sample_failure_still_active:{active}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("author-command-center-failed-sample-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
