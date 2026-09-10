from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.chapter_units import chinese_chars
from app.services.reference_craft import build_reference_craft_cards, evaluate_reference_craft
from app.services.story_bible_logic_gate import evaluate_story_bible_logic
from app.services.system_artifact_gate import evaluate_system_artifacts


@dataclass
class UnitQualityReport:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checks: dict[str, int] = field(default_factory=dict)
    diagnostic_cards: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "warnings": self.warnings,
            "checks": self.checks,
            "diagnostic_cards": self.diagnostic_cards,
        }


def evaluate_unit_quality(
    text: str,
    *,
    unit: dict | None = None,
    prev_tail: str = "",
    unit_min: int = 240,
    unit_max: int = 520,
    story_bible_text: str = "",
    canon_context: str = "",
    constraints: str = "",
    is_first: bool = False,
    is_last: bool = False,
) -> UnitQualityReport:
    content = str(text or "").strip()
    unit = unit or {}
    issues: list[str] = []
    warnings: list[str] = []

    chars = chinese_chars(content)
    if chars < max(120, int(unit_min * 0.82)):
        issues.append(f"unit_too_short:{chars}<{unit_min}")
    if chars > int(unit_max * (1.18 if is_last else 1.08)):
        issues.append(f"unit_too_long:{chars}>{unit_max}")

    artifacts = evaluate_system_artifacts(content)
    issues.extend(f"system_artifact:{issue}" for issue in artifacts.issues)

    logic = evaluate_story_bible_logic(
        content,
        story_bible_text=story_bible_text,
        canon_context=canon_context,
        constraints=constraints,
    )
    issues.extend(f"story_bible_logic:{issue}" for issue in logic.issues)
    stage_lock_issues, stage_lock_warnings = _stage_lock_forbidden_findings(
        content,
        story_bible_text=story_bible_text,
        canon_context=canon_context,
        constraints=constraints,
    )
    issues.extend(stage_lock_issues)
    warnings.extend(stage_lock_warnings)

    if prev_tail and not _opening_links_to_prev(prev_tail, content):
        issues.append("unit_opening_detached_from_prev_tail")

    action = str(unit.get("action") or "")
    handoff = str(unit.get("handoff") or "")
    if action and not _covers_intent(content, action):
        issues.append("unit_action_underfulfilled")
    if handoff and not is_last and not _covers_intent(content[-260:], handoff):
        warnings.append("unit_handoff_weak")

    craft = evaluate_reference_craft(content)
    if craft.score < 40:
        issues.append(f"unit_reference_craft_underlearned:{craft.score}")
    elif craft.score < 55:
        warnings.append(f"unit_reference_craft_weak:{craft.score}")
    diagnostic_cards = _unit_diagnostic_cards(
        craft_checks=craft.checks,
        issues=issues,
        warnings=warnings,
        unit=unit,
        is_first=is_first,
        is_last=is_last,
    )

    checks = {
        "chars": chars,
        "reference_craft": craft.score,
        "paragraph_count": len([p for p in re.split(r"\n+", content) if p.strip()]),
        **{f"craft_{name}": value for name, value in craft.checks.items()},
    }
    return UnitQualityReport(
        passed=not issues,
        issues=issues,
        warnings=warnings,
        checks=checks,
        diagnostic_cards=diagnostic_cards,
    )


def unit_repair_contract(report: UnitQualityReport) -> str:
    if report.passed:
        return ""
    labels = {
        "unit_too_short": "补足本场景的动作、反应和后果，不要用说明凑字",
        "unit_too_long": "压缩旁枝，只保留本单元动作链和交接点",
        "system_artifact": "删除系统残留、JSON、说明文字，只保留小说正文",
        "story_bible_logic": "修正违反 Story Bible 的能力/世界规则",
        "stage_lock_forbidden_pattern": "删除当前阶段禁用的修真觉醒/功力运转描写，只用动作、疼痛、姿势和可见结果表现进步",
        "unit_opening_detached_from_prev_tail": "开头第一段必须承接前文最后动作或后果",
        "unit_action_underfulfilled": "补写本单元指定动作，不要绕开任务",
        "unit_reference_craft_underlearned": "补足场景描绘、心理链和具体动词",
    }
    lines = []
    if report.diagnostic_cards:
        lines.append("【小单元诊断卡】")
        for card in report.diagnostic_cards[:4]:
            lines.append(f"- 补{card.get('title')}：{card.get('repair_hint')}")
        lines.append("【小单元诊断卡结束】")
    for issue in report.issues[:6]:
        key = issue.split(":", 1)[0]
        lines.append(f"- {labels.get(key, issue)}")
    return "\n".join(lines)


