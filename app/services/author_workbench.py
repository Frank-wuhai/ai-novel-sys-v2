from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Character,
    CharacterState,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    FeedbackAdjustment,
    Foreshadow,
    PlatformFeedback,
    PlotThread,
    QualityReport,
)


@dataclass(frozen=True)
class AuthorWorkbenchReport:
    quality_profile: dict
    continuity_memory: dict
    revision_director: dict

    def to_dict(self) -> dict:
        return {
            "quality_profile": self.quality_profile,
            "continuity_memory": self.continuity_memory,
            "revision_director": self.revision_director,
        }

    @property
    def prompt_text(self) -> str:
        parts = [
            _quality_prompt(self.quality_profile),
            _continuity_prompt(self.continuity_memory),
            _revision_prompt(self.revision_director),
        ]
        return "\n\n".join(part for part in parts if part.strip())


def build_author_workbench_report(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    sample_limit: int = 8,
) -> AuthorWorkbenchReport:
    return AuthorWorkbenchReport(
        quality_profile=build_author_quality_profile(session, book_id=book_id, limit=sample_limit),
        continuity_memory=build_continuity_memory(session, book_id=book_id, chapter_number=chapter_number),
        revision_director=build_revision_director(session, book_id=book_id, chapter_number=chapter_number),
    )


