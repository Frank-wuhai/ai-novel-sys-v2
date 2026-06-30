from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import plan_chapters
from app.services.production_strategy import assess_production_strategy
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-strategy-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="strategy", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第2章生产说明",
            required_beats="承接第1章后果，采用茶棚遇同行小样气质。",
            constraints="3000-4500中文字符；不得丢失上一章承接。",
            status="ready",
        )
        revision_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="依据质检报告继续修订第2章",
            required_beats="质检报告 #3\nrevision_mode:rewrite\n必须继承已采用小样方向。",
            constraints="不要只替换形容词；必须落到场景、动作、对白、后果。",
            status="revision_ready",
        )
        session.add_all([brief, revision_brief])
        session.flush()
        latest = None
        for number, score in enumerate([74, 75, 75], start=1):
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=number,
                title=f"第2章 v{number}",
                content="正文" * 1200,
                status="needs_revision",
                source="revision:ark_openai_compatible",
            )
            session.add(version)
            session.flush()
            latest = version
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=False,
                    report=json.dumps({"score": score, "dimensions": {"reader_momentum": score}}, ensure_ascii=False),
                )
            )
        session.flush()
        latest_quality = session.query(QualityReport).filter_by(chapter_version_id=latest.id).order_by(QualityReport.id.desc()).first()

        strategy = assess_production_strategy(
            session,
            chapter_id=chapter.id,
            latest_version=latest,
            latest_quality=latest_quality,
            revision_brief=revision_brief,
            has_sample_adoption=True,
            has_continuity_context=True,
        )
        if strategy.action != "generate_rebuild_candidates" or strategy.category != "score_plateau":
            failures.append(f"strategy_not_plateau_escape:{strategy}")

        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "generate_rebuild_candidates":
            failures.append(f"planner_did_not_apply_strategy:{item.next_action}:{item.reason}")
        if "多候选" not in item.reason:
            failures.append(f"strategy_reason_not_visible:{item.reason}")

    with session_scope() as session:
        book = Book(title="active budget recovery", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        recovery_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="自动重建第2章修订目标",
            required_beats="system_revision_budget_recovery: detected\n修订模式:rewrite",
            constraints="revision_mode:rewrite\nrevision_mode:targeted\nsystem_revision_budget_recovery: 系统自行换策略。",
            status="revision_ready",
        )
        session.add(recovery_brief)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第2章",
            content="恢复底稿" * 1000,
            status="needs_revision",
            source="revision_budget_recovery:v1",
        )
        session.add(version)
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "revise_chapter":
            failures.append(f"active_budget_recovery_looped:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="active readable restore with stale contract", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        stale_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="旧局部修订合同",
            required_beats="revision_mode:local_patch\n当前待修底稿：v1\n合同当前底稿：v2",
            constraints="reading_assessment_contract: 系统自动阅读评估生成\nrevision_mode:targeted",
            status="revision_ready",
        )
        session.add(stale_brief)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="第2章",
            content="历史通过恢复稿" * 1000,
            status="needs_revision",
            source="revision_budget_readable_restore:v1",
        )
        session.add(version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=version.id,
                score=70,
                passed=True,
                report=json.dumps({"score": 70, "status": "PASS"}, ensure_ascii=False),
            )
        )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action == "revision_budget_recovery":
            failures.append(f"readable_restore_repeated_recovery:{item.next_action}:{item.reason}")
        latest = session.get(ChapterVersion, version.id)
        latest_quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
        if latest.status != "needs_revision":
            failures.append(f"weak_readable_restore_should_stay_revision:{latest.status}")
        if latest_quality.passed:
            failures.append("weak_readable_restore_formally_passed")
        if item.next_action not in {"revise_chapter", "generate_rebuild_candidates"}:
            failures.append(f"weak_readable_restore_wrong_next_action:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="active rebuild candidate", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        revision_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估重建第2章：以当前作品剧情承诺为准。",
            required_beats="reading_assessment_auto_quality#9\n当前阅读层级：需重建\n失败结构不得沿用。",
            constraints="revision_mode:rewrite",
            status="revision_ready",
        )
        session.add(revision_brief)
        session.flush()
        latest = None
        for number, score, source in [
            (1, 45, "revision:ark_openai_compatible"),
            (2, 45, "revision:ark_openai_compatible"),
            (3, 45, "revision:ark_openai_compatible"),
            (4, 69, "rebuild_candidate_selected:v3"),
        ]:
            latest = ChapterVersion(
                chapter_id=chapter.id,
                version_number=number,
                title=f"第2章 v{number}",
                content="候选稿" * 1000,
                status="needs_revision",
                source=source,
            )
            session.add(latest)
            session.flush()
            session.add(
                QualityReport(
                    chapter_version_id=latest.id,
                    score=score,
                    passed=False,
                    report=json.dumps(
                        {
                            "score": score,
                            "reading_assessment": {"action": "auto_rebuild", "status": "needs_revision"},
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "revise_chapter":
            failures.append(f"active_rebuild_candidate_looped:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="regressed rebuild candidate", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        revision_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估重建第2章：以当前作品剧情承诺为准。",
            required_beats="reading_assessment_auto_quality#10\n当前阅读层级：需重建\n失败结构不得沿用。",
            constraints="revision_mode:rewrite",
            status="revision_ready",
        )
        session.add(revision_brief)
        session.flush()
        for number, score, source in [
            (1, 76, "revision_compare_restore:v1"),
            (2, 63, "rebuild_candidate_selected:v9"),
        ]:
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=number,
                title=f"第2章 v{number}",
                content="候选稿" * 1000,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=False,
                    report=json.dumps({"score": score, "reading_assessment": {"action": "auto_rebuild"}}, ensure_ascii=False),
                )
            )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "generate_rebuild_candidates":
            failures.append(f"regressed_rebuild_candidate_not_recovered:{item.next_action}:{item.reason}")
        if "历史最佳稿" not in item.reason:
            failures.append(f"regressed_rebuild_reason_not_visible:{item.reason}")

    with session_scope() as session:
        book = Book(title="readable revision deadlock", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        revision_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估继续修订第2章。",
            required_beats="reading_assessment_auto_quality#20\n质量门禁未关闭。",
            constraints="revision_mode:targeted",
            status="revision_ready",
        )
        session.add(revision_brief)
        session.flush()
        rows = [
            (1, 70, "revision:ark_openai_compatible"),
            (2, 71, "revision:ark_openai_compatible"),
            (3, 72, "rebuild_candidate_selected:v99"),
            (4, 71, "revision:ark_openai_compatible"),
            (5, 72, "revision:ark_openai_compatible"),
            (6, 73, "rebuild_candidate_selected:v100"),
        ]
        for number, score, source in rows:
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=number,
                title=f"第2章 v{number}",
                content="可读但未达发布门禁的正文" * 1200,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=False,
                    report=json.dumps(
                        {
                            "score": score,
                            "passed": False,
                            "issues": ["chapter_type_gate_failed:conflict_pressure=50<68"],
                            "reading_assessment": {"action": "auto_revise"},
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "generate_rebuild_candidates":
            failures.append(f"readable_deadlock_not_forced_rebuild:{item.next_action}:{item.reason}")
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if chapter.status == "continuity_deferred":
            failures.append(f"readable_deadlock_should_not_defer_chapter:{item.next_action}:{chapter.status}")

    with session_scope() as session:
        book = Book(title="narrow repairable gate", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        session.add(ChapterBrief(chapter_id=chapter.id, goal="第2章说明", required_beats="承接", constraints="", status="ready"))
        revision_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估自动修订第2章：以当前稿为底稿，把能读修到想追。",
            required_beats="reading_assessment_auto_quality#99\n当前阅读层级：质量门禁未关闭\n源版本锁定：v999",
            constraints="revision_mode:targeted",
            status="revision_ready",
        )
        session.add(revision_brief)
        session.flush()
        rows = [
            (1, 81, "rebuild_candidate_incumbent_restore:v1", ["chapter_type_gate_failed:conflict_pressure=58<68,choice_and_cost=66<68"]),
            (2, 70, "revision:ark_openai_compatible", ["chapter_type_gate_failed:conflict_pressure=58<68,choice_and_cost=50<68"]),
            (3, 78, "revision:llm_local_patch", ["chapter_type_gate_failed:brief_coverage=51<62"]),
        ]
        for number, score, source, issues in rows:
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=number,
                title=f"第2章 v{number}",
                content="阻断逐步收敛的正文" * 1200,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=False,
                    report=json.dumps(
                        {
                            "score": score,
                            "passed": False,
                            "issues": issues,
                            "reading_assessment": {"action": "auto_revise", "blockers": issues},
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "revise_chapter":
            failures.append(f"narrow_repairable_gate_should_continue_revision:{item.next_action}:{item.reason}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-strategy-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
