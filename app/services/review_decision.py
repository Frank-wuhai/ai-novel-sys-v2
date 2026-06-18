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
    soft_blockers = soft_override_blockers(dimensions)
    soft_rule_override = bool(editor_passed and hard_gate_passed and not blocking_issues and not soft_blockers)
    final_passed = bool(editor_passed and (rule_result.passed or soft_rule_override))

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
            "硬门禁通过且主编审稿通过；软维度不足交给人工审批判断。"
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


def soft_override_blockers(dimensions: dict) -> list[str]:
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
        value = dimensions.get(name)
        if isinstance(value, int) and value < threshold:
            blockers.append(f"{name}={value}<{threshold}")
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
