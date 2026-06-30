from __future__ import annotations

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, PlatformFeedback, QualityReport, StoryFoundation
from app.services.continuity import default_chapter_continuity_summary, record_chapter_continuity
from app.services.feedback import record_platform_feedback
from app.services.planning import plan_chapters
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database


def main() -> int:
    isolated_database("sample-adoption-continuity-regression")
    failures: list[str] = []

    with session_scope() as session:
        book = Book(title="Sample Adoption Continuity", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        session.add(StoryFoundation(book_id=book.id, premise="游戏存档同步现实", reader_promise="玩家竞争和两界后果"))

        chapter1 = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        chapter2 = Chapter(book_id=book.id, chapter_number=2, title="第二章", status="briefing")
        session.add_all([chapter1, chapter2])
        session.flush()
        ending = "章末，林北攥着客栈木牌，听见隔壁玩家低声说出青木堂暗号，他意识到自己已经被同行盯上。"
        session.add(
            ChapterVersion(
                chapter_id=chapter1.id,
                version_number=1,
                title="第一章",
                content=("开头铺垫。" * 80) + ending,
                status="approved",
                source="regression",
            )
        )
        session.flush()
        summary = default_chapter_continuity_summary(session, book_id=book.id, chapter_number=1)
        record_chapter_continuity(session, book_id=book.id, chapter_number=1, summary=summary)
        if "章末后果/下一章承接" not in summary or "被同行盯上" not in summary:
            failures.append(f"continuity_summary_not_ending_based:{summary}")

        brief = ChapterBrief(
            chapter_id=chapter2.id,
            goal="第2章进入茶棚，承接第1章被玩家盯上的后果。",
            required_beats="林北遇到同行玩家试探；误判规则；付出代价；章末留下同步钩子。",
            constraints="3000-4500 中文字符。",
            status="ready",
        )
        session.add(brief)
        session.flush()
        feedback = record_platform_feedback(
            session,
            book_id=book.id,
            chapter_number=2,
            platform="chapter_sample_lab",
            metric_name="revision_suggestion",
            metric_value="targeted",
            raw_text="采用章节小样 #99-3 作为本章新版方向。小样名：茶棚遇同行。读者体验方向：呈现多个玩家在真实江湖里的不同发展路线，制造信息差张力。",
        )
        if not isinstance(feedback, PlatformFeedback):
            failures.append("sample_feedback_not_recorded")

        version2 = ChapterVersion(
            chapter_id=chapter2.id,
            version_number=1,
            title="第二章",
            content="旧稿没有采用小样。" * 400,
            status="needs_revision",
            source="regression",
        )
        revision_brief = ChapterBrief(
            chapter_id=chapter2.id,
            goal="自动恢复修订第2章",
            required_beats="revision_mode:targeted",
            constraints="system_revision_budget_recovery: detected",
            status="revision_ready",
        )
        session.add_all([version2, revision_brief])
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=version2.id,
                score=70,
                passed=False,
                report='{"score":70,"passed":false}',
            )
        )
        session.flush()
        plan_chapters(session, book_id=book.id, start=2, count=1, apply_state_repairs=True)
        if "茶棚遇同行" not in (revision_brief.constraints or ""):
            failures.append("active_revision_brief_did_not_inherit_sample")

        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=2,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            chapter_id=chapter2.id,
            chapter_brief_id=brief.id,
        )
        if "本章已采用小样方向" not in packet.context.author_preferences:
            failures.append(f"sample_context_missing:{packet.context.author_preferences}")
        if "茶棚遇同行" not in packet.context.author_preferences:
            failures.append("sample_title_missing_from_author_context")
        if "被同行盯上" not in packet.context.previous_chapter_context:
            failures.append("previous_chapter_ending_missing")
        if "茶棚遇同行" not in packet.director_sheet:
            failures.append("sample_missing_from_director_sheet")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("sample-adoption-continuity-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
