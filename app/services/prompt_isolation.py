from __future__ import annotations

import re
from dataclasses import dataclass


STALE_PROMPT_MARKERS = (
    "依据质检报告",
    "上次质检分数",
    "采纳二审建议",
    "修复质检问题",
    "执行修订合同",
    "修订合同:",
    "修订合同：",
    "原始机器修订建议",
    "验收清单",
    "reading_assessment_auto_quality",
    "system_revision_budget_recovery",
    "clean_rebuild_contract",
    "失败结构不得沿用",
    "质检报告 #",
    "[LONG_TERM_STATE]",
)
FORBIDDEN_POSITIVE_PROMPT_PATTERNS = (
    r"(游戏里|游戏中).{0,30}(发力链条|肌肉记忆|握力|反应|伤势|淤青|掌风|痛感).{0,30}(现实|出租屋|身体|神经|右手)",
    r"(发力链条|肌肉记忆|神经反馈|反向刻印).{0,30}(现实|出租屋|身体|神经)",
    r"(论坛|玩家|NPC|任务面板|系统提示).{0,20}(必须|要|作为|触发|推进|出现|写出)",
)


@dataclass(frozen=True)
class PromptIsolationPacket:
    goal: str
    required_beats: str
    constraints: str
    canon_context: str
    previous_chapter_context: str
    warnings: list[str]


def isolate_generation_inputs(
    *,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    canon_context: str = "",
    previous_chapter_context: str = "",
    strict_authority_fields: bool = True,
) -> PromptIsolationPacket:
    warnings: list[str] = []
    authority_text = "\n".join([constraints or "", canon_context or ""])
    clean_goal = strip_stale_prompt_lines(goal, field="goal", warnings=warnings, authority_text=authority_text)
    clean_required = strip_stale_prompt_lines(
        required_beats,
        field="required_beats",
        warnings=warnings,
        authority_text=authority_text,
    )
    clean_previous = strip_stale_prompt_lines(
        previous_chapter_context,
        field="previous_chapter_context",
        warnings=warnings,
        authority_text=authority_text,
    )
    audit_prompt_field("goal", clean_goal)
    audit_prompt_field("required_beats", clean_required)
    audit_prompt_field("previous_chapter_context", clean_previous)
    if strict_authority_fields:
        audit_prompt_field("constraints", constraints)
        audit_prompt_field("canon_context", canon_context)
    return PromptIsolationPacket(
        goal=clean_goal,
        required_beats=clean_required,
        constraints=constraints or "",
        canon_context=canon_context or "",
        previous_chapter_context=clean_previous,
        warnings=warnings,
    )


def strip_stale_prompt_lines(
    text: str,
    *,
    field: str,
    warnings: list[str],
    authority_text: str = "",
) -> str:
    kept: list[str] = []
    skipping_contract = False
    skipping_block = ""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if skipping_block:
            if line == f"[/{skipping_block}]":
                warnings.append(f"{field}:removed_stale_block_end:{line[:40]}")
                skipping_block = ""
            else:
                warnings.append(f"{field}:removed_stale_block_child:{line[:40]}")
            continue
        block_name = stale_block_name(line)
        if block_name:
            warnings.append(f"{field}:removed_stale_block_start:{line[:40]}")
            skipping_block = block_name
            skipping_contract = False
            continue
        if line_has_stale_prompt_marker(line):
            warnings.append(f"{field}:removed_stale_line:{line[:40]}")
            skipping_contract = True
            continue
        if line_conflicts_with_authority(line, authority_text):
            warnings.append(f"{field}:removed_authority_conflict:{line[:40]}")
            skipping_contract = False
            continue
        if skipping_contract and looks_like_stale_contract_child(line):
            warnings.append(f"{field}:removed_stale_contract_child:{line[:40]}")
            continue
        skipping_contract = False
        kept.append(line)
    return "\n".join(kept)


def line_has_stale_prompt_marker(line: str) -> bool:
    return any(marker in (line or "") for marker in STALE_PROMPT_MARKERS)


def stale_block_name(line: str) -> str:
    stripped = (line or "").strip()
    if stripped == "[LONG_TERM_STATE]":
        return "LONG_TERM_STATE"
    return ""


def looks_like_stale_contract_child(line: str) -> bool:
    if any(marker in (line or "") for marker in ("真实结尾", "上一章", "前章", "上章", "正文事实")):
        return False
    return line.startswith(("-", "•")) or "修复" in line or "验收" in line or "score" in line or "weak_" in line


def audit_prompt_field(field: str, text: str) -> None:
    value = text or ""
    for pattern in FORBIDDEN_POSITIVE_PROMPT_PATTERNS:
        match = re.search(pattern, value)
        if match:
            if _positive_match_is_limited_authority_context(value, match):
                continue
            raise ValueError(
                f"prompt isolation failed: {field} contains positive contaminated instruction: {match.group(0)[:80]}"
            )


def _positive_match_is_limited_authority_context(value: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - 40)
    end = min(len(value), match.end() + 60)
    context = value[start:end]
    return any(
        marker in context
        for marker in (
            "允许但限用",
            "长期伏线",
            "克制",
            "不得进入",
            "不能替代",
            "禁止误写",
            "不得把",
            "不能把",
            "只作为",
            "现实侧误判外壳",
            "像真人",
            "真实人物",
            "NPC真实化",
            "NPC必须按真实人物",
            "NPC像真人",
        )
    )


def line_conflicts_with_authority(line: str, authority_text: str) -> bool:
    """Drop stale memory/brief lines that ask for a payoff the current authority forbids."""
    authority = authority_text or ""
    if not _authority_forbids_reality_power(authority):
        return False
    value = line or ""
    conflict_patterns = (
        r"(热流|真气|内力|气感).{0,30}(还在|动了|运转|小臂|手心|掌心|胸腔|现实|出租屋|身体)",
        r"(同步|映射|回传|带回|外溢).{0,30}(现实|身体|右手|指关节|掌心|手心|副作用|回报|收益)",
        r"(现实|出租屋|身体|右手|指关节|掌心|手心).{0,30}(热流|真气|内力|气感|发麻|副作用|同步|映射|回传|回报)",
        r"(用意念|游戏里的法子).{0,30}(热流|真气|内力|气感)",
    )
    return any(re.search(pattern, value) for pattern in conflict_patterns)


def _authority_forbids_reality_power(authority_text: str) -> bool:
    authority = authority_text or ""
    return any(
        marker in authority
        for marker in (
            "游戏与现实彻底隔离",
            "禁游戏修为外溢现实",
            "游戏修为不得外溢现实",
            "游戏能力不得外溢现实",
            "凡人阶段绝不修真",
            "禁掌心发热",
            "禁经脉热流",
            "禁现实灵气",
            "禁现实修真",
        )
    )
