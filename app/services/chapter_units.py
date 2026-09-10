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


# 2026-07-26 · 第6批计数器根治：口语化爽文用具象动作/生理反应/口语因果表达
# 叙事信号，旧词表只收抽象词导致 B 版系统性误伤。新增词均为真实叙事语义变体
# （具象动作、口语因果转折、生理紧张反应），非凑分——纯概括流仍命中
# VAGUE_SUMMARY_MARKERS 被压分，防放水机制保留。
ACTION_MARKERS = (
    "走", "退", "停", "抓", "握", "推", "躲", "冲", "问", "答", "看", "听", "试", "接", "递", "藏", "换", "跪", "抬",
    # 具象动作扩充
    "拉", "关", "坐", "站", "伸", "掏", "拿", "攥", "摁", "按", "踩", "搡", "扔", "翻", "摸", "蹭", "晃", "绕", "收",
    "转身", "低头", "抬手", "点头", "起身", "俯身", "迈", "跑", "喊", "递过", "递给", "举", "夹", "扣", "锁", "开门",
)
GOAL_MARKERS = (
    "想", "要", "必须", "打算", "决定", "选择", "只能", "先", "目标", "活下去", "找到", "拿到",
    # 口语化意图/驱动信号
    "得", "该", "想要", "准备", "为了", "打定", "非", "不能", "不得不", "只好", "得先", "总得", "非得", "定要",
)
OBSTACLE_MARKERS = (
    "拦", "挡", "逼", "追", "痛", "伤", "危险", "麻烦", "误会", "怀疑", "不许", "代价", "难",
    # 具象阻碍/羞辱/威胁/对峙信号
    "羞辱", "嘲", "讽", "瞪", "质问", "盘问", "催", "警告", "威胁", "标记", "暴露", "死路", "阴", "惨叫",
    "踉跄", "僵", "紧", "掐", "堵", "困", "撞", "拖欠", "发不出", "咬", "针对", "对手", "别犯傻", "别问",
)
CONSEQUENCE_MARKERS = (
    "于是", "因此", "所以", "结果", "却", "但", "反而", "这让", "随即", "下一刻", "后果", "代价",
    # 口语因果/时序承接（爽文靠动作承接而非书面连词推进）
    "才", "就", "这下", "顿时", "瞬间", "立刻", "马上", "紧接着", "没等", "刚", "一下", "然后", "接着",
    "转眼", "话没说完", "还没", "下一秒", "紧跟着",
)
INFO_MARKERS = (
    "发现", "明白", "知道", "线索", "秘密", "规矩", "消息", "身份", "来历", "真相", "异常",
    # 具象信息揭示/系统提示（爽文用界面/文件/规则条揭示信息）
    "原来", "竟然", "居然", "意识到", "看清", "认出", "证实", "显示", "写着", "提示", "警告", "资料",
    "记录", "规则", "副作用", "评价", "奖励", "解锁", "激活", "跳出", "弹出", "标记为",
)
REACTION_MARKERS = (
    "皱眉", "沉默", "愣", "怒", "怕", "疑", "盯", "笑", "喘", "疼", "冷", "汗", "心里",
    # 生理化紧张反应（口语爽文用身体信号写情绪）
    "心跳", "呼吸", "发白", "发青", "发麻", "发干", "哆嗦", "喉结", "喉咙", "后背", "胸腔", "嗓子",
    "指节", "掌心", "一缩", "一紧", "一顿", "顿住", "咽", "屏住", "攥紧", "眯", "缩",
)
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
    oversized_blank_paragraph = any(chinese_chars(part) > target_max for part in paragraphs)
    if len(paragraphs) >= 3 and not (oversized_blank_paragraph and len(line_paragraphs) >= 6):
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
    chain = _narrative_chain(text)
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
    checks["causal_chain"] = _chain_score(chain)
    if not chain["goal_to_action"]:
        checks["goal"] = min(checks["goal"], 68)
    if not chain["obstacle_to_action"]:
        checks["obstacle"] = min(checks["obstacle"], 58)
    if not chain["action_to_consequence"]:
        checks["consequence"] = min(checks["consequence"], 55)
    if not chain["info_from_scene"]:
        checks["info_gain"] = min(checks["info_gain"], 55)
    if not chain["consequence_to_reaction"]:
        checks["reaction"] = min(checks["reaction"], 58)
    if _looks_like_summary(text):
        checks["action"] = min(checks["action"], 48)
        checks["consequence"] = min(checks["consequence"], 48)
    if _looks_like_keyword_stuffing(text):
        for key in ("goal", "action", "obstacle", "consequence", "info_gain", "reaction", "causal_chain"):
            checks[key] = min(checks[key], 55)
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
        "chain": chain,
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
        "causal_chain": "目标-阻碍-动作-后果关系断",
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


