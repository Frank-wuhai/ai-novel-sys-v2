from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.feedback import submit_revision_suggestion
from app.workflows.state_machine import move


WATCHED_READING_DIMS = {
    "author_intent": 60,
    "brief_coverage": 60,
    "readability": 60,
    "reader_momentum": 60,
    "hook_strength": 65,
    "scene_atmosphere": 55,
    "payoff_grounding": 65,
    "chapter_necessity": 65,
    "dialogue_fullness": 55,
    "character_voice": 60,
    "prose_voice": 65,
    "chapter_unit_flow": 65,
    "imageable_paragraphs": 60,
}


@dataclass(frozen=True)
class ReadingAssessment:
    level: str
    action: str
    label: str
    summary: str
    revision_mode: str
    preserve: list[str]
    improve: list[str]
    blockers: list[str]
    quality_id: int | None = None
    revision_brief_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "label": self.label,
            "summary": self.summary,
            "revision_mode": self.revision_mode,
            "preserve": self.preserve,
            "improve": self.improve,
            "blockers": self.blockers,
            "quality_id": self.quality_id,
            "revision_brief_id": self.revision_brief_id,
            "source": "reading_assessment@v1",
        }


def maybe_apply_reading_assessment(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    quality: QualityReport,
) -> ReadingAssessment:
    data = _loads_json(quality.report)
    existing = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
    if existing.get("source") == "reading_assessment@v1":
        return _assessment_from_dict(existing)

    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    version = session.get(ChapterVersion, quality.chapter_version_id)
    if not chapter or not version:
        assessment = ReadingAssessment(
            "system_error",
            "inspect",
            "系统异常",
            "找不到章节或版本，不能做阅读评估。",
            "inspect",
            [],
            [],
            ["missing_chapter_or_version"],
            quality_id=quality.id,
        )
        return _store_assessment(quality, data, assessment)

    assessment = assess_reading_quality(data, quality_id=quality.id)
    human_brief = _active_human_revision_brief(session, chapter_id=chapter.id)
    if human_brief:
        assessment = ReadingAssessment(
            "human_revision_contract",
            "auto_revise",
            "人工修订合同未完成",
            "当前稿虽已过基础质检，但仍有人工/审批反馈修订合同，必须继续按该合同修订。",
            "targeted",
            assessment.preserve,
            assessment.improve,
            [f"active_human_revision_brief#{human_brief.id}"],
            quality_id=quality.id,
            revision_brief_id=human_brief.id,
        )
        if version.status == "reviewed_pass":
            version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
        stored = _store_assessment(quality, data, assessment)
        session.flush()
        return stored
    if assessment.action == "approve_ready":
        _close_revision_briefs(session, chapter_id=chapter.id)
        if version.status == "needs_revision":
            version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    elif assessment.action in {"auto_polish", "auto_revise", "auto_rebuild"}:
        brief = _ensure_revision_brief(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            chapter_id=chapter.id,
            version=version,
            quality=quality,
            assessment=assessment,
        )
        assessment = ReadingAssessment(
            assessment.level,
            assessment.action,
            assessment.label,
            assessment.summary,
            assessment.revision_mode,
            assessment.preserve,
            assessment.improve,
            assessment.blockers,
            quality_id=quality.id,
            revision_brief_id=brief.id,
        )
        if version.status == "reviewed_pass":
            version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
    stored = _store_assessment(quality, data, assessment)
    session.flush()
    return stored


