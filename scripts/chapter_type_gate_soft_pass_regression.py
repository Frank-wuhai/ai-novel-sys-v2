"""Regression: soft-pass semantics for chapter_type_gate.

Ch44 exposed: when LLM editorial gate passes (base_quality_passed=True,
editorial_gate.passed=True) but chapter_type_gate fails on structural
dimensions (e.g. brief_coverage=45<60, chapter_unit_flow=63<64), the
system forces final passed=False, wasting 21 rounds burning tokens on
revisions that never converge (v8 and v20 both score 80 brief_coverage=45).

Root fix: when the gap between actual and required is small (<=15pt total),
LLM says pass, and no hard_gate violations exist, promote to soft-pass:
type_gate_passed remains False (audit trail preserved) but final passed
stays True with `soft_pass` marker. Matches the user's manual open-loop
policy for the same class of blocker.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.production_optimization import enrich_quality_report_with_optimization


def _base_report(*, score: int = 80, dimensions: dict | None = None) -> dict:
    """Build a report shape close to what production emits at Ch44."""
    return {
        "score": score,
        "passed": True,  # base quality path already said pass
        "status": "REVIEWED_PASS",
        "dimensions": dimensions or {
            "brief_coverage": 80,
            "chapter_unit_flow": 80,
            "dialogue_fullness": 80,
            "scene_atmosphere": 80,
            "hook_strength": 80,
        },
        "hard_gate": {"passed": True},
        "editorial_gate": {"passed": True, "score": 82},
        "base_quality_passed": True,
        "issues": [],
    }


def test_all_dimensions_pass_keeps_passed_true():
    """Baseline: no gate failures, passed stays True (no soft-pass needed)."""
    r = enrich_quality_report_with_optimization(_base_report(), chapter_number=44)
    assert r["passed"] is True
    assert r["chapter_type_gate"]["passed"] is True
    assert not r["chapter_type_gate"].get("soft_pass")


def test_small_gap_soft_pass_promotes_to_passed_true():
    """Ch44 case: brief_coverage=45<60 (gap=15), chapter_unit_flow=63<64 (gap=1).
    Total gap = 16... but max single gap = 15 -> soft-pass allowed."""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    r = enrich_quality_report_with_optimization(_base_report(dimensions=dims), chapter_number=44)
    gate = r["chapter_type_gate"]
    assert gate["passed"] is False, "structural gate still records the failure"
    assert gate.get("soft_pass") is True, "but soft_pass activated"
    assert r["passed"] is True, "final passed stays True per open-loop policy"
    assert gate.get("soft_pass_reason"), "audit trail required"


def test_large_gap_still_blocks():
    """Gap >15pt on any single dimension: NO soft-pass, remain blocked.
    Example: brief_coverage=30<60 (gap=30) — LLM likely wrong or draft too far off."""
    dims = {
        "brief_coverage": 30,
        "chapter_unit_flow": 80,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    r = enrich_quality_report_with_optimization(_base_report(dimensions=dims), chapter_number=44)
    gate = r["chapter_type_gate"]
    assert gate["passed"] is False
    assert not gate.get("soft_pass"), "gap>15pt must not soft-pass"
    assert r["passed"] is False


def test_base_quality_false_no_soft_pass():
    """If base quality itself failed (passed=False on entry), no soft-pass."""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    rpt = _base_report(dimensions=dims)
    rpt["passed"] = False
    rpt["base_quality_passed"] = False
    rpt["editorial_gate"] = {"passed": False, "score": 65}
    r = enrich_quality_report_with_optimization(rpt, chapter_number=44)
    assert r["passed"] is False
    assert not r["chapter_type_gate"].get("soft_pass")


def test_hard_gate_violation_no_soft_pass():
    """hard_gate.passed=False (word count, canonical violation, etc.): no soft-pass."""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    rpt = _base_report(dimensions=dims)
    rpt["hard_gate"] = {"passed": False, "reason": "char_count<min"}
    r = enrich_quality_report_with_optimization(rpt, chapter_number=44)
    assert r["passed"] is False
    assert not r["chapter_type_gate"].get("soft_pass")


def test_low_score_no_soft_pass():
    """If base_score < profile.pass_score, structural signal is dominated by
    quality issues — do not soft-pass on structural dimensions alone."""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    rpt = _base_report(score=65, dimensions=dims)  # < 70 pass_score
    r = enrich_quality_report_with_optimization(rpt, chapter_number=44)
    assert r["passed"] is False, "low base_score blocks soft-pass"
    assert not r["chapter_type_gate"].get("soft_pass")


def test_enforce_gate_false_bypasses_check():
    """Existing behaviour: enforce_gate=False means the gate never touches passed.
    soft_pass logic must not interfere."""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    r = enrich_quality_report_with_optimization(
        _base_report(dimensions=dims),
        chapter_number=44,
        enforce_gate=False,
    )
    assert r["passed"] is True


if __name__ == "__main__":
    tests = [
        test_all_dimensions_pass_keeps_passed_true,
        test_small_gap_soft_pass_promotes_to_passed_true,
        test_large_gap_still_blocks,
        test_base_quality_false_no_soft_pass,
        test_hard_gate_violation_no_soft_pass,
        test_low_score_no_soft_pass,
        test_enforce_gate_false_bypasses_check,
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
    print(f"\n{'chapter-type-gate-soft-pass-regression: PASS' if fail == 0 else f'FAIL ({fail}/{len(tests)})'}")
    sys.exit(0 if fail == 0 else 1)
