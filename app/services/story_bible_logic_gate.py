from __future__ import annotations

import re
from dataclasses import dataclass, field


REALITY_TERMS = ("现实", "出租屋", "硬板床", "床上", "头盔", "电脑", "手机", "视网膜", "工地", "房租")
POWER_TERMS = ("热流", "真气", "内力", "丹田", "经脉", "修为", "口诀", "灵气", "掌心发热", "气感")
REALITY_TRANSFER_TERMS = ("发力链条", "肌肉记忆", "刻进现实", "刻在现实", "神经反馈溢出", "现实神经", "反向刻印")
META_WORLD_TERMS = ("NPC", "世界频道", "萌新求带", "刷城外", "任务面板", "任务栏", "玩家频道", "公会频道")

ISOLATION_RULE_MARKERS = (
    "游戏与现实彻底隔离",
    "禁游戏修为外溢现实",
    "游戏修为不得外溢现实",
    "游戏能力不得外溢现实",
    "游戏所得不得外溢现实",
    "现实里他还是那个底层小人物",
    "禁现实灵气",
    "禁现实修真",
    "禁掌心发热",
    "禁经脉热流",
)
GLOBAL_POWER_BAN_MARKERS = (
    "禁掌心发热",
    "禁经脉热流",
    "禁章末硬塞修真觉醒",
    "修真元素在凡人阶段:绝不出现",
    "修真元素在凡人阶段：绝不出现",
    "凡人阶段绝不修真",
)
TRUE_WORLD_MARKERS = ("有血有肉的异世界", "不是任务 NPC", "不是任务NPC", "真实异世界", "写实蜀山")


@dataclass
class StoryBibleLogicReport:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "warnings": self.warnings,
            "examples": self.examples[:8],
        }


def evaluate_story_bible_logic(
    text: str,
    *,
    story_bible_text: str = "",
    canon_context: str = "",
    constraints: str = "",
) -> StoryBibleLogicReport:
    content = text or ""
    rule_source = "\n".join([story_bible_text or "", canon_context or "", constraints or ""])
    issues: list[str] = []
    warnings: list[str] = []
    examples: list[str] = []

    deprecated_hits = _deprecated_content_hits(content, _extract_deprecated_terms(rule_source))
    if deprecated_hits:
        issues.append("deprecated_pollution:" + "/".join(deprecated_hits[:6]))
        examples.extend(_term_examples(content, deprecated_hits[:6]))

    if _has_any(rule_source, ISOLATION_RULE_MARKERS):
        for sentence in _sentences(content):
            if _has_any(sentence, REALITY_TERMS) and (_has_any(sentence, POWER_TERMS) or _has_any(sentence, REALITY_TRANSFER_TERMS)):
                issues.append("reality_power_leak")
                examples.append(sentence)
                break
        for pattern in (
            r"带回现实",
            r"跟着.{0,12}回了现实",
            r"(伤势|淤青|伤口|痛感|掌风|刀伤|拳伤).{0,24}(刻在现实|刻进现实|留在现实|跟到现实|带回现实)",
            r"(游戏里|游戏中).{0,30}(发力链条|肌肉记忆|伤势|淤青|痛感).{0,30}(现实|出租屋|身体|右手|神经)",
            r"(发力链条|肌肉记忆|神经反馈|反向刻印).{0,30}(现实|出租屋|身体|右手|神经)",
            r"现实.{0,20}(热流|真气|内力|丹田|经脉|修为|灵气)",
            r"现实.{0,20}(身体|右手|掌心|小臂|经脉|丹田).{0,20}(热流|真气|内力|气感|暖流)",
            r"(这不是|不是).{0,12}游戏.{0,20}(身体|现实).{0,20}(热流|真气|内力|一股气|气感|暖流)",
            r"(游戏里|游戏中).{0,20}(练成|修成|获得|得到).{0,20}(现实|出租屋|身体|右手|掌心|小臂|丹田|经脉)",
            r"(游戏里|游戏中).{0,20}(功法|吐纳法|口诀|修为|内力|真气|气感).{0,30}(现实|出租屋|身体|右手|掌心|小臂|丹田|经脉)",
            r"(游戏里|游戏中).{0,20}(带出来|带回|带到现实)",
        ):
            match = re.search(pattern, content)
            if match:
                excerpt = _excerpt(content, match.start(), match.end())
                if re.search(r"(不像|不是).{0,12}从游戏里带出来", excerpt):
                    continue
                if "reality_power_leak" not in issues:
                    issues.append("reality_power_leak")
                examples.append(excerpt)
                break

    if _has_any(rule_source, GLOBAL_POWER_BAN_MARKERS):
        for pattern in (
            r"掌心.{0,8}(发热|滚过|热流)",
            r"掌心.{0,8}发烫.{0,12}(像|仿佛|好像|似乎).{0,12}(火|热流|气|电|苏醒|活物)",
            r"手心.{0,8}发热",
            r"手心.{0,8}发烫.{0,12}(像|仿佛|好像|似乎).{0,12}(火|热流|气|电|苏醒|活物)",
            r"(脚底|拳头|小腹|胸口).{0,8}(忽然一热|一热|发烫|发热)",
            r"热流.{0,20}(掌心|拳面|小腿|腰胯|脊背|右臂|手臂|血管|身体|胸口)",
            r"(丹田|经脉).{0,20}(热流|真气|内力|气感|运转|缓缓|窜)",
            r"骨头里.{0,12}(苏醒|发热|发烫)",
            r"感觉得到气",
        ):
            match = re.search(pattern, content)
            if match:
                excerpt = _excerpt(content, match.start(), match.end())
                if _physical_heat_context(excerpt):
                    continue
                issues.append("forbidden_power_body_sensation")
                examples.append(excerpt)
                break

    if _has_any(rule_source, TRUE_WORLD_MARKERS):
        meta_hits = [term for term in META_WORLD_TERMS if term in content]
        if meta_hits:
            issues.append(f"game_meta_intrusion:{'/'.join(meta_hits[:4])}")
            examples.extend(_term_examples(content, meta_hits[:4]))

    return StoryBibleLogicReport(
        passed=not issues,
        issues=issues,
        warnings=warnings,
        examples=examples,
    )


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term and term in (text or "") for term in terms)


