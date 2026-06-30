from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import plan_chapters
from app.services.revision_success_boost import BOOST_MARKER, apply_revision_success_boost
from regression_db import isolated_database


def main() -> int:
    isolated_database("revision-success-boost-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="revision boost", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估自动修订第2章",
            required_beats="reading_assessment_auto_quality#1\n旧的泛化要求",
            constraints="revision_mode:targeted\n旧块\nrevision_success_boost@v1\n过期指令\nrevision_success_boost@end",
            status="revision_ready",
        )
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=7,
            title="第2章",
            content="候选稿" * 1200,
            status="needs_revision",
            source="rebuild_candidate_selected:v6",
        )
        session.add_all([brief, version])
        session.flush()
        report = {
            "score": 72,
            "dimensions": {
                "brief_coverage": 50,
                "scene_atmosphere": 37,
                "dialogue_fullness": 33,
                "imageable_paragraphs": 44,
                "naming_governance": 18,
            },
            "issues": ["dialogue_underdeveloped: 33"],
            "reading_assessment": {
                "blockers": ["brief_coverage=50<60", "scene_atmosphere=37<55"],
                "blocker_notes": ["章节说明里的关键承诺没有写足", "场景氛围没有真正改变人物判断或行动"],
                "improve": ["让对白承担试探、遮掩、交易或情绪变化。"],
            },
        }
        session.add(QualityReport(chapter_version_id=version.id, score=72, passed=False, report=json.dumps(report, ensure_ascii=False)))
        session.flush()

        result = apply_revision_success_boost(session, book_id=book.id, chapter_number=2)
        if not result.applied or result.focus_count < 4:
            failures.append(f"boost_not_applied:{result}")
        text = "\n".join([brief.required_beats or "", brief.constraints or ""])
        if text.count(BOOST_MARKER) != 1:
            failures.append("boost_marker_not_single")
        if "禁止再生成候选" not in text:
            failures.append("selected_candidate_not_locked")
        if "章节说明里的关键承诺没有写足" not in text or "dialogue_underdeveloped" not in text:
            failures.append("quality_focus_not_visible")
        if "过期指令" in text:
            failures.append("stale_boost_not_removed")

        apply_revision_success_boost(session, book_id=book.id, chapter_number=2)
        text_after_second_call = "\n".join([brief.required_beats or "", brief.constraints or ""])
        if text_after_second_call.count(BOOST_MARKER) != 1:
            failures.append("boost_not_idempotent")

        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "revise_chapter":
            failures.append(f"planner_changed_revision_action:{item.next_action}")
        planned_text = "\n".join([brief.required_beats or "", brief.constraints or ""])
        if planned_text.count(BOOST_MARKER) != 1:
            failures.append("planner_state_repairs_removed_boost")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("revision-success-boost-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
