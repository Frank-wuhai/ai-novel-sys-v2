from __future__ import annotations

from types import SimpleNamespace

from app.services.production_decision import decide_chapter_production


def main() -> int:
    failures: list[str] = []

    protected_revision = decide_chapter_production(
        SimpleNamespace(
            next_action="revise_chapter",
            latest_version_status="needs_revision",
            latest_quality_passed=True,
            reason="latest version needs revision and clean revision brief exists",
        )
    )
    if protected_revision.needs_author or protected_revision.status != "needs_revision":
        failures.append(f"passed_needs_revision_marked_for_author:{protected_revision.to_dict()}")
    if protected_revision.primary_intent != "continue":
        failures.append(f"passed_needs_revision_not_continue:{protected_revision.to_dict()}")

    sample_continuity_revision = decide_chapter_production(
        SimpleNamespace(
            next_action="revise_chapter",
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            reason="阅读评估已自动生成修订合同，继续修到可读候选稿；已采用小样方向必须继承；必须承接上一章后果",
        )
    )
    if "小样" not in sample_continuity_revision.headline or "上一章" not in sample_continuity_revision.headline:
        failures.append(f"sample_continuity_revision_headline_not_specific:{sample_continuity_revision.to_dict()}")

    reading_candidate = decide_chapter_production(
        SimpleNamespace(
            next_action="reading_assessment_review",
            latest_version_status="needs_revision",
            latest_quality_passed=True,
            reason="基础质检已通过，但阅读评估合同仍未确认，需要阅读判断。",
        )
    )
    if reading_candidate.needs_author or not reading_candidate.can_continue or reading_candidate.primary_intent != "continue":
        failures.append(f"reading_candidate_not_auto_revision:{reading_candidate.to_dict()}")

    trend_recovery = decide_chapter_production(
        SimpleNamespace(
            next_action="revision_trend_recovery",
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            reason="修订趋势劣化：最新未通过稿没有高于上一版。",
        )
    )
    if trend_recovery.needs_author or not trend_recovery.can_continue:
        failures.append(f"trend_recovery_not_auto:{trend_recovery.to_dict()}")

    budget_recovery = decide_chapter_production(
        SimpleNamespace(
            next_action="revision_budget_recovery",
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            reason="当前修订合同混入互相冲突的旧策略标记，先重写为单一结构修订合同再继续。",
        )
    )
    if budget_recovery.needs_author or not budget_recovery.can_continue or budget_recovery.primary_intent != "continue":
        failures.append(f"budget_recovery_not_auto:{budget_recovery.to_dict()}")
    if budget_recovery.primary_label == "刷新状态":
        failures.append(f"budget_recovery_shown_as_refresh:{budget_recovery.to_dict()}")

    blocked_defer = decide_chapter_production(
        SimpleNamespace(
            next_action="defer_chapter_for_later",
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            reason="旧策略残留：未通过但尝试暂存推进。",
        )
    )
    if blocked_defer.can_continue or blocked_defer.primary_intent == "next_chapter":
        failures.append(f"defer_should_not_continue_or_next:{blocked_defer.to_dict()}")

    deferred_backlog = decide_chapter_production(
        SimpleNamespace(
            next_action="deferred_revision_backlog",
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            reason="历史暂存稿仍未通过。",
        )
    )
    if deferred_backlog.primary_intent == "next_chapter" or "下一章" in deferred_backlog.primary_label:
        failures.append(f"deferred_backlog_should_not_show_next:{deferred_backlog.to_dict()}")

    draft_review = decide_chapter_production(
        SimpleNamespace(
            next_action="review_chapter",
            latest_version_status="draft",
            latest_quality_passed=None,
            reason="latest version is draft",
        )
    )
    if draft_review.primary_label != "审核当前草稿" or "可读稿" in draft_review.next_step:
        failures.append(f"draft_review_label_regressed:{draft_review.to_dict()}")

    continuity = decide_chapter_production(
        SimpleNamespace(
            next_action="record_chapter_continuity",
            latest_version_status="reviewed_pass",
            latest_quality_passed=True,
            reason="quality passed but continuity has not been recorded",
        )
    )
    if continuity.needs_author or not continuity.can_continue or continuity.status != "continuity_ready":
        failures.append(f"continuity_not_auto:{continuity.to_dict()}")

    approval = decide_chapter_production(
        SimpleNamespace(
            next_action="approve_chapter",
            latest_version_status="reviewed_pass",
            latest_quality_passed=True,
            reason="quality and continuity are complete",
        )
    )
    if not approval.needs_author or approval.status != "needs_confirmation":
        failures.append(f"approval_not_confirmation:{approval.to_dict()}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-decision-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
