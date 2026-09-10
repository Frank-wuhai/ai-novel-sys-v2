from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProseNaturalnessReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    recommendations: list[str]
    dry_sentences: list[str]
    awkward_hits: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "dry_sentences": self.dry_sentences,
            "awkward_hits": self.awkward_hits,
        }


NATURAL_PARTICLES = tuple("了着过的地得吗呢吧啊呀嘛啦呗么哦嗯")
NATURAL_CONNECTIVES = (
    "但", "可", "却", "就", "才", "又", "还", "也", "都", "便", "只", "先", "再",
    "刚", "还没", "没等", "偏", "偏偏", "倒", "反倒", "不过", "其实", "毕竟",
)
CHECKLIST_MARKERS = (
    "本章", "本轮", "必须", "禁止", "目标", "阻碍", "动作", "反应", "信息增量",
    "局面变化", "承接", "钩子", "验收", "修订", "读者要", "爽点来自",
)
EXPLANATORY_MARKERS = (
    "说明", "意味着", "代表", "证明", "原因", "逻辑", "规则", "机制", "设定",
    "信息", "结果是", "问题是", "核心是",
)
DECORATIVE_MARKERS = (
    "仿佛", "似乎", "像是", "某种", "微微", "轻轻", "缓缓", "猛地", "忽然",
    "下意识", "不由", "莫名", "说不清", "难以言喻",
)
AWKWARD_PATTERNS = (
    "冷冷寒", "酸酸涩", "身子骨锈", "身体生锈", "骨头像生锈", "心里像有什么东西",
    "脑子里像有什么东西", "空气安静下来", "时间仿佛停住", "目光复杂",
)


def evaluate_prose_naturalness(text: str) -> ProseNaturalnessReport:
    body = str(text or "")
    sentences = _sentences(body)
    dialogue = _dialogue_lines(body)
    awkward_hits = _awkward_hits(body)
    dry = _dry_sentences(sentences)
    checks = {
        "natural_sentence_glue": _natural_sentence_glue_score(body, sentences),
        "non_checklist_narration": _non_checklist_narration_score(sentences),
        "diction_fit": _diction_fit_score(body, awkward_hits),
        "decorative_restraint": _decorative_restraint_score(body),
        "dialogue_particle_flow": _dialogue_particle_flow_score(dialogue),
    }
    score = round(sum(checks.values()) / len(checks)) if checks else 0
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    recommendations = _recommendations(checks, dry=dry, awkward_hits=awkward_hits)
    return ProseNaturalnessReport(
        score=_clamp(score),
        checks=checks,
        issues=issues,
        recommendations=recommendations,
        dry_sentences=dry[:8],
        awkward_hits=awkward_hits[:10],
    )


def _sentences(text: str) -> list[str]:
    rows = [item.strip() for item in re.split(r"[。！？!?]\s*", text or "") if item.strip()]
    return [item for item in rows if _chinese_chars(item) >= 4]


