"""Regression for LLM-override on structure_rewrite reading assessment.

Change C (2026-07-02): when the rule-side ``production_failure_classification``
classifies a version as ``structure_rewrite`` but the LLM chief editor review
comes back with ``verdict=pass`` (score >= 78), hard_gate is PASS, editorial
tier is B or above, and no forbidden markers were used, the reading assessment
must respect the LLM verdict and downgrade the action from ``auto_rebuild``
to ``auto_polish`` (or ``approve_ready`` when other gates are clean).

Prior behaviour: the rule-side ``structure_rewrite`` branch fired before any
LLM signal was inspected and forced ``auto_rebuild``. This caused the baseline
chapter 1 run (book_id=3) to sit at ``passed=False`` even after v449 scored
76 rule / 80 LLM (both passing) because rule ``brief_coverage=47`` /
``author_intent=50`` tripped the chapter_type_gate → structural failure
classification → auto_rebuild, ignoring the LLM verdict entirely.
"""

from __future__ import annotations

import json
from typing import Any

from app.services.reading_assessment import assess_reading_quality


def _report(**over: Any) -> dict:
    base: dict[str, Any] = {
        "score": 76,
        "passed": False,
        "base_quality_passed": True,
        "hard_gate": {"passed": True, "status": "PASS"},
        "dimensions": {
            "brief_coverage": 47,
            "arc_alignment": 50,
            "author_intent": 50,
            "hook_strength": 100,
            "reader_momentum": 66,
            "conflict_pressure": 50,
            "choice_and_cost": 58,
            "readability": 67,
            "chapter_unit_flow": 70,
            "chapter_necessity": 59,
            "payoff_grounding": 76,
            "scene_atmosphere": 39,
            "imageable_paragraphs": 60,
        },
        "issues": ["chapter_type_gate_failed:author_intent=50<65,brief_coverage=47<60"],
        "chapter_type_gate": {
            "passed": False,
            "failures": ["author_intent=50<65", "brief_coverage=47<60"],
        },
        "production_failure_classification": {
            "schema": "quality_failure_classification_v1",
            "category": "structure_rewrite",
            "structural_reasons": ["brief_coverage_structural"],
        },
        "editorial_stratification": {"tier": "B_solid_draft"},
        "llm_review": {
            "status": "completed",
            "verdict": "pass",
            "score": 80,
            "strengths": ["opening hook works"],
        },
        "editorial_gate": {
            "status": "completed",
            "passed": True,
            "soft_rule_override": True,
        },
    }
    base.update(over)
    return base


def _case(label: str, report: dict, expected_action: str, expected_level: str | None = None) -> str | None:
    assessment = assess_reading_quality(report)
    if assessment.action != expected_action:
        return f"[{label}] expected action={expected_action}, got {assessment.action} (level={assessment.level})"
    if expected_level and assessment.level != expected_level:
        return f"[{label}] expected level={expected_level}, got {assessment.level}"
    return None


def main() -> int:
    failures: list[str | None] = []

    # ---- Change C: LLM-approved structure_rewrite -> auto_polish ----
    failures.append(_case(
        "llm_approved_structure_rewrite_downgrades",
        _report(),
        expected_action="auto_polish",
        expected_level="polish_ready",
    ))

    # LLM verdict != pass -> keep auto_rebuild (LLM disagrees with itself)
    failures.append(_case(
        "llm_needs_revision_keeps_rebuild",
        _report(llm_review={"status": "completed", "verdict": "needs_revision", "score": 60}),
        expected_action="auto_rebuild",
    ))

    # LLM score < 78 -> keep auto_rebuild (borderline LLM not enough)
    failures.append(_case(
        "llm_borderline_score_keeps_rebuild",
        _report(llm_review={"status": "completed", "verdict": "pass", "score": 76}),
        expected_action="auto_rebuild",
    ))

    # LLM skipped/not run -> keep auto_rebuild
    failures.append(_case(
        "llm_skipped_keeps_rebuild",
        _report(llm_review={"status": "skipped"}),
        expected_action="auto_rebuild",
    ))

    # hard_gate FAIL -> even LLM pass cannot override
    failures.append(_case(
        "hard_gate_fail_blocks_override",
        _report(hard_gate={"passed": False, "status": "FAIL"}),
        expected_action="auto_rebuild",
    ))

    # editorial_stratification tier=D_rebuild -> LLM cannot override deep-rebuild
    failures.append(_case(
        "editorial_tier_D_blocks_override",
        _report(editorial_stratification={"tier": "D_rebuild"}),
        expected_action="auto_rebuild",
    ))

    # editorial_gate not soft_rule_override -> keep auto_rebuild
    failures.append(_case(
        "editorial_gate_not_override_keeps_rebuild",
        _report(editorial_gate={"status": "completed", "passed": False, "soft_rule_override": False}),
        expected_action="auto_rebuild",
    ))

    failures = [f for f in failures if f]
    if failures:
        print("reading_assessment_llm_override_regression=FAIL")
        for f in failures:
            print(f"- {f}")
        return 1

    print("reading_assessment_llm_override_regression=PASS")
    print("cases_evaluated=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
