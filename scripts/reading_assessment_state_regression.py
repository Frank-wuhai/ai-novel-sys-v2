from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import build_human_decision_package, plan_chapters
from app.services.production import approve_chapter
from app.services.production_decision import decide_chapter_production
from regression_db import isolated_database


def main() -> int:
    isolated_database("reading-assessment-state-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="Reading Assessment State Regression", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="已过基础质检但仍待阅读评估的正文" * 1000,
            status="needs_revision",
            source="revision:regression",
        )
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估结论：当前稿不是正式批准稿，需要阅读判断。",
            required_beats="reading_assessment_contract: 检查开头、场景、人物动机是否真正变好。",
            constraints="当前稿不是正式批准稿；通过阅读评估后才能关闭修订合同。",
            status="revision_ready",
        )
        session.add_all([version, brief])
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=78,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 78, "passed": True}, ensure_ascii=False),
        )
        session.add(quality)
        session.flush()

        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        decision = decide_chapter_production(item)
        package = build_human_decision_package(session, book_id=book.id, start=1, count=1)

        if item.next_action != "reading_assessment_review":
            failures.append(f"unexpected_next_action:{item.next_action}:{item.reason}")
        if not decision.needs_author or decision.primary_intent != "approve":
            failures.append(f"unexpected_decision:{decision.to_dict()}")
        if package.approval_count != 1:
            failures.append(f"missing_human_approval_package:{package.to_dict() if hasattr(package, 'to_dict') else package.approval_count}")

        approve_chapter(session, version_id=version.id, reviewer="regression")
        session.flush()
        if brief.status != "superseded":
            failures.append(f"approval_did_not_close_revision_brief:{brief.status}")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("reading-assessment-state-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
