from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class DesignQualityReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    recommendations: list[str]
    new_terms: list[str]
    ungrounded_terms: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "recommendations": self.recommendations,
            "new_terms": self.new_terms,
            "ungrounded_terms": self.ungrounded_terms,
        }


VISUAL_MARKERS = (
    "光",
    "影",
    "灯",
    "火",
    "雨",
    "雾",
    "风",
    "血",
    "灰",
    "青",
    "黑",
    "白",
    "红",
    "铜",
    "铁",
    "木",
    "石",
    "纸",
    "布",
    "裂",
    "旧",
    "湿",
    "冷",
)
SPATIAL_MARKERS = (
    "左",
    "右",
    "前",
    "后",
    "上",
    "下",
    "里",
    "外",
    "门口",
    "墙角",
    "地上",
    "身后",
    "眼前",
    "深处",
    "裂缝",
    "石门",
    "石廊",
    "木榻",
)
OBJECT_MARKERS = (
    "账册",
    "油布包",
    "铜铃",
    "铁杖",
    "药篓",
    "银针",
    "药碗",
    "草鞋",
    "陶罐",
    "蜡封",
    "血印",
    "青瘴灰",
    "火把",
    "草药",
    "手机",
    "屏幕",
    "短信",
    "录音",
    "栏杆",
    "水箱",
    "玻璃",
    "碎玻璃",
    "车门",
    "车窗",
    "站牌",
    "监控",
    "摄像头",
    "电梯",
    "地铁",
    "背包",
    "钥匙",
    "门锁",
    "纸条",
    "照片",
    "芯片",
    "终端",
    "手环",
    "药瓶",
    "工牌",
    "雨伞",
)
FUNCTION_MARKERS = (
    "用来",
    "能",
    "会",
    "开",
    "锁",
    "救",
    "挡",
    "藏",
    "引",
    "吊",
    "换",
    "欠",
    "付",
    "烧",
    "毒",
    "封",
)
GROUNDING_MARKERS = (
    "因",
    "旧",
    "二十年",
    "十年",
    "血",
    "裂",
    "铜",
    "铁",
    "青",
    "黑",
    "药",
    "账",
    "盐",
    "镖",
    "欠",
    "仇",
    "门",
    "谷",
    "印",
    "铃",
    "线",
    "封",
    "用",
    "救",
    "毒",
    "开",
)
ABSTRACT_MARKERS = (
    "江湖不是",
    "真实得",
    "旧年的账",
    "外面的恩怨",
    "这江湖",
    "规矩",
    "因果",
    "命运",
    "秘密",
)
KNOWN_TERMS = (
    "青河剑派",
    "梅家镖局",
    "旧药王谷",
    "旧药篓道",
    "梅家血印",
    "梅家账册",
    "锈铜铃",
    "黑虎帮",
    "漕帮",
    "梅谷",
    "内谷",
    "梅家",
    "梅引",
    "青瘴灰",
    "旧债窟",
)
TERM_CANDIDATE_PATTERNS = (
    r"[\u4e00-\u9fff]{1,8}剑派",
    r"[\u4e00-\u9fff]{1,8}镖局",
    r"[\u4e00-\u9fff]{1,8}盐道",
    r"[\u4e00-\u9fff]{1,8}药王谷",
    r"[\u4e00-\u9fff]{1,8}药篓道",
    r"[\u4e00-\u9fff]{1,8}血印",
    r"[\u4e00-\u9fff]{1,8}账册",
    r"[\u4e00-\u9fff]{1,8}铜铃",
)


def evaluate_design_quality(text: str, *, canon_context: str = "") -> DesignQualityReport:
    body = str(text or "")
    terms = _extract_terms(body)
    new_terms = [term for term in terms if term not in (canon_context or "")]
    ungrounded = [term for term in new_terms if not _term_grounded(body, term)]
    checks = {
        "visual_staging": _visual_staging_score(body),
        "spatial_continuity": _spatial_continuity_score(body),
        "object_functionality": _object_functionality_score(body),
        "designed_nomenclature": _designed_nomenclature_score(new_terms, ungrounded),
        "imageable_paragraphs": _imageable_paragraph_score(body),
    }
    score = round(sum(checks.values()) / len(checks))
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    recommendations = _recommendations(checks, new_terms=new_terms, ungrounded=ungrounded)
    return DesignQualityReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        recommendations=recommendations,
        new_terms=new_terms[:16],
        ungrounded_terms=ungrounded[:12],
    )


def _visual_staging_score(text: str) -> int:
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return 0
    visual_count = sum(1 for item in paragraphs if _has_any(item, VISUAL_MARKERS))
    spatial_count = sum(1 for item in paragraphs if _has_any(item, SPATIAL_MARKERS))
    ratio = (visual_count + spatial_count) / (len(paragraphs) * 2)
    score = 35 + round(ratio * 55)
    score -= min(18, sum(text.count(marker) for marker in ABSTRACT_MARKERS) * 2)
    return _clamp(score)


def _spatial_continuity_score(text: str) -> int:
    unique = sum(1 for marker in SPATIAL_MARKERS if marker in text)
    repeated = sum(1 for marker in SPATIAL_MARKERS if text.count(marker) >= 2)
    return _clamp(35 + min(unique, 10) * 4 + min(repeated, 6) * 3)


