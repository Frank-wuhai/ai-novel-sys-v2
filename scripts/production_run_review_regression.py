from __future__ import annotations

import json
from pathlib import Path

from app.db import session as db_session
from app.db.base import Base
from app.db.session import configure_database, session_scope
from app.models.entities import Book, Chapter, ProductionRunReview
from app.services.production_run_review import build_production_pattern_memory, build_production_run_review_payload


ROOT = Path(__file__).resolve().parents[1]
TEST_DB = ROOT / "data/production-run-review-regression.db"


def main() -> int:
    payload = build_production_run_review_payload(
        chapter_number=3,
        task_type="draft_chapter",
        version_id=12,
        task_id=34,
        output_data={
            "unit_flow_repair": {
                "attempted": True,
                "accepted": True,
                "mode": "local_units",
                "before": {"score": 62, "unit_count": 6, "units": []},
                "after": {
                    "score": 82,
                    "unit_count": 7,
                    "units": [
                        {"index": 2, "score": 66, "issues": ["handoff"], "summary": "承接偏弱"},
                        {"index": 5, "score": 78, "issues": [], "summary": "通过"},
                    ],
                },
                "unit_results": [
                    {"unit": 2, "accepted": True, "strategy": "- 承接断裂：补清前后后果"}
                ],
            },
            "unit_plan_alignment": {
                "expected_unit_count": 7,
                "actual_unit_count": 7,
                "unit_flow_score": 82,
                "alignment_score": 88,
                "passed": True,
                "issues": [],
            },
        },
    )
    failures: list[str] = []
    if payload.get("repair_mode") != "local_units":
        failures.append("repair_mode_not_local")
    if payload.get("status") != "pass":
        failures.append("status_not_pass")
    if "计划 7 个" not in payload.get("headline", ""):
        failures.append("headline_missing_plan_count")
    if not payload.get("repair_summary", {}).get("unit_results"):
        failures.append("missing_unit_results")
    memory = _memory_payload()
    if "承接" not in memory.get("headline", ""):
        failures.append("memory_headline_missing_handoff")
    if "生产复盘记忆" not in memory.get("prompt_block", ""):
        failures.append("memory_prompt_block_missing")
    if not memory.get("recommendations"):
        failures.append("memory_missing_recommendations")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "payload": payload,
                "memory": memory,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


def _memory_payload() -> dict:
    if TEST_DB.exists():
        TEST_DB.unlink()
    configure_database("sqlite:///data/production-run-review-regression.db")
    Base.metadata.create_all(db_session.engine)
    with session_scope() as session:
        book = Book(title="Run Review Regression", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter1 = Chapter(book_id=book.id, chapter_number=1, title="第一章")
        chapter2 = Chapter(book_id=book.id, chapter_number=2, title="第二章")
        session.add_all([chapter1, chapter2])
        session.flush()
        for index, chapter in enumerate([chapter1, chapter2], start=1):
            review = build_production_run_review_payload(
                chapter_number=index,
                task_type="draft_chapter",
                version_id=index,
                task_id=index,
                output_data={
                    "unit_flow_repair": {
                        "attempted": True,
                        "accepted": True,
                        "mode": "local_units",
                        "before": {"score": 58, "unit_count": 4, "units": []},
                        "after": {
                            "score": 68,
                            "unit_count": 5,
                            "units": [
                                {"index": 2, "score": 55, "issues": ["handoff", "reaction"], "summary": "承接和反应不足"}
                            ],
                        },
                    },
                    "unit_plan_alignment": {
                        "expected_unit_count": 7,
                        "actual_unit_count": 5,
                        "unit_flow_score": 68,
                        "alignment_score": 66,
                        "passed": False,
                        "issues": ["unit_count_low:5<7", "weak_units:2"],
                    },
                },
            )
            session.add(
                ProductionRunReview(
                    book_id=book.id,
                    chapter_id=chapter.id,
                    status="attention",
                    review_json=json.dumps(review, ensure_ascii=False),
                )
            )
        session.flush()
        return build_production_pattern_memory(session, book_id=book.id, chapter_number=3)


if __name__ == "__main__":
    raise SystemExit(main())
