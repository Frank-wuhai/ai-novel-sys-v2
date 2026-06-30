from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, ProductionRunReview, QualityReport


OPTIMIZATION_MARKER = "production_optimization@v1"
OPTIMIZATION_END_MARKER = "production_optimization@end"


@dataclass(frozen=True)
class ChapterTypeProfile:
    code: str
    label: str
    pass_score: int
    required_dimensions: dict[str, int]
    reader_experience: str
    skeleton_requirements: tuple[str, ...]


@dataclass(frozen=True)
class RevisionEfficiencyDecision:
    tier: str
    label: str
    confidence: int
    predicted_pass_delta: int
    should_rebuild: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SkeletonPreflightResult:
    passed: bool
    missing: tuple[str, ...]
    block: str


def chapter_type_profile(chapter_number: int, *, goal: str = "", required_beats: str = "", constraints: str = "") -> ChapterTypeProfile:
    text = "\n".join([goal or "", required_beats or "", constraints or ""])
    if chapter_number == 1:
        return ChapterTypeProfile(
            code="opening",
            label="开篇章",
            pass_score=72,
            required_dimensions={
                "reader_momentum": 65,
                "hook_strength": 68,
                "author_intent": 65,
                "brief_coverage": 60,
                "chapter_unit_flow": 62,
            },
            reader_experience="让读者立刻明白主角处境、核心卖点和章末期待。",
            skeleton_requirements=("主角处境", "核心卖点", "可见阻碍", "主动选择", "章末钩子"),
        )
    if any(marker in text for marker in ("高潮", "决战", "爆发", "反转", "转折")):
        return ChapterTypeProfile(
            code="turning_point",
            label="转折/高潮章",
            pass_score=74,
            required_dimensions={
                "conflict_pressure": 68,
                "choice_and_cost": 68,
                "earned_payoff": 65,
                "payoff_grounding": 62,
                "chapter_unit_flow": 65,
            },
            reader_experience="让读者看到选择、代价和局面变化同时落地。",
            skeleton_requirements=("冲突升级", "主动选择", "明确代价", "回报落地", "局面反转"),
        )
    if chapter_number <= 5:
        return ChapterTypeProfile(
            code="early_serial",
            label="前五章推进章",
            pass_score=72,
            required_dimensions={
                "brief_coverage": 62,
                "reader_momentum": 64,
                "choice_and_cost": 62,
                "hook_strength": 65,
                "chapter_unit_flow": 64,
            },
            reader_experience="让读者确认这本书能连续追：上一章后果、本章新压力、章末新期待都要清楚。",
            skeleton_requirements=("承接上一章", "本章目标", "外部阻碍", "行动代价", "章末变化"),
        )
    return ChapterTypeProfile(
        code="serial_progress",
        label="连载推进章",
        pass_score=70,
        required_dimensions={
            "brief_coverage": 60,
            "chapter_unit_flow": 64,
            "dialogue_fullness": 55,
            "scene_atmosphere": 55,
            "hook_strength": 62,
        },
        reader_experience="让读者顺着场景推进往下读：目标、阻碍、信息增量和章末压力不断交接。",
        skeleton_requirements=("本章目标", "外部阻碍", "信息释放", "行动后果", "章末压力"),
    )


