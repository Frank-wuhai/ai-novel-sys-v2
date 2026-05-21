from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, PlatformFeedback
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
