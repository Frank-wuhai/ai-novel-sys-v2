from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.production_optimization import predict_revision_pass


BOOST_MARKER = "revision_success_boost@v1"
BOOST_END_MARKER = "revision_success_boost@end"


@dataclass(frozen=True)
class RevisionSuccessBoostResult:
    applied: bool
    brief_id: int | None
    focus_count: int
    message: str


def apply_revision_success_boost(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
) -> RevisionSuccessBoostResult:
    """Refresh the active revision brief with the latest high-yield quality targets."""
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return RevisionSuccessBoostResult(False, None, 0, "chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version or version.status != "needs_revision":
        return RevisionSuccessBoostResult(False, None, 0, "latest version does not require revision")
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not brief:
        return RevisionSuccessBoostResult(False, None, 0, "active revision brief not found")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc())
    )
    if not quality or quality.passed:
        return RevisionSuccessBoostResult(False, brief.id, 0, "latest quality report is missing or passed")

    quality_data = _loads_json(quality.report)
    focus = _focus_lines(quality_data)
    if not focus:
        return RevisionSuccessBoostResult(False, brief.id, 0, "quality report has no actionable focus")

    block = _boost_block(version=version, quality=quality, quality_data=quality_data, chapter_number=chapter_number, focus=focus)
    required = _replace_block(brief.required_beats or "", block)
    if required != (brief.required_beats or ""):
        brief.required_beats = required
    constraints = _replace_block(brief.constraints or "", "")
    if constraints != (brief.constraints or ""):
        brief.constraints = constraints
    session.flush()
    return RevisionSuccessBoostResult(True, brief.id, len(focus), f"revision brief focused from quality report {quality.id}")


def _boost_block(*, version: ChapterVersion, quality: QualityReport, quality_data: dict, chapter_number: int, focus: list[str]) -> str:
    source = str(version.source or "")
    decision = predict_revision_pass(quality_data, chapter_number=chapter_number)
    lines = [
        BOOST_MARKER,
        f"当前待修底稿：v{version.id}，版本号 {version.version_number}，质检 {quality.score} 分。",
        f"修订档位：{decision.label}；预测提分：+{decision.predicted_pass_delta}；置信度：{decision.confidence}。",
    ]
    if decision.should_rebuild:
        lines.append("策略提醒：当前更适合候选重建；若生产路由仍要求修订，本轮必须按场景级重写失败段落，不做表层润色。")
    if source.startswith("rebuild_candidate_selected:"):
        lines.append("已采用重建候选稿；禁止再生成候选或另起新章，必须修选中稿。")
    elif source.startswith(("revision_budget_recovery:", "revision_budget_readable_restore:")):
        lines.append("当前处于自动换策略恢复；禁止回到旧线性修订，必须按恢复稿补强。")
    lines.extend(
        [
            "本轮只打以下最高收益问题，不要泛泛润色：",
            *[f"- {item}" for item in focus[:10]],
            "落地方式：每个问题都必须变成场景空间、人物站位、动作后果、对白试探或章末变化；不得只替换形容词。",
            "验收方式：正文优先，不输出质检报告、修订合同、系统说明或自检清单。",
            BOOST_END_MARKER,
        ]
    )
    return "\n".join(lines)


def _focus_lines(data: dict) -> list[str]:
    assessment = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
    dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
    focus: list[str] = []
    for blocker, note in zip(_as_list(assessment.get("blockers")), _as_list(assessment.get("blocker_notes"))):
        focus.append(f"{blocker}：{note}" if note else blocker)
    for item in _as_list(assessment.get("improve")):
        focus.append(item)
    thresholds = {
        "brief_coverage": 60,
        "scene_atmosphere": 55,
        "dialogue_fullness": 55,
        "imageable_paragraphs": 60,
        "scene_expansion": 60,
        "chapter_unit_flow": 65,
        "prose_voice": 65,
        "author_intent": 65,
        "naming_governance": 60,
    }
    for name, threshold in thresholds.items():
        score = _int_or_none(dimensions.get(name))
        if score is not None and score < threshold:
            focus.append(f"{name}={score}<{threshold}")
    for issue in _as_list(data.get("issues")):
        focus.append(f"质检问题：{issue}")
    return _dedupe(focus)[:10]


def _replace_block(text: str, block: str) -> str:
    cleaned = re.sub(
        rf"\n?{re.escape(BOOST_MARKER)}.*?{re.escape(BOOST_END_MARKER)}\n?",
        "\n",
        text or "",
        flags=re.S,
    ).strip()
    if not block:
        return cleaned
    return "\n".join(item for item in [cleaned, block] if item).strip()


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _as_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = re.sub(r"\s+", " ", item).strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result
