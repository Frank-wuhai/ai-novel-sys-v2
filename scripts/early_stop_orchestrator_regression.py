"""Regression: early-stop signal correctly preempts the revise route.

Verifies the orchestrator wiring introduced in phase2/1b:

1. When ``early_stop_should_stop=True`` and best_score >= 75 the route
   returns ``action='accept_early_stop'`` (preempts every revise branch).
2. When ``early_stop_should_stop=True`` without a passing candidate,
   route falls to ``generate_rebuild_candidates`` with
   intent ``escalate_early_stop_ceiling``.
3. Queue-in-flight still wins (never preempt a running revision).
4. Strategy override (``generate_rebuild_candidates`` from the strategy
   pipeline) still wins because it encodes higher-order signals.
5. ``early_stop_should_stop=False`` leaves the pre-existing revise route
   intact.
"""

from __future__ import annotations

from app.services.production_orchestrator import ProductionSituation, decide_production_route


def _base_situation(**overrides) -> ProductionSituation:
    """Situation shaped like a mid-revision chapter."""
    defaults = dict(
        chapter_number=3,
        chapter_status="in_progress",
        has_brief=True,
        latest_version_status="needs_revision",
        latest_quality_passed=False,
        has_revision_brief=True,
        revision_matches_quality_or_feedback=True,  # would normally trigger revise
    )
    defaults.update(overrides)
    return ProductionSituation(**defaults)


def main() -> int:
    failures: list[str] = []

    # 1. accept_early_stop when threshold met.
    situation = _base_situation(
        early_stop_should_stop=True,
        early_stop_best_score=78,
        early_stop_best_version=27,
        early_stop_reason="accept_score_threshold met: v27 score=78",
        early_stop_triggered_rules=("accept_score_threshold",),
    )
    route = decide_production_route(situation)
    if route.action != "accept_early_stop":
        failures.append(f"case1: expected accept_early_stop, got {route.action!r}; reason={route.reason!r}")
    if "v27" not in route.reason or "78" not in route.reason:
        failures.append(f"case1: reason should quote best version/score, got {route.reason!r}")
    if not any("best=v27@78" in ev for ev in route.evidence):
        failures.append(f"case1: evidence must include best marker, got {route.evidence}")

    # 2. ceiling: max_versions hit without a pass -> rebuild candidates.
    situation = _base_situation(
        early_stop_should_stop=True,
        early_stop_best_score=72,  # below 75
        early_stop_best_version=30,
        early_stop_reason="max_versions reached (30/30); best_score=72",
        early_stop_triggered_rules=("max_versions",),
    )
    route = decide_production_route(situation)
    if route.action != "generate_rebuild_candidates":
        failures.append(f"case2: expected generate_rebuild_candidates, got {route.action!r}")
    if route.intent != "escalate_early_stop_ceiling":
        failures.append(f"case2: intent should be escalate_early_stop_ceiling, got {route.intent!r}")

    # 2b. no scored versions at all (best_score is None) -> also rebuild.
    situation = _base_situation(
        early_stop_should_stop=True,
        early_stop_best_score=None,
        early_stop_best_version=None,
        early_stop_reason="max_versions reached (30/30); best_score=n/a",
        early_stop_triggered_rules=("max_versions",),
    )
    route = decide_production_route(situation)
    if route.action != "generate_rebuild_candidates":
        failures.append(f"case2b: expected rebuild for None score, got {route.action!r}")

    # 3. Queue in flight wins.
    situation = _base_situation(
        revision_queue_status="running",
        revision_queue_id=42,
        early_stop_should_stop=True,
        early_stop_best_score=90,
        early_stop_best_version=27,
    )
    route = decide_production_route(situation)
    if route.action != "wait_generation_task":
        failures.append(f"case3: queue-in-flight must win over early-stop, got {route.action!r}")

    # 4. Sprint 2 Phase 2 P2-Ch28 (bfae946): early-stop with best_score>=75
    #    preempts strategy_action (rationale: score-passing draft should
    #    accept, not detour into budget recovery). Verify preempt.
    situation = _base_situation(
        strategy_action="revision_budget_recovery",
        strategy_intent="recover_revision_budget",
        strategy_reason="budget exceeded",
        early_stop_should_stop=True,
        early_stop_best_score=90,
        early_stop_best_version=27,
    )
    route = decide_production_route(situation)
    if route.action != "accept_early_stop":
        failures.append(f"case4: early-stop best>=75 must preempt strategy, got {route.action!r}")

    # 4b. When early-stop best_score<75, strategy_action wins (strategy is
    #     still authoritative for below-threshold scores).
    situation = _base_situation(
        strategy_action="revision_budget_recovery",
        strategy_intent="recover_revision_budget",
        strategy_reason="budget exceeded",
        early_stop_should_stop=True,
        early_stop_best_score=45,
        early_stop_best_version=27,
    )
    route = decide_production_route(situation)
    if route.action != "revision_budget_recovery":
        failures.append(f"case4b: strategy must win when early-stop best<75, got {route.action!r}")

    # 5. early_stop_should_stop=False leaves the classic revise route.
    situation = _base_situation(
        early_stop_should_stop=False,
        early_stop_best_score=70,
        early_stop_best_version=14,
    )
    route = decide_production_route(situation)
    if route.action != "revise_chapter":
        failures.append(f"case5: classic revise path broken, got {route.action!r}")

    # 6. Threshold exactly at 75 fires accept path.
    situation = _base_situation(
        early_stop_should_stop=True,
        early_stop_best_score=75,
        early_stop_best_version=10,
        early_stop_triggered_rules=("accept_score_threshold",),
    )
    route = decide_production_route(situation)
    if route.action != "accept_early_stop":
        failures.append(f"case6: score==75 boundary must accept, got {route.action!r}")

    # 7. Below threshold (74) with should_stop=True should escalate, not accept.
    situation = _base_situation(
        early_stop_should_stop=True,
        early_stop_best_score=74,
        early_stop_best_version=10,
        early_stop_triggered_rules=("no_improvement_window",),
    )
    route = decide_production_route(situation)
    if route.action != "generate_rebuild_candidates":
        failures.append(f"case7: score=74 must escalate not accept, got {route.action!r}")

    if failures:
        print("early_stop_orchestrator_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("early_stop_orchestrator_regression=PASS")
    print("cases_evaluated=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
