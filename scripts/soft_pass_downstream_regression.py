"""Regression: soft-pass gate must not block planning/production downstream.

Companion to chapter_type_gate_soft_pass_regression which verified the
report shape. This one verifies the DOWNSTREAM consumers respect soft_pass:

- app/services/planning.py:_quality_report_has_unresolved_gate_blocker
- app/services/production.py:_quality_report_has_unresolved_gate_blocker

Both were originally coded to treat chapter_type_gate.passed=False as a
hard blocker, which meant even after soft_pass was set on the report
they would still refuse to promote/continuity-record the chapter.

Ch44 root cause coverage: LLM editorial + base agree; gate.passed=False
+ soft_pass=True → downstream blockers must return False (not a blocker).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.planning import _quality_report_has_unresolved_gate_blocker as planner_blocker
from app.services.production import _quality_has_unresolved_gate_blocker as production_blocker


def _quality(report: dict) -> SimpleNamespace:
    return SimpleNamespace(report=json.dumps(report, ensure_ascii=False))


def test_planner_blocker_none_report():
    assert planner_blocker(None) is False
    assert production_blocker(None) is False


def test_gate_passed_true_no_blocker():
    q = _quality({
        "chapter_type_gate": {"passed": True, "soft_pass": False},
        "hard_gate": {"passed": True},
        "issues": [],
    })
    assert planner_blocker(q) is False
    assert production_blocker(q) is False


def test_gate_failed_no_soft_pass_blocks():
    """Regression baseline: gate.passed=False without soft_pass still blocks."""
    q = _quality({
        "chapter_type_gate": {"passed": False, "soft_pass": False, "failures": ["brief_coverage=30<60"]},
        "hard_gate": {"passed": True},
        "issues": ["chapter_type_gate_failed:brief_coverage=30<60"],
    })
    assert planner_blocker(q) is True
    assert production_blocker(q) is True


def test_gate_failed_with_soft_pass_does_not_block():
    """P2-Ch44 the fix: gate.passed=False + soft_pass=True → NOT a blocker."""
    q = _quality({
        "chapter_type_gate": {
            "passed": False,
            "soft_pass": True,
            "soft_pass_reason": "gap=15",
            "failures": ["brief_coverage=45<60", "chapter_unit_flow=63<64"],
        },
        "hard_gate": {"passed": True},
        "issues": ["chapter_type_gate_failed:brief_coverage=45<60,chapter_unit_flow=63<64"],
    })
    assert planner_blocker(q) is False, "planner must allow soft-pass through"
    assert production_blocker(q) is False, "production must allow soft-pass through"


def test_hard_gate_failure_always_blocks():
    """Even with soft_pass=True, hard_gate.passed=False remains a hard blocker."""
    q = _quality({
        "chapter_type_gate": {"passed": False, "soft_pass": True, "failures": ["brief_coverage=45<60"]},
        "hard_gate": {"passed": False, "reason": "char_count<min"},
        "issues": [],
    })
    assert planner_blocker(q) is True
    assert production_blocker(q) is True


def test_issues_only_no_gate_object_still_blocks_without_soft_pass():
    """Legacy path: only `issues` array carries the failure; no gate object.
    Cannot infer soft_pass → block."""
    q = _quality({
        "issues": ["chapter_type_gate_failed:brief_coverage=45<60"],
        "hard_gate": {"passed": True},
    })
    assert planner_blocker(q) is True
    assert production_blocker(q) is True


if __name__ == "__main__":
    tests = [
        test_planner_blocker_none_report,
        test_gate_passed_true_no_blocker,
        test_gate_failed_no_soft_pass_blocks,
        test_gate_failed_with_soft_pass_does_not_block,
        test_hard_gate_failure_always_blocks,
        test_issues_only_no_gate_object_still_blocks_without_soft_pass,
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
    print(f"\n{'soft-pass-downstream-regression: PASS' if fail == 0 else f'FAIL ({fail}/{len(tests)})'}")
    sys.exit(0 if fail == 0 else 1)
