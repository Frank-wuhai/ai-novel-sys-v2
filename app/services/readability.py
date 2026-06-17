from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class ReadabilityReport:
    score: int
    dimensions: dict[str, int]
    issues: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "dimensions": self.dimensions,
            "issues": self.issues,
            "recommendations": self.recommendations,
        }


def evaluate_readability(text: str) -> ReadabilityReport:
    content = text or ""
    opening = content[:700]
    ending = content[-700:]
    dimensions = {
        "opening_grip": _opening_grip(opening),
        "protagonist_agency": _marker_score(content, ("主角", "他", "她", "选择", "决定", "试探", "冒险", "出手", "开口", "观察", "判断")),
        "scene_continuity": _scene_continuity(content),
        "dialogue_liveliness": _dialogue_score(content),
        "concrete_sensory": _marker_score(content, ("脚步", "火把", "血", "痛", "风", "声", "眼神", "气息", "青砖", "刀", "掌")),
        "payoff_specificity": _marker_score(content, ("代价", "后果", "机会", "收获", "破局", "反噬", "主动权", "欠", "账册", "线索", "遗物", "旧债", "三日不能动气")),
        "ending_pull": _ending_pull(ending),
    }
    issues: list[str] = []
    recommendations: list[str] = []
    for name, score in dimensions.items():
        if score < 50:
            issues.append(f"weak_readability:{name}={score}")
    if dimensions["opening_grip"] < 50:
        recommendations.append("前300-700字应更快进入具体处境，可用人物欲望、关系张力、异常细节、利益交换、行动后果或悬念牵引读者。")
    if dimensions["protagonist_agency"] < 50:
        recommendations.append("强化主角主动观察、判断、选择和承担后果，减少被动接受安排。")
    if dimensions["scene_continuity"] < 50:
        recommendations.append("减少概述式跳跃，让场景动作和后果连续推进。")
    if dimensions["dialogue_liveliness"] < 50:
        recommendations.append("增加人物互动或对话，让角色像现场活人一样反应。")
    if dimensions["ending_pull"] < 50:
        recommendations.append("章末必须留下具体危险、发现、误会、机会或关系变化。")
    score = round(sum(dimensions.values()) / len(dimensions))
    return ReadabilityReport(
        score=max(0, min(100, score)),
        dimensions=dimensions,
        issues=issues,
        recommendations=recommendations,
    )


def _opening_grip(opening: str) -> int:
    markers = (
        "想要",
        "欠",
        "账",
        "约定",
        "规矩",
        "眼神",
        "沉默",
        "试探",
        "怀疑",
        "误会",
        "秘密",
        "异样",
        "发现",
        "声音",
        "脚步",
        "门",
        "选择",
        "压力",
        "危机",
        "危险",
    )
    score = _marker_score(opening, markers)
    if chinese_chars(opening) < 120:
        score -= 20
    if any(marker in opening for marker in ("设定", "世界观", "说明", "简单来说")):
        score -= 20
    return max(0, min(100, score))


def _scene_continuity(text: str) -> int:
    transition_markers = ("于是", "下一刻", "刚", "还没", "随即", "却", "但", "因为", "所以", "只见", "话音未落")
    summary_markers = ("总之", "随后几天", "很快过去", "简单来说", "大概", "一番")
    score = _marker_score(text, transition_markers)
    score -= min(30, sum(text.count(marker) for marker in summary_markers) * 10)
    return max(0, min(100, score))


def _dialogue_score(text: str) -> int:
    quote_count = sum(text.count(mark) for mark in ("“", "”", "「", "」", "『", "』"))
    if quote_count >= 12:
        return 90
    if quote_count >= 8:
        return 75
    if quote_count >= 4:
        return 55
    return 30


def _ending_pull(ending: str) -> int:
    markers = (
        "下一", "门外", "脚步", "黑影", "消息", "发现", "危险", "机会", "秘密", "转身", "抬头", "声音", "来了",
        "追兵", "铜铃", "铃", "账册", "旧债", "铁杖", "梅家", "内谷", "门缝", "裂缝",
    )
    return _marker_score(ending, markers)


def _marker_score(text: str, markers: tuple[str, ...]) -> int:
    hits = sum(1 for marker in markers if marker in (text or ""))
    return max(30, min(100, 30 + hits * 10))


def chinese_chars(text: str) -> int:
    return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")
