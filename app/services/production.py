from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterReview,
    ChapterVersion,
    PromptTemplate,
    QualityReport,
    StoryFoundation,
)
from app.services.chapter_drafting import draft_chapter
from app.services.chapter_revision import create_revision_brief, revise_chapter
from app.services.chapter_standards import ensure_chapter_production_standard
from app.services.context_contamination import context_anchor_lines
from app.services.brief_sanitizer import sanitize_chapter_brief_fields
from app.services.prompts import seed_prompt_templates
from app.services.production_publishing import (
    auto_prepare_publish_job,
    create_publish_job,
    execute_publish_job,
    get_publish_job,
    get_publishing_target,
    list_publish_executions,
    list_publish_jobs,
    list_publishing_targets,
    mark_publish_job,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    upsert_publishing_target,
)
from app.services.production_reviewing import review_chapter
from app.services.production_state import (
    get_or_create_chapter,
    next_version_number as _next_version,
)
from app.workflows.state_machine import move


def create_book(session: Session, *, title: str, genre: str = "", platform: str = "") -> Book:
    existing = session.scalar(select(Book).where(Book.title == title))
    if existing:
        return existing
    book = Book(title=title, genre=genre, target_platform=platform, status="planning")
    session.add(book)
    session.flush()
    return book


def create_foundation(
    session: Session,
    *,
    book_id: int,
    premise: str,
    reader_promise: str = "",
    world_engine: str = "",
    protagonist_engine: str = "",
    conflict_engine: str = "",
) -> StoryFoundation:
    foundation = StoryFoundation(
        book_id=book_id,
        premise=premise,
        reader_promise=reader_promise,
        world_engine=world_engine,
        protagonist_engine=protagonist_engine,
        conflict_engine=conflict_engine,
        status="draft",
    )
    session.add(foundation)
    session.flush()
    return foundation


def create_chapter_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str = "",
    constraints: str = "",
) -> ChapterBrief:
    chapter = get_or_create_chapter(session, book_id=book_id, chapter_number=chapter_number)
    anchors = context_anchor_lines(session, book_id=book_id)
    effective_required_beats = "\n".join([item for item in [required_beats, *anchors] if item])
    goal, effective_required_beats, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=effective_required_beats,
        constraints=constraints,
    )
    brief = ChapterBrief(
        chapter_id=chapter.id,
        goal=goal,
        required_beats=effective_required_beats,
        constraints=ensure_chapter_production_standard(constraints, chapter_number=chapter_number),
        status="ready",
    )
    session.add(brief)
    session.flush()
    return brief


def create_manual_chapter_version(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    title: str,
    content: str,
    source: str = "manual",
) -> ChapterVersion:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    chapter = get_or_create_chapter(session, book_id=book_id, chapter_number=chapter_number, title=title)
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=_next_version(session, chapter.id),
        title=title,
        content=content,
        status="draft",
        source=source,
    )
    session.add(version)
    session.flush()
    return version


def seed_prompts(session: Session) -> list[PromptTemplate]:
    return seed_prompt_templates(session)


def approve_chapter(session: Session, *, version_id: int, reviewer: str) -> ChapterVersion:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    quality = session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version.id)
        .order_by(QualityReport.id.desc())
    )
    if _quality_has_unresolved_gate_blocker(quality):
        raise ValueError("当前版本仍有章节类型/硬门禁失败项，不能采用。")
    if version.status == "needs_revision":
        if not quality or not quality.passed:
            raise ValueError("当前版本仍未通过质检，不能采用。")
        version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    for brief in session.scalars(
        select(ChapterBrief).where(ChapterBrief.chapter_id == version.chapter_id, ChapterBrief.status == "revision_ready")
    ):
        brief.status = "superseded"
    version.status = move("chapter_version", version.status, "approved", "human_approve")
    session.add(ChapterReview(chapter_version_id=version.id, verdict="approved", reviewer=reviewer, notes="manual approval"))
    session.flush()
    return version


def _quality_has_unresolved_gate_blocker(quality: QualityReport | None) -> bool:
    if not quality:
        return False
    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    chapter_type_gate = data.get("chapter_type_gate") if isinstance(data.get("chapter_type_gate"), dict) else {}
    # Sprint 2 P2-Ch44 soft-pass: same rationale as planning.py mirror.
    soft_pass_active = bool(chapter_type_gate.get("soft_pass"))
    issues = [str(item) for item in data.get("issues") or []]
    if any(item.startswith("chapter_type_gate_failed") for item in issues) and not soft_pass_active:
        return True
    if chapter_type_gate and not bool(chapter_type_gate.get("passed")) and not soft_pass_active:
        return True
    hard_gate = data.get("hard_gate") if isinstance(data.get("hard_gate"), dict) else {}
    if hard_gate and not bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS"):
        return True
    return False


def list_books(session: Session) -> list[Book]:
    return list(session.scalars(select(Book).order_by(Book.id)))


def get_book(session: Session, *, book_id: int) -> Book:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    return book


def list_chapters(session: Session, *, book_id: int) -> list[Chapter]:
    return list(session.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number)))


def latest_chapter_version(session: Session, *, chapter_id: int) -> ChapterVersion | None:
    return session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.id.desc()))
