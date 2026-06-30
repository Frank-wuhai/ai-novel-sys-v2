from __future__ import annotations

import json

from sqlalchemy import func, select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport, StoryArc
from app.services.feedback import record_platform_feedback
from app.services.planning import plan_chapters, run_next_action
from app.services.production import create_book, create_foundation
from app.services.rebuild_candidates import (
    TASK_TYPE_REBUILD_CANDIDATES,
    IncumbentDraft,
    _best_incumbent_draft,
    _should_restore_incumbent_over_candidate,
    generate_rebuild_candidates,
)
from regression_db import isolated_database


def main() -> int:
    isolated_database("rebuild-candidates-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = create_book(session, title="多候选回归书", genre="网游武侠", platform="manual")
        foundation = create_foundation(
            session,
            book_id=book.id,
            premise="主角进入江湖游戏，通过桥段演绎改变现实和游戏双线局面。",
            reader_promise="热闹江湖、主动破局、收益代价同场落地。",
            world_engine="游戏江湖逐步升维。",
            protagonist_engine="主角靠观察、试探、交易和行动破局。",
            conflict_engine="冲突来自桥段误判、现实同步和江湖规矩。",
        )
        arc = StoryArc(
            book_id=book.id,
            arc_number=1,
            title="初入江湖",
            start_chapter=1,
            end_chapter=12,
            goal="主角确认桥段演绎能力的真实规则，并拿到第一条升维线索。",
            climax="主角用有代价的桥段复刻破掉第一个江湖死局。",
            turn="现实副作用证明游戏和现实正在同步。",
            status="planning",
        )
        session.add(arc)
        session.flush()
        approvals = {
            "premise": foundation.premise,
            "reader_promise": foundation.reader_promise,
            "world_engine": foundation.world_engine,
            "protagonist_engine": foundation.protagonist_engine,
            "conflict_engine": foundation.conflict_engine,
            "arc_goal": arc.goal,
            "arc_climax": arc.climax,
            "arc_turn": arc.turn,
        }
        for key, value in approvals.items():
            record_platform_feedback(
                session,
                book_id=book.id,
                platform="regression",
                metric_name="skeleton_approval",
                metric_value=key,
                raw_text=value,
            )
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估要求重建第1章，旧稿结构不得沿用。",
            required_beats="reading_assessment_auto_quality#1\nrevision_mode:fresh\n主角主动破局；章末现实副作用。",
            constraints="clean_rebuild_contract@1\n失败结构不得沿用；需重建；3000字以上。",
            status="revision_ready",
        )
        session.add(brief)
        session.flush()
        for index, score in enumerate([20, 21, 22], start=1):
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=index,
                title=f"失败稿{index}",
                content="旧稿内容。" * 1200,
                status="needs_revision",
                source="revision:regression",
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
                            "status": "FAIL",
                            "score": score,
                            "reading_assessment": {"action": "auto_rebuild"},
                            "issues": ["单稿重建连续失败"],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            session.flush()

        plan = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        if plan.next_action != "generate_rebuild_candidates":
            failures.append(f"plan_not_candidate_rebuild:{plan.next_action}")
        preview = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=True)
        if preview.action != "generate_rebuild_candidates" or preview.status != "preview":
            failures.append(f"preview_not_candidate_rebuild:{preview.action}:{preview.status}:{preview.message}")
        result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=1, dry_run=True)
        selected = session.get(ChapterVersion, int(result.selected_version_id or 0))
        if not selected or not selected.source.startswith("rebuild_candidate_selected:v"):
            failures.append(f"selected_version_missing:{result.selected_version_id}")
        candidate_count = session.scalar(
            select(func.count())
            .select_from(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status == "candidate")
        )
        if candidate_count != 3:
            failures.append(f"candidate_count_wrong:{candidate_count}")
        latest = session.scalar(
            select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc())
        )
        if selected and (not latest or latest.id != selected.id):
            failures.append("latest_version_not_selected_copy")
        if selected:
            selected_quality = session.scalar(
                select(QualityReport).where(QualityReport.chapter_version_id == selected.id).order_by(QualityReport.id.desc())
            )
            if not selected_quality:
                failures.append("selected_quality_missing")
            elif "selected_from_candidate_version_id" not in json.loads(selected_quality.report or "{}"):
                failures.append("selected_quality_missing_candidate_trace")

        budget_chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(budget_chapter)
        session.flush()
        budget_brief = ChapterBrief(
            chapter_id=budget_chapter.id,
            goal="自动重建第2章修订目标",
            required_beats="system_revision_budget_recovery: detected\n修订模式:rewrite；预算恢复后重建章节承诺。",
            constraints="system_revision_budget_recovery: 系统自行换策略。\ncoverage_rebuild: brief_coverage",
            status="revision_ready",
        )
        session.add(budget_brief)
        session.flush()
        for index, source in enumerate(
            [
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
            ],
            start=1,
        ):
            version = ChapterVersion(
                chapter_id=budget_chapter.id,
                version_number=index,
                title="第2章",
                content=("失败稿" if source.startswith("revision:") else "恢复稿") * 1200,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            if source.startswith("revision:"):
                session.add(
                    QualityReport(
                        chapter_version_id=version.id,
                        score=45,
                        passed=False,
                        report=json.dumps({"score": 45, "passed": False, "dimensions": {"brief_coverage": 52}}, ensure_ascii=False),
                    )
                )
        session.flush()
        budget_plan = plan_chapters(session, book_id=book.id, start=2, count=1, apply_state_repairs=False)[0]
        if budget_plan.next_action != "generate_rebuild_candidates":
            failures.append(f"budget_recovery_pingpong_not_candidates:{budget_plan.next_action}:{budget_plan.reason}")

        protected_chapter = Chapter(book_id=book.id, chapter_number=3, title="第3章", status="briefing")
        session.add(protected_chapter)
        session.flush()
        protected_brief = ChapterBrief(
            chapter_id=protected_chapter.id,
            goal="根据用户意见重建候选，但不能丢掉首屏衔接。",
            required_beats=(
                "reading_assessment_auto_quality#3\n"
                "修订方向: 只做定向首屏衔接修订，不推翻第3章茶棚遇同行主线。\n"
                "必须在开头300-600字补齐上一章结尾到本章茶棚的过渡。\n"
                "保留第3章既有茶棚遇赵乾、青字纸、玩家试探、捕快压力主线。"
            ),
            constraints="不新增追杀、官方机构、昏迷、系统面板解题。",
            status="revision_ready",
        )
        session.add(protected_brief)
        session.flush()
        protected_version = ChapterVersion(
            chapter_id=protected_chapter.id,
            version_number=1,
            title="第3章",
            content="旧稿" * 1200,
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        session.add(protected_version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=protected_version.id,
                score=76,
                passed=False,
                report=json.dumps({"status": "NEEDS_REVISION", "score": 76, "passed": False}, ensure_ascii=False),
            )
        )
        session.flush()
        protected_result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=3, dry_run=True)
        protected_task = session.scalar(
            select(GenerationTask)
            .where(GenerationTask.id == protected_result.task_id, GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES)
        )
        task_input = json.loads(protected_task.input_json or "{}") if protected_task else {}
        protected_text = task_input.get("protected_rebuild_constraints") or ""
        if "只做定向首屏衔接修订" not in protected_text:
            failures.append("protected_rebuild_missing_user_direction")
        if "茶棚遇同行" not in protected_text:
            failures.append("protected_rebuild_missing_retained_mainline")
        if "不新增追杀、官方机构、昏迷、系统面板解题" not in protected_text:
            failures.append("protected_rebuild_missing_user_forbidden_rules")

        floor_chapter = Chapter(book_id=book.id, chapter_number=4, title="第4章", status="briefing")
        session.add(floor_chapter)
        session.flush()
        high_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=1,
            title="第4章",
            content="历史最佳稿。" * 1200,
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        session.add(high_version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=high_version.id,
                score=99,
                passed=True,
                report=json.dumps({"status": "PASS", "score": 99, "passed": True}, ensure_ascii=False),
            )
        )
        low_latest = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=2,
            title="第4章",
            content="当前低分稿。" * 1200,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(low_latest)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=low_latest.id,
                score=45,
                passed=False,
                report=json.dumps({"status": "FAIL", "score": 45, "passed": False}, ensure_ascii=False),
            )
        )
        floor_brief = ChapterBrief(
            chapter_id=floor_chapter.id,
            goal="重建第4章，但不得低于历史最佳稿。",
            required_beats="reading_assessment_auto_quality#4\n需重建失败结构。",
            constraints="3000-4500中文字符。",
            status="revision_ready",
        )
        session.add(floor_brief)
        session.flush()
        floor_result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=4, dry_run=True)
        floor_selected = session.get(ChapterVersion, floor_result.selected_version_id)
        floor_quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == floor_result.selected_version_id)
            .order_by(QualityReport.id.desc())
        )
        if not floor_selected or not str(floor_selected.source or "").startswith("rebuild_candidate_incumbent_restore:"):
            failures.append(f"rebuild_floor_did_not_restore_incumbent:{floor_selected.source if floor_selected else None}")
        if not floor_quality or int(floor_quality.score or 0) != 99:
            failures.append(f"rebuild_floor_score_regressed:{floor_quality.score if floor_quality else None}")

        blocker_incumbent_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=99,
            title="第4章",
            content="高分但阻断稿。" * 1200,
            status="needs_revision",
            source="regression:blocking_incumbent",
        )
        clean_candidate_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=100,
            title="第4章",
            content="低分但已关闭阻断候选。" * 1200,
            status="candidate",
            source="regression:clean_candidate",
        )
        session.add_all([blocker_incumbent_version, clean_candidate_version])
        session.flush()
        blocker_quality = QualityReport(
            chapter_version_id=blocker_incumbent_version.id,
            score=81,
            passed=False,
            report=json.dumps(
                {
                    "score": 81,
                    "passed": False,
                    "issues": ["chapter_type_gate_failed:conflict_pressure=58<68"],
                },
                ensure_ascii=False,
            ),
        )
        clean_quality = QualityReport(
            chapter_version_id=clean_candidate_version.id,
            score=71,
            passed=True,
            report=json.dumps({"score": 71, "passed": True, "issues": []}, ensure_ascii=False),
        )
        session.add_all([blocker_quality, clean_quality])
        session.flush()
        should_restore = _should_restore_incumbent_over_candidate(
            incumbent=IncumbentDraft(blocker_incumbent_version, blocker_quality, 81, False),
            candidate={"version_id": clean_candidate_version.id, "score": 71, "passed": True},
            candidate_quality=clean_quality,
        )
        if should_restore:
            failures.append("rebuild_selection_preferred_blocking_incumbent_over_clean_candidate")

        source_only_chapter = Chapter(book_id=book.id, chapter_number=5, title="第5章", status="briefing")
        session.add(source_only_chapter)
        session.flush()
        source_only = ChapterVersion(
            chapter_id=source_only_chapter.id,
            version_number=1,
            title="第5章",
            content="当前失败源稿。" * 1200,
            status="needs_revision",
            source="revision:regression_failed_source",
        )
        session.add(source_only)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=source_only.id,
                score=91,
                passed=False,
                report=json.dumps({"status": "FAIL", "score": 91, "passed": False}, ensure_ascii=False),
            )
        )
        session.flush()
        incumbent = _best_incumbent_draft(session, chapter_id=source_only_chapter.id, exclude_version_id=source_only.id)
        if incumbent is not None:
            failures.append(f"rebuild_incumbent_included_current_failed_source:{incumbent.version.id}")

        broken_chapter = Chapter(book_id=book.id, chapter_number=6, title="第6章", status="briefing")
        session.add(broken_chapter)
        session.flush()
        broken_brief = ChapterBrief(
            chapter_id=broken_chapter.id,
            goal="重建第6章。",
            required_beats="reading_assessment_auto_quality#6\n需重建失败结构。",
            constraints="3000-4500中文字符。",
            status="revision_ready",
        )
        broken_source = ChapterVersion(
            chapter_id=broken_chapter.id,
            version_number=1,
            title="第6章",
            content="坏源稿。" * 1200,
            status="needs_revision",
            source="revision:regression",
        )
        historical_incumbent = ChapterVersion(
            chapter_id=broken_chapter.id,
            version_number=0,
            title="第6章",
            content="历史可用稿。" * 1200,
            status="reviewed_pass",
            source="regression:historical_incumbent",
        )
        session.add_all([broken_brief, historical_incumbent])
        session.flush()
        session.add(broken_source)
        session.flush()
        session.add_all(
            [
                QualityReport(
                    chapter_version_id=broken_source.id,
                    score=10,
                    passed=False,
                    report=json.dumps({"status": "FAIL", "score": 10, "passed": False}, ensure_ascii=False),
                ),
                QualityReport(
                    chapter_version_id=historical_incumbent.id,
                    score=99,
                    passed=True,
                    report="{not valid json",
                ),
            ]
        )
        session.flush()
        try:
            generate_rebuild_candidates(session, book_id=book.id, chapter_number=6, dry_run=True)
            failures.append("rebuild_invalid_quality_report_did_not_fail")
        except json.JSONDecodeError:
            failed_task = session.scalar(
                select(GenerationTask)
                .where(GenerationTask.book_id == book.id, GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES)
                .order_by(GenerationTask.id.desc())
            )
            if not failed_task or failed_task.status != "failed":
                failures.append(f"rebuild_post_generation_exception_left_task_running:{failed_task.status if failed_task else None}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("rebuild-candidates-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