def assess_reading_quality(report_data: dict, *, quality_id: int | None = None) -> ReadingAssessment:
    score = int(report_data.get("score") or 0)
    passed = bool(report_data.get("passed"))
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    review = report_data.get("llm_review") if isinstance(report_data.get("llm_review"), dict) else {}
    editorial = report_data.get("editorial_stratification") if isinstance(report_data.get("editorial_stratification"), dict) else {}
    editor_score = int(review.get("score") or 0)
    editor_verdict = str(review.get("verdict") or "")
    blockers = _reading_blockers(dimensions)
    severe = _severe_blockers(dimensions)
    preserve = [str(item) for item in review.get("strengths") or []][:6]
    improve = _improvement_targets(dimensions, review)

    if not passed or score < 60 or editorial.get("tier") in {"D_rebuild", "E_contaminated"}:
        return ReadingAssessment(
            "rebuild_required",
            "auto_rebuild",
            "需重建",
            "当前稿不能沿局部修补继续，系统应回到章节承诺重建场景链。",
            "rewrite",
            preserve,
            improve,
            blockers or severe,
            quality_id=quality_id,
        )
    if severe or int(dimensions.get("author_intent") or 100) < 45 or int(dimensions.get("brief_coverage") or 100) < 55:
        return ReadingAssessment(
            "usable_draft_needs_revision",
            "auto_revise",
            "可用底稿，需自动升华",
            "当前稿有可保留结构，但关键承诺或读者体验不足；系统应自动生成定点升华修订。",
            "targeted",
            preserve,
            improve,
            blockers or severe,
            quality_id=quality_id,
        )
    if score >= 85 and editor_score >= 85 and editor_verdict == "pass" and not blockers:
        return ReadingAssessment(
            "publish_candidate",
            "approve_ready",
            "可进入审批",
            "当前稿已达到可读候选标准，系统关闭修订合同并进入阅读审批。",
            "none",
            preserve,
            improve[:3],
            [],
            quality_id=quality_id,
        )
    if score >= 78 and editor_score >= 78 and len(blockers) <= 1:
        return ReadingAssessment(
            "near_final",
            "approve_ready",
            "准定稿",
            "当前稿只剩轻微瑕疵，继续自动大修风险高，进入阅读审批。",
            "none",
            preserve,
            improve[:3],
            blockers[:1],
            quality_id=quality_id,
        )
    if len(blockers) >= 3:
        return ReadingAssessment(
            "readable_needs_polish",
            "auto_polish",
            "可读但需润色",
            "当前稿可读，但多个读感维度偏弱；系统先做一轮低风险局部润色。",
            "local_patch",
            preserve,
            improve,
            blockers,
            quality_id=quality_id,
        )
    return ReadingAssessment(
        "author_taste_review",
        "author_review",
        "待作者口味判断",
        "当前稿达到可读底线，剩余主要是作者口味取舍。",
        "none",
        preserve,
        improve[:4],
        blockers[:2],
        quality_id=quality_id,
    )


def reading_assessment_requires_revision(report_data: dict) -> bool:
    assessment = report_data.get("reading_assessment") if isinstance(report_data.get("reading_assessment"), dict) else {}
    return assessment.get("action") in {"auto_polish", "auto_revise", "auto_rebuild"}


def reading_assessment_approval_ready(report_data: dict) -> bool:
    assessment = report_data.get("reading_assessment") if isinstance(report_data.get("reading_assessment"), dict) else {}
    return assessment.get("action") in {"approve_ready", "author_review"}


def _ensure_revision_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    chapter_id: int,
    version: ChapterVersion,
    quality: QualityReport,
    assessment: ReadingAssessment,
) -> ChapterBrief:
    marker = f"reading_assessment_auto_quality#{quality.id}"
    existing = _active_brief_with_marker(session, chapter_id=chapter_id, marker=marker)
    if existing:
        return existing
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"
    suggestion = _revision_suggestion(chapter_number=chapter_number, version=version, quality=quality, assessment=assessment, marker=marker)
    _feedback, _adjustment, brief, _version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="reading_assessment",
        suggestion_text=suggestion,
        revision_mode=assessment.revision_mode if assessment.revision_mode != "none" else "targeted",
    )
    brief.goal = f"阅读评估自动修订第{chapter_number}章：以 v{version.id} 为底稿，把“能读”修到“想追”。"
    brief.required_beats = "\n".join(
        [
            marker,
            f"当前阅读层级：{assessment.label}",
            f"源版本锁定：v{version.id}；不得换开场、不得换主事件、不得新开故事线。",
            "必须保留：" + "；".join(assessment.preserve[:6]),
            "本轮只解决：" + "；".join((assessment.improve or assessment.blockers)[:6]),
        ]
    )
    brief.constraints = "\n".join(
        [
            brief.constraints or "",
            "reading_assessment_contract: 系统自动阅读评估生成；下一版必须解决上述读感问题。",
            f"revision_mode:{assessment.revision_mode}",
            "禁止：追杀模板、现实机构关注、门派通缉、系统面板直接解题、冷硬装酷式精炼。",
            "禁止推翻合格底稿；禁止只替换形容词；必须把问题落到场景、动作、对白、后果。",
        ]
    )
    sanitize_existing_chapter_brief(session, book_id=book_id, brief=brief)
    session.flush()
    return brief