def _extract_deprecated_terms(rule_source: str) -> list[str]:
    rows: list[str] = []
    for line in (rule_source or "").splitlines():
        stripped = line.strip().lstrip("-•* ").strip()
        if not stripped:
            continue
        if not (
            stripped.startswith("废弃污染:")
            or stripped.startswith("废弃污染：")
            or stripped.startswith("deprecated_pollution:")
            or stripped.startswith("deprecated_pollution：")
        ):
            continue
        payload = re.split(r"[:：]", stripped, 1)[1] if re.search(r"[:：]", stripped) else ""
        for piece in re.split(r"[；;、,，\n]+", payload):
            item = _clean_deprecated_term(piece)
            if item:
                rows.append(item)
    return _dedupe(rows)


def _clean_deprecated_term(text: str) -> str:
    value = re.sub(r"《([^》]{2,24})》", r"\1", text or "")
    value = re.split(r"不得|禁止|作废|废弃|污染|误写|作为", value, 1)[0]
    value = value.strip(" ：:，,；;-。 ")
    if 2 <= len(value) <= 18:
        return value
    return ""


def _deprecated_content_hits(content: str, terms: list[str]) -> list[str]:
    hits: list[str] = []
    value = content or ""
    for term in terms:
        if term and term in value:
            hits.append(term)
            continue
        if _compound_deprecated_hit(value, term):
            hits.append(term)
    return _dedupe(hits)


def _compound_deprecated_hit(content: str, term: str) -> bool:
    if term == "五十块旧头盔":
        return bool(re.search(r"五十块.{0,80}(旧盔|旧头盔|头盔|全感头盔|接驳头盔)", content))
    if term == "胶布保险丝修脑机":
        return all(item in content for item in ("胶布", "保险丝")) and any(item in content for item in ("头盔", "脑机", "接驳"))
    if term == "房东抢设备抵租":
        return bool(re.search(r"房东.{0,60}(抢|拿|扣|搬).{0,60}(设备|头盔|电脑).{0,60}(抵租|抵房租|房租)", content))
    return False


def _physical_heat_context(text: str) -> bool:
    return any(marker in (text or "") for marker in ("铜钱硌", "摩擦", "烫伤", "火炉", "热汤", "晒得", "磨得", "攥得"))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"(?<=[。！？!?])|\n+", text or "") if part.strip()]


def _term_examples(text: str, terms: list[str]) -> list[str]:
    examples: list[str] = []
    for term in terms:
        idx = text.find(term)
        if idx >= 0:
            examples.append(_excerpt(text, idx, idx + len(term)))
    return examples


def _excerpt(text: str, start: int, end: int, radius: int = 36) -> str:
    left = max(0, start - radius)
    right = min(len(text), end + radius)
    return re.sub(r"\s+", " ", text[left:right]).strip()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        rows.append(item)
    return rows
