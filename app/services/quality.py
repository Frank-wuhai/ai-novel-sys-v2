from __future__ import annotations

import json
import re
from dataclasses import dataclass

from app.services.anti_ai_flavor import evaluate_anti_ai_flavor
from app.services.bias import evaluate_generation_bias
from app.services.chapter_units import evaluate_chapter_units
from app.services.design_quality import evaluate_design_quality
from app.services.expression_precision import evaluate_expression_precision
from app.services.humanized_quality import evaluate_humanized_delivery
from app.services.intent_acceptance import evaluate_author_intent
from app.services.naming_governance import evaluate_naming_governance
from app.services.narrative_logic import evaluate_narrative_logic
from app.services.paragraph_aesthetic import evaluate_paragraph_aesthetic
from app.services.prose_voice import evaluate_prose_voice
from app.services.readability import evaluate_readability
from app.services.writer_craft import evaluate_writer_craft


HARD_FLOOR = 65
PASS_FLOOR = 75


def classify_quality_verdict(*, score: int, hard_dimension_ok: bool, has_blocking_issues: bool) -> str:
    """Three-tier quality verdict: hard_fail / soft_pass / pass.

    Phase 2/3 quality gate stratification. Pure function so callers, tests,
    and dashboards can share one canonical definition.

    - ``hard_fail``: score < 65 OR blocking issues present OR hard dimension
      floor breached. Must continue revising.
    - ``soft_pass``: 65 <= score < 75 with hard gate cleared. Publishable
      with human acceptance; early-stop still nudges revisions upward.
    - ``pass``: score >= 75 with hard gate cleared. Recommended stop.
    """
    hard_gate_ok = hard_dimension_ok and not has_blocking_issues and score >= HARD_FLOOR
    if not hard_gate_ok:
        return "hard_fail"
    if score >= PASS_FLOOR:
        return "pass"
    return "soft_pass"


@dataclass
class QualityResult:
    passed: bool
    score: int
    report: str
    dimensions: dict[str, int]
    issues: list[str]


FORBIDDEN_MARKERS = ["Runtime Draft", "generated_by_agent", "model_used", "系统提示", "作为AI"]
BLOCKING_CONTRADICTIONS = ["无代价", "没有代价", "无需代价", "无限使用", "永久无敌"]
MOMENTUM_MARKERS = ["压力", "危机", "选择", "代价", "发现", "钩子", "异象", "秘密"]
CONFLICT_MARKERS = ["压力", "危机", "冲突", "阻碍", "危险", "逼近", "追查", "失控"]
CHOICE_COST_MARKERS = ["选择", "代价", "付出", "损耗", "承担", "交换", "收益", "后果"]
HOOK_MARKERS = ["钩子", "秘密", "发现", "转折", "章末", "疑问", "源头", "倒影", "异象", "陌生", "消息", "脚步", "门外", "黑影", "下一次"]
FILLER_MARKERS = ["水字数", "无意义", "随便", "重复一遍", "占位", "凑字数"]


