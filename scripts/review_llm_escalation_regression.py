"""Regression for ``app.services.production_reviewing._should_run_llm_review``.

Covers the editorial-recovery expansion: hard_gate PASS + score in [55, 75)
must escalate to LLM review, otherwise the quality gate is deadlocked at
rule-based scores (the failure mode observed in the 2026-07-02 chapter 1
baseline where three revise rounds all scored 45).

Also covers the plateau guard: after N consecutive rule-score-flat versions
that already went through LLM review, further LLM escalation is skipped to
stop bleeding tokens on a chapter that isn't improving.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.config import settings
from app.services.production_reviewing import _should_run_llm_review


@dataclass
class _Result:
    score: int = 0
    passed: bool = False


def _report(**over: Any) -> dict:
    base: dict[str, Any] = {
        "score": 60,
        "passed": False,
        "hard_gate": {"passed": True},
        "blockers": [],
        "recent_scores": [],
        "llm_review_history": [],
    }
    base.update(over)
    return base


def _case(label: str, *, rule_score: int, passed: bool, report: dict, expected: bool, expected_reason_contains: str) -> str | None:
    result = _Result(score=rule_score, passed=passed)
    should, reason = _should_run_llm_review(result, report)
    if should != expected:
        return f"[{label}] expected should={expected}, got {should}; reason={reason!r}"
    if expected_reason_contains and expected_reason_contains not in reason:
        return f"[{label}] expected reason contains {expected_reason_contains!r}, got {reason!r}"
    return None


def _set_profile(profile: str) -> None:
    # settings is a frozen dataclass; bypass with object.__setattr__.
    object.__setattr__(settings, "production_profile", profile)


def main() -> int:
    original_profile = settings.production_profile
    _set_profile("production")
    try:
        failures: list[str | None] = []

        # ---- production_profile=fast skips everything, even a clean pass.
        _set_profile("fast")
        failures.append(_case(
            "fast_profile_skips",
            rule_score=80, passed=True,
            report=_report(score=80, passed=True),
            expected=False, expected_reason_contains="production_profile_fast",
        ))
        _set_profile("production")

        # ---- existing behaviour preserved (production profile) ----
        failures.append(_case(
            "base_pass_still_reviews",
            rule_score=76, passed=True,
            report=_report(score=76, passed=True),
            expected=True, expected_reason_contains="base_quality_passed",
        ))

        failures.append(_case(
            "high_score_disagreement_78",
            rule_score=78, passed=False,
            report=_report(score=78, hard_gate={"passed": False}),
            expected=True, expected_reason_contains="high_score",
        ))

        failures.append(_case(
            "near_pass_needs_editorial_72",
            rule_score=72, passed=False,
            report=_report(score=72, hard_gate={"passed": True}),
            expected=True, expected_reason_contains="near_pass",
        ))

        # ---- NEW: editorial recovery window [55, 72) with hard_gate PASS ----
        failures.append(_case(
            "editorial_recovery_at_55",
            rule_score=55, passed=False,
            report=_report(score=55, hard_gate={"passed": True}),
            expected=True, expected_reason_contains="editorial_recovery",
        ))
        failures.append(_case(
            "editorial_recovery_at_65",
            rule_score=65, passed=False,
            report=_report(score=65, hard_gate={"passed": True}),
            expected=True, expected_reason_contains="editorial_recovery",
        ))

        # score below floor stays skipped
        failures.append(_case(
            "below_recovery_floor_45",
            rule_score=45, passed=False,
            report=_report(score=45, hard_gate={"passed": True}),
            expected=False, expected_reason_contains="rule_score_too_low",
        ))

        # hard_gate FAIL bans recovery
        failures.append(_case(
            "recovery_blocked_by_hard_gate",
            rule_score=65, passed=False,
            report=_report(score=65, hard_gate={"passed": False}),
            expected=False, expected_reason_contains="rule_score_too_low",
        ))

        # severe blockers ban even score >= 55
        failures.append(_case(
            "severe_blockers_block_recovery",
            rule_score=65, passed=False,
            report=_report(score=65, hard_gate={"passed": True}, blockers=["canon_violation"]),
            expected=False, expected_reason_contains="hard_rule_blockers",
        ))

        # ---- NEW: plateau guard — 3 flat rule scores + already reviewed once ----
        # A score in the editorial-recovery window would normally trigger LLM,
        # but after 3 flat rule scores AND at least one prior LLM review we
        # skip further escalations to stop bleeding tokens on a stuck chapter.
        failures.append(_case(
            "plateau_after_llm_review_history",
            rule_score=60, passed=False,
            report=_report(
                score=60,
                hard_gate={"passed": True},
                recent_scores=[60, 60, 60],
                llm_review_history=[{"score": 60}],
            ),
            expected=False,
            expected_reason_contains="plateau_llm_skip",
        ))

        # Plateau but *no* prior LLM review — must still try LLM at least once
        # to give the editorial layer a shot at breaking the deadlock.
        failures.append(_case(
            "plateau_first_llm_attempt_allowed",
            rule_score=60, passed=False,
            report=_report(
                score=60,
                hard_gate={"passed": True},
                recent_scores=[60, 60, 60],
                llm_review_history=[],
            ),
            expected=True,
            expected_reason_contains="editorial_recovery",
        ))

        failures = [f for f in failures if f]
        if failures:
            print("review_llm_escalation_regression=FAIL")
            for f in failures:
                print(f"- {f}")
            return 1

        print("review_llm_escalation_regression=PASS")
        print("cases_evaluated=11")
        return 0
    finally:
        _set_profile(original_profile)


if __name__ == "__main__":
    raise SystemExit(main())
