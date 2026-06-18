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

    reading_candidate = decide_chapter_production(
        SimpleNamespace(
            next_action="reading_assessment_review",
            latest_version_status="needs_revision",
            latest_quality_passed=True,
            reason="基础质检已通过，但阅读评估合同仍未确认，需要阅读判断。",
        )
    )
    if not reading_candidate.needs_author or reading_candidate.can_continue or reading_candidate.stage != "approve":
        failures.append(f"reading_candidate_not_author_review:{reading_candidate.to_dict()}")

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

    continuity = decide_chapter_production(
        SimpleNamespace(
            next_action="record_chapter_continuity",
            latest_version_status="reviewed_pass",
            latest_quality_passed=True,
            reason="quality passed but continuity has not been recorded",
        )
    )
    if continuity.needs_author or continuity.status != "quality_passed":
        failures.append(f"continuity_marked_as_author_approval:{continuity.to_dict()}")

    approval = decide_chapter_production(
        SimpleNamespace(
            next_action="approve_chapter",
            latest_version_status="reviewed_pass",
            latest_quality_passed=True,
            reason="quality and continuity are complete",
        )
    )
    if not approval.needs_author or approval.status != "needs_author":
        failures.append(f"approval_not_author_decision:{approval.to_dict()}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-decision-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
