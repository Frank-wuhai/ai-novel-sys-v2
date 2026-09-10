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


def test_large_gap_when_hard_gate_passed_defers_to_quality():
    """A 方案（2026-07-23）契约变更：type_gate 不再拥有独立否决权。
    即使某维度 gap 大（brief_coverage=30<60），只要 quality 层已放行
    (hard_gate.passed=True + base passed=True)，type_gate 就不翻 passed=False。
    理由：brief_coverage 是字面 token 匹配的学院派维度，'是否覆盖 brief' 的裁决
    权归 intent_acceptance 层(已加 LLM 语义复核)+ hard_gate，不归 type_gate。
    type_gate 仅保留诊断记录(gate.passed=False 留痕)，不越权否决。"""
    dims = {
        "brief_coverage": 30,
        "chapter_unit_flow": 80,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    r = enrich_quality_report_with_optimization(_base_report(dimensions=dims), chapter_number=44)
    gate = r["chapter_type_gate"]
    assert gate["passed"] is False, "结构门仍记录失败(诊断留痕)"
    assert r["passed"] is True, "quality 层已放行(hard_gate过) → type_gate 不否决"


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


def test_low_score_when_hard_gate_passed_defers_to_quality():
    """A 方案契约变更：score=65 正是 quality 层 HARD_FLOOR，hard_gate.passed=True
    即代表 quality 层已判 soft_pass。type_gate 不得用更高的 pass_score(70/72)
    二次否决。裁决权归 quality 层的 65 分门槛。
    注意：若 hard_gate 不过 或 base passed=False，仍会被 hard_gate 拦(见上两例)。"""
    dims = {
        "brief_coverage": 45,
        "chapter_unit_flow": 63,
        "dialogue_fullness": 80,
        "scene_atmosphere": 80,
        "hook_strength": 80,
    }
    rpt = _base_report(score=65, dimensions=dims)  # 65 = quality HARD_FLOOR
    r = enrich_quality_report_with_optimization(rpt, chapter_number=44)
    assert r["passed"] is True, "score=65 + hard_gate过 = quality soft_pass · type_gate 不否决"


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
        test_large_gap_when_hard_gate_passed_defers_to_quality,
        test_base_quality_false_no_soft_pass,
        test_hard_gate_violation_no_soft_pass,
        test_low_score_when_hard_gate_passed_defers_to_quality,
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
