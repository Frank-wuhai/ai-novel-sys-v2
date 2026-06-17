from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ProseVoiceReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    recommendations: list[str]
    terse_dialogue_examples: list[str]
    translationese_hits: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "terse_dialogue_examples": self.terse_dialogue_examples,
            "translationese_hits": self.translationese_hits,
        }


SEVERE_TRANSLATIONESE_MARKERS = (
    "普通解释是",
    "证据推翻是",
    "这话问得",
    "这才像",
    "这就更不像",
    "为什么在这里能成立",
)
MILD_TRANSLATIONESE_MARKERS = (
    "不是因为",
    "而是",
    "所谓",
    "原来是",
    "像是在",
    "让自己看起来像",
    "也不是",
)
FUNCTIONAL_DIALOGUE_MARKERS = (
    "你到底想要什么",
    "你想要什么",
    "你知道",
    "你若",
    "你不是",
    "你是谁",
    "里面是",
    "这是",
    "是谁",
    "什么",
)


def evaluate_prose_voice(text: str) -> ProseVoiceReport:
    body = str(text or "")
    dialogue = _dialogue_lines(body)
    hits = _translationese_hits(body)
    terse_examples = _terse_dialogue_examples(dialogue)
    checks = {
        "native_chinese_flow": _native_chinese_flow_score(body, hits),
        "dialogue_fullness": _dialogue_fullness_score(dialogue),
        "character_voice": _character_voice_score(body, dialogue),
        "sentence_texture": _sentence_texture_score(body),
    }
    score = round(sum(checks.values()) / len(checks)) if checks else 0
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    recommendations = _recommendations(checks, hits=hits, terse_examples=terse_examples)
    return ProseVoiceReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        recommendations=recommendations,
        terse_dialogue_examples=terse_examples[:8],
        translationese_hits=hits[:10],
    )


def _dialogue_lines(text: str) -> list[str]:
    return [item.strip() for item in re.findall(r"[“「『](.*?)[”」』]", text or "", flags=re.S) if item.strip()]


def _translationese_hits(text: str) -> list[str]:
    hits: list[str] = []
    for marker in (*SEVERE_TRANSLATIONESE_MARKERS, *MILD_TRANSLATIONESE_MARKERS):
        if marker in (text or ""):
            hits.append(marker)
    return hits


def _native_chinese_flow_score(text: str, hits: list[str]) -> int:
    paragraphs = _paragraphs(text)
    severe_hits = sum(1 for marker in SEVERE_TRANSLATIONESE_MARKERS if marker in text)
    mild_hits = sum(1 for marker in MILD_TRANSLATIONESE_MARKERS if marker in text)
    score = 88
    score -= min(28, severe_hits * 5)
    score -= min(8, mild_hits * 1)
    score -= min(8, _repeated_sentence_starts(paragraphs, "这") * 1)
    score -= min(5, _repeated_sentence_starts(paragraphs, "不是") * 2)
    if "普通解释是" in text and "证据推翻是" in text:
        score -= 6
    return _clamp(score)


def _dialogue_fullness_score(dialogue: list[str]) -> int:
    if not dialogue:
        return 35
    lengths = [_chinese_chars(item) for item in dialogue]
    short_count = sum(1 for length in lengths if length <= 6)
    very_short_count = sum(1 for length in lengths if length <= 3)
    functional_count = sum(1 for line in dialogue if any(marker in line for marker in FUNCTIONAL_DIALOGUE_MARKERS))
    average = sum(lengths) / len(lengths)
    score = 82
    if average >= 14:
        score += 12
    elif average < 8:
        score -= 10
    score -= min(18, round((short_count / len(lengths)) * 25))
    score -= min(8, very_short_count)
    score -= min(14, functional_count * 2)
    return _clamp(score)


def _character_voice_score(text: str, dialogue: list[str]) -> int:
    if not dialogue:
        return 35
    functional = sum(1 for line in dialogue if any(marker in line for marker in FUNCTIONAL_DIALOGUE_MARKERS))
    reaction_after_dialogue = sum(1 for marker in ("怔", "笑", "沉默", "皱", "咬", "盯", "喘", "低声", "声音", "眼神") if marker in text)
    named_voice = min(4, len(set(re.findall(r"[\u4e00-\u9fff]{2,4}(?=(?:说|问|笑|皱眉|低声|抬头|看|盯|摇头|沉默))", text or ""))))
    score = 55 + named_voice * 4 + min(reaction_after_dialogue, 8) * 4
    score -= min(22, functional * 3)
    return _clamp(score)


def _sentence_texture_score(text: str) -> int:
    sentences = [item for item in re.split(r"[。！？!?]\s*", text or "") if item.strip()]
    if not sentences:
        return 0
    lengths = [_chinese_chars(item) for item in sentences]
    average = sum(lengths) / len(lengths)
    long_count = sum(1 for length in lengths if length >= 34)
    short_count = sum(1 for length in lengths if length <= 8)
    score = 70
    if 11 <= average <= 28:
        score += 12
    if long_count / len(lengths) > 0.28:
        score -= 14
    if short_count / len(lengths) > 0.38:
        score -= 14
    return _clamp(score)


def _terse_dialogue_examples(dialogue: list[str]) -> list[str]:
    examples: list[str] = []
    for line in dialogue:
        if _chinese_chars(line) <= 6 and line not in examples:
            examples.append(line)
    return examples


def _recommendations(checks: dict[str, int], *, hits: list[str], terse_examples: list[str]) -> list[str]:
    rows: list[str] = []
    if checks.get("native_chinese_flow", 100) < 60:
        rows.append("把分析腔、翻译腔句式改成中文小说里的现场表达，少用“普通解释是/证据推翻是/不是因为而是”这类显性的逻辑标签。")
    if checks.get("dialogue_fullness", 100) < 60:
        examples = "、".join(terse_examples[:5])
        rows.append(f"部分对白过短或只承担功能：{examples}。修订时让角色多说半句立场、情绪、顾虑或试探。")
    if checks.get("character_voice", 100) < 60:
        rows.append("为主要人物建立不同声线：主角、关键配角和对立方都应有各自的立场、情绪、顾虑和说话习惯。")
    if checks.get("sentence_texture", 100) < 60:
        rows.append("调整长短句节奏，避免连续短断句或连续解释长句，让叙述更像自然中文正文。")
    if hits and not rows:
        rows.append("存在少量翻译腔/分析腔痕迹，人工精修时优先改成动作、感官或人物即时反应。")
    return rows


def _paragraphs(text: str) -> list[str]:
    return [item.strip() for item in (text or "").splitlines() if item.strip()]


def _repeated_sentence_starts(paragraphs: list[str], prefix: str) -> int:
    return sum(1 for item in paragraphs if item.startswith(prefix))


def _chinese_chars(text: str) -> int:
    return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