def chinese_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def evaluate_chapter(
    text: str,
    *,
    min_chars: int = 1200,
    max_chars: int = 8000,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    canon_context: str = "",
) -> QualityResult:
    issues: list[str] = []
    count = chinese_chars(text)
    if count < min_chars:
        issues.append(f"too_short: {count} < {min_chars}")
    if count > max_chars:
        issues.append(f"too_long: {count} > {max_chars}")
    for marker in FORBIDDEN_MARKERS:
        if _has_forbidden_marker(text, marker):
            issues.append(f"forbidden_marker: {marker}")
    for marker in BLOCKING_CONTRADICTIONS:
        if _has_blocking_contradiction(text, marker):
            issues.append(f"setting_contradiction: {marker}")
    bias = evaluate_generation_bias(
        content=text,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
    )
    for blocker in bias.blockers:
        issues.append(f"bias_blocker: {blocker}")
    intent = evaluate_author_intent(
        content=text,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
    )
    readability = evaluate_readability(text)
    humanized = evaluate_humanized_delivery(text)
    chapter_units = evaluate_chapter_units(text)
    design = evaluate_design_quality(text, canon_context=canon_context)
    prose_voice = evaluate_prose_voice(text)
    expression_precision = evaluate_expression_precision(text)
    naming = evaluate_naming_governance(text, canon_context=canon_context)
    narrative_logic = evaluate_narrative_logic(text)
    anti_ai = evaluate_anti_ai_flavor(design=design, prose_voice=prose_voice, humanized=humanized)
    writer_craft = evaluate_writer_craft(text)
    paragraph_aesthetic = evaluate_paragraph_aesthetic(text)
    for blocker in intent.blockers:
        if blocker != "intent_underfulfilled":
            issues.append(f"intent_blocker: {blocker}")
    if design.score < 60:
        issues.append(f"design_underdeveloped: {design.score}")
    if design.checks.get("visual_staging", 100) < 50 or (design.checks.get("visual_staging", 100) < 55 and design.score < 65):
        issues.append(f"visual_underdeveloped: {design.checks.get('visual_staging', 0)}")
    if design.checks.get("imageable_paragraphs", 100) < 48 or (
        design.checks.get("imageable_paragraphs", 100) < 55 and design.score < 65
    ):
        issues.append(f"imageable_underdeveloped: {design.checks.get('imageable_paragraphs', 0)}")
    if prose_voice.checks.get("native_chinese_flow", 100) < 60:
        issues.append(f"translationese_risk: {prose_voice.checks.get('native_chinese_flow', 0)}")
    if prose_voice.checks.get("dialogue_fullness", 100) < 45 or (
        prose_voice.checks.get("dialogue_fullness", 100) < 50 and prose_voice.score < 65
    ):
        issues.append(f"dialogue_underdeveloped: {prose_voice.checks.get('dialogue_fullness', 0)}")
    if expression_precision.score < 60:
        issues.append(f"expression_precision_risk: {expression_precision.score}")
    # Naming/aesthetic evaluators are diagnostic by default. They are useful for
    # editor guidance, but treating them as production blockers made readable
    # chapters fall into endless revision loops.
    if narrative_logic.score < 60:
        issues.append(f"narrative_logic_risk: {narrative_logic.score}")
    if anti_ai.score < 60:
        issues.append(f"ai_flavor_risk: {anti_ai.score}")
    if writer_craft["score"] < 55:
        issues.append(f"writer_craft_underdeveloped: {writer_craft['score']}")
    if writer_craft["checks"].get("embodied_pov", 100) < 55:
        issues.append(f"embodied_pov_underdeveloped: {writer_craft['checks'].get('embodied_pov', 0)}")
    if writer_craft["checks"].get("scene_expansion", 100) < 55:
        issues.append(f"scene_expansion_underdeveloped: {writer_craft['checks'].get('scene_expansion', 0)}")

    dimensions = {
        "basic_publishability": _basic_publishability_score(count, min_chars, max_chars, text),
        "brief_coverage": _coverage_score(text, coverage_points_for_brief(goal, required_beats, constraints)),
        "canon_consistency": _canon_score(text, canon_context),
        "reader_momentum": _marker_score(text, MOMENTUM_MARKERS),
        "conflict_pressure": _marker_score(text, CONFLICT_MARKERS),
        "choice_and_cost": _marker_score(text, CHOICE_COST_MARKERS),
        "hook_strength": _hook_score(text),
        "prose_density": _prose_density_score(text),
        "arc_alignment": _arc_alignment_score(text, goal=goal, required_beats=required_beats, constraints=constraints),
        "production_standard": _production_standard_score(text, min_chars=min_chars),
        "setting_risk": _setting_risk_score(text),
        "platform_risk": _platform_risk_score(text),
        "author_intent": intent.score,
        "readability": readability.score,
        "opening_variety": _opening_variety_score(text),
        "causal_scene_chain": _causal_scene_chain_score(text),
        "reaction_chain": _reaction_chain_score(text),
        "earned_payoff": _earned_payoff_score(text),
        "design_texture": design.score,
        "visual_staging": design.checks.get("visual_staging", 0),
        "designed_nomenclature": design.checks.get("designed_nomenclature", 0),
        "naming_governance": naming.score,
        "narrative_logic": narrative_logic.score,
        "causal_continuity_quality": narrative_logic.checks.get("causal_continuity", 0),
        "cost_plausibility": narrative_logic.checks.get("cost_plausibility", 0),
        "scene_atmosphere": narrative_logic.checks.get("scene_atmosphere", 0),
        "payoff_grounding": narrative_logic.checks.get("payoff_grounding", 0),
        "imageable_paragraphs": design.checks.get("imageable_paragraphs", 0),
        "prose_voice": prose_voice.score,
        "expression_precision": expression_precision.score,
        "object_verb_collocation": expression_precision.checks.get("object_verb_collocation", 0),
        "observation_logic": expression_precision.checks.get("observation_logic", 0),
        "inference_chain": expression_precision.checks.get("inference_chain", 0),
        "wording_specificity": expression_precision.checks.get("wording_specificity", 0),
        "native_chinese_flow": prose_voice.checks.get("native_chinese_flow", 0),
        "dialogue_fullness": prose_voice.checks.get("dialogue_fullness", 0),
        "character_voice": prose_voice.checks.get("character_voice", 0),
        "anti_ai_flavor": anti_ai.score,
        "chapter_unit_flow": chapter_units.score,
        "writer_craft": writer_craft["score"],
        "memorable_image": writer_craft["checks"].get("memorable_image", 0),
        "memorable_dialogue": writer_craft["checks"].get("memorable_dialogue", 0),
        "designed_asset": writer_craft["checks"].get("designed_asset", 0),
        "character_action": writer_craft["checks"].get("character_action", 0),
        "chapter_necessity": writer_craft["checks"].get("chapter_necessity", 0),
        "embodied_pov": writer_craft["checks"].get("embodied_pov", 0),
        "scene_expansion": writer_craft["checks"].get("scene_expansion", 0),
        "paragraph_aesthetic": paragraph_aesthetic.score,
    }
    if dimensions["brief_coverage"] < 45:
        issues.append(f"brief_coverage_underfulfilled: {dimensions['brief_coverage']}")
    if dimensions["object_verb_collocation"] < 50:
        issues.append(f"expression_collocation_blocker: {dimensions['object_verb_collocation']}")
    if dimensions["cost_plausibility"] < 50:
        issues.append(f"cost_plausibility_blocker: {dimensions['cost_plausibility']}")
    if dimensions["causal_continuity_quality"] < 50:
        issues.append(f"causal_continuity_blocker: {dimensions['causal_continuity_quality']}")
    if dimensions["payoff_grounding"] < 50:
        issues.append(f"payoff_grounding_blocker: {dimensions['payoff_grounding']}")
    warnings: list[str] = []
    for name in (
        "brief_coverage",
        "conflict_pressure",
        "choice_and_cost",
        "hook_strength",
        "prose_density",
        "arc_alignment",
        "production_standard",
        "causal_scene_chain",
        "reaction_chain",
        "earned_payoff",
        "design_texture",
        "visual_staging",
        "designed_nomenclature",
        "naming_governance",
        "narrative_logic",
        "causal_continuity_quality",
        "cost_plausibility",
        "scene_atmosphere",
        "payoff_grounding",
        "imageable_paragraphs",
        "prose_voice",
        "expression_precision",
        "object_verb_collocation",
        "observation_logic",
        "inference_chain",
        "wording_specificity",
        "native_chinese_flow",
        "dialogue_fullness",
        "character_voice",
        "anti_ai_flavor",
        "chapter_unit_flow",
        "writer_craft",
        "memorable_image",
        "memorable_dialogue",
        "designed_asset",
        "character_action",
        "chapter_necessity",
            "embodied_pov",
            "paragraph_aesthetic",
        ):
        if dimensions[name] < 50:
            warnings.append(f"weak_narrative_dimension: {name}={dimensions[name]}")
        elif name in {
            "design_texture",
            "visual_staging",
            "designed_nomenclature",
            "naming_governance",
            "narrative_logic",
            "causal_continuity_quality",
            "cost_plausibility",
            "scene_atmosphere",
            "payoff_grounding",
            "imageable_paragraphs",
            "prose_voice",
            "expression_precision",
            "object_verb_collocation",
            "observation_logic",
            "inference_chain",
            "wording_specificity",
            "native_chinese_flow",
            "dialogue_fullness",
            "character_voice",
            "anti_ai_flavor",
            "chapter_unit_flow",
            "writer_craft",
            "memorable_image",
            "memorable_dialogue",
            "designed_asset",
            "character_action",
            "chapter_necessity",
            "embodied_pov",
            "paragraph_aesthetic",
        } and dimensions[name] < 65:
            warnings.append(f"weak_design_dimension: {name}={dimensions[name]}")
    for issue in readability.issues:
        warnings.append(issue)
    for issue in humanized.issues:
        warnings.append(f"humanized_delivery: {issue}")
    for issue in design.issues:
        warnings.append(f"design_quality: {issue}")
    for issue in prose_voice.issues:
        warnings.append(f"prose_voice: {issue}")
    for issue in expression_precision.issues:
        warnings.append(f"expression_precision: {issue}")
    for issue in naming.issues:
        warnings.append(f"naming_governance: {issue}")
    for issue in narrative_logic.issues:
        warnings.append(f"narrative_logic: {issue}")
    for issue in anti_ai.issues:
        warnings.append(f"anti_ai_flavor: {issue}")
    for issue in chapter_units.issues:
        warnings.append(f"chapter_unit_flow: {issue}")
    for issue in writer_craft["issues"]:
        warnings.append(f"writer_craft: {issue}")
    for issue in paragraph_aesthetic.issues:
        warnings.append(f"paragraph_aesthetic: {issue}")
    blocking = [issue for issue in issues if issue.startswith(("forbidden_marker", "setting_contradiction"))]
    score = round(sum(dimensions.values()) / len(dimensions))
    if count < min_chars:
        score = min(score, dimensions["basic_publishability"])
    if blocking:
        score = min(score, 40)
    if bias.blockers:
        score = min(score, 45)
    if intent.blockers:
        score = min(score, max(45, intent.score))
    if design.issues:
        score = min(score, max(50, design.score))
    score = max(0, min(100, score))
    hard_dimension_ok = all(
        dimensions[name] >= 50
        for name in ("basic_publishability", "production_standard", "setting_risk", "platform_risk")
    )
    hard_dimension_ok = hard_dimension_ok and not bias.blockers
    # ------------------------------------------------------------------
    # Phase 2/3: three-tier quality verdict via classify_quality_verdict.
    # ``passed`` remains a boolean (backwards compatibility): it means
    # "cleared the hard gate", i.e. verdict in {"soft_pass", "pass"}.
    verdict = classify_quality_verdict(
        score=score,
        hard_dimension_ok=hard_dimension_ok,
        has_blocking_issues=bool(issues),
    )
    passed = verdict in {"soft_pass", "pass"}
    hard_issues = [
        issue
        for issue in issues
        if issue.startswith(("too_short", "too_long", "forbidden_marker", "setting_contradiction", "bias_blocker"))
    ]
    hard_gate = {
        "status": "PASS" if not hard_issues and hard_dimension_ok else "FAIL",
        "passed": bool(not hard_issues and hard_dimension_ok),
        "dimensions": {
            name: dimensions[name]
            for name in ("basic_publishability", "production_standard", "setting_risk", "platform_risk")
        },
        "issues": hard_issues,
        "threshold": 50,
    }
    report = json.dumps(
        {
            "status": "PASS" if passed else "FAIL",
            "verdict": verdict,
            "score": score,
            "chinese_chars": count,
            "hard_gate": hard_gate,
            "dimensions": dimensions,
            "issues": issues,
            "warnings": warnings,
            "bias_report": bias.to_dict(),
            "intent_acceptance": intent.to_dict(),
            "readability_report": readability.to_dict(),
            "humanized_report": humanized.to_dict(),
            "design_quality_report": design.to_dict(),
            "prose_voice_report": prose_voice.to_dict(),
            "expression_precision_report": expression_precision.to_dict(),
            "naming_governance_report": naming.to_dict(),
            "narrative_logic_report": narrative_logic.to_dict(),
            "anti_ai_flavor_report": anti_ai.to_dict(),
            "chapter_unit_report": chapter_units.to_dict(),
            "writer_craft_report": writer_craft,
            "paragraph_aesthetic_report": paragraph_aesthetic.to_dict(),
            "thresholds": {
                "pass_score": PASS_FLOOR,
                "soft_pass_floor": HARD_FLOOR,
                "hard_floor": HARD_FLOOR,
                "hard_min_dimension": 50,
                "min_chars": min_chars,
                "max_chars": max_chars,
            },
        },
        ensure_ascii=False,
    )
    return QualityResult(passed=passed, score=score, report=report, dimensions=dimensions, issues=issues)


