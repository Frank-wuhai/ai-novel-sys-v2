from __future__ import annotations

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief
from app.services.legacy_trace_cleanup import cleanup_active_production_traces
from regression_db import isolated_database


def main() -> int:
    isolated_database("legacy-trace-cleanup-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="legacy cleanup", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="自动重建第2章修订目标",
            required_beats="\n".join(
                [
                    "system_revision_budget_recovery: detected",
                    "revision_mode:local_patch",
                    "reading_assessment_auto_quality#9",
                    "当前最新待修稿：v8；不得继续沿无效方向堆修。",
                ]
            ),
            constraints="\n".join(
                [
                    "reading_assessment_contract: 系统自动阅读评估生成；下一版必须解决上述读感问题。",
                    "revision_mode:local_patch",
                    "revision_mode:targeted",
                    "system_revision_budget_recovery: 系统自行换策略。",
                    "本章已采用小样方向（高于普通作者偏好；后续 draft/revision/recovery 不得丢失）：",
                    "- 小样名：茶棚遇同行",
                ]
            ),
            status="revision_ready",
        )
        session.add(brief)
        session.flush()
        result = cleanup_active_production_traces(session, book_id=book.id)
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if result.changed_count != 1 or brief.id not in result.changed_brief_ids:
            failures.append(f"cleanup_not_applied:{result}")
        if "local_patch" in text or "revision_mode:targeted" in text or "reading_assessment_contract" in text or "reading_assessment_auto_quality#" in text:
            failures.append(f"legacy_markers_remain:{text}")
        if "system_revision_budget_recovery" not in text or "修订模式:rewrite" not in text:
            failures.append(f"budget_rewrite_not_preserved:{text}")
        if "茶棚遇同行" not in text:
            failures.append("sample_adoption_lost")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("legacy-trace-cleanup-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