def _unit_diagnostic_cards(
    *,
    craft_checks: dict[str, int],
    issues: list[str],
    warnings: list[str],
    unit: dict,
    is_first: bool,
    is_last: bool,
) -> list[dict]:
    cards = {card.key: card for card in build_reference_craft_cards(chapter_number=1 if is_first else None)}
    rows: list[dict] = []

    def add(key: str, reason: str, repair_hint: str) -> None:
        card = cards.get(key)
        if not card:
            return
        rows.append(
            {
                "key": key,
                "title": card.title,
                "reason": reason,
                "repair_hint": repair_hint,
                "instruction": card.instruction,
                "anchor": card.anchors[0] if card.anchors else "",
            }
        )

    scene = int(craft_checks.get("scene_craft") or 0)
    psychology = int(craft_checks.get("psychological_chain") or 0)
    rhetoric = int(craft_checks.get("rhetoric_specificity") or 0)
    diction = int(craft_checks.get("diction_vividness") or 0)
    reaction = int(craft_checks.get("action_reaction_chain") or 0)
    if scene < 55:
        add(
            "scene_description",
            f"scene_craft={scene}",
            "补空间边界、光源/声音/气味、人物站位和一个可互动物件，再让动作发生。",
        )
    if psychology < 55:
        add(
            "psychological_chain",
            f"psychological_chain={psychology}",
            "补身体反应->误判/判断->迟疑->选择动作，避免直接写“震惊/害怕/复杂”。",
        )
    if rhetoric < 55 or diction < 55:
        add(
            "rhetoric_diction",
            f"rhetoric={rhetoric},diction={diction}",
            "用当场物象生成一个具体比喻，并把泛动词换成能改变局面的动作词。",
        )
    if reaction < 55:
        add(
            "rhythm_restraint",
            f"action_reaction_chain={reaction}",
            "动作后补对方反应、环境后果或局面变化；短句只用在转折收束处。",
        )
    if any(issue.startswith("unit_opening_detached") for issue in issues):
        add(
            "causal_hook",
            "unit_opening_detached_from_prev_tail",
            "首段直接承接上一单元最后动作或后果，用新证据/新代价推动下一步。",
        )
    if any(issue.startswith("unit_action_underfulfilled") for issue in issues):
        action = str(unit.get("action") or "").strip()
        add(
            "causal_hook",
            "unit_action_underfulfilled",
            f"把本单元动作写成场景内选择和后果：{action or '补清本单元指定行动'}。",
        )
    if is_last and any("hook" in item or "handoff" in item for item in [*issues, *warnings]):
        add(
            "causal_hook",
            "last_unit_hook_or_handoff_weak",
            "章末钩子必须来自未完成选择、代价、异常证据或新问题，不输出说明文字。",
        )
    seen = set()
    unique = []
    for row in rows:
        key = row["key"]
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique[:5]


def _stage_lock_forbidden_findings(
    text: str,
    *,
    story_bible_text: str = "",
    canon_context: str = "",
    constraints: str = "",
) -> tuple[list[str], list[str]]:
    rule_text = "\n".join([story_bible_text or "", canon_context or "", constraints or ""])
    if not any(marker in rule_text for marker in ("凡人阶段", "阶段锁", "禁丹田", "禁经脉", "禁掌心发热", "禁经脉热流")):
        return [], []
    content = text or ""
    issues: list[str] = []
    warnings: list[str] = []
    hard_patterns = (
        r"(热流|真气|内力|气感).{0,18}(涌|窜|滚|游|钻|冲|入|进|过|走|运转|流转|扩散)",
        r"(涌|窜|滚|游|钻|冲|入|进|过|走|运转|流转|扩散).{0,18}(热流|真气|内力|气感)",
        r"(丹田|经脉).{0,18}(热流|真气|内力|气感|运转|流转|鼓荡|震动|发热|发烫|苏醒)",
        r"(热流|真气|内力|气感).{0,18}(丹田|经脉)",
        r"(修为|功力).{0,18}(提升|暴涨|突破|运转|外溢)",
        r"(传功|灌体|符咒显形|修真觉醒)",
    )
    for pattern in hard_patterns:
        match = re.search(pattern, content)
        if match:
            issues.append(f"stage_lock_forbidden_pattern:{match.group(0)[:24]}")
            break

    soft_terms = ("丹田", "经脉", "真气", "内力", "热流", "气感", "符咒", "修为")
    soft_hits = [term for term in soft_terms if term in content]
    if soft_hits and not issues:
        warnings.append(f"stage_lock_sensitive_terms:{'/'.join(soft_hits[:6])}")
    return issues, warnings


REALITY_LINK_TERMS = ("现实", "出租屋", "硬板床", "头盔", "床", "手机", "房租", "矿泉水")
JIANGHU_LINK_TERMS = ("老大夫", "药铺", "山口", "镖行", "客栈", "道观", "城门", "山道")


def _opening_links_to_prev(prev_tail: str, text: str) -> bool:
    opening = (text or "")[:220]
    prev = prev_tail or ""
    if any(term in prev for term in REALITY_LINK_TERMS):
        return any(term in opening for term in REALITY_LINK_TERMS)
    if any(term in prev for term in JIANGHU_LINK_TERMS):
        return any(term in opening for term in JIANGHU_LINK_TERMS) or any(
            marker in opening for marker in ("刚才", "方才", "还没", "没等", "于是", "接着")
        )
    return any(marker in opening for marker in ("刚才", "方才", "还没", "没等", "于是", "接着", "那", "这"))


def _covers_intent(text: str, intent: str) -> bool:
    tokens = _intent_tokens(intent)
    if not tokens:
        return True
    hits = sum(1 for token in tokens if token in (text or ""))
    return hits >= max(1, min(3, len(tokens) // 2))


def _intent_tokens(text: str) -> list[str]:
    raw = re.findall(r"[\u4e00-\u9fff]{2,}", str(text or ""))
    stop = {"主角", "场景", "这个", "一个", "遇到", "什么", "此刻", "真实", "情绪", "结束", "钩子"}
    tokens: list[str] = []
    for item in raw:
        if item in stop:
            continue
        if len(item) > 6:
            tokens.extend(item[i : i + 2] for i in range(0, len(item) - 1, 2))
        else:
            tokens.append(item)
    return list(dict.fromkeys(tokens))[:8]
