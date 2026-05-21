from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterBrief, FeedbackAdjustment, PlatformFeedback
from app.services.evidence import add_evidence_source, add_market_signal


@dataclass(frozen=True)
class FeedbackSummary:
    total: int
    by_metric: dict[str, int]
    by_platform: dict[str, int]
    latest: list[PlatformFeedback]


def record_platform_feedback(
    session: Session,
    *,
    book_id: int,
    platform: str,
    metric_name: str,
    metric_value: str = "",
    raw_text: str = "",
    chapter_number: int | None = None,
) -> PlatformFeedback:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    chapter_id = None
    if chapter_number is not None:
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
        if not chapter:
            raise ValueError(f"chapter not found: {chapter_number}")
        chapter_id = chapter.id
    feedback = PlatformFeedback(
        book_id=book_id,
        chapter_id=chapter_id,
        platform=platform,
        metric_name=metric_name,
        metric_value=metric_value,
        raw_text=raw_text,
    )
    session.add(feedback)
    session.flush()
    return feedback


def list_platform_feedback(
    session: Session,
    *,
    book_id: int,
    platform: str = "",
    metric_name: str = "",
    limit: int = 20,
) -> list[PlatformFeedback]:
    stmt = select(PlatformFeedback).where(PlatformFeedback.book_id == book_id).order_by(PlatformFeedback.id.desc()).limit(limit)
    if platform:
        stmt = stmt.where(PlatformFeedback.platform == platform)
    if metric_name:
        stmt = stmt.where(PlatformFeedback.metric_name == metric_name)
    return list(session.scalars(stmt))


def summarize_platform_feedback(session: Session, *, book_id: int, limit: int = 20) -> FeedbackSummary:
    items = list_platform_feedback(session, book_id=book_id, limit=limit)
    return FeedbackSummary(
        total=len(items),
        by_metric=dict(Counter(item.metric_name for item in items)),
        by_platform=dict(Counter(item.platform for item in items)),
        latest=items[:5],
    )


def convert_feedback_to_market_signal(
    session: Session,
    *,
    feedback_id: int,
    genre: str,
    signal_text: str,
    confidence: int = 65,
    source_status: str = "verified",
    source_reliability: int = 3,
) -> tuple[int, int]:
    feedback = session.get(PlatformFeedback, feedback_id)
    if not feedback:
        raise ValueError(f"feedback not found: {feedback_id}")
    source_key = f"feedback-{feedback.id}"
    source = add_evidence_source(
        session,
        source_id=source_key,
        title=f"{feedback.platform} feedback #{feedback.id}",
        url="",
        reliability=source_reliability,
        status=source_status,
    )
    signal = add_market_signal(
        session,
        source_key=source.source_id,
        genre=genre,
        signal_text=signal_text,
        confidence=confidence,
    )
    return source.id, signal.id


def create_feedback_adjustment(
    session: Session,
    *,
    book_id: int,
    target_chapter_number: int,
    feedback_ids: list[int],
    adjustment_text: str = "",
    status: str = "ready",
) -> FeedbackAdjustment:
    if target_chapter_number < 1:
        raise ValueError("target chapter number must be >= 1")
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    feedback_items = _feedback_items(session, book_id=book_id, feedback_ids=feedback_ids)
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == target_chapter_number))
    adjustment = FeedbackAdjustment(
        book_id=book_id,
        chapter_id=chapter.id if chapter else None,
        target_chapter_number=target_chapter_number,
        feedback_ids=",".join(str(item.id) for item in feedback_items),
        adjustment_text=adjustment_text or _default_adjustment_text(feedback_items, target_chapter_number),
        status=status,
    )
    session.add(adjustment)
    session.flush()
    return adjustment


def list_feedback_adjustments(
    session: Session,
    *,
    book_id: int,
    status: str = "",
    limit: int = 20,
) -> list[FeedbackAdjustment]:
    stmt = (
        select(FeedbackAdjustment)
        .where(FeedbackAdjustment.book_id == book_id)
        .order_by(FeedbackAdjustment.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(FeedbackAdjustment.status == status)
    return list(session.scalars(stmt))


def apply_feedback_adjustment_to_brief(session: Session, *, adjustment_id: int) -> ChapterBrief:
    adjustment = session.get(FeedbackAdjustment, adjustment_id)
    if not adjustment:
        raise ValueError(f"feedback adjustment not found: {adjustment_id}")
    chapter = _get_or_create_chapter(
        session,
        book_id=adjustment.book_id,
        chapter_number=adjustment.target_chapter_number,
    )
    latest = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    label = f"反馈调整#{adjustment.id}"
    if latest:
        goal = latest.goal
        required_beats = _append_unique(latest.required_beats, "回应读者反馈")
        constraints = _append_unique(latest.constraints, f"{label}:{adjustment.adjustment_text}")
    else:
        goal = f"根据平台反馈调整第{adjustment.target_chapter_number}章"
        required_beats = "回应读者反馈"
        constraints = f"{label}:{adjustment.adjustment_text}"
    brief = ChapterBrief(
        chapter_id=chapter.id,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        status="ready",
    )
    session.add(brief)
    adjustment.chapter_id = chapter.id
    adjustment.status = "applied"
    session.flush()
    return brief


def _feedback_items(session: Session, *, book_id: int, feedback_ids: list[int]) -> list[PlatformFeedback]:
    if not feedback_ids:
        raise ValueError("at least one feedback id is required")
    items: list[PlatformFeedback] = []
    for feedback_id in feedback_ids:
        feedback = session.get(PlatformFeedback, feedback_id)
        if not feedback:
            raise ValueError(f"feedback not found: {feedback_id}")
        if feedback.book_id != book_id:
            raise ValueError(f"feedback {feedback_id} does not belong to book {book_id}")
        items.append(feedback)
    return items


def _default_adjustment_text(items: list[PlatformFeedback], target_chapter_number: int) -> str:
    metric_counts = Counter(item.metric_name for item in items)
    metric_part = "，".join(f"{name}x{count}" for name, count in sorted(metric_counts.items()))
    raw_parts = [item.raw_text.strip() for item in items if item.raw_text.strip()]
    value_parts = [f"{item.metric_name}={item.metric_value}" for item in items if item.metric_value.strip()]
    evidence = "；".join(raw_parts[:3] or value_parts[:3])
    if evidence:
        return f"第{target_chapter_number}章需回应近期反馈（{metric_part}）：{evidence}"
    return f"第{target_chapter_number}章需回应近期反馈（{metric_part}）。"


def _get_or_create_chapter(session: Session, *, book_id: int, chapter_number: int) -> Chapter:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if chapter:
        return chapter
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    return chapter


def _append_unique(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}，{addition}"