def _object_functionality_score(text: str) -> int:
    objects = sum(1 for marker in OBJECT_MARKERS if marker in text)
    functions = sum(1 for marker in FUNCTION_MARKERS if marker in text)
    score = 35 + min(objects, 9) * 5 + min(functions, 8) * 3
    if objects >= 5 and functions < 3:
        score -= 15
    return _clamp(score)


def _designed_nomenclature_score(new_terms: list[str], ungrounded_terms: list[str]) -> int:
    if not new_terms:
        return 80
    overload_penalty = max(0, len(new_terms) - 5) * 5
    ungrounded_penalty = len(ungrounded_terms) * 6
    grounding_bonus = round(((len(new_terms) - len(ungrounded_terms)) / len(new_terms)) * 25)
    return _clamp(65 + grounding_bonus - overload_penalty - ungrounded_penalty)


def _imageable_paragraph_score(text: str) -> int:
    paragraphs = _paragraphs(text)
    if not paragraphs:
        return 0
    imageable = 0
    for item in paragraphs:
        has_object_or_body = _has_any(item, OBJECT_MARKERS) or _has_any(item, ("手", "眼", "脸", "喉", "膝", "肩", "背", "掌", "血"))
        has_visual_or_space = _has_any(item, VISUAL_MARKERS) or _has_any(item, SPATIAL_MARKERS)
        if has_object_or_body and has_visual_or_space:
            imageable += 1
    ratio = imageable / len(paragraphs)
    return _clamp(30 + round(ratio * 70))


def _extract_terms(text: str) -> list[str]:
    terms: list[str] = []
    for term in KNOWN_TERMS:
        if term in (text or ""):
            terms.append(term)
    for pattern in TERM_CANDIDATE_PATTERNS:
        for match in re.finditer(pattern, text or ""):
            term = _normalize_term(match.group(0))
            if term and term not in terms and not _generic_term(term):
                terms.append(term)
    return terms


def _normalize_term(candidate: str) -> str:
    for term in sorted(KNOWN_TERMS, key=len, reverse=True):
        if term in candidate:
            return term

    for suffix in ("剑派", "镖局", "盐道"):
        if candidate.endswith(suffix):
            prefix = candidate[: -len(suffix)]
            prefix = _trim_prefix_context(prefix)
            if len(prefix) < 2 or prefix.startswith(("一", "二", "三", "两", "几")):
                return ""
            return (prefix[-4:] + suffix)[-8:]

    for suffix in ("药王谷", "药篓道", "血印"):
        if candidate.endswith(suffix):
            prefix = _trim_prefix_context(candidate[: -len(suffix)])
            if len(prefix) < 1:
                return ""
            return (prefix[-3:] + suffix)[-8:]

    if candidate.endswith("账册"):
        prefix = _trim_prefix_context(candidate[:-2])
        if "梅家" in prefix:
            return "梅家账册"
        if prefix.endswith(("旧", "血", "密", "药")):
            return prefix[-1:] + "账册"
        return ""

    if candidate.endswith("铜铃"):
        prefix = _trim_prefix_context(candidate[:-2])
        if prefix.endswith(("锈", "旧", "乌")):
            return prefix[-1:] + "铜铃"
        return ""

    return ""


def _trim_prefix_context(prefix: str) -> str:
    value = prefix
    for marker in ("这边有", "你不是", "你是", "下面是", "的人在搜", "牵连的是", "知道", "替", "走", "把", "交", "说", "看", "来", "和", "为", "是"):
        if marker in value:
            value = value.split(marker)[-1]
    return value


def _generic_term(term: str) -> bool:
    generic = {"不是梅家", "这些梅家", "那本账册", "这本账册", "三家镖局", "两处盐道"}
    return term in generic or term.startswith(("不是", "这里", "那个", "那只", "这本", "一只", "三只", "三家", "两处"))


def _term_grounded(text: str, term: str) -> bool:
    index = (text or "").find(term)
    if index < 0:
        return False
    start = max(0, index - 70)
    end = min(len(text), index + len(term) + 90)
    window = text[start:end]
    return _has_any(window, GROUNDING_MARKERS) and (
        _has_any(window, VISUAL_MARKERS)
        or _has_any(window, OBJECT_MARKERS)
        or _has_any(window, FUNCTION_MARKERS)
        or _has_any(window, ("害", "欠", "救", "追", "灭口", "私吞", "托", "死"))
    )


def _recommendations(checks: dict[str, int], *, new_terms: list[str], ungrounded: list[str]) -> list[str]:
    rows: list[str] = []
    if checks.get("visual_staging", 100) < 60 or checks.get("imageable_paragraphs", 100) < 60:
        rows.append("重写时先建立可画出来的镜头：人物站位、光源、空间边界、关键物件和动作轨迹都要稳定。")
    if checks.get("spatial_continuity", 100) < 60:
        rows.append("减少抽象推进，用门、墙、桌、裂缝、灯火、距离和方向承接每次动作。")
    if checks.get("object_functionality", 100) < 60:
        rows.append("关键物件不能只当名词出现，要写清外观、用途、代价、谁在乎它以及它如何改变局面。")
    if checks.get("designed_nomenclature", 100) < 60:
        terms = "、".join(ungrounded[:5] or new_terms[:5])
        rows.append(f"本章新专名过多或缺少设计锚点：{terms}。删减或补足来源、功能、利益关系和可见证据。")
    return rows


def _paragraphs(text: str) -> list[str]:
    return [item.strip() for item in (text or "").splitlines() if item.strip()]


def _has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in (text or "") for marker in markers)


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