def split_points(value: str) -> list[str]:
    normalized = value.replace("\r", "\n").replace("\n", ",").replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def coverage_points_for_brief(goal: str, required_beats: str, constraints: str = "") -> list[str]:
    points = [goal, *split_points(required_beats)]
    for item in split_points(constraints):
        if _is_constraint_coverage_candidate(item):
            points.append(item)
    return points


def _basic_publishability_score(text_len: int, min_chars: int, max_chars: int, text: str) -> int:
    score = 100
    if text_len < min_chars:
        score -= min(70, (min_chars - text_len) // 10)
    if text_len > max_chars:
        score -= min(50, (text_len - max_chars) // 50)
    score -= 25 * sum(1 for marker in FORBIDDEN_MARKERS if _has_forbidden_marker(text, marker))
    return max(0, min(100, score))


def _coverage_score(text: str, points: list[str]) -> int:
    meaningful = _meaningful_coverage_points(points)
    if not meaningful:
        return 70
    hits = sum(1 for point in meaningful if _point_is_covered(text, point))
    partial_hits = sum(1 for point in meaningful if not _point_is_covered(text, point) and _point_has_partial_coverage(text, point))
    ratio = (hits + partial_hits * 0.5) / len(meaningful)
    return max(35, min(100, round(45 + ratio * 55)))


def _meaningful_coverage_points(points: list[str]) -> list[str]:
    normalized: list[str] = []
    for point in points:
        point = point.strip()
        if len(point) < 2 or _is_diagnostic_point(point):
            continue
        if len(point) > 80:
            tokens = [
                token
                for token in _coverage_tokens(point)
                if 2 <= len(token) <= 12 and not _is_diagnostic_point(token)
            ]
            normalized.extend(tokens[:5])
            continue
        normalized.append(point)
    return list(dict.fromkeys(normalized))[:24]


def _is_diagnostic_point(point: str) -> bool:
    diagnostic_markers = [
        "质检报告 #",
        "提升维度：",
        "weak_narrative_dimension",
        "hook_strength",
        "brief_coverage",
        "reader_momentum",
        "conflict_pressure",
        "choice_and_cost",
        "prose_density",
        "arc_alignment",
        "正文必须",
        "必要节拍",
        "不要只改",
        "开场三百字",
        "增加可见",
        "主角必须",
        "付出清晰代价",
        "章末最后三百字",
        "减少解释",
        "修订必须",
        "修订执行摘要",
        "修订合同",
        "修订模式",
        "定点修订合同",
        "原始机器修订建议",
        "意见理解规则",
        "目标读者体验",
        "必须满足",
        "禁止项",
        "禁止:",
        "验收:",
        "验收清单",
        "修订方向",
        "范围:",
        "系统修订判定",
        "处理强度",
        "置信度",
        "判定理由",
        "保留:",
        "替换:",
        "升级规则",
        "原始意见",
        "system_revision_loop_guard",
        "system_revision_trend_recovery",
        "恢复底稿",
        "废弃劣化稿",
        "换策略修订",
        "不沿坏稿继续",
        "不得继续沿最新劣化稿",
        "当前主角锚点",
        "当前世界/作品锚点",
        "当前能力/卖点锚点",
        "必须遵守最新作品DNA",
        "作品DNA",
        "禁区",
        "少量界面/提示",
        "不要输出导演单",
        "对白和动作必须承接",
        "前五章每章",
        "当前阻断问题",
        "当前优化提醒",
        "局部修复合同",
        "不要继续 fresh",
        "保留当前稿",
        "不要求逐字复刻",
        "结尾要推动",
        "删除系统提示",
        "保留已登记 Canon",
        "不引入",
        "不输出系统元信息",
        "修订后必须",
        "必须响应修订方向",
        "修复质检问题",
        "采纳二审建议",
        "规避风险",
        "词语或短段落",
        "必须按最小范围处理",
        "保留其余正文",
        "保留当前最佳稿已验证",
        "除非它违反最新骨架",
        "下一版必须能被修订方向逐条验收",
        "质检术语",
        "通用章节生产标准",
        "正文字数",
        "章节阶段",
        "开篇牵引",
        "开篇反雷同",
        "主角行动链",
        "人物反应链",
        "拟人化小单元",
        "场景推进",
        "信息释放",
        "爽点/期待",
        "每个约",
        "后一单元",
        "每2个单元",
        "设定只能",
        "至少完成",
        "本章只能",
        "必须凭判断",
        "真实存在的武侠世界",
        "成长不靠",
        "套路触发器",
        "少量游戏界面",
        "必须保持",
        "补足本章核心承诺",
        "让读者能",
        "人物目标",
        "场景阻碍",
        "局面变化",
        "具体处境",
        "人物欲望",
        "关系张力",
        "异常细节",
        "利益交换",
        "行动后果",
        "阅读牵引",
        "reading_assessment_auto_quality",
        "当前阅读层级",
        "源版本锁定",
        "第1章硬性交付",
        "不得以",
        "醒来",
        "睁眼",
        "摸手机",
        "宿舍回忆",
        "系统菜单",
        "环境确认",
        "利益冲突",
        "逼近风险",
        "个单元",
        "单元需局部重修",
        "目标不清",
        "动作链弱",
        "阻碍不足",
        "后果没落地",
        "信息增量弱",
        "人物反应弱",
        "保留本单元有效信息",
        "补清目标",
        "动作后果",
        "承接点",
        "当前片段",
        "单元验收",
        "局部修订闭环",
        "imageable_paragraphs",
        "抽象设定句",
        "关键段落",
        "画面中心",
        "goal",
        "action",
        "obstacle",
        "consequence",
        "info_gain",
        "reaction",
        "handoff",
    ]
    stripped = point.strip()
    if stripped.startswith(("不要", "不能", "禁止", "不得", "避免", "只修改", "只修复", "只改", "未被点名", "开场", "修订必须", "共 ")):
        return True
    return any(marker in point for marker in diagnostic_markers)


def _is_constraint_coverage_candidate(point: str) -> bool:
    stripped = point.strip()
    if len(stripped) < 4 or len(stripped) > 60 or _is_diagnostic_point(stripped):
        return False
    if "\n" in stripped or "\r" in stripped:
        return False
    if any(marker in stripped for marker in ("禁止", "修订说明", "质检术语", "系统信息", "主编验收", "读感目标", "因果链和章末事实")):
        return False
    if stripped.startswith(("-", "【", "当前", "通用", "正文字数", "章节阶段")):
        return False
    positive_markers = ("完成", "出现", "发现", "选择", "代价", "后果", "目标", "阻碍", "行动", "章末", "钩子", "承接", "回报")
    return any(marker in stripped for marker in positive_markers)


def _point_is_covered(text: str, point: str) -> bool:
    if point in text:
        return True
    tokens = _coverage_tokens(point)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in text)
    return hits >= max(1, min(3, len(tokens)))


def _point_has_partial_coverage(text: str, point: str) -> bool:
    return any(token in text for token in _coverage_tokens(point))


def _coverage_tokens(point: str) -> list[str]:
    raw_tokens: list[str] = []
    for part in split_points(point):
        raw_tokens.extend(re.split(r"(?:或|和|与|必须|成为|推动|引出|自然|推向|修复|保留|补清|[\\/：:（）()《》“”\"'，。！？、\\s])+", part))
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip(" ：:#0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
        for suffix in ("压力", "桥段", "恐惧", "恩怨", "关系", "后果", "承接点", "不足", "不清", "没落地"):
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                token = token[: -len(suffix)]
                break
        if len(token) >= 2 and not _is_diagnostic_point(token):
            tokens.append(token)
    return tokens


def _canon_score(text: str, canon_context: str) -> int:
    if not canon_context or "未登记 Canon" in canon_context:
        return 60
    names = []
    for line in canon_context.splitlines():
        if line.startswith("- character#"):
            parts = line.split()
            if len(parts) >= 3:
                names.append(parts[2].split("｜")[0])
    if not names:
        return 75
    hits = sum(1 for name in names if name and name in text)
    return max(45, min(100, round(55 + (hits / len(names)) * 45)))


def _marker_score(text: str, markers: list[str]) -> int:
    hits = sum(1 for marker in markers if marker in text)
    return max(45, min(100, 50 + hits * 8))


def _hook_score(text: str) -> int:
    tail = text[-300:] if len(text) > 300 else text
    full_hits = sum(1 for marker in HOOK_MARKERS if marker in text)
    tail_hits = sum(1 for marker in HOOK_MARKERS if marker in tail)
    question_bonus = 12 if any(marker in tail for marker in ("？", "?", "为什么", "是谁", "怎么会", "如果")) else 0
    cliffhanger_bonus = 10 if any(marker in tail for marker in ("响了", "亮起", "推开", "出现", "盯上", "别再", "来不及")) else 0
    return max(40, min(100, 45 + full_hits * 6 + tail_hits * 10 + question_bonus + cliffhanger_bonus))


def _prose_density_score(text: str) -> int:
    paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
    if not paragraphs:
        return 0
    unique_ratio = len(set(paragraphs)) / len(paragraphs)
    average_len = chinese_chars(text) / len(paragraphs)
    score = 55
    score += min(25, int(unique_ratio * 25))
    if average_len >= 80:
        score += 15
    elif average_len < 30:
        score -= 15
    score -= 12 * sum(1 for marker in FILLER_MARKERS if marker in text)
    return max(0, min(100, score))


def _arc_alignment_score(text: str, *, goal: str, required_beats: str, constraints: str) -> int:
    arc_points = [
        point
        for point in [goal, *split_points(required_beats), *split_points(constraints)]
        if any(marker in point for marker in ("剧情段", "阶段", "目标", "高潮", "转折", "边界", "Story Bible", "Canon"))
    ]
    if not arc_points:
        return 70
    hits = sum(1 for point in arc_points if point in text)
    partial_hits = sum(1 for point in arc_points if point not in text and any(token in text for token in split_points(point)))
    ratio = (hits + partial_hits * 0.5) / len(arc_points)
    return max(45, min(100, round(50 + ratio * 50)))


def _production_standard_score(text: str, *, min_chars: int) -> int:
    count = chinese_chars(text)
    paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
    opening = text[:350]
    tail = text[-350:] if len(text) > 350 else text
    score = 35
    if count >= min_chars:
        score += 20
    elif count >= int(min_chars * 0.85):
        score += 10
    if len(paragraphs) >= 12:
        score += 10
    elif len(paragraphs) >= 7:
        score += 6
    if any(marker in opening for marker in CONFLICT_MARKERS + ["门", "手机", "屏幕", "声音", "脚步", "订单", "短信", "血", "雨", "灯"]):
        score += 10
    if sum(1 for marker in CHOICE_COST_MARKERS if marker in text) >= 3:
        score += 10
    if any(marker in text for marker in ("他冲", "她冲", "伸手", "咬牙", "抬手", "转身", "抓起", "推开", "按下", "挡住")):
        score += 5
    if sum(1 for marker in ("对话", "问", "喊", "低声", "咬牙", "笑", "沉默", "盯着")) and "“" in text:
        score += 5
    if any(marker in tail for marker in HOOK_MARKERS + ["倒计时", "通知", "裂开", "响起", "推门", "盯上"]):
        score += 5
    return max(0, min(100, score))


def _setting_risk_score(text: str) -> int:
    penalties = sum(1 for marker in BLOCKING_CONTRADICTIONS if _has_blocking_contradiction(text, marker))
    return max(0, 100 - penalties * 35)


def _has_blocking_contradiction(text: str, marker: str) -> bool:
    if marker not in text:
        return False
    allowed_prefixes = ("不得", "不能", "不可", "禁止", "避免", "拒绝", "不许", "别让", "别把", "不要")
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return False
        prefix = text[max(0, index - 8):index]
        if not any(prefix.endswith(item) for item in allowed_prefixes):
            return True
        start = index + len(marker)


def _has_forbidden_marker(text: str, marker: str) -> bool:
    if marker not in text:
        return False
    if marker != "系统提示":
        return True
    if not _has_meta_system_prompt_leak(text):
        return False
    allowed_prefixes = ("没有", "无", "不是", "不再", "不会", "别写", "不要", "禁止", "避免", "不得", "不能", "不可", "不许")
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return False
        prefix = text[max(0, index - 10):index]
        if not any(prefix.endswith(item) for item in allowed_prefixes):
            return True
        start = index + len(marker)


def _has_meta_system_prompt_leak(text: str) -> bool:
    meta_patterns = (
        "系统提示词",
        "系统提示语",
        "系统提示、作者说明",
        "系统提示或作者说明",
        "系统提示进入正文",
        "输出系统提示",
        "不要输出系统提示",
        "禁止系统提示",
        "避免系统提示",
    )
    if any(pattern in text for pattern in meta_patterns):
        return True
    for line in (text or "").splitlines():
        stripped = line.strip(" \t-")
        if stripped.startswith(("系统提示:", "系统提示：")):
            return True
    return False


def _platform_risk_score(text: str) -> int:
    penalties = sum(1 for marker in FORBIDDEN_MARKERS if _has_forbidden_marker(text, marker))
    meta_markers = ["JSON", "数据库", "发布任务链路"]
    penalties += sum(1 for marker in meta_markers if marker in text)
    return max(0, 100 - penalties * 15)


def _opening_variety_score(text: str) -> int:
    opening = (text or "")[:700]
    score = 55
    strategy_hits = 0
    strategies = [
        ("异常细节", ("异样", "声音", "脚步", "血", "灯", "门", "痕迹", "规矩")),
        ("人物欲望", ("想要", "必须拿到", "不甘心", "等不起", "要去")),
        ("关系张力", ("看着", "盯着", "沉默", "误会", "旧账", "师", "掌柜")),
        ("利益交换", ("交易", "人情", "欠", "账", "银", "换", "价")),
        ("行动后果", ("昨夜", "伤", "疼", "追", "后果", "醒来", "没来得及")),
        ("悬念误导", ("以为", "原来", "不是", "却", "直到")),
    ]
    for _name, markers in strategies:
        if any(marker in opening for marker in markers):
            strategy_hits += 1
    score += min(30, strategy_hits * 10)
    if any(marker in opening for marker in ("世界观", "设定", "说明", "简单来说", "众所周知")):
        score -= 25
    if chinese_chars(opening) < 180:
        score -= 10
    return max(0, min(100, score))


def _causal_scene_chain_score(text: str) -> int:
    markers = ("于是", "因此", "所以", "刚", "还没", "话音未落", "下一刻", "却", "但", "因为", "只见", "逼得", "换来", "导致")
    summary_markers = ("几天后", "很快过去", "总之", "一番", "随后众人", "接下来")
    paragraphs = [item.strip() for item in (text or "").splitlines() if item.strip()]
    score = 45 + min(35, sum(1 for marker in markers if marker in text) * 5)
    if len(paragraphs) >= 10:
        score += 10
    score -= min(30, sum(text.count(marker) for marker in summary_markers) * 8)
    return max(0, min(100, score))


def _reaction_chain_score(text: str) -> int:
    stages = [
        ("感知", ("看见", "听见", "摸到", "闻到", "眼神", "声音", "脚步", "疼")),
        ("普通解释", ("以为", "只当", "原本", "按理", "大概", "可能")),
        ("证据推翻", ("却", "不是", "不对", "直到", "证据", "痕迹", "偏偏")),
        ("试探", ("试探", "开口", "伸手", "观察", "判断", "问", "交涉", "表演")),
        ("修正行动", ("决定", "转身", "改口", "收回", "换了", "咬牙", "主动", "选择")),
    ]
    hits = sum(1 for _name, markers in stages if any(marker in text for marker in markers))
    return max(30, min(100, 30 + hits * 14))


def _earned_payoff_score(text: str) -> int:
    body = text or ""
    tail = body[-500:]
    action_hits = sum(1 for marker in ("试探", "选择", "决定", "出手", "观察", "交涉", "交易", "修炼", "冒险", "判断") if marker in body)
    cost_hits = sum(1 for marker in ("代价", "后果", "反噬", "伤", "欠", "损耗", "失去", "暴露", "误会") if marker in body)
    tail_causal = any(marker in tail for marker in ("所以", "因此", "换来", "导致", "这才", "原来", "下一", "门外", "消息", "机会", "危险"))
    score = 35 + min(25, action_hits * 5) + min(25, cost_hits * 5)
    if tail_causal:
        score += 15
    if any(marker in tail for marker in ("突然出现一个危险", "莫名其妙", "毫无征兆")):
        score -= 20
    return max(0, min(100, score))
