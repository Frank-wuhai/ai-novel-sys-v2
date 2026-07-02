"""Sprint 2 P1-1 regression: editorial slack for LLM-endorsed drafts.

Guards two invariants of ``review_decision.soft_override_blockers``:

  1. When the LLM chief editor endorses the draft (verdict=pass +
     score>=75), soft-override thresholds relax by 5 points so a B-tier
     draft is not gated by a single aesthetic dim 3-5 points below the
     threshold. This is the Ch2 v467 scenario: rule=75, LLM verdict=pass,
     tier=B but blocked by dialogue_fullness=47<50 and chapter_necessity=49<55.

  2. ``brief_coverage`` never relaxes — it's a structural "did the chapter
     deliver the promised beats" signal, and softening it would let
     LLM-endorsed drafts skip the outline entirely.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parent))
sys.path.insert(0, str(ROOT))

from app.services.review_decision import apply_review_decision, soft_override_blockers


def _case_no_slack_baseline() -> str | None:
    # slack=0 keeps the historical thresholds intact.
    blockers = soft_override_blockers(
        {"dialogue_fullness": 47, "chapter_necessity": 49, "brief_coverage": 60},
        slack=0,
    )
    joined = ",".join(sorted(blockers))
    expected = "chapter_necessity=49<55,dialogue_fullness=47<50"
    if joined != expected:
        return f"no_slack: blockers={joined!r} expected={expected!r}"
    return None


def _case_slack_6_absorbs_small_gaps() -> str | None:
    # slack=6 lets a B-tier draft through the aesthetic-dim gate.
    blockers = soft_override_blockers(
        {"dialogue_fullness": 47, "chapter_necessity": 49, "brief_coverage": 60},
        slack=6,
    )
    if blockers:
        return f"slack_6_absorbs_small_gaps: unexpected blockers={blockers!r}"
    return None


def _case_slack_6_still_blocks_large_gaps() -> str | None:
    # A dim 10+ points below the threshold is not absorbed by slack=6.
    blockers = soft_override_blockers({"readability": 40}, slack=6)
    joined = ",".join(sorted(blockers))
    if joined != "readability=40<54":
        return f"slack_6_still_blocks_large_gaps: blockers={joined!r} expected=readability=40<54"
    return None


def _case_brief_coverage_never_relaxes() -> str | None:
    # brief_coverage=42 with slack=8 must still block (structural signal).
    blockers = soft_override_blockers({"brief_coverage": 42}, slack=8)
    joined = ",".join(sorted(blockers))
    if joined != "brief_coverage=42<46":
        return f"brief_coverage_never_relaxes: blockers={joined!r} expected=brief_coverage=42<46"
    return None


def _case_slack_capped_at_8() -> str | None:
    # slack>8 is capped so a bad draft can't be waved through.
    blockers = soft_override_blockers({"readability": 45}, slack=100)
    # readability threshold 60 - min(100, 8) = 52; 45 < 52 → blocker.
    joined = ",".join(sorted(blockers))
    if joined != "readability=45<52":
        return f"slack_capped_at_8: blockers={joined!r} expected=readability=45<52"
    return None


def _case_apply_decision_uses_slack() -> str | None:
    # End-to-end: apply_review_decision passes slack when editor endorses.
    report = {
        "score": 75,
        "passed": False,
        "issues": [],
        "dimensions": {
            "brief_coverage": 60,
            "dialogue_fullness": 47,
            "chapter_necessity": 49,
            "readability": 62,
            "design_texture": 68,
            "visual_staging": 62,
            "designed_nomenclature": 68,
            "imageable_paragraphs": 58,
            "native_chinese_flow": 62,
            "character_voice": 62,
            "anti_ai_flavor": 62,
            "expression_precision": 62,
            "object_verb_collocation": 62,
            "observation_logic": 62,
            "inference_chain": 62,
            "wording_specificity": 62,
            "writer_craft": 58,
            "memorable_image": 58,
            "memorable_dialogue": 52,
            "designed_asset": 58,
            "character_action": 58,
            "embodied_pov": 58,
            "scene_expansion": 60,
        },
        "hard_gate": {"passed": True, "issues": []},
        "llm_review": {"status": "completed", "score": 82, "verdict": "pass"},
    }
    from app.services.review_decision import ReviewRuleResult
    rule = ReviewRuleResult(passed=False, score=75)
    apply_review_decision(rule, report)
    gate = report.get("editorial_gate") or {}
    if not report.get("passed"):
        return f"apply_decision_uses_slack: expected passed=True, got={report.get('passed')} gate={gate}"
    if not gate.get("soft_rule_override"):
        return f"apply_decision_uses_slack: expected soft_rule_override=True, gate={gate}"
    return None


def main() -> int:
    checks = [
        _case_no_slack_baseline,
        _case_slack_6_absorbs_small_gaps,
        _case_slack_6_still_blocks_large_gaps,
        _case_brief_coverage_never_relaxes,
        _case_slack_capped_at_8,
        _case_apply_decision_uses_slack,
    ]
    failures = [msg for check in checks if (msg := check()) is not None]
    if failures:
        print("editorial-slack-regression: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"editorial-slack-regression: PASS ({len(checks)} cases)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