def enrich_quality_report_with_optimization(
    report_data: dict[str, Any],
    *,
    chapter_number: int,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    enforce_gate: bool = True,
) -> dict[str, Any]:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    gate_failures = [
        f"{name}={int(dimensions.get(name) or 0)}<{threshold}"
        for name, threshold in profile.required_dimensions.items()
        if int(dimensions.get(name) or 0) < threshold
    ]
    base_score = int(report_data.get("score") or 0)
    predicted = predict_revision_pass(
        report_data,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
    )
    type_gate_passed = base_score >= profile.pass_score and not gate_failures
    report_data["chapter_type_gate"] = {
        "schema": "chapter_type_gate_v1",
        "chapter_type": profile.code,
        "label": profile.label,
        "pass_score": profile.pass_score,
        "reader_experience": profile.reader_experience,
        "required_dimensions": profile.required_dimensions,
        "failures": gate_failures,
        "passed": type_gate_passed,
    }
    report_data["revision_pass_prediction"] = {
        "schema": "revision_pass_prediction_v1",
        "tier": predicted.tier,
        "label": predicted.label,
        "confidence": predicted.confidence,
        "predicted_pass_delta": predicted.predicted_pass_delta,
        "should_rebuild": predicted.should_rebuild,
        "reasons": list(predicted.reasons),
    }
    if report_data.get("passed") and enforce_gate and not type_gate_passed:
        report_data["passed"] = False
        report_data["status"] = "FAIL"
        issues = [str(item) for item in report_data.get("issues") or []]
        issues.append("chapter_type_gate_failed:" + ",".join(gate_failures[:5]))
        report_data["issues"] = list(dict.fromkeys(issues))
    return report_data


def predict_revision_pass(
    report_data: dict[str, Any],
    *,
    chapter_number: int,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
) -> RevisionEfficiencyDecision:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    score = int(report_data.get("score") or 0)
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    issues = [str(item) for item in report_data.get("issues") or []]
    low = {name: int(value) for name, value in dimensions.items() if _int(value) < 60}
    hard_failed = not bool(hard_gate.get("passed", True)) or any(
        issue.startswith(("too_short", "too_long", "forbidden_marker", "setting_contradiction", "bias_blocker"))
        for issue in issues
    )
    structure_low = [name for name in ("brief_coverage", "author_intent", "arc_alignment", "chapter_necessity") if _int(dimensions.get(name)) < 55]
    craft_low = [
        name
        for name in ("dialogue_fullness", "scene_atmosphere", "imageable_paragraphs", "prose_voice", "chapter_unit_flow")
        if _int(dimensions.get(name)) < 65
    ]
    gate_failures = [
        name for name, threshold in profile.required_dimensions.items() if _int(dimensions.get(name)) < threshold
    ]
    if hard_failed or len(structure_low) >= 2 or score < 62:
        return RevisionEfficiencyDecision(
            tier="rebuild",
            label="候选重建",
            confidence=90 if hard_failed else 82,
            predicted_pass_delta=18,
            should_rebuild=True,
            reasons=tuple([*structure_low[:4], *issues[:3]] or ["结构或硬门禁风险高"]),
        )
    if score >= profile.pass_score - 4 and len(gate_failures) <= 3 and len(craft_low) <= 4:
        return RevisionEfficiencyDecision(
            tier="light",
            label="轻修",
            confidence=78,
            predicted_pass_delta=5,
            should_rebuild=False,
            reasons=tuple(gate_failures[:4] or craft_low[:4] or ["接近通过，只需局部补强"]),
        )
    return RevisionEfficiencyDecision(
        tier="targeted",
        label="定点重修",
        confidence=74,
        predicted_pass_delta=10,
        should_rebuild=False,
        reasons=tuple([*gate_failures[:4], *list(low)[:4]] or ["需要场景级定点重修"]),
    )


def apply_skeleton_preflight_to_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> SkeletonPreflightResult:
    profile = chapter_type_profile(
        chapter_number,
        goal=brief.goal or "",
        required_beats=brief.required_beats or "",
        constraints=brief.constraints or "",
    )
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    missing = tuple(item for item in profile.skeleton_requirements if not _skeleton_requirement_covered(item, text))
    block = _optimization_block(profile=profile, missing=missing, memory=passed_chapter_memory(session, book_id=book_id, chapter_number=chapter_number))
    if missing:
        brief.required_beats = _replace_optimization_block(brief.required_beats or "", block)
        session.flush()
    return SkeletonPreflightResult(passed=not missing, missing=missing, block=block)


