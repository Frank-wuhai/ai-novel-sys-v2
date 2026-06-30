from __future__ import annotations

from app.services.production_orchestrator import ProductionSituation, decide_production_route


def main() -> int:
    failures: list[str] = []

    base = dict(
        chapter_number=2,
        chapter_status="briefing",
        has_brief=True,
        latest_version_status="needs_revision",
        latest_quality_passed=False,
        has_revision_brief=True,
        has_sample_adoption=True,
        has_continuity_context=True,
        reading_assessment_requires_revision=True,
    )

    queued = decide_production_route(
        ProductionSituation(
            **base,
            revision_queue_id=10,
            revision_queue_status="running",
            budget_blocker="persistent_revision_budget:3>=2",
        )
    )
    if queued.action != "wait_generation_task":
        failures.append(f"queue_not_first:{queued}")

    budget = decide_production_route(ProductionSituation(**base, budget_blocker="persistent_revision_budget:3>=2", trend_blocker="score_degraded"))
    if budget.action != "revision_budget_recovery":
        failures.append(f"budget_not_before_trend:{budget}")

    trend = decide_production_route(ProductionSituation(**base, trend_blocker="score_degraded"))
    if trend.action != "revision_trend_recovery":
        failures.append(f"trend_not_before_reading:{trend}")

    reading = decide_production_route(ProductionSituation(**base))
    if reading.action != "revise_chapter":
        failures.append(f"reading_not_revision:{reading}")
    if "小样" not in reading.reason or "上一章" not in reading.reason:
        failures.append(f"protected_inputs_missing_from_reason:{reading.reason}")
    if "chapter_sample_adoption" not in reading.protected_inputs or "previous_chapter_continuity" not in reading.protected_inputs:
        failures.append(f"protected_inputs_missing:{reading.protected_inputs}")

    draft = decide_production_route(
        ProductionSituation(
            chapter_number=3,
            chapter_status="briefing",
            has_brief=True,
            latest_version_status="missing",
            latest_quality_passed=None,
        )
    )
    if draft.action != "draft_chapter":
        failures.append(f"draft_route_wrong:{draft}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-orchestrator-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
