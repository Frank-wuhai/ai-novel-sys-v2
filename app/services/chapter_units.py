from __future__ import annotations

import re
from dataclasses import dataclass

from app.services.expression_precision import evaluate_expression_precision


@dataclass(frozen=True)
class ChapterUnit:
    index: int
    text: str
    chars: int


@dataclass(frozen=True)
class ChapterUnitReport:
    score: int
    unit_count: int
    units: list[dict]
    issues: list[str]
    repair_contract: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "unit_count": self.unit_count,
            "units": self.units,
            "issues": self.issues,
            "repair_contract": self.repair_contract,
        }


ACTION_MARKERS = ("走", "退", "停", "抓", "握", "推", "躲", "冲", "问", "答", "看", "听", "试", "接", "递", "藏", "换", "跪", "抬")
GOAL_MARKERS = ("想", "要", "必须", "打算", "决定", "选择", "只能", "先", "目标", "活下去", "找到", "拿到")
OBSTACLE_MARKERS = ("拦", "挡", "逼", "追", "痛", "伤", "危险", "麻烦", "误会", "怀疑", "不许", "代价", "难")
CONSEQUENCE_MARKERS = ("于是", "因此", "所以", "结果", "却", "但", "反而", "这让", "随即", "下一刻", "后果", "代价")
INFO_MARKERS = ("发现", "明白", "知道", "线索", "秘密", "规矩", "消息", "身份", "来历", "真相", "异常")
REACTION_MARKERS = ("皱眉", "沉默", "愣", "怒", "怕", "疑", "盯", "笑", "喘", "疼", "冷", "汗", "心里")
VAGUE_SUMMARY_MARKERS = ("大概", "总之", "一番", "经过", "随后发生", "事情变得", "局势变得", "众人")


def chinese_chars(text: str) -> int:
    return sum(1 for ch in str(text or "") if "\u4e00" <= ch <= "\u9fff")


def evaluate_chapter_units(text: str, *, target_min: int = 300, target_max: int = 700) -> ChapterUnitReport:
    units = split_chapter_units(text, target_min=target_min, target_max=target_max)
    rows: list[dict] = []
    issues: list[str] = []
    repair_contract: list[str] = []
    previous_anchor = ""
    for unit in units:
        row = _evaluate_unit(unit, previous_anchor=previous_anchor, target_min=target_min, target_max=target_max)
        rows.append(row)
        previous_anchor = _unit_anchor(unit.text)
        for issue in row["issues"]:
            issues.append(f"unit{unit.index}:{issue}")
        if row["score"] < 70:
            repair_contract.append(_repair_line(row))
    score = round(sum(row["score"] for row in rows) / len(rows)) if rows else 0
    if len(units) < 3 and chinese_chars(text) >= 1200:
        score = min(score, 58)
        issues.append("unit_count_low")
        repair_contract.append("章节切分后小单元过少：重修时按 300-700 字拆成连续场景单元，每单元必须有目标、阻碍、动作后果和信息增量。")
    return ChapterUnitReport(
        score=max(0, min(100, score)),
        unit_count=len(units),
        units=rows,
        issues=issues,
        repair_contract=repair_contract[:8],
    )


