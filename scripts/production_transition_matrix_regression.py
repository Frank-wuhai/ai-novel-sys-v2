from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, PlatformFeedback, QualityReport, StoryArc, StoryBible, StoryFoundation
from app.services.planning import plan_chapters, run_next_action
from app.services.rebuild_candidates import generate_rebuild_candidates
from app.services.revision_supervisor import apply_revision_budget_recovery
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-transition-matrix-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="transition matrix", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        _approve_minimal_skeleton(session, book_id=book.id)
        _budget_case(session, book_id=book.id, chapter_number=1)
        _candidate_case(session, book_id=book.id, chapter_number=2)
        session.flush()

        before_budget = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        preview = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=True, preview_only=False)
        if preview.status != "preview":
            failures.append(f"budget_preview_failed:{preview}")
        recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=1, force_rebuild_reason=before_budget.reason)
        after_budget = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        if recovery.status not in {"recovered", "restored_readable", "restored_readable_needs_revision"}:
            failures.append(f"budget_execute_failed:{recovery}")
        if after_budget.next_action == "revision_budget_recovery":
            failures.append(f"budget_loop_after_execute:{after_budget.next_action}:{after_budget.reason}")

        before_candidate = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if before_candidate.next_action != "generate_rebuild_candidates":
            failures.append(f"candidate_before_wrong:{before_candidate.next_action}:{before_candidate.reason}")
        preview = run_next_action(session, book_id=book.id, chapter_number=2, dry_run=True)
        if preview.action != "generate_rebuild_candidates" or preview.status != "preview":
            failures.append(f"candidate_preview_failed:{preview}")
        result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=2, dry_run=True)
        after_candidate = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if not result.selected_version_id:
            failures.append(f"candidate_execute_failed:{result}")
        if after_candidate.next_action == "generate_rebuild_candidates":
            failures.append(f"candidate_loop_after_execute:{after_candidate.next_action}:{after_candidate.reason}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-transition-matrix-regression: PASS")
    return 0


def _budget_case(session, *, book_id: int, chapter_number: int) -> None:
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    session.add(ChapterBrief(chapter_id=chapter.id, goal="基础章纲", required_beats="承接", constraints="", status="ready"))
    session.add(
        ChapterBrief(
            chapter_id=chapter.id,
            goal="冲突修订合同",
            required_beats="system_revision_budget_recovery: detected\nreading_assessment_auto_quality#1",
            constraints="revision_mode:rewrite\nrevision_mode:fresh",
            status="revision_ready",
        )
    )
    for number in range(1, 4):
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=number,
            title=f"第{chapter_number}章 v{number}",
            content="失败稿" * 1000,
            status="needs_revision",
            source="revision:ark_openai_compatible",
        )
        session.add(version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=version.id,
                score=45,
                passed=False,
                report=json.dumps({"score": 45, "dimensions": {"brief_coverage": 40}}, ensure_ascii=False),
            )
        )


def _approve_minimal_skeleton(session, *, book_id: int) -> None:
    values = {
        "premise": "主角在游戏江湖与现实后果之间同步成长。",
        "reader_promise": "每章都有玩家竞争、可见回报和现实代价。",
        "world_engine": "游戏桥段会以神经记忆形式反向影响现实判断。",
        "protagonist_engine": "主角用误判、试探和复盘逐步掌握同步规则。",
        "conflict_engine": "玩家竞争、江湖规则和现实牵连持续挤压主角选择。",
    }
    session.add(StoryFoundation(book_id=book_id, status="approved", **values))
    session.add(
        StoryBible(
            book_id=book_id,
            positioning=values["premise"],
            reader_promise=values["reader_promise"],
            power_curve=values["world_engine"],
            protagonist_arc=values["protagonist_engine"],
            main_plot=values["conflict_engine"],
            status="approved",
        )
    )
    arc_values = {
        "arc_goal": "主角确认同步规则并建立第一个玩家同盟。",
        "arc_climax": "同盟交易暴露真正的江湖代价。",
        "arc_turn": "主角发现现实也开始响应游戏选择。",
    }
    session.add(
        StoryArc(
            book_id=book_id,
            arc_number=1,
            start_chapter=1,
            end_chapter=5,
            goal=arc_values["arc_goal"],
            climax=arc_values["arc_climax"],
            turn=arc_values["arc_turn"],
            status="approved",
        )
    )
    for key, value in {**values, **arc_values}.items():
        session.add(
            PlatformFeedback(
                book_id=book_id,
                platform="system",
                metric_name="skeleton_approval",
                metric_value=key,
                raw_text=value,
            )
        )


def _candidate_case(session, *, book_id: int, chapter_number: int) -> None:
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    session.add(ChapterBrief(chapter_id=chapter.id, goal="基础章纲", required_beats="承接", constraints="", status="ready"))
    session.add(
        ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估重建",
            required_beats="reading_assessment_auto_quality#2\n当前阅读层级：需重建\n失败结构不得沿用。",
            constraints="revision_mode:rewrite",
            status="revision_ready",
        )
    )
    for number in range(1, 4):
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=number,
            title=f"第{chapter_number}章 v{number}",
            content="失败稿" * 1000,
            status="needs_revision",
            source="revision:ark_openai_compatible",
        )
        session.add(version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=version.id,
                score=45,
                passed=False,
                report=json.dumps(
                    {"score": 45, "reading_assessment": {"action": "auto_rebuild", "status": "needs_revision"}},
                    ensure_ascii=False,
                ),
            )
        )


if __name__ == "__main__":
    raise SystemExit(main())
