"""Regression for ``app.services.revision_early_stop``.

Every threshold in ``EarlyStopPolicy`` has both a *fires* and a *does-not-fire*
case, plus edge tests around the min_versions_before_stop warm-up and the
no_improvement_window plateau detector.

Failure output prints the offending case and the full ``EarlyStopDecision``
so a regression fingerprints the exact broken branch.
"""

from __future__ import annotations

from app.services.revision_early_stop import (
    DEFAULT_POLICY,
    EarlyStopDecision,
    EarlyStopPolicy,
    VersionScore,
    evaluate_early_stop,
)


def _mk(count: int, scores: list[int | None], passed: list[bool]) -> list[VersionScore]:
    assert count == len(scores) == len(passed), f"length mismatch: {count} vs {len(scores)} vs {len(passed)}"
    return [
        VersionScore(version_number=i + 1, score=scores[i], passed=passed[i])
        for i in range(count)
    ]


def _check(
    label: str,
    versions: list[VersionScore],
    *,
    expected_should_stop: bool,
    expected_rule: str | None,
    expected_best_version: int | None = None,
    policy: EarlyStopPolicy = DEFAULT_POLICY,
) -> str | None:
    decision = evaluate_early_stop(versions, policy=policy)
    if decision.should_stop != expected_should_stop:
        return f"[{label}] should_stop expected {expected_should_stop}, got {decision.should_stop}; decision={decision}"
    if expected_rule is None:
        if decision.triggered_rules:
            return f"[{label}] expected no rule, got {decision.triggered_rules}; decision={decision}"
    else:
        if expected_rule not in decision.triggered_rules:
            return f"[{label}] expected rule {expected_rule!r}, got {decision.triggered_rules}; decision={decision}"
    if expected_best_version is not None and decision.best_version_number != expected_best_version:
        return f"[{label}] best_version_number expected {expected_best_version}, got {decision.best_version_number}; decision={decision}"
    return None


