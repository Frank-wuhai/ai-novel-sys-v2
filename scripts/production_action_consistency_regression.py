from __future__ import annotations

import json
import re
from pathlib import Path

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import AUTO_ACTIONS, build_human_decision_package, plan_chapters, run_next_action
from app.services.production_actions import MANUAL_ACTIONS
from app.services.production_decision import decide_chapter_production
from regression_db import isolated_database


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    isolated_database("production-action-consistency-regression")
    failures: list[str] = []

    if "record_chapter_continuity" not in AUTO_ACTIONS:
        failures.append("continuity_missing_from_backend_auto_actions")
    if "record_chapter_continuity" in MANUAL_ACTIONS:
        failures.append("continuity_still_manual_backend_action")

    dashboard = (ROOT / "app" / "dashboard.html").read_text(encoding="utf-8")
    auto_match = re.search(r"const AUTO_PRODUCTION_ACTIONS = \[(.*?)\];", dashboard)
    auto_text = auto_match.group(1) if auto_match else ""
    if "record_chapter_continuity" not in auto_text:
        failures.append("continuity_missing_from_dashboard_auto_actions")
    if "reading_assessment_review" in auto_text:
        failures.append("legacy_reading_assessment_still_dashboard_auto_action")

    with session_scope() as session:
        book = Book(title="Action Consistency", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(chapter_id=chapter.id, goal="生成第一章", required_beats="主角行动", constraints="完整正文", status="ready")
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="可读正文" * 1200,
            status="reviewed_pass",
            source="draft:regression",
        )
        session.add_all([brief, version])
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=88,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 88, "passed": True}, ensure_ascii=False),
        )
        session.add(quality)
        session.flush()

        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        decision = decide_chapter_production(item)
        package_before = build_human_decision_package(session, book_id=book.id, start=1, count=1)
        if item.next_action != "record_chapter_continuity":
            failures.append(f"expected_continuity_next_action:{item.next_action}")
        if not decision.can_continue or decision.needs_author:
            failures.append(f"continuity_decision_not_auto:{decision.to_dict()}")
        if package_before.continuity_count != 0:
            failures.append(f"continuity_still_in_human_package:{package_before.continuity_count}")

        result = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=False)
        if result.action != "record_chapter_continuity" or result.status != "executed":
            failures.append(f"continuity_run_not_executed:{result}")
        item_after = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        package_after = build_human_decision_package(session, book_id=book.id, start=1, count=1)
        if item_after.next_action != "approve_chapter":
            failures.append(f"expected_approval_after_continuity:{item_after.next_action}")
        if package_after.approval_count != 1 or package_after.continuity_count != 0:
            failures.append(f"approval_package_wrong:{package_after.approval_count}:{package_after.continuity_count}")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("production-action-consistency-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

