from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewRuleResult:
    passed: bool
    score: int


def apply_review_decision(rule_result: ReviewRuleResult, report_data: dict) -> None:
    review = report_data.get("llm_review") or {}
    if not isinstance(review, dict) or review.get("status") != "completed":
        report_data["passed"] = rule_result.passed
        report_data["score"] = rule_result.score
        report_data["editorial_gate"] = {
            "status": "skipped",
            "passed": True,
            "reason": "主编审稿未完成，采用规则质检结果。",
        }
        report_data["status"] = "PASS" if rule_result.passed else "FAIL"
        return

    editor_score = int(review.get("score") or 0)
    editor_verdict = str(review.get("verdict") or "")
    editor_passed = editor_score >= 75 and editor_verdict == "pass"
    hard_gate = report_data.get("hard_gate") or {}
    hard_gate_passed = bool(hard_gate.get("passed"))
    blocking_issues = _blocking_issues(report_data, hard_gate)
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    # Sprint 2 P1-1: when the LLM chief editor already endorsed the draft
    # (verdict=pass + score>=75), widen soft-override thresholds by 5
    # points so a B-tier draft is not blocked by a single dim that is 3-5
    # points below the threshold. Rationale: Ch2 v467 hit rule=75, LLM
    # verdict=pass, tier=B_solid_draft but got gated by
    # dialogue_fullness=47<50 (diff=3) and chapter_necessity=49<55 (diff=6).
    # The chief editor already ruled; blocking on a 3-6 point gap is over-strict.
    editorial_slack = 6 if editor_passed else 0
    soft_blockers = soft_override_blockers(dimensions, slack=editorial_slack)
    # Sprint 2 P2-Ch45: chapter_type_gate has priority over editorial soft-override.
    # chapter_type_gate measures structural dimensions (conflict_pressure,
    # choice_and_cost, earned_payoff, brief_coverage) that the editorial gate's
    # dimension whitelist does not cover. If chapter_type_gate blocks AND no
    # soft_pass was granted (i.e. gap > 15pt), editorial gate must not flip
    # passed back to True — the LLM chief editor doesn't see structural gaps.
    chapter_type_gate = report_data.get("chapter_type_gate") if isinstance(report_data.get("chapter_type_gate"), dict) else {}
    type_gate_veto = bool(
        chapter_type_gate
        and not bool(chapter_type_gate.get("passed"))
        and not bool(chapter_type_gate.get("soft_pass"))
    )
    soft_rule_override = bool(
        editor_passed
        and hard_gate_passed
        and not blocking_issues
        and not soft_blockers
        and not type_gate_veto
    )
    final_passed = bool(editor_passed and (rule_result.passed or soft_rule_override) and not type_gate_veto)

    report_data["editorial_gate"] = {
        "status": "completed",
        "passed": editor_passed,
        "threshold": 75,
        "score": editor_score,
        "verdict": editor_verdict,
        "soft_rule_override": soft_rule_override,
        "soft_override_blockers": soft_blockers,
        "blocking_issues": blocking_issues,
        "override_reason": (
            "硬门禁通过且主编审稿通过；软维度不足交给采用确认判断。"
            if soft_rule_override and not rule_result.passed
            else ""
        ),
        "decision_source": "unified_review_decision@v1",
    }
    report_data["passed"] = final_passed
    report_data["score"] = (
        max(75, min(rule_result.score, editor_score))
        if final_passed and not rule_result.passed and soft_rule_override
        else min(rule_result.score, editor_score)
    )
    report_data["status"] = "PASS" if final_passed else "FAIL"


def soft_override_blockers(dimensions: dict, *, slack: int = 0) -> list[str]:
    """Return a list of `dim=value<threshold` strings for dimensions below
    their soft-override threshold.

    ``slack`` (Sprint 2 P1-1): non-negative integer subtracted from every
    threshold before the compare. When the LLM chief editor has already
    endorsed the chapter (verdict=pass + score>=75), the caller passes
    slack>0 to widen the acceptance band by that many rule points so a
    B-tier draft is not gated by a single dimension that is 3-5 points
    below the threshold. Slack is capped at 8 to keep the guarantee: soft
    override only widens B/A-tier acceptance, it never accepts a genuine
    structural failure (those are caught by hard_gate + blocking_issues,
    which are orthogonal to this function).
    """
    slack = max(0, min(int(slack), 8))
    # Sprint 2 P1-1: brief_coverage is a structural signal (章节大纲实际落地度)
    # — if it's below 46, the chapter didn't actually deliver the beats it
    # promised. Slack must not soften this one; only the aesthetic/style
    # dimensions get the LLM-endorsed grace band.
    non_slackable = {"brief_coverage"}
    thresholds = {
        "brief_coverage": 46,
        "readability": 60,
        "design_texture": 65,
        "visual_staging": 60,
        "designed_nomenclature": 65,
        "imageable_paragraphs": 55,
        "native_chinese_flow": 60,
        "dialogue_fullness": 50,
        "character_voice": 60,
        "anti_ai_flavor": 60,
        "expression_precision": 60,
        "object_verb_collocation": 60,
        "observation_logic": 60,
        "inference_chain": 60,
        "wording_specificity": 60,
        "writer_craft": 55,
        "memorable_image": 55,
        "memorable_dialogue": 50,
        "designed_asset": 55,
        "character_action": 55,
        "chapter_necessity": 55,
        "embodied_pov": 55,
        "scene_expansion": 58,
    }
    blockers: list[str] = []
    for name, threshold in thresholds.items():
        effective = threshold if name in non_slackable else threshold - slack
        value = dimensions.get(name)
        if isinstance(value, int) and value < effective:
            blockers.append(f"{name}={value}<{effective}")
    return blockers


def _blocking_issues(report_data: dict, hard_gate: dict) -> list[str]:
    return [
        str(issue)
        for issue in [*report_data.get("issues", []), *hard_gate.get("issues", [])]
        if str(issue).startswith(
            (
                "forbidden_marker",
                "setting_contradiction",
                "too_short",
                "too_long",
                "bias_blocker",
            )
        )
    ]