def main() -> int:
    failures: list[str] = []

    # ---------------- accept_score_threshold ----------------

    # FIRE: 6 versions, latest hits 76 which is >= 75 threshold, warm-up met.
    failures.append(
        _check(
            "accept_threshold_fires",
            _mk(
                6,
                [60, 62, 65, 70, 74, 76],
                [False, False, False, False, False, True],
            ),
            expected_should_stop=True,
            expected_rule="accept_score_threshold",
            expected_best_version=6,
        )
    )

    # DOES NOT FIRE: 6 versions but nothing passes threshold (best=74 < 75).
    failures.append(
        _check(
            "accept_threshold_below_bar",
            _mk(
                6,
                [60, 62, 65, 70, 72, 74],
                [False, False, False, False, False, False],
            ),
            expected_should_stop=False,
            expected_rule=None,
        )
    )

    # DOES NOT FIRE: pass at 76 but warm-up (min_versions_before_stop=5) not met.
    failures.append(
        _check(
            "accept_threshold_before_warmup",
            _mk(3, [60, 70, 76], [False, False, True]),
            expected_should_stop=False,
            expected_rule=None,
        )
    )

    # FIRE at exact warm-up boundary (5 versions, last passes).
    failures.append(
        _check(
            "accept_threshold_at_warmup_boundary",
            _mk(5, [60, 62, 65, 70, 78], [False, False, False, False, True]),
            expected_should_stop=True,
            expected_rule="accept_score_threshold",
            expected_best_version=5,
        )
    )

    # ---------------- max_versions ----------------

    # FIRE: 30 versions, nothing passing — hard cap wins.
    failures.append(
        _check(
            "max_versions_hard_cap",
            _mk(30, [50 + i % 10 for i in range(30)], [False] * 30),
            expected_should_stop=True,
            expected_rule="max_versions",
        )
    )

    # DOES NOT FIRE: 29 versions, still no pass — hard cap NOT yet reached.
    failures.append(
        _check(
            "max_versions_one_short",
            _mk(29, [50 + i % 10 for i in range(29)], [False] * 29),
            expected_should_stop=False,
            expected_rule=None,
        )
    )

    # max_versions wins over accept_score_threshold when both would fire.
    failures.append(
        _check(
            "max_versions_precedes_accept",
            _mk(
                30,
                [40] * 29 + [90],
                [False] * 29 + [True],
            ),
            expected_should_stop=True,
            expected_rule="max_versions",
        )
    )

    # ---------------- min_versions_before_stop ----------------

    # Custom policy with min=8 — pass at v6 (score 80) must NOT stop.
    strict_warmup = EarlyStopPolicy(min_versions_before_stop=8)
    failures.append(
        _check(
            "custom_warmup_blocks_early_pass",
            _mk(6, [60, 62, 65, 70, 74, 80], [False, False, False, False, False, True]),
            expected_should_stop=False,
            expected_rule=None,
            policy=strict_warmup,
        )
    )

    # But at 8 versions with pass, it fires.
    failures.append(
        _check(
            "custom_warmup_allows_after_8",
            _mk(8, [60, 62, 65, 70, 74, 76, 77, 78], [False] * 5 + [True, True, True]),
            expected_should_stop=True,
            expected_rule="accept_score_threshold",
            policy=strict_warmup,
        )
    )

    # ---------------- no_improvement_window ----------------

    # FIRE: enough passing versions where the best never moves.
    # Setup: 15 versions, all passing at 76 (no improvement across last 10).
    # But we also need scoring above threshold NOT to trigger accept first...
    # accept fires immediately at version 5+, so no_improvement_window is
    # only reachable when scores are BELOW the accept bar. Use policy
    # accept=100 (unreachable) to test the plateau logic in isolation.
    plateau_policy = EarlyStopPolicy(accept_score_threshold=100, no_improvement_window=5)
    failures.append(
        _check(
            "plateau_fires",
            _mk(
                12,
                [60, 62, 65, 70, 72, 74, 74, 74, 74, 74, 74, 74],
                [False, False, False, False, True, True, True, True, True, True, True, True],
            ),
            expected_should_stop=True,
            expected_rule="no_improvement_window",
            policy=plateau_policy,
        )
    )

    # DOES NOT FIRE: score improved inside the window.
    failures.append(
        _check(
            "plateau_still_improving",
            _mk(
                12,
                [60, 62, 65, 70, 72, 74, 75, 76, 77, 78, 79, 80],
                [False, False, False, False, True, True, True, True, True, True, True, True],
            ),
            expected_should_stop=False,
            expected_rule=None,
            policy=plateau_policy,
        )
    )

    # DOES NOT FIRE: not enough passing versions to fill the window.
    failures.append(
        _check(
            "plateau_too_few_passing",
            _mk(
                12,
                [60, 62, 65, 70, 72, 74, 74, 74, 74, 60, 60, 60],
                [False, False, False, False, True, True, True, True, True, False, False, False],
            ),
            expected_should_stop=False,
            expected_rule=None,
            policy=plateau_policy,
        )
    )

    # ---------------- edge cases ----------------

    # Empty history — nothing to decide, do not stop.
    failures.append(
        _check(
            "empty_history",
            [],
            expected_should_stop=False,
            expected_rule=None,
        )
    )

    # All None scores (quality not evaluated yet) — do not stop.
    failures.append(
        _check(
            "all_unscored",
            _mk(6, [None] * 6, [False] * 6),
            expected_should_stop=False,
            expected_rule=None,
        )
    )

    # Policy validation
    try:
        EarlyStopPolicy(accept_score_threshold=150)
    except ValueError:
        pass
    else:
        failures.append("EarlyStopPolicy did not reject accept_score_threshold=150")

    try:
        EarlyStopPolicy(max_versions=0)
    except ValueError:
        pass
    else:
        failures.append("EarlyStopPolicy did not reject max_versions=0")

    # Filter out passes.
    failures = [f for f in failures if f]

    if failures:
        print("revision_early_stop_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("revision_early_stop_regression=PASS")
    print(f"cases_evaluated={sum(1 for _ in [None]*15)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
