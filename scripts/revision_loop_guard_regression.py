from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    FeedbackAdjustment,
    GenerationTask,
    PlatformFeedback,
    QualityReport,
)
from app.services.chapter_revision import _fallback_quality_for_recovery_revision
from app.services.llm_queue import _guard_revision_enqueue_policy
from app.services.planning import _active_revision_budget_recovery, _maybe_apply_revision_loop_guard, plan_chapters, run_next_action
from app.services.author_runner import author_terminal_status
from app.services.feedback import submit_revision_suggestion
from app.services.revision_supervisor import apply_revision_budget_recovery
from regression_db import isolated_database


def main() -> int:
    isolated_database("revision-loop-guard-regression")
    failures: list[str] = []
    terminal = author_terminal_status(
        [
            {
                "action": "revise_chapter",
                "status": "blocked",
                "message": "自动修订预算已用完，系统已暂停以避免继续消耗；请查看当前最佳稿或重新开始本章。",
            }
        ]
    )
    if terminal.get("status") != "auto_paused":
        failures.append("revision_budget_block_still_requests_author_direction")
    if "给一句明确方向" in terminal.get("message", ""):
        failures.append("revision_budget_message_too_vague")
    created: dict[str, list[object]] = {
        "feedback_adjustments": [],
        "platform_feedback": [],
        "quality_reports": [],
        "generation_tasks": [],
        "chapter_versions": [],
        "chapter_briefs": [],
        "chapters": [],
        "books": [],
    }
    with session_scope() as session:
        stamp = datetime.utcnow().timestamp()
        book = Book(title=f"revision-loop-guard-regression-{stamp}", genre="test", target_platform="test")
        session.add(book)
        session.flush()
        created["books"].append(book)
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        created["chapters"].append(chapter)
        heavy_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="按最新生产骨架重启本章",
            required_beats="修订模式:fresh",
            constraints="采用章节小样，必须继承的叙事发动机合同。" + ("整章重写。" * 500),
            status="revision_ready",
        )
        session.add(heavy_brief)
        session.flush()
        created["chapter_briefs"].append(heavy_brief)
        versions = []
        for index in range(1, 4):
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=index,
                title=f"v{index}",
                content="正文" * 1800,
                status="needs_revision",
                source="revision:regression",
            )
            session.add(version)
            session.flush()
            versions.append(version)
            created["chapter_versions"].append(version)
            report = QualityReport(
                chapter_version_id=version.id,
                score=62,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": 62,
                        "issues": ["brief_coverage_underfulfilled: 48", "dialogue_underdeveloped: 47"],
                        "warnings": ["weak_narrative_dimension: scene_atmosphere=43"],
                        "chapter_unit_report": {
                            "repair_contract": ["第1单元需局部重修：补清目标、阻碍、动作后果和下一单元承接点。"]
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(report)
            session.flush()
            created["quality_reports"].append(report)
        for index in range(2):
            task = GenerationTask(
                book_id=book.id,
                task_type="revise_chapter",
                status="completed",
                input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
                output_json=json.dumps(
                    {"elapsed_ms": 180000 + index, "actual_total_tokens": 17000, "prompt_chars": 19000},
                    ensure_ascii=False,
                ),
            )
            session.add(task)
            session.flush()
            created["generation_tasks"].append(task)

        guard = _maybe_apply_revision_loop_guard(session, book_id=book.id, chapter_number=1)
        if not guard:
            failures.append("guard_not_applied")
        else:
            created["chapter_briefs"].append(guard)
            text = "\n".join([guard.goal or "", guard.required_beats or "", guard.constraints or ""])
            if "修订模式:local_patch" not in text:
                failures.append("guard_not_local_patch")
            if "system_revision_loop_guard" not in text:
                failures.append("guard_marker_missing")
            if "不要继续 fresh/整章重写" not in text:
                failures.append("guard_message_missing")
        created["feedback_adjustments"].extend(
            list(session.query(FeedbackAdjustment).filter(FeedbackAdjustment.book_id == book.id))
        )
        created["platform_feedback"].extend(list(session.query(PlatformFeedback).filter(PlatformFeedback.book_id == book.id)))

        broad_chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="draft")
        session.add(broad_chapter)
        session.flush()
        created["chapters"].append(broad_chapter)
        clean_brief = ChapterBrief(
            chapter_id=broad_chapter.id,
            goal="定点重构第2章：保留主线但重塑行动链和读感。",
            required_beats="主角必须主动选择，章末钩子必须来自行动后果。",
            constraints="禁止旧设定残留。",
            status="revision_ready",
        )
        local_brief = ChapterBrief(
            chapter_id=broad_chapter.id,
            goal="局部修订第2章：system_revision_loop_guard: detected",
            required_beats="修订模式:local_patch",
            constraints="system_revision_loop_guard",
            status="revision_ready",
        )
        session.add_all([clean_brief, local_brief])
        session.flush()
        created["chapter_briefs"].extend([clean_brief, local_brief])
        broad_version = ChapterVersion(
            chapter_id=broad_chapter.id,
            version_number=1,
            title="v1",
            content="正文" * 1800,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(broad_version)
        session.flush()
        created["chapter_versions"].append(broad_version)
        broad_quality = QualityReport(
            chapter_version_id=broad_version.id,
            score=70,
            passed=False,
            report=json.dumps(
                {
                    "status": "FAIL",
                    "score": 70,
                    "issues": ["brief_coverage_underfulfilled: 48"],
                    "dimensions": {
                        "chapter_necessity": 49,
                        "scene_atmosphere": 40,
                        "payoff_grounding": 52,
                    },
                    "llm_review": {
                        "issues": ["文笔生硬，缺乏感情基调，章末钩子执行不力。"],
                        "revision_suggestions": ["强化桥段复刻奖励感和主角心理活动。"],
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(broad_quality)
        session.flush()
        created["quality_reports"].append(broad_quality)
        for index in range(2):
            task = GenerationTask(
                book_id=book.id,
                task_type="revise_chapter",
                status="completed",
                input_json=json.dumps({"chapter_number": 2}, ensure_ascii=False),
                output_json=json.dumps({"elapsed_ms": 180000 + index, "actual_total_tokens": 17000}, ensure_ascii=False),
            )
            session.add(task)
            session.flush()
            created["generation_tasks"].append(task)
        broad_guard = _maybe_apply_revision_loop_guard(session, book_id=book.id, chapter_number=2)
        if broad_guard:
            failures.append("broad_revision_wrongly_downgraded_to_local_patch")
            created["chapter_briefs"].append(broad_guard)
        _feedback, _adjustment, targeted_brief, _version = submit_revision_suggestion(
            session,
            book_id=book.id,
            chapter_number=2,
            platform="manual",
            suggestion_text="文笔描述过于生硬，整体读感没有感情基调，重塑关键场景的情绪、动作和章末钩子。",
            revision_mode="targeted",
        )
        created["chapter_briefs"].append(targeted_brief)
        created["feedback_adjustments"].extend(
            list(session.query(FeedbackAdjustment).filter(FeedbackAdjustment.book_id == book.id))
        )
        created["platform_feedback"].extend(list(session.query(PlatformFeedback).filter(PlatformFeedback.book_id == book.id)))
        targeted_text = "\n".join([targeted_brief.goal or "", targeted_brief.required_beats or "", targeted_brief.constraints or ""])
        if "system_revision_loop_guard" in targeted_text or targeted_brief.goal.startswith("局部修订"):
            failures.append("targeted_feedback_inherited_local_patch_brief")

        trend_chapter = Chapter(book_id=book.id, chapter_number=3, title="第3章", status="draft")
        session.add(trend_chapter)
        session.flush()
        created["chapters"].append(trend_chapter)
        trend_brief = ChapterBrief(
            chapter_id=trend_chapter.id,
            goal="第3章：正常修订",
            required_beats="主角行动链；章末钩子。",
            constraints="禁止越修越偏。",
            status="revision_ready",
        )
        session.add(trend_brief)
        session.flush()
        created["chapter_briefs"].append(trend_brief)
        for index, score in enumerate([56, 56], start=1):
            trend_version = ChapterVersion(
                chapter_id=trend_chapter.id,
                version_number=index,
                title=f"trend-v{index}",
                content="正文" * 1800,
                status="needs_revision",
                source="revision:trend",
            )
            session.add(trend_version)
            session.flush()
            created["chapter_versions"].append(trend_version)
            trend_quality = QualityReport(
                chapter_version_id=trend_version.id,
                score=score,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": score,
                        "dimensions": {
                            "brief_coverage": score,
                            "reader_momentum": score,
                            "hook_strength": score,
                            "chapter_unit_flow": score,
                            "scene_atmosphere": score,
                            "writer_craft": score,
                        },
                        "issues": ["latest revision did not improve"],
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(trend_quality)
            session.flush()
            created["quality_reports"].append(trend_quality)
        trend_plan = plan_chapters(session, book_id=book.id, start=3, count=1)[0]
        if trend_plan.next_action != "revision_trend_recovery":
            failures.append("degrading_revision_trend_not_blocked")
        if "修订趋势劣化" not in trend_plan.reason:
            failures.append("degrading_revision_reason_missing")
        recovery_result = run_next_action(
            session,
            book_id=book.id,
            chapter_number=3,
            dry_run=True,
            queue_generation=False,
        )
        if recovery_result.action != "revision_trend_recovery" or recovery_result.status != "executed":
            failures.append("degrading_revision_not_auto_recovered")
        recovered_plan = plan_chapters(session, book_id=book.id, start=3, count=1)[0]
        if recovered_plan.next_action != "revise_chapter":
            failures.append("recovered_revision_not_ready_to_continue")
        recovered_brief = session.get(ChapterBrief, recovery_result.object_id) if recovery_result.object_id else None
        recovered_text = "\n".join([recovered_brief.goal or "", recovered_brief.required_beats or "", recovered_brief.constraints or ""]) if recovered_brief else ""
        if "不要向作者索要方向" not in recovered_text:
            failures.append("recovery_brief_still_pushes_to_author")
        if "不得继续沿最新劣化稿修" not in recovered_text:
            failures.append("recovery_brief_missing_bad_draft_boundary")
        stale_bad_version = ChapterVersion(
            chapter_id=trend_chapter.id,
            version_number=4,
            title="trend-stale-bad",
            content="又坏了" * 1800,
            status="needs_revision",
            source="revision:trend",
        )
        session.add(stale_bad_version)
        session.flush()
        created["chapter_versions"].append(stale_bad_version)
        stale_quality = QualityReport(
            chapter_version_id=stale_bad_version.id,
            score=56,
            passed=False,
            report=json.dumps({"status": "FAIL", "score": 56, "dimensions": {"brief_coverage": 56}}, ensure_ascii=False),
        )
        session.add(stale_quality)
        session.flush()
        created["quality_reports"].append(stale_quality)
        stale_recovery = run_next_action(
            session,
            book_id=book.id,
            chapter_number=3,
            dry_run=True,
            queue_generation=False,
        )
        if stale_recovery.action != "revision_trend_recovery" or stale_recovery.status != "executed":
            failures.append("stale_recovery_marker_blocked_new_recovery")

        budget_chapter = Chapter(book_id=book.id, chapter_number=4, title="第4章", status="draft")
        session.add(budget_chapter)
        session.flush()
        created["chapters"].append(budget_chapter)
        active_budget_brief = ChapterBrief(
            chapter_id=budget_chapter.id,
            goal="第4章：预算耗尽前的旧修订",
            required_beats="继续修，但不要让作者给方向。",
            constraints="禁止冷硬装酷式精炼。",
            status="revision_ready",
        )
        session.add(active_budget_brief)
        session.flush()
        created["chapter_briefs"].append(active_budget_brief)
        budget_versions = []
        for index, score in enumerate([52, 64, 58], start=1):
            budget_version = ChapterVersion(
                chapter_id=budget_chapter.id,
                version_number=index,
                title=f"budget-v{index}",
                content=("第4章正文" + str(index)) * 1200,
                status="needs_revision",
                source="revision:budget",
            )
            session.add(budget_version)
            session.flush()
            budget_versions.append(budget_version)
            created["chapter_versions"].append(budget_version)
            budget_quality = QualityReport(
                chapter_version_id=budget_version.id,
                score=score,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": score,
                        "issues": ["dialogue_underdeveloped: 42", "scene_expansion_underdeveloped: 50"],
                        "dimensions": {
                            "brief_coverage": score,
                            "reader_momentum": score,
                            "dialogue_fullness": 42,
                            "scene_expansion": 50,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(budget_quality)
            session.flush()
            created["quality_reports"].append(budget_quality)
        for task_index in range(2):
            budget_task = GenerationTask(
                book_id=book.id,
                task_type="revise_chapter",
                status="completed",
                input_json=json.dumps({"chapter_number": 4, "revision_mode": "rewrite"}, ensure_ascii=False),
                output_json=json.dumps(
                    {
                        "version_id": budget_versions[min(task_index, len(budget_versions) - 1)].id,
                        "provider": "regression",
                        "elapsed_ms": 1000,
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(budget_task)
            session.flush()
            created["generation_tasks"].append(budget_task)
        try:
            _guard_revision_enqueue_policy(session, book_id=book.id, chapter_number=4)
            failures.append("direct_revision_enqueue_bypassed_budget")
        except ValueError as exc:
            if "persistent_revision_budget" not in str(exc):
                failures.append(f"direct_revision_enqueue_wrong_blocker:{exc}")
        budget_recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=4)
        if budget_recovery.status != "recovered":
            failures.append("budget_recovery_not_applied")
        if budget_recovery.source_version_id != budget_versions[1].id:
            failures.append("budget_recovery_did_not_choose_best_failed_draft")
        budget_brief = session.get(ChapterBrief, budget_recovery.recovery_brief_id) if budget_recovery.recovery_brief_id else None
        budget_text = "\n".join([budget_brief.goal or "", budget_brief.required_beats or "", budget_brief.constraints or ""]) if budget_brief else ""
        if "system_revision_budget_recovery" not in budget_text:
            failures.append("budget_recovery_marker_missing")
        if "禁止要求作者给方向" not in budget_text:
            failures.append("budget_recovery_still_requests_author_direction")
        if "保留最佳稿的主事件" not in budget_text:
            failures.append("budget_recovery_missing_preserve_boundary")
        budget_recovery_version = (
            session.get(ChapterVersion, budget_recovery.recovery_version_id) if budget_recovery.recovery_version_id else None
        )
        if not _active_revision_budget_recovery(budget_recovery_version, budget_brief):
            failures.append("budget_recovery_not_routed_through_active_recovery")
        repeated_budget_recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=4)
        if (
            repeated_budget_recovery.recovery_version_id != budget_recovery.recovery_version_id
            or repeated_budget_recovery.recovery_brief_id != budget_recovery.recovery_brief_id
        ):
            failures.append("budget_recovery_not_idempotent")
        budget_plan = plan_chapters(session, book_id=book.id, start=4, count=1)[0]
        if budget_plan.next_action != "revise_chapter":
            failures.append(f"author_runner_budget_recovery_not_pending:{budget_plan.next_action}:{budget_plan.reason}")
        fallback_quality = (
            _fallback_quality_for_recovery_revision(
                session,
                source_version=budget_recovery_version,
                revision_brief=budget_brief,
            )
            if budget_recovery_version and budget_brief
            else None
        )
        if not fallback_quality or fallback_quality.chapter_version_id != budget_versions[1].id:
            failures.append("budget_recovery_missing_source_quality_fallback")
        try:
            _guard_revision_enqueue_policy(session, book_id=book.id, chapter_number=4)
        except ValueError as exc:
            failures.append(f"active_budget_recovery_enqueue_blocked:{exc}")

        readable_restore_chapter = Chapter(book_id=book.id, chapter_number=40, title="第40章", status="drafting")
        session.add(readable_restore_chapter)
        session.flush()
        created["chapters"].append(readable_restore_chapter)
        readable_restore_brief = ChapterBrief(
            chapter_id=readable_restore_chapter.id,
            goal="第40章：已有通过稿后预算恢复",
            required_beats="继续修订但不应覆盖历史可读稿。",
            constraints="system_revision_budget_recovery: active",
            status="revision_ready",
        )
        session.add(readable_restore_brief)
        session.flush()
        created["chapter_briefs"].append(readable_restore_brief)
        readable_version = ChapterVersion(
            chapter_id=readable_restore_chapter.id,
            version_number=1,
            title="readable",
            content="历史通过稿正文" * 1200,
            status="needs_revision",
            source="regression:previous_pass",
        )
        session.add(readable_version)
        session.flush()
        created["chapter_versions"].append(readable_version)
        readable_quality = QualityReport(
            chapter_version_id=readable_version.id,
            score=78,
            passed=True,
            report=json.dumps(
                {
                    "status": "PASS",
                    "score": 78,
                    "passed": True,
                    "issues": [],
                    "dimensions": {"brief_coverage": 52, "hook_strength": 75},
                },
                ensure_ascii=False,
            ),
        )
        session.add(readable_quality)
        session.flush()
        created["quality_reports"].append(readable_quality)
        bad_after_pass = ChapterVersion(
            chapter_id=readable_restore_chapter.id,
            version_number=2,
            title="bad-after-pass",
            content="后续失败稿正文" * 1200,
            status="needs_revision",
            source="revision:after_pass",
        )
        session.add(bad_after_pass)
        session.flush()
        created["chapter_versions"].append(bad_after_pass)
        bad_after_pass_quality = QualityReport(
            chapter_version_id=bad_after_pass.id,
            score=50,
            passed=False,
            report=json.dumps(
                {
                    "status": "FAIL",
                    "score": 50,
                    "passed": False,
                    "issues": ["brief_coverage_underfulfilled: 40"],
                    "dimensions": {"brief_coverage": 40, "hook_strength": 55},
                },
                ensure_ascii=False,
            ),
        )
        session.add(bad_after_pass_quality)
        session.flush()
        created["quality_reports"].append(bad_after_pass_quality)
        readable_recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=40)
        restored_readable = session.get(ChapterVersion, readable_recovery.recovery_version_id) if readable_recovery.recovery_version_id else None
        if readable_recovery.status != "restored_readable":
            failures.append(f"budget_recovery_did_not_restore_passed:{readable_recovery.status}")
        if not restored_readable or restored_readable.status != "reviewed_pass":
            failures.append("budget_recovery_restored_version_not_readable")
        active_readable_briefs = list(
            session.scalars(
                select(ChapterBrief).where(
                    ChapterBrief.chapter_id == readable_restore_chapter.id,
                    ChapterBrief.status == "revision_ready",
                )
            )
        )
        if active_readable_briefs:
            failures.append("budget_recovery_left_revision_brief_after_passed_restore")

        protected_restore_chapter = Chapter(book_id=book.id, chapter_number=41, title="第41章", status="drafting")
        session.add(protected_restore_chapter)
        session.flush()
        created["chapters"].append(protected_restore_chapter)
        protected_brief = ChapterBrief(
            chapter_id=protected_restore_chapter.id,
            goal="阅读评估后定点修订第41章",
            required_beats="修订模式:targeted",
            constraints="reading_assessment_contract: 当前稿不是正式批准稿，只作为可用底稿。",
            status="superseded",
        )
        session.add(protected_brief)
        session.flush()
        created["chapter_briefs"].append(protected_brief)
        protected_pass = ChapterVersion(
            chapter_id=protected_restore_chapter.id,
            version_number=1,
            title="protected-pass",
            content="阅读评估未解决的历史通过稿" * 1200,
            status="needs_revision",
            source="regression:protected_pass",
        )
        session.add(protected_pass)
        session.flush()
        created["chapter_versions"].append(protected_pass)
        protected_pass_quality = QualityReport(
            chapter_version_id=protected_pass.id,
            score=75,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 75, "passed": True, "issues": []}, ensure_ascii=False),
        )
        session.add(protected_pass_quality)
        session.flush()
        created["quality_reports"].append(protected_pass_quality)
        protected_failed = ChapterVersion(
            chapter_id=protected_restore_chapter.id,
            version_number=2,
            title="protected-failed",
            content="阅读评估修坏稿" * 1200,
            status="needs_revision",
            source="revision:protected",
        )
        session.add(protected_failed)
        session.flush()
        created["chapter_versions"].append(protected_failed)
        protected_failed_quality = QualityReport(
            chapter_version_id=protected_failed.id,
            score=55,
            passed=False,
            report=json.dumps({"status": "FAIL", "score": 55, "passed": False, "issues": ["brief_coverage_underfulfilled: 40"]}, ensure_ascii=False),
        )
        session.add(protected_failed_quality)
        session.flush()
        created["quality_reports"].append(protected_failed_quality)
        protected_recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=41)
        protected_restored = session.get(ChapterVersion, protected_recovery.recovery_version_id) if protected_recovery.recovery_version_id else None
        session.refresh(protected_brief)
        if protected_recovery.status != "restored_readable_needs_revision":
            failures.append(f"protected_recovery_marked_approvable:{protected_recovery.status}")
        if not protected_restored or protected_restored.status != "needs_revision":
            failures.append("protected_recovery_version_not_held_for_revision")
        if protected_brief.status != "revision_ready":
            failures.append("protected_recovery_did_not_reactivate_reading_brief")

        stalled_chapter = Chapter(book_id=book.id, chapter_number=5, title="第5章", status="drafting")
        session.add(stalled_chapter)
        session.flush()
        created["chapters"].append(stalled_chapter)
        stalled_brief = ChapterBrief(
            chapter_id=stalled_chapter.id,
            goal="第5章：旧覆盖停滞修订",
            required_beats="继续修覆盖，但不要让作者给方向。",
            constraints="必须遵守最新作品DNA：【作品DNA】 - 题材主味: 玄幻脑洞 【作品DNA结束】",
            status="revision_ready",
        )
        session.add(stalled_brief)
        session.flush()
        created["chapter_briefs"].append(stalled_brief)
        for index, score in enumerate([56, 55, 57], start=1):
            stalled_version = ChapterVersion(
                chapter_id=stalled_chapter.id,
                version_number=index,
                title=f"stalled-v{index}",
                content=("第5章正文" + str(index)) * 1200,
                status="needs_revision",
                source="revision:budget",
            )
            session.add(stalled_version)
            session.flush()
            created["chapter_versions"].append(stalled_version)
            stalled_quality = QualityReport(
                chapter_version_id=stalled_version.id,
                score=score,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": score,
                        "issues": ["brief_coverage_underfulfilled: 47"],
                        "dimensions": {
                            "brief_coverage": 47,
                            "canon_consistency": 55,
                            "arc_alignment": 50,
                            "chapter_necessity": 53,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(stalled_quality)
            session.flush()
            created["quality_reports"].append(stalled_quality)
        stalled_recovery = apply_revision_budget_recovery(session, book_id=book.id, chapter_number=5)
        stalled_recovery_brief = session.get(ChapterBrief, stalled_recovery.recovery_brief_id) if stalled_recovery.recovery_brief_id else None
        stalled_text = "\n".join([stalled_recovery_brief.goal or "", stalled_recovery_brief.required_beats or "", stalled_recovery_brief.constraints or ""]) if stalled_recovery_brief else ""
        if "自动重建" not in stalled_text or "修订模式:rewrite" not in stalled_text:
            failures.append("stalled_budget_did_not_rebuild_brief")
        if "玄幻脑洞" in stalled_text:
            failures.append("stalled_budget_kept_stale_genre_dna")

        trend_chapter = Chapter(book_id=book.id, chapter_number=6, title="第6章", status="drafting")
        session.add(trend_chapter)
        session.flush()
        created["chapters"].append(trend_chapter)
        trend_brief = ChapterBrief(
            chapter_id=trend_chapter.id,
            goal="换策略修订第6章：以近期最佳稿为底稿。",
            required_beats="system_revision_trend_recovery: detected\n修订模式:targeted。",
            constraints="system_revision_trend_recovery: 自动趋势恢复，不向作者索要方向。",
            status="revision_ready",
        )
        session.add(trend_brief)
        session.flush()
        created["chapter_briefs"].append(trend_brief)
        best_trend = ChapterVersion(
            chapter_id=trend_chapter.id,
            version_number=1,
            title="trend-best",
            content="最佳稿正文" * 1200,
            status="needs_revision",
            source="revision:budget",
        )
        recovered_trend = ChapterVersion(
            chapter_id=trend_chapter.id,
            version_number=2,
            title="trend-recovered",
            content="恢复稿正文" * 1200,
            status="needs_revision",
            source="revision_recovery:v1",
        )
        bad_trend = ChapterVersion(
            chapter_id=trend_chapter.id,
            version_number=3,
            title="trend-bad",
            content="劣化稿正文" * 1200,
            status="needs_revision",
            source="revision:budget",
        )
        session.add_all([best_trend, recovered_trend, bad_trend])
        session.flush()
        created["chapter_versions"].extend([best_trend, recovered_trend, bad_trend])
        for version, score, brief_score, hook_score in [(best_trend, 77, 57, 89), (bad_trend, 75, 49, 75)]:
            quality = QualityReport(
                chapter_version_id=version.id,
                score=score,
                passed=False,
                report=json.dumps(
                    {
                        "status": "FAIL",
                        "score": score,
                        "issues": ["brief_coverage_underfulfilled: 49"],
                        "dimensions": {
                            "brief_coverage": brief_score,
                            "hook_strength": hook_score,
                            "canon_consistency": 55,
                            "arc_alignment": 50,
                            "chapter_necessity": 53,
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            session.add(quality)
            session.flush()
            created["quality_reports"].append(quality)
        trend_plan = plan_chapters(session, book_id=book.id, start=6, count=1)[0]
        if trend_plan.next_action != "revision_trend_recovery":
            failures.append(f"trend_recovery_not_planned:{trend_plan.next_action}")
        trend_result = run_next_action(session, book_id=book.id, chapter_number=6, dry_run=False)
        latest_trend_brief = session.scalar(
            select(ChapterBrief).where(ChapterBrief.chapter_id == trend_chapter.id).order_by(ChapterBrief.id.desc())
        )
        trend_text = "\n".join([latest_trend_brief.goal or "", latest_trend_brief.required_beats or "", latest_trend_brief.constraints or ""]) if latest_trend_brief else ""
        if trend_result.action != "revision_trend_recovery" or "修订模式:rewrite" not in trend_text or "trend_recovery_failed" not in trend_text:
            failures.append("trend_recovery_failure_did_not_escalate_to_rebuild")
        trend_after = plan_chapters(session, book_id=book.id, start=6, count=1)[0]
        if trend_after.next_action != "revise_chapter":
            failures.append(f"trend_rebuild_not_routed_to_revise:{trend_after.next_action}")

        for key in (
            "feedback_adjustments",
            "platform_feedback",
            "quality_reports",
            "generation_tasks",
            "chapter_versions",
            "chapter_briefs",
            "chapters",
            "books",
        ):
            for item in reversed(created[key]):
                if item in session:
                    session.delete(item)

    print(json.dumps({"status": "fail" if failures else "pass", "failures": failures}, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