def optimization_prompt_block(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
) -> str:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    missing = tuple(item for item in profile.skeleton_requirements if not _skeleton_requirement_covered(item, "\n".join([goal, required_beats, constraints])))
    return _optimization_block(profile=profile, missing=missing, memory=passed_chapter_memory(session, book_id=book_id, chapter_number=chapter_number))


def passed_chapter_memory(session: Session, *, book_id: int, chapter_number: int, limit: int = 5) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(ProductionRunReview)
            .where(ProductionRunReview.book_id == book_id, ProductionRunReview.status == "pass")
            .order_by(ProductionRunReview.id.desc())
            .limit(max(limit, 1))
        )
    )
    lessons: list[str] = []
    chapters: list[int] = []
    for row in rows:
        data = _loads_json(row.review_json)
        source_chapter = int(data.get("chapter_number") or 0)
        if source_chapter >= chapter_number:
            continue
        chapters.append(source_chapter)
        headline = str(data.get("headline") or "").strip()
        if headline:
            lessons.append(f"第{source_chapter}章有效经验：{headline}")
        for item in data.get("recommendations") or []:
            lessons.append(str(item))
    return {
        "schema": "passed_chapter_memory_v1",
        "source_chapters": chapters[:limit],
        "lessons": list(dict.fromkeys(lessons))[:6],
    }


def _optimization_block(*, profile: ChapterTypeProfile, missing: tuple[str, ...], memory: dict[str, Any]) -> str:
    lines = [
        OPTIMIZATION_MARKER,
        f"章节类型：{profile.label}；目标读者体验：{profile.reader_experience}",
        "章节骨架验收：目标、阻碍、行动、代价/回报、章末变化必须在正文场景里出现。",
    ]
    if missing:
        lines.append("当前 brief 缺口：" + "、".join(missing))
        lines.append("生成前必须先把缺口落成具体人物行动、对话试探、空间阻碍或章末后果。")
    lessons = [str(item) for item in memory.get("lessons") or [] if item]
    if lessons:
        lines.append("合格章样本记忆：")
        lines.extend(f"- {item}" for item in lessons[:4])
    lines.append(OPTIMIZATION_END_MARKER)
    return "\n".join(lines)


def _replace_optimization_block(text: str, block: str) -> str:
    cleaned = re.sub(
        rf"\n?{re.escape(OPTIMIZATION_MARKER)}.*?{re.escape(OPTIMIZATION_END_MARKER)}\n?",
        "\n",
        text or "",
        flags=re.S,
    ).strip()
    return "\n".join(item for item in [cleaned, block] if item).strip()


def _skeleton_requirement_covered(requirement: str, text: str) -> bool:
    aliases = {
        "主角处境": ("主角", "处境", "身份", "困境"),
        "核心卖点": ("卖点", "能力", "金手指", "核心"),
        "可见阻碍": ("阻碍", "压力", "冲突", "误判", "危险"),
        "主动选择": ("选择", "主动", "决定", "答应", "拒绝"),
        "章末钩子": ("章末", "钩子", "下一章", "新线索", "变化"),
        "承接上一章": ("承接", "上一章", "后果", "前章"),
        "本章目标": ("目标", "本章", "任务", "想要"),
        "外部阻碍": ("阻碍", "压力", "冲突", "对手", "环境"),
        "行动代价": ("代价", "后果", "损耗", "交换", "风险"),
        "章末变化": ("章末", "变化", "局面", "新危险", "新机会"),
        "冲突升级": ("冲突", "升级", "逼近", "爆发"),
        "明确代价": ("代价", "后果", "付出", "损耗"),
        "回报落地": ("回报", "奖励", "收益", "结果", "落地"),
        "局面反转": ("反转", "转折", "变局", "改变"),
        "信息释放": ("信息", "线索", "发现", "证据"),
        "行动后果": ("行动", "后果", "代价", "影响"),
        "章末压力": ("章末", "压力", "危险", "未解决"),
    }
    return any(alias in text for alias in aliases.get(requirement, (requirement,)))


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
