from __future__ import annotations

import json
from datetime import datetime

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
from app.services.planning import _maybe_apply_revision_loop_guard, plan_chapters, run_next_action
from app.services.author_runner import author_terminal_status
from app.services.feedback import submit_revision_suggestion
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
