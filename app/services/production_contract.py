from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


CONTRACT_VERSION = "production_contract_v1"


@dataclass(frozen=True)
class ContractConflictRule:
    code: str
    allow_patterns: tuple[str, ...]
    deny_patterns: tuple[str, ...]
    severity: str = "hard"
    applies_to: tuple[str, ...] = ("opening", "early_serial", "serial_progress")
    resolution: str = ""


@dataclass
class ProductionContractSnapshot:
    version: str
    chapter_type: str
    fingerprint: str
    hard_gate_names: list[str] = field(default_factory=list)
    soft_gate_names: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    removed_or_downgraded: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not any(item.get("severity") == "hard" for item in self.conflicts)

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "chapter_type": self.chapter_type,
            "fingerprint": self.fingerprint,
            "passed": self.passed,
            "hard_gate_names": self.hard_gate_names,
            "soft_gate_names": self.soft_gate_names,
            "conflicts": self.conflicts,
            "removed_or_downgraded": self.removed_or_downgraded,
        }


CONFLICT_RULES: tuple[ContractConflictRule, ...] = (
    ContractConflictRule(
        code="opening_conflict_mechanical_vs_world_promise",
        applies_to=("opening",),
        allow_patterns=("不强制第一句冲突", "不强制前300字爆发矛盾", "不强制前700字盘问", "世界承诺成立"),
        deny_patterns=("前300字内形成拉扯", "前3段内出现外部压力", "前 700 字盘问段", "第一句必须", "必须有现场牵引"),
        resolution="第一章采用 opening_world_promise_v1：世界承诺优先，冲突/钩子是手段而非机械格式。",
    ),
    ContractConflictRule(
        code="opening_setup_vs_no_setup",
        applies_to=("opening",),
        allow_patterns=("现实底座", "世界观入口", "核心卖点", "世界承诺"),
        deny_patterns=("不得先讲设定百科", "禁止完全不介绍", "不要讲设定"),
        severity="soft",
        resolution="设定必须场景化释放：禁止百科说明，但不能省略世界入口和核心卖点。",
    ),
    ContractConflictRule(
        code="game_reality_isolation_vs_leak",
        allow_patterns=("游戏与现实彻底隔离", "禁游戏修为外溢现实", "游戏修为不得外溢现实"),
        deny_patterns=("带回现实", "同步到现实", "正向映射回现实", "游戏所得能反馈到现实身体"),
        severity="hard",
        resolution="若 Story Bible 要求隔离，生成合同不得同时要求游戏能力反馈现实。",
    ),
)


def build_production_contract_snapshot(
    *,
    chapter_type: str,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    director_sheet: str = "",
    canon_context: str = "",
    previous_chapter_context: str = "",
) -> ProductionContractSnapshot:
    text = "\n".join(
        part
        for part in [
            goal,
            required_beats,
            constraints,
            director_sheet,
            canon_context,
            previous_chapter_context,
        ]
        if part
    )
    conflicts = _detect_conflicts(text, chapter_type=chapter_type)
    removed = _removed_or_downgraded(text, chapter_type=chapter_type)
    return ProductionContractSnapshot(
        version=CONTRACT_VERSION,
        chapter_type=chapter_type or "serial_progress",
        fingerprint=_fingerprint(text),
        hard_gate_names=[
            "system_artifact",
            "story_bible_logic",
            "chapter_continuity",
            "consistency_gate",
            "fanqie_hard_metrics",
        ],
        soft_gate_names=[
            "reference_craft",
            "writer_craft",
            "prose_naturalness",
            "chapter_unit_flow",
            "reading_assessment",
        ],
        conflicts=conflicts,
        removed_or_downgraded=removed,
    )


def sanitize_production_contract_text(text: str, *, chapter_type: str = "") -> str:
    value = str(text or "")
    if chapter_type != "opening":
        return value
    replacements = (
        (r"第1章硬性交付：第一句必须[^\n。]*(?:。|\n)", "第1章硬性交付：开篇必须让现实底座、世界入口和主角当下处境在场景中成立；不强制第一句冲突。\n"),
        (r"第1章硬性交付：前700字内必须出现具体外部压力或关系盘问[^\n。]*(?:。|\n)", "第1章硬性交付：前半章必须自然落地世界观入口、核心卖点和主角进入该世界的动机；不强制前700字盘问。\n"),
        (r"前3段内出现外部压力[^\n。]*(?:。|\n)?", "世界吸引力优先于硬塞危机；"),
        (r"前300字内形成拉扯", "不强制前300字爆发矛盾"),
        (r"前 700 字盘问段", "第一章世界承诺段"),
    )
    for pattern, replacement in replacements:
        value = re.sub(pattern, replacement, value)
    return value


def assert_contract_snapshot(snapshot: ProductionContractSnapshot) -> None:
    hard = [item for item in snapshot.conflicts if item.get("severity") == "hard"]
    if hard:
        codes = ", ".join(item.get("code", "") for item in hard[:5])
        raise ValueError(f"production contract conflict: {codes}")


def production_contract_for_quality(*, chapter_type: str = "") -> dict:
    return {
        "version": CONTRACT_VERSION,
        "chapter_type": chapter_type or "serial_progress",
        "hard_gate_names": [
            "system_artifact",
            "story_bible_logic",
            "chapter_continuity",
            "consistency_gate",
            "fanqie_hard_metrics",
        ],
        "soft_gate_names": [
            "reference_craft",
            "writer_craft",
            "prose_naturalness",
            "chapter_unit_flow",
            "reading_assessment",
        ],
    }


def _detect_conflicts(text: str, *, chapter_type: str) -> list[dict]:
    conflicts: list[dict] = []
    for rule in CONFLICT_RULES:
        if chapter_type and chapter_type not in rule.applies_to:
            continue
        allow_hits = _pattern_hits(text, rule.allow_patterns)
        deny_hits = _pattern_hits(text, rule.deny_patterns)
        if allow_hits and deny_hits:
            conflicts.append(
                {
                    "code": rule.code,
                    "severity": rule.severity,
                    "allow_hits": allow_hits[:6],
                    "deny_hits": deny_hits[:6],
                    "resolution": rule.resolution,
                }
            )
    return conflicts


def _removed_or_downgraded(text: str, *, chapter_type: str) -> list[str]:
    if chapter_type != "opening":
        return []
    rows: list[str] = []
    if "opening_world_promise_v1" in text:
        rows.append("opening:mechanical_conflict_rules_downgraded_to_optional")
    if "不强制前700字盘问" in text:
        rows.append("opening:interrogation_opening_not_required")
    return rows


def _pattern_hits(text: str, patterns: tuple[str, ...]) -> list[str]:
    hits: list[str] = []
    value = text or ""
    for pattern in patterns:
        if re.search(re.escape(pattern), value):
            hits.append(pattern)
    return hits


def _fingerprint(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]
