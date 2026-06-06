from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterUnitPlan


@dataclass(frozen=True)
class PlannedChapterUnit:
    index: int
    role: str
    goal: str
    obstacle: str
    action: str
    reaction: str
    info_gain: str
    handoff: str
    target_chars: str = "300-700"

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "role": self.role,
            "goal": self.goal,
            "obstacle": self.obstacle,
            "action": self.action,
            "reaction": self.reaction,
            "info_gain": self.info_gain,
            "handoff": self.handoff,
            "target_chars": self.target_chars,
        }


def ensure_chapter_unit_plan(
    session: Session,
    *,
    chapter_id: int,
    chapter_brief_id: int | None,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str = "",
    mode: str = "draft",
    source: str = "system",
    pattern_memory: dict[str, Any] | None = None,
) -> ChapterUnitPlan:
    existing = session.scalar(
        select(ChapterUnitPlan)
        .where(
            ChapterUnitPlan.chapter_id == chapter_id,
            ChapterUnitPlan.chapter_brief_id == chapter_brief_id,
            ChapterUnitPlan.status == "active",
        )
        .order_by(ChapterUnitPlan.id.desc())
    )
    payload = build_chapter_unit_plan_payload(
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        previous_chapter_context=previous_chapter_context,
        mode=mode,
        pattern_memory=pattern_memory,
    )
    if existing and _loads(existing.plan_json) == payload:
        return existing
    if existing:
        existing.status = "superseded"
    row = ChapterUnitPlan(
        chapter_id=chapter_id,
        chapter_brief_id=chapter_brief_id,
        source=source,
        status="active",
        plan_json=json.dumps(payload, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row


def build_chapter_unit_plan_payload(
    *,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str = "",
    mode: str = "draft",
    pattern_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    beats = _split_beats(required_beats)
    if not beats:
        beats = _fallback_beats(chapter_number=chapter_number, goal=goal, previous_chapter_context=previous_chapter_context)
    unit_count = max(6, min(8, len(beats)))
    selected = _spread(beats, unit_count)
    units = [
        _planned_unit(
            index=index,
            beat=beat,
            chapter_number=chapter_number,
            goal=goal,
            previous_chapter_context=previous_chapter_context,
            constraints=constraints,
            mode=mode,
            total=unit_count,
        ).to_dict()
        for index, beat in enumerate(selected, start=1)
    ]
    return {
        "schema": "chapter_unit_plan_v1",
        "chapter_number": chapter_number,
        "mode": mode,
        "target_unit_count": unit_count,
        "target_chars_per_unit": "300-700",
        "chapter_goal": _one_line(goal, 220),
        "units": units,
        "pattern_memory": _compact_pattern_memory(pattern_memory),
        "acceptance": [
            "正文不显示单元编号，但实际段落推进必须能对应这些单元。",
            "每个单元都有小目标、阻碍、可见动作、人物反应、信息增量和承接点。",
            "后一单元必须承接前一单元动作后果，不跳成剧情梗概。",
            "章末单元必须由本章行动自然引出新危险、新发现或未解决压力。",
            *_pattern_acceptance(pattern_memory),
        ],
    }


def format_chapter_unit_plan(plan: ChapterUnitPlan | dict[str, Any] | None) -> str:
    payload = _plan_payload(plan)
    if not payload:
        return ""
    lines = [
        "拟人化小单元计划（必须执行，正文不要输出编号）：",
        f"- 目标单元数：{payload.get('target_unit_count', '')}；每单元 {payload.get('target_chars_per_unit', '300-700')} 中文字符。",
    ]
    for unit in payload.get("units") or []:
        if not isinstance(unit, dict):
            continue
        lines.append(
            f"{unit.get('index')}. {unit.get('role')}｜目标：{unit.get('goal')}｜阻碍：{unit.get('obstacle')}｜"
            f"动作：{unit.get('action')}｜反应：{unit.get('reaction')}｜信息：{unit.get('info_gain')}｜承接：{unit.get('handoff')}"
        )
    acceptance = [str(item) for item in payload.get("acceptance") or [] if item]
    if acceptance:
        lines.extend(["验收：", *[f"- {item}" for item in acceptance]])
    return "\n".join(lines)


def align_chapter_unit_plan(plan: ChapterUnitPlan | dict[str, Any] | None, unit_report: dict[str, Any] | None) -> dict[str, Any]:
    payload = _plan_payload(plan)
    report = unit_report if isinstance(unit_report, dict) else {}
    expected = int(payload.get("target_unit_count") or 0) if payload else 0
    actual = int(report.get("unit_count") or 0)
    score = int(report.get("score") or 0)
    units = report.get("units") if isinstance(report.get("units"), list) else []
    weak_units = [
        {"index": item.get("index"), "score": item.get("score"), "issues": item.get("issues", [])}
        for item in units
        if isinstance(item, dict) and int(item.get("score") or 0) < 70
    ]
    count_score = 100
    if expected:
        count_score = max(0, 100 - abs(expected - actual) * 12)
    alignment_score = round((score * 0.7) + (count_score * 0.3)) if report else 0
    issues: list[str] = []
    if expected and actual < max(3, expected - 2):
        issues.append(f"unit_count_low:{actual}<{expected}")
    if score < 70:
        issues.append(f"unit_flow_low:{score}<70")
    if weak_units:
        issues.append("weak_units:" + ",".join(str(item.get("index")) for item in weak_units[:6]))
    return {
        "schema": "chapter_unit_plan_alignment_v1",
        "expected_unit_count": expected,
        "actual_unit_count": actual,
        "unit_flow_score": score,
        "alignment_score": max(0, min(100, alignment_score)),
        "passed": bool(expected and actual and alignment_score >= 70 and not issues),
        "issues": issues,
        "weak_units": weak_units[:8],
    }


def _planned_unit(
    *,
    index: int,
    beat: str,
    chapter_number: int,
    goal: str,
    previous_chapter_context: str,
    constraints: str,
    mode: str,
    total: int,
) -> PlannedChapterUnit:
    role = _role(index, total)
    compact_beat = _one_line(beat, 130)
    if index == 1:
        return PlannedChapterUnit(
            index=index,
            role=role,
            goal="接住前章后果并让主角进入具体处境" if chapter_number > 1 else "用具体处境建立主角欲望和阅读牵引",
            obstacle=_obstacle_from_text(previous_chapter_context) or "身份、处境或信息差让主角不能直接解决问题",
            action="主角先做一个低成本试探或求生动作",
            reaction="写出身体感受、误判、迟疑或嘴硬反应",
            info_gain=compact_beat,
            handoff="让第2单元承接这次动作带来的新阻碍",
        )
    if index == total:
        return PlannedChapterUnit(
            index=index,
            role=role,
            goal="收束本章行动链并制造下一章期待",
            obstacle="收益落地的同时出现更具体的危险、误会、交易或未解问题",
            action="主角基于本章获得的信息做出最后一个选择",
            reaction="让人物意识到代价或风险已经改变自己处境",
            info_gain=compact_beat,
            handoff="章末留下由本章行动导致的新危险、新发现或下一步目标",
        )
    return PlannedChapterUnit(
        index=index,
        role=role,
        goal=_goal_from_beat(compact_beat, fallback=goal),
        obstacle=_obstacle_from_text(compact_beat + constraints) or "具体人物、环境、伤势、利益或误判形成阻碍",
        action=_action_for_index(index, mode=mode),
        reaction="人物必须有可见反应：犹豫、试探、疼痛、怀疑、愤怒、沉默或临场找补",
        info_gain=compact_beat,
        handoff="单元末让局面微变，并把后果递给下一单元",
    )


def _role(index: int, total: int) -> str:
    if index == 1:
        return "承接/开场"
    if index == total:
        return "章末钩子"
    roles = ["试探", "受阻", "转圜", "反压", "变局", "代价"]
    return roles[(index - 2) % len(roles)]


def _split_beats(value: str) -> list[str]:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    parts = re.split(r"[；;\n]+|(?<=。)", text)
    items = [_one_line(part, 180) for part in parts if _useful_beat(part)]
    return list(dict.fromkeys(items))[:12]


def _useful_beat(value: str) -> bool:
    text = _one_line(value, 220)
    if len(text) < 4:
        return False
    blocked = ("质检报告 #", "weak_", "修订模式:", "验收方式", "禁止输出", "self_check")
    return not any(marker in text for marker in blocked)


def _fallback_beats(*, chapter_number: int, goal: str, previous_chapter_context: str) -> list[str]:
    opening = "承接上一章后果" if chapter_number > 1 else "建立主角处境和欲望"
    return [
        opening,
        _one_line(goal, 160) or "主角进入本章核心事件",
        "主角试探规则或人物关系",
        "阻碍升级，迫使主角付出代价",
        "通过动作或对话释放关键信息",
        "主角获得阶段性主动权",
        "章末由本章行动引出新危险或新机会",
    ]


def _spread(items: list[str], count: int) -> list[str]:
    if len(items) >= count:
        return items[:count]
    result = list(items)
    fallback = _fallback_beats(chapter_number=1, goal="", previous_chapter_context="")
    for item in fallback:
        if len(result) >= count:
            break
        if item not in result:
            result.append(item)
    return result[:count]


def _goal_from_beat(beat: str, *, fallback: str) -> str:
    if any(marker in beat for marker in ("找到", "拿到", "救", "逃", "查", "问", "换", "进入")):
        return beat
    return _one_line(fallback, 120) or beat


def _obstacle_from_text(text: str) -> str:
    mapping = [
        (("追", "危险", "逼", "堵"), "追逼、危险或外部压力让主角不能按原计划推进"),
        (("伤", "毒", "疼", "虚弱"), "身体状态或伤势限制主角行动"),
        (("误会", "怀疑", "身份"), "身份疑点或人物怀疑形成阻碍"),
        (("交易", "债", "钱", "价"), "利益交换和代价谈判形成阻碍"),
        (("规矩", "门派", "江湖"), "江湖规矩或门派关系限制选择"),
    ]
    for markers, value in mapping:
        if any(marker in text for marker in markers):
            return value
    return ""


def _action_for_index(index: int, *, mode: str) -> str:
    actions = {
        2: "主角用动作或对话试探局面，不靠旁白下结论",
        3: "主角第一次方案受阻后改用更具体的选择",
        4: "主角通过交易、观察、冒险或对话拿到信息",
        5: "主角获得小收益，同时暴露代价或更大麻烦",
        6: "主角主动反压局面，把问题推向章末选择",
    }
    return actions.get(index, "主角做出可见选择并承担后果")


def _plan_payload(plan: ChapterUnitPlan | dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(plan, dict):
        return plan
    if not plan:
        return {}
    return _loads(plan.plan_json)


def _loads(value: str) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _one_line(value: str, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _compact_pattern_memory(memory: dict[str, Any] | None) -> dict[str, Any]:
    if not memory or int(memory.get("source_review_count") or 0) <= 0:
        return {}
    return {
        "headline": _one_line(str(memory.get("headline") or ""), 180),
        "avg_unit_flow_score": memory.get("avg_unit_flow_score") or 0,
        "avg_unit_count_gap": memory.get("avg_unit_count_gap") or 0,
        "top_weak_unit_issues": (memory.get("top_weak_unit_issues") or [])[:5],
        "recommendations": [str(item) for item in (memory.get("recommendations") or [])[:5]],
    }


def _pattern_acceptance(memory: dict[str, Any] | None) -> list[str]:
    if not memory or int(memory.get("source_review_count") or 0) <= 0:
        return []
    rows = [str(item) for item in (memory.get("recommendations") or [])[:4]]
    return [f"复盘避坑：{item}" for item in rows if item]
