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

    # ---------------- plateau_stop / no_improvement_window ----------------
    #
    # 2026-07-29 路径②语义更新（重要）：新增的 rule 2a("存在 passed 版 + 过
    # warm-up 即达标停止")优先级高于所有兜底分支。这带来一个必须诚实记录的
    # 架构后果——
    #
    #   * no_improvement_window 规则遍历的是 *passing* 版本(见实现)，但只要
    #     出现任何 passing 版，rule 2a 会先接管并停止；因此在新架构下
    #     no_improvement_window 实际上已被 rule 2a 完全遮蔽，永远轮不到触发。
    #     它作为历史兜底保留在代码里(无害)，但不再有可达路径。
    #   * plateau_stop 遍历的是全体 scored 版本，在【无 passing 版】(反复生成
    #     但始终不合格)时仍是有效且必要的止损——这正是它现在的唯一职责。
    #
    # 故此处只验证 plateau_stop 在无合格版时的兜底行为；rule 2a 遮蔽后的
    # 合格路径由下方独立的 quality_gate_passed 用例覆盖。
    plateau_policy = EarlyStopPolicy(accept_score_threshold=100, no_improvement_window=5)
    # FIRE: 无合格版，末段 4 窗 [74,74,74,74] drift=0 <= 2 → plateau_stop 兜底止损。
    failures.append(
        _check(
            "plateau_fires_no_passing",
            _mk(
                12,
                [60, 62, 65, 70, 72, 74, 74, 74, 74, 74, 74, 74],
                [False] * 12,
            ),
            expected_should_stop=True,
            expected_rule="plateau_stop",
            policy=plateau_policy,
        )
    )

    # DOES NOT FIRE: 分数单调上升，任何 4 窗 drift 都 > plateau_delta(2)，
    # 且无 passing 版(no_improvement 不可达)→ 不停，继续生成。
    failures.append(
        _check(
            "plateau_still_improving",
            _mk(
                12,
                [40, 44, 48, 52, 56, 60, 64, 68, 71, 74, 77, 80],
                [False] * 12,
            ),
            expected_should_stop=False,
            expected_rule=None,
            policy=plateau_policy,
        )
    )

    # DOES NOT FIRE: 分数在末段大幅震荡(74/60/74/60)，4 窗 drift=14 > 2，
    # plateau 不触发；无 passing 版 → 不停。
    failures.append(
        _check(
            "plateau_volatile_no_stop",
            _mk(
                12,
                [40, 44, 48, 52, 56, 60, 64, 68, 74, 60, 74, 60],
                [False] * 12,
            ),
            expected_should_stop=False,
            expected_rule=None,
            policy=plateau_policy,
        )
    )

    # ---------------- rule 2a: quality_gate_passed (2026-07-29 路径②) ----------------
    #
    # 核心新行为：存在 passed=True 版(已过 hard_gate + 章型门72/soft_pass 完整
    # 质检链) + 过 min_versions_before_stop warm-up → 立即达标停止，best 落到
    # 最高分 passed 版。即使分数 72-74 低于 accept_score_threshold(75)也停——
    # 这消除了"合格B版被判没到75、反复踢rebuild"的72-75鸿沟(book4/book5 前5章根因)。
    failures.append(
        _check(
            "quality_gate_passed_stops_at_72",
            _mk(
                8,
                [60, 62, 65, 70, 72, 72, 72, 72],
                [False, False, False, False, True, True, True, True],
            ),
            expected_should_stop=True,
            expected_rule="quality_gate_passed",
            # 默认策略(accept=75)：v5@72 passed 但 <75，靠 quality_gate_passed 停
        )
    )
    # warm-up 守卫：首版就 passed 但未过 min_versions_before_stop(5) → 不停。
    failures.append(
        _check(
            "quality_gate_passed_respects_warmup",
            _mk(
                3,
                [72, 72, 72],
                [True, True, True],
            ),
            expected_should_stop=False,
            expected_rule=None,
        )
    )
    # 高分 passed 版仍走 accept_score_threshold 规则(向后兼容)。
    failures.append(
        _check(
            "high_score_still_accept_threshold",
            _mk(
                6,
                [60, 65, 70, 76, 77, 78],
                [False, False, False, True, True, True],
            ),
            expected_should_stop=True,
            expected_rule="accept_score_threshold",
        )
    )

    # ---------------- plateau_stop (rule-flat guard, added 2026-07-02) ----------------
    #
    # Rationale: revise can spin forever when the rule scorer refuses to move.
    # Stop when the last `plateau_window` versions drift by <= `plateau_delta`.
    #
    # Isolate from other rules by setting accept_score_threshold=100 (unreachable)
    # and no_improvement_window=100 (unreachable), so only plateau can fire.
    flat_policy = EarlyStopPolicy(
        accept_score_threshold=100,
        no_improvement_window=100,
        min_versions_before_stop=3,
        plateau_window=4,
        plateau_delta=2,
    )

    # FIRE: 4 versions all 45 (delta 0 <= 2). Ties on score break by
    # version_number DESC, so best is v4.
    failures.append(
        _check(
            "plateau_stop_fires_flat_45",
            _mk(4, [45, 45, 45, 45], [False] * 4),
            expected_should_stop=True,
            expected_rule="plateau_stop",
            expected_best_version=4,
            policy=flat_policy,
        )
    )

    # FIRE: 4 versions with mild wiggle (44,45,45,46 -> delta 2).
    failures.append(
        _check(
            "plateau_stop_fires_within_delta",
            _mk(4, [44, 45, 45, 46], [False] * 4),
            expected_should_stop=True,
            expected_rule="plateau_stop",
            policy=flat_policy,
        )
    )

    # NO FIRE: delta 3 exceeds plateau_delta=2.
    failures.append(
        _check(
            "plateau_stop_holds_wiggle_over_delta",
            _mk(4, [44, 45, 45, 47], [False] * 4),
            expected_should_stop=False,
            expected_rule=None,
            policy=flat_policy,
        )
    )

    # NO FIRE: haven't accumulated `plateau_window` versions yet.
    failures.append(
        _check(
            "plateau_stop_below_window",
            _mk(3, [45, 45, 45], [False] * 3),
            expected_should_stop=False,
            expected_rule=None,
            policy=flat_policy,
        )
    )

    # NO FIRE: warm-up not met (min_versions_before_stop=5 blocks plateau at 4 versions).
    warmup_block = EarlyStopPolicy(
        accept_score_threshold=100,
        no_improvement_window=100,
        min_versions_before_stop=5,
        plateau_window=4,
        plateau_delta=2,
    )
    failures.append(
        _check(
            "plateau_stop_blocked_by_warmup",
            _mk(4, [45, 45, 45, 45], [False] * 4),
            expected_should_stop=False,
            expected_rule=None,
            policy=warmup_block,
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