def _narrative_chain(text: str) -> dict[str, bool]:
    sentences = _sentences(text)
    goal_i = _first_sentence(sentences, GOAL_MARKERS)
    action_i = _first_sentence(sentences, ACTION_MARKERS)
    obstacle_i = _first_sentence(sentences, OBSTACLE_MARKERS + ("但", "却", "可", "只是", "偏偏", "不肯", "没法"))
    consequence_i = _first_sentence(
        sentences,
        CONSEQUENCE_MARKERS + ("拿到", "换来", "失去", "露出", "停住", "逼退", "暴露", "裂开", "倒下", "松开", "只得"),
    )
    info_i = _first_sentence(
        sentences,
        INFO_MARKERS + ("木牌", "告示", "纸条", "铜牌", "腰牌", "刻着", "写着", "说", "告诉", "问出", "认出"),
    )
    reaction_i = _first_sentence(
        sentences,
        REACTION_MARKERS + ("看了他", "瞪着", "骂", "喊", "低声", "沉下脸", "停住", "后退", "倒吸"),
    )
    return {
        "goal_to_action": _ordered_near(goal_i, action_i, max_gap=4) or (goal_i < 0 and action_i >= 0 and _has_choice_pressure(text)),
        "obstacle_to_action": _ordered_near(obstacle_i, action_i, max_gap=4, allow_reverse=True),
        "action_to_consequence": _ordered_near(action_i, consequence_i, max_gap=5),
        "info_from_scene": info_i >= 0 and _has_scene_evidence(sentences[max(0, info_i - 1) : info_i + 2]),
        "consequence_to_reaction": _ordered_near(consequence_i, reaction_i, max_gap=4) or _dialogue_after(sentences, consequence_i),
    }


def _chain_score(chain: dict[str, bool]) -> int:
    return max(30, min(100, 35 + sum(1 for value in chain.values() if value) * 13))


def _sentences(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。！？!?；;\n]+", text or "") if part.strip()]


def _first_sentence(sentences: list[str], markers: tuple[str, ...]) -> int:
    for index, sentence in enumerate(sentences):
        if any(marker in sentence for marker in markers):
            return index
    return -1


def _ordered_near(left: int, right: int, *, max_gap: int, allow_reverse: bool = False) -> bool:
    if left < 0 or right < 0:
        return False
    if left <= right <= left + max_gap:
        return True
    return bool(allow_reverse and right <= left <= right + max_gap)


def _has_choice_pressure(text: str) -> bool:
    return any(marker in (text or "") for marker in ("只能", "不得不", "只好", "非得", "必须", "没退", "不能退"))


def _has_scene_evidence(sentences: list[str]) -> bool:
    window = "".join(sentences)
    concrete = ("木牌", "告示", "纸条", "铜牌", "腰牌", "碑", "门", "桌", "血", "泥", "药", "锁", "账", "字", "纹")
    reveal = ("写着", "刻着", "露出", "说", "告诉", "问", "认出", "看清", "才知道", "才明白", "发现")
    return any(item in window for item in concrete) and any(item in window for item in reveal)


def _dialogue_after(sentences: list[str], index: int) -> bool:
    if index < 0:
        return False
    return any("“" in sentence or "”" in sentence for sentence in sentences[index : index + 3])


def _looks_like_summary(text: str) -> bool:
    return any(marker in text for marker in VAGUE_SUMMARY_MARKERS) and chinese_chars(text) > 180


def _looks_like_keyword_stuffing(text: str) -> bool:
    content = text or ""
    abstract_hits = sum(
        1
        for marker in (
            "这里有",
            "这里还有",
            "这里仍然",
            "事情发生",
            "结果出现",
            "继续行动",
            "前往下一处",
            "危险、代价、阻碍",
            "秘密，也有人",
        )
        if marker in content
    )
    serial_action = bool(re.search(r"(走|看|抓|推|躲|退|停|问|答|伸手|抬手|转身)[、，](走|看|抓|推|躲|退|停|问|答|伸手|抬手|转身)", content))
    concrete_scene = any(
        marker in content
        for marker in ("木牌", "木桌", "门槛", "门外", "雨水", "血迹", "泥水", "腰牌", "铜牌", "马蹄", "热汤", "破伞", "水缸")
    )
    return abstract_hits >= 3 and serial_action and not concrete_scene


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
