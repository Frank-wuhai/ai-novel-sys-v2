"""Regression: chapter_type_gate must veto editorial soft-override.

Ch45 exposed: production_reviewing runs enrich_quality_report_with_optimization
(sets chapter_type_gate.passed=False + soft_pass=False when gap > 15pt) then
runs apply_review_decision (editorial gate). The editorial gate looked only
at its own dimension whitelist (dialogue, staging, voice) — which does not
overlap with chapter_type_gate's structural dimensions (conflict_pressure,
choice_and_cost, earned_payoff, brief_coverage). So the editorial gate
would flip report_data["passed"] back to True via soft_rule_override, even
though the structural gate blocked with gap=20pt.

Fix: chapter_type_gate.passed=False + soft_pass=False must veto both
soft_rule_override AND final_passed inside apply_review_decision.
soft_pass=True still allows editorial to accept (that's the whole point
of the P2-Ch44 soft-pass semantics).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.review_decision import apply_review_decision, ReviewRuleResult


def _base_report(*, dimensions: dict | None = None, hard_gate_passed: bool = True, gate_passed: bool = True, soft_pass: bool = False) -> dict:
    return {
        "hard_gate": {"passed": hard_gate_passed, "issues": []},
        "issues": [],
        "dimensions": dimensions or {
            "brief_coverage": 60, "readability": 70, "dialogue_fullness": 60,
            "chapter_necessity": 60, "design_texture": 70,
        },
        "chapter_type_gate": {"passed": gate_passed, "soft_pass": soft_pass, "failures": []},
        "llm_review": {"status": "completed", "score": 82, "verdict": "pass"},
    }


def test_editor_pass_gate_pass_final_passed():
    r = _base_report()
    apply_review_decision(ReviewRuleResult(passed=True, score=78), r)
    assert r["passed"] is True


def test_editor_pass_gate_fail_gap_large_no_soft_pass_final_blocked():
    """P2-Ch45 the fix: gate.passed=False + soft_pass=False → editorial must NOT flip pass."""
    r = _base_report(gate_passed=False, soft_pass=False)
    r["chapter_type_gate"]["failures"] = ["conflict_pressure=50<68", "earned_payoff=45<65"]
    apply_review_decision(ReviewRuleResult(passed=False, score=75), r)
    assert r["passed"] is False, "chapter_type_gate must veto editorial soft-override"
    assert r["editorial_gate"]["soft_rule_override"] is False


def test_editor_pass_gate_fail_with_soft_pass_final_passed():
    """soft_pass=True (gap <=15) → editorial gate can accept."""
    r = _base_report(gate_passed=False, soft_pass=True)
    r["chapter_type_gate"]["soft_pass_reason"] = "max_gap=15<=15"
    apply_review_decision(ReviewRuleResult(passed=True, score=76), r)
    assert r["passed"] is True, "soft_pass must allow editorial acceptance"


def test_editor_fail_no_final_pass_regardless_of_gate():
    """editor_passed=False → final always False regardless of gate."""
    r = _base_report(gate_passed=True)
    r["llm_review"] = {"status": "completed", "score": 60, "verdict": "revise"}
    apply_review_decision(ReviewRuleResult(passed=True, score=78), r)
    assert r["passed"] is False


def test_editor_pass_rule_pass_gate_pass_final_pass():
    """happy path: everything green → pass."""
    r = _base_report()
    apply_review_decision(ReviewRuleResult(passed=True, score=80), r)
    assert r["passed"] is True


def test_llm_review_not_completed_uses_rule_result():
    """when llm_review skipped, fall back to rule result (chapter_type_gate not checked here)."""
    r = _base_report()
    r["llm_review"] = {"status": "skipped"}
    apply_review_decision(ReviewRuleResult(passed=True, score=78), r)
    assert r["passed"] is True


if __name__ == "__main__":
    tests = [
        test_editor_pass_gate_pass_final_passed,
        test_editor_pass_gate_fail_gap_large_no_soft_pass_final_blocked,
        test_editor_pass_gate_fail_with_soft_pass_final_passed,
        test_editor_fail_no_final_pass_regardless_of_gate,
        test_editor_pass_rule_pass_gate_pass_final_pass,
        test_llm_review_not_completed_uses_rule_result,
    ]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            fail += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:
            fail += 1
            print(f"ERR  {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'chapter-type-gate-veto-regression: PASS' if fail == 0 else f'FAIL ({fail}/{len(tests)})'}")
    sys.exit(0 if fail == 0 else 1)