def build_author_quality_profile(session: Session, *, book_id: int, limit: int = 8) -> dict:
    rows = list(
        session.scalars(
            select(ChapterVersion)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(
                Chapter.book_id == book_id,
                ChapterVersion.status.in_(["approved", "reviewed_pass"]),
            )
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    samples = []
    dimension_totals: dict[str, list[int]] = {}
    for version in rows:
        chapter = session.get(Chapter, version.chapter_id)
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        data = _loads_json(quality.report if quality else "")
        dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
        for name, score in dimensions.items():
            if isinstance(score, (int, float)):
                dimension_totals.setdefault(name, []).append(int(score))
        llm = data.get("llm_review") if isinstance(data.get("llm_review"), dict) else {}
        samples.append(
            {
                "chapter_number": chapter.chapter_number if chapter else 0,
                "version_id": version.id,
                "title": version.title,
                "status": version.status,
                "quality_score": quality.score if quality else 0,
                "editor_score": int(llm.get("score") or 0),
                "chars": _chinese_chars(version.content),
                "strengths": _list(llm.get("strengths"))[:3],
            }
        )
    averages = {
        name: round(sum(values) / len(values))
        for name, values in dimension_totals.items()
        if values
    }
    weak = [name for name, score in sorted(averages.items(), key=lambda item: item[1]) if score < 65][:5]
    strong = [name for name, score in sorted(averages.items(), key=lambda item: item[1], reverse=True) if score >= 75][:5]
    preferences = list(
        session.scalars(
            select(PlatformFeedback)
            .where(
                PlatformFeedback.book_id == book_id,
                PlatformFeedback.platform == "author",
                PlatformFeedback.metric_name == "author_preference",
            )
            .order_by(PlatformFeedback.id.desc())
            .limit(8)
        )
    )
    return {
        "sample_count": len(samples),
        "samples": samples,
        "average_dimensions": averages,
        "strong_dimensions": strong,
        "weak_dimensions": weak,
        "author_preferences": [
            {"category": item.metric_value, "text": item.raw_text}
            for item in preferences
            if item.raw_text.strip()
        ],
        "recommendations": _quality_recommendations(weak),
    }


def build_continuity_memory(session: Session, *, book_id: int, chapter_number: int) -> dict:
    previous = session.scalar(
        select(Chapter)
        .where(Chapter.book_id == book_id, Chapter.chapter_number < chapter_number)
        .order_by(Chapter.chapter_number.desc())
    )
    previous_version = None
    if previous:
        previous_version = session.scalar(
            select(ChapterVersion)
            .where(
                ChapterVersion.chapter_id == previous.id,
                ChapterVersion.status.in_(["approved", "reviewed_pass"]),
            )
            .order_by(ChapterVersion.id.desc())
        ) or session.scalar(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == previous.id)
            .order_by(ChapterVersion.id.desc())
        )
    foreshadows = list(
        session.scalars(
            select(Foreshadow)
            .where(Foreshadow.book_id == book_id, Foreshadow.status == "open")
            .order_by(Foreshadow.id.desc())
            .limit(6)
        )
    )
    threads = list(
        session.scalars(
            select(PlotThread)
            .where(PlotThread.book_id == book_id, PlotThread.status == "open")
            .order_by(PlotThread.id.desc())
            .limit(6)
        )
    )
    states = _latest_character_states(session, book_id=book_id, limit=6)
    ending = (previous_version.content or "")[-900:] if previous_version else ""
    return {
        "has_previous": bool(previous and previous_version),
        "previous_chapter_number": previous.chapter_number if previous else None,
        "previous_title": previous_version.title if previous_version else "",
        "previous_summary": previous.summary if previous else "",
        "previous_ending_excerpt": ending,
        "handoff": _handoff_points(previous.summary if previous else "", ending),
        "open_threads": [{"id": item.id, "name": item.name, "description": item.description} for item in threads],
        "open_foreshadows": [{"id": item.id, "setup_text": item.setup_text} for item in foreshadows],
        "character_states": states,
    }


def build_revision_director(session: Session, *, book_id: int, chapter_number: int) -> dict:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    latest_brief = (
        session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
        if chapter
        else None
    )
    adjustment = session.scalar(
        select(FeedbackAdjustment)
        .where(FeedbackAdjustment.book_id == book_id, FeedbackAdjustment.target_chapter_number == chapter_number)
        .order_by(FeedbackAdjustment.id.desc())
    )
    text = adjustment.adjustment_text if adjustment else ""
    mode = _revision_mode(text, latest_brief.required_beats if latest_brief else "")
    return {
        "has_feedback": bool(adjustment),
        "adjustment_id": adjustment.id if adjustment else None,
        "mode": mode,
        "keep": _extract_bucket(text, ("保留", "继续保留", "可用")),
        "remove": _extract_bucket(text, ("删除", "去掉", "不要", "禁止")),
        "add": _extract_bucket(text, ("新增", "强化", "补足", "增加")),
        "avoid": _extract_bucket(text, ("绝对不要", "禁止", "不得")),
        "acceptance": _extract_bucket(text, ("验收", "读者体验", "下一版必须")),
        "brief_status": latest_brief.status if latest_brief else "",
        "brief_id": latest_brief.id if latest_brief else None,
    }


def _latest_character_states(session: Session, *, book_id: int, limit: int) -> list[dict]:
    characters = list(session.scalars(select(Character).where(Character.book_id == book_id).order_by(Character.id).limit(16)))
    rows = []
    for character in characters:
        state = session.scalar(
            select(CharacterState)
            .where(CharacterState.character_id == character.id)
            .order_by(CharacterState.id.desc())
        )
        if state:
            rows.append({"character": character.name, "state": state.state_text, "source": state.source})
        if len(rows) >= limit:
            break
    return rows


def _quality_prompt(profile: dict) -> str:
    if not profile.get("sample_count"):
        return ""
    weak = "、".join(_label_dimension(item) for item in profile.get("weak_dimensions", [])) or "暂无"
    strong = "、".join(_label_dimension(item) for item in profile.get("strong_dimensions", [])) or "暂无"
    samples = [
        f"第{item['chapter_number']}章《{item['title']}》质检{item['quality_score']}，主编{item['editor_score']}"
        for item in profile.get("samples", [])[:3]
    ]
    return "\n".join(
        [
            "作者质量标尺：",
            f"- 已有可用样章：{'; '.join(samples) if samples else '暂无'}",
            f"- 强项保持：{strong}",
            f"- 弱项优先补：{weak}",
            *[f"- {item}" for item in profile.get("recommendations", [])[:4]],
        ]
    )


def _continuity_prompt(memory: dict) -> str:
    if not memory.get("has_previous"):
        return "连续性记忆：本章是开局章，优先建立具体场景、主角处境和章末钩子。"
    lines = [
        "连续性记忆：",
        f"- 上一章：第{memory.get('previous_chapter_number')}章《{memory.get('previous_title') or ''}》",
    ]
    for item in memory.get("handoff", [])[:5]:
        lines.append(f"- 必须承接：{item}")
    for item in memory.get("open_threads", [])[:3]:
        lines.append(f"- 未解决主线：{item['name']} {item['description']}")
    return "\n".join(lines)


def _revision_prompt(report: dict) -> str:
    if not report.get("has_feedback"):
        return ""
    lines = [f"人工意见导演单：修订模式 {report.get('mode') or 'targeted'}"]
    for key, label in (("keep", "保留"), ("remove", "删除"), ("add", "新增/强化"), ("avoid", "禁止"), ("acceptance", "验收")):
        values = report.get(key) or []
        if values:
            lines.append(f"- {label}：" + "；".join(values[:4]))
    return "\n".join(lines)


def _quality_recommendations(weak: list[str]) -> list[str]:
    mapping = {
        "protagonist_agency": "每章至少让主角做一次主动判断和有代价的选择。",
        "brief_coverage": "生成前先把 brief 压成可执行导演单，避免只写到表层设定。",
        "payoff_specificity": "爽点必须具体到收获、代价、关系变化或新危险。",
        "reader_momentum": "减少解释性段落，用场景压力推动读者往下读。",
        "scene_continuity": "每个场景单元必须承接上一段动作后果。",
    }
    return [mapping[item] for item in weak if item in mapping]


def _handoff_points(summary: str, ending: str) -> list[str]:
    text = "\n".join([summary or "", ending or ""])
    markers = ("追", "伤", "欠", "危险", "秘密", "误会", "发现", "机会", "门派", "人情", "代价", "剑", "功")
    points = []
    for sentence in _sentences(text):
        if any(marker in sentence for marker in markers):
            points.append(sentence[:120])
        if len(points) >= 6:
            break
    return points


def _extract_bucket(text: str, markers: tuple[str, ...]) -> list[str]:
    rows = []
    for sentence in _sentences(text):
        if any(marker in sentence for marker in markers):
            cleaned = sentence.strip(" -\t")
            if cleaned and cleaned not in rows:
                rows.append(cleaned[:140])
        if len(rows) >= 6:
            break
    return rows


def _revision_mode(text: str, brief_beats: str) -> str:
    merged = f"{text}\n{brief_beats}"
    for mode in ("local_patch", "targeted", "rewrite", "fresh", "polish"):
        if f"修订模式:{mode}" in merged or f"revision_mode:{mode}" in merged:
            return mode
    if "局部" in merged:
        return "local_patch"
    if "整章重写" in merged or "重启" in merged:
        return "fresh"
    return "targeted"


def _sentences(text: str) -> list[str]:
    normalized = str(text or "").replace("\r\n", "\n")
    for mark in ("。", "；", ";", "\n"):
        normalized = normalized.replace(mark, "\n")
    return [line.strip() for line in normalized.splitlines() if len(line.strip()) >= 4]


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _chinese_chars(text: str) -> int:
    return sum(1 for ch in text or "" if "\u4e00" <= ch <= "\u9fff")


def _label_dimension(value: str) -> str:
    labels = {
        "brief_coverage": "写作说明兑现",
        "protagonist_agency": "主角主动性",
        "payoff_specificity": "爽点明确度",
        "reader_momentum": "追读动力",
        "scene_continuity": "场景连续性",
        "readability": "可读性",
    }
    return labels.get(value, value)