def _revision_suggestion(*, chapter_number: int, version: ChapterVersion, quality: QualityReport, assessment: ReadingAssessment, marker: str) -> str:
    return "\n".join(
        [
            marker,
            f"第{chapter_number}章阅读评估：{assessment.summary}",
            f"源版本：v{version.id}；质量报告：#{quality.id}。",
            "保留项：",
            *[f"- {item}" for item in assessment.preserve[:6]],
            "修订目标：",
            *[f"- {item}" for item in (assessment.improve or assessment.blockers)[:8]],
            "边界：只做阅读体验升华，不换题材路线，不扩大到整章重写，除非评估层级为需重建。",
        ]
    )


def _reading_blockers(dimensions: dict) -> list[str]:
    rows: list[str] = []
    for name, threshold in WATCHED_READING_DIMS.items():
        value = int(dimensions.get(name) or 0)
        if value and value < threshold:
            rows.append(f"{name}={value}<{threshold}")
    return rows


def _severe_blockers(dimensions: dict) -> list[str]:
    severe_thresholds = {
        "author_intent": 45,
        "brief_coverage": 50,
        "readability": 55,
        "chapter_unit_flow": 55,
        "hook_strength": 55,
        "prose_voice": 55,
    }
    rows: list[str] = []
    for name, threshold in severe_thresholds.items():
        value = int(dimensions.get(name) or 0)
        if value and value < threshold:
            rows.append(f"{name}={value}<{threshold}")
    return rows


def _improvement_targets(dimensions: dict, review: dict) -> list[str]:
    labels = {
        "author_intent": "把作者承诺写进具体行动和后果，不只停留在设定说明。",
        "brief_coverage": "补齐章节 brief 的关键节拍，尤其是本章承诺、回报、代价和章末压力。",
        "readability": "压缩说明性内心独白，更快进入可见冲突。",
        "scene_atmosphere": "把氛围从概括词改成空间、声音、触感、人物站位和现场反应。",
        "dialogue_fullness": "让对白承担试探、遮掩、交易或情绪变化。",
        "hook_strength": "章末钩子要具体到动作或异常后果。",
        "chapter_necessity": "强化本章不可替代的变化：主角获得什么、失去什么、发现什么。",
        "imageable_paragraphs": "补足可画面化段落，让读者看见场景而不是只知道事件。",
    }
    weak = sorted(
        [(name, int(dimensions.get(name) or 0)) for name in labels if int(dimensions.get(name) or 0)],
        key=lambda row: row[1],
    )
    rows = [labels[name] for name, value in weak if value < WATCHED_READING_DIMS.get(name, 60)]
    rows.extend(str(item) for item in review.get("revision_suggestions") or [])
    return list(dict.fromkeys(rows))[:8]


def _active_brief_with_marker(session: Session, *, chapter_id: int, marker: str) -> ChapterBrief | None:
    return session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    ) if marker in _latest_active_brief_text(session, chapter_id=chapter_id) else None


def _latest_active_brief_text(session: Session, *, chapter_id: int) -> str:
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]) if brief else ""


def _close_revision_briefs(session: Session, *, chapter_id: int) -> None:
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"


def _active_human_revision_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    for brief in session.scalars(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
        .limit(8)
    ):
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if any(marker in text for marker in ("反馈调整#", "人工意图:", "原始人工意见", "manual_approval")):
            return brief
    return None


def _store_assessment(quality: QualityReport, data: dict, assessment: ReadingAssessment) -> ReadingAssessment:
    data["reading_assessment"] = assessment.to_dict()
    quality.report = json.dumps(data, ensure_ascii=False)
    return assessment


def _assessment_from_dict(data: dict) -> ReadingAssessment:
    return ReadingAssessment(
        level=str(data.get("level") or ""),
        action=str(data.get("action") or ""),
        label=str(data.get("label") or ""),
        summary=str(data.get("summary") or ""),
        revision_mode=str(data.get("revision_mode") or ""),
        preserve=[str(item) for item in data.get("preserve") or []],
        improve=[str(item) for item in data.get("improve") or []],
        blockers=[str(item) for item in data.get("blockers") or []],
        quality_id=int(data.get("quality_id") or 0) or None,
        revision_brief_id=int(data.get("revision_brief_id") or 0) or None,
    )


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
