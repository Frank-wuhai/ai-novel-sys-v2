from __future__ import annotations

import json
from dataclasses import dataclass


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
HOOK_MARKERS = ["钩子", "秘密", "发现", "转折", "章末", "疑问", "源头", "倒影", "异象"]
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
        if marker in text:
            issues.append(f"forbidden_marker: {marker}")
    for marker in BLOCKING_CONTRADICTIONS:
        if _has_blocking_contradiction(text, marker):
            issues.append(f"setting_contradiction: {marker}")

    dimensions = {
        "basic_publishability": _basic_publishability_score(count, min_chars, max_chars, text),
        "brief_coverage": _coverage_score(text, [goal, *split_points(required_beats), *split_points(constraints)]),
        "canon_consistency": _canon_score(text, canon_context),
        "reader_momentum": _marker_score(text, MOMENTUM_MARKERS),
        "conflict_pressure": _marker_score(text, CONFLICT_MARKERS),
        "choice_and_cost": _marker_score(text, CHOICE_COST_MARKERS),
        "hook_strength": _hook_score(text),
        "prose_density": _prose_density_score(text),
        "arc_alignment": _arc_alignment_score(text, goal=goal, required_beats=required_beats, constraints=constraints),
        "setting_risk": _setting_risk_score(text),
        "platform_risk": _platform_risk_score(text),
    }
    for name in ("conflict_pressure", "choice_and_cost", "hook_strength", "prose_density", "arc_alignment"):
        if dimensions[name] < 50:
            issues.append(f"weak_narrative_dimension: {name}={dimensions[name]}")
    blocking = [issue for issue in issues if issue.startswith(("forbidden_marker", "setting_contradiction"))]
    score = round(sum(dimensions.values()) / len(dimensions))
    if count < min_chars:
        score = min(score, dimensions["basic_publishability"])
    if blocking:
        score = min(score, 40)
    score = max(0, min(100, score))
    passed = not issues and score >= 70 and min(dimensions.values()) >= 50
    report = json.dumps(
        {
            "status": "PASS" if passed else "FAIL",
            "score": score,
            "chinese_chars": count,
            "dimensions": dimensions,
            "issues": issues,
            "thresholds": {
                "pass_score": 70,
                "min_dimension": 50,
                "min_chars": min_chars,
                "max_chars": max_chars,
            },
        },
        ensure_ascii=False,
    )
    return QualityResult(passed=passed, score=score, report=report, dimensions=dimensions, issues=issues)


def split_points(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _basic_publishability_score(text_len: int, min_chars: int, max_chars: int, text: str) -> int:
    score = 100
    if text_len < min_chars:
        score -= min(70, (min_chars - text_len) // 10)
    if text_len > max_chars:
        score -= min(50, (text_len - max_chars) // 50)
    score -= 25 * sum(1 for marker in FORBIDDEN_MARKERS if marker in text)
    return max(0, min(100, score))


def _coverage_score(text: str, points: list[str]) -> int:
    meaningful = [point for point in points if len(point) >= 2]
    if not meaningful:
        return 70
    hits = sum(1 for point in meaningful if point in text)
    partial_hits = sum(1 for point in meaningful if point not in text and any(token in text for token in split_points(point)))
    ratio = (hits + partial_hits * 0.5) / len(meaningful)
    return max(35, min(100, round(45 + ratio * 55)))


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
    return max(40, min(100, 45 + full_hits * 6 + tail_hits * 10))


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


def _setting_risk_score(text: str) -> int:
    penalties = sum(1 for marker in BLOCKING_CONTRADICTIONS if _has_blocking_contradiction(text, marker))
    return max(0, 100 - penalties * 35)


def _has_blocking_contradiction(text: str, marker: str) -> bool:
    if marker not in text:
        return False
    allowed_prefixes = ("不得", "不能", "不可", "禁止", "避免", "拒绝")
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return False
        prefix = text[max(0, index - 4):index]
        if not any(prefix.endswith(item) for item in allowed_prefixes):
            return True
        start = index + len(marker)


def _platform_risk_score(text: str) -> int:
    penalties = sum(1 for marker in FORBIDDEN_MARKERS if marker in text)
    meta_markers = ["JSON", "数据库", "发布任务链路"]
    penalties += sum(1 for marker in meta_markers if marker in text)
    return max(0, 100 - penalties * 15)