def _dialogue_lines(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[“「『](.*?)[”」』]", text or "", flags=re.S) if item.strip()]


def _natural_sentence_glue_score(text: str, sentences: list[str]) -> int:
    chars = max(1, _chinese_chars(text))
    particle_density = sum(text.count(item) for item in NATURAL_PARTICLES) * 1000 / chars
    connective_density = sum(text.count(item) for item in NATURAL_CONNECTIVES) * 1000 / chars
    dry_ratio = len(_dry_sentences(sentences)) / max(1, len(sentences))
    short_ratio = _short_sentence_ratio(sentences)
    score = 76
    if particle_density < 55:
        score -= 18
    elif particle_density < 70:
        score -= 8
    if connective_density < 10:
        score -= 10
    elif connective_density > 38:
        score -= 5
    if short_ratio > 0.30:
        score -= min(18, round((short_ratio - 0.30) * 80))
    score -= min(24, round(dry_ratio * 60))
    return _clamp(score)


def _short_sentence_ratio(sentences: list[str]) -> float:
    if not sentences:
        return 0.0
    lengths = [_chinese_chars(item) for item in sentences]
    return sum(1 for length in lengths if length <= 8) / len(lengths)


def _non_checklist_narration_score(sentences: list[str]) -> int:
    if not sentences:
        return 0
    hits = [sent for sent in sentences if sum(1 for marker in CHECKLIST_MARKERS if marker in sent) >= 2]
    explanatory = [sent for sent in sentences if sum(1 for marker in EXPLANATORY_MARKERS if marker in sent) >= 2]
    ratio = (len(hits) + len(explanatory)) / len(sentences)
    short_ratio = _short_sentence_ratio(sentences)
    avg_len = sum(_chinese_chars(item) for item in sentences) / max(1, len(sentences))
    score = 84 - min(42, round(ratio * 140))
    if avg_len < 16 and short_ratio > 0.30:
        score -= min(20, round((0.30 - min(0.30, 0)) + (short_ratio - 0.30) * 90))
    return _clamp(score)


def _diction_fit_score(text: str, awkward_hits: list[str]) -> int:
    vague = sum(text.count(item) for item in ("某种", "一些", "东西", "感觉", "似乎", "显得", "之类"))
    chars = max(1, _chinese_chars(text))
    negation_explain = text.count("不是") + text.count("而是")
    score = 82
    score -= min(30, len(awkward_hits) * 8)
    score -= min(18, round(vague * 1000 / chars * 2))
    if negation_explain * 1000 / chars > 2.0:
        score -= min(18, round((negation_explain * 1000 / chars - 2.0) * 4))
    return _clamp(score)


def _decorative_restraint_score(text: str) -> int:
    chars = max(1, _chinese_chars(text))
    decorative_density = (sum(text.count(item) for item in DECORATIVE_MARKERS) + max(0, text.count("像") - 4)) * 1000 / chars
    similes = len(re.findall(r"像[^，。！？!?]{2,24}(?:一样|似的|般)", text or ""))
    loose_like = max(0, text.count("像") - max(5, chars // 500))
    score = 80
    if decorative_density > 8:
        score -= min(34, round((decorative_density - 8) * 2.2))
    if similes > max(2, chars // 800):
        score -= min(20, (similes - max(2, chars // 800)) * 4)
    if loose_like:
        score -= min(18, loose_like * 2)
    return _clamp(score)


def _dialogue_particle_flow_score(dialogue: list[str]) -> int:
    if not dialogue:
        return 55
    long_dialogue = [line for line in dialogue if _chinese_chars(line) >= 8]
    if not long_dialogue:
        return 58
    natural = sum(1 for line in long_dialogue if any(item in line for item in NATURAL_PARTICLES + NATURAL_CONNECTIVES))
    ratio = natural / len(long_dialogue)
    functional = sum(1 for line in long_dialogue if any(marker in line for marker in ("你是谁", "你想要什么", "这是", "什么规则", "为什么")))
    avg_len = sum(_chinese_chars(line) for line in dialogue) / max(1, len(dialogue))
    very_short_ratio = sum(1 for line in dialogue if _chinese_chars(line) <= 6) / max(1, len(dialogue))
    score = 54 + round(ratio * 30)
    if avg_len < 10:
        score -= min(18, round((10 - avg_len) * 3))
    if very_short_ratio > 0.35:
        score -= min(16, round((very_short_ratio - 0.35) * 45))
    score -= min(16, functional * 3)
    return _clamp(score)


def _dry_sentences(sentences: list[str]) -> list[str]:
    rows: list[str] = []
    for sent in sentences:
        if _chinese_chars(sent) < 12:
            continue
        has_glue = any(item in sent for item in NATURAL_PARTICLES + NATURAL_CONNECTIVES)
        has_pulse = any(item in sent for item in ("手", "脚", "眼", "嘴", "汗", "疼", "咳", "喘", "笑", "骂", "看", "盯", "摸", "攥", "推", "拽"))
        if not has_glue and not has_pulse:
            rows.append(_one_line(sent, 80))
    return rows


def _awkward_hits(text: str) -> list[str]:
    hits = [item for item in AWKWARD_PATTERNS if item in (text or "")]
    for match in re.findall(r"[\u4e00-\u9fff]{1,3}(?:感|性|度|式|化)[的地得][\u4e00-\u9fff]{1,4}", text or ""):
        if match not in hits:
            hits.append(match)
    return hits


def _recommendations(checks: dict[str, int], *, dry: list[str], awkward_hits: list[str]) -> list[str]:
    rows: list[str] = []
    if checks.get("natural_sentence_glue", 100) < 60:
        rows.append("补自然中文里的语气和承接，不是堆连接词，而是让句子像人顺着当下反应说出来、想出来。")
    if checks.get("non_checklist_narration", 100) < 60:
        rows.append("把目标、阻碍、信息增量这类执行痕迹改成场景内动作、对话和后果。")
    if checks.get("diction_fit", 100) < 60:
        rows.append("逐句检查词义是否贴合本体，删掉词不达意、生造搭配和空泛词。")
    if checks.get("decorative_restraint", 100) < 60:
        rows.append("减少不必要的比喻、形容词和似是而非的氛围词，让关键名词和动作自己成立。")
    if checks.get("dialogue_particle_flow", 100) < 60:
        rows.append("对白允许有自然语气词、半截话和临场找补，避免角色像在报功能信息。")
    if dry and len(rows) < 4:
        rows.append("干硬句示例：" + " / ".join(dry[:3]))
    if awkward_hits and len(rows) < 4:
        rows.append("疑似别扭搭配：" + "、".join(awkward_hits[:5]))
    return rows


def _chinese_chars(text: str) -> int:
    return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")


def _one_line(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