def split_chapter_units(text: str, *, target_min: int = 300, target_max: int = 700) -> list[ChapterUnit]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n+", str(text or "")) if part.strip()]
    line_paragraphs = [part.strip() for part in str(text or "").splitlines() if part.strip()]
    pieces: list[str] = []
    if len(paragraphs) >= 3:
        pieces = paragraphs
    elif len(line_paragraphs) >= 6:
        pieces = line_paragraphs
    elif paragraphs:
        pieces = paragraphs
    else:
        pieces = [part.strip() for part in re.split(r"(?<=[。！？!?])", str(text or "")) if part.strip()]
    units: list[ChapterUnit] = []
    buffer: list[str] = []
    for piece in pieces:
        candidate = "\n\n".join([*buffer, piece]).strip()
        if buffer and chinese_chars(candidate) > target_max:
            units.append(_unit(len(units) + 1, "\n\n".join(buffer)))
            buffer = [piece]
        else:
            buffer.append(piece)
        if chinese_chars("\n\n".join(buffer)) >= target_min and _ends_scenelet(buffer[-1]):
            units.append(_unit(len(units) + 1, "\n\n".join(buffer)))
            buffer = []
    if buffer:
        if units and chinese_chars("\n\n".join(buffer)) < max(160, target_min // 2):
            prev = units.pop()
            units.append(_unit(prev.index, prev.text.rstrip() + "\n\n" + "\n\n".join(buffer)))
        else:
            units.append(_unit(len(units) + 1, "\n\n".join(buffer)))
    return units


def _unit(index: int, text: str) -> ChapterUnit:
    clean = str(text or "").strip()
    return ChapterUnit(index=index, text=clean, chars=chinese_chars(clean))


def _evaluate_unit(unit: ChapterUnit, *, previous_anchor: str, target_min: int, target_max: int) -> dict:
    text = unit.text
    checks = {
        "length": _length_score(unit.chars, target_min=target_min, target_max=target_max),
        "goal": _marker_score(text, GOAL_MARKERS, base=35, per_hit=18, max_hits=3),
        "action": _marker_score(text, ACTION_MARKERS, base=25, per_hit=9, max_hits=7),
        "obstacle": _marker_score(text, OBSTACLE_MARKERS, base=35, per_hit=14, max_hits=4),
        "consequence": _marker_score(text, CONSEQUENCE_MARKERS, base=30, per_hit=14, max_hits=4),
        "info_gain": _marker_score(text, INFO_MARKERS, base=35, per_hit=14, max_hits=4),
        "reaction": _marker_score(text, REACTION_MARKERS, base=35, per_hit=11, max_hits=5),
        "handoff": _handoff_score(text),
        "precision": evaluate_expression_precision(text).score,
    }
    if _looks_like_summary(text):
        checks["action"] = min(checks["action"], 48)
        checks["consequence"] = min(checks["consequence"], 48)
    issues = [name for name, value in checks.items() if value < 60]
    if previous_anchor and not _has_continuity_link(text):
        checks["handoff"] = min(checks["handoff"], 55)
        if "handoff" not in issues:
            issues.append("handoff")
    score = round(sum(checks.values()) / len(checks))
    return {
        "index": unit.index,
        "chars": unit.chars,
        "score": max(0, min(100, score)),
        "status": "pass" if score >= 70 and not issues else "attention",
        "checks": checks,
        "issues": issues,
        "anchor": _unit_anchor(text),
        "summary": _summary(text),
    }


def _repair_line(row: dict) -> str:
    label_map = {
        "length": "长度不稳",
        "goal": "目标不清",
        "action": "动作链弱",
        "obstacle": "阻碍不足",
        "consequence": "后果没落地",
        "info_gain": "信息增量弱",
        "reaction": "人物反应弱",
        "handoff": "承接点断",
        "precision": "表达/观察逻辑风险",
    }
    issues = "、".join(label_map.get(item, item) for item in row.get("issues", [])[:4])
    return f"第{row.get('index')}单元需局部重修：{issues}；保留本单元有效信息，补清目标、阻碍、动作后果和下一单元承接点。"


def _length_score(chars: int, *, target_min: int, target_max: int) -> int:
    if target_min <= chars <= target_max:
        return 100
    if chars < target_min:
        return max(35, 100 - (target_min - chars) // 3)
    return max(45, 100 - (chars - target_max) // 8)


def _marker_score(text: str, markers: tuple[str, ...], *, base: int, per_hit: int, max_hits: int) -> int:
    hits = sum(1 for marker in markers if marker in text)
    return max(0, min(100, base + min(hits, max_hits) * per_hit))


def _handoff_score(text: str) -> int:
    return _marker_score(text, CONSEQUENCE_MARKERS + ("接着", "刚才", "方才", "才", "还没", "没等", "转而"), base=36, per_hit=13, max_hits=4)


def _looks_like_summary(text: str) -> bool:
    return any(marker in text for marker in VAGUE_SUMMARY_MARKERS) and chinese_chars(text) > 180


def _has_continuity_link(text: str) -> bool:
    return any(marker in text[:180] for marker in ("刚才", "方才", "于是", "因此", "还没", "没等", "疼", "血", "那句话", "上一刻", "这让", "接着", "随即"))


def _unit_anchor(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text or "") if part.strip()]
    return sentences[-1][:80] if sentences else ""


def _summary(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"[。！？!?]\s*", text or "") if part.strip()]
    if not sentences:
        return ""
    return " / ".join(sentences[:2])[:140]


def _ends_scenelet(text: str) -> bool:
    return str(text or "").rstrip().endswith(("。", "！", "？", "!", "?"))
