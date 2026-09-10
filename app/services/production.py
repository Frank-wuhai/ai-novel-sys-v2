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
from app.services.chapter_standards import ensure_chapter_production_standard, _resolve_chapter_type
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
        constraints=ensure_chapter_production_standard(constraints, chapter_number=chapter_number, chapter_type=_resolve_chapter_type(chapter_number)),
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
    # approve_chapter is workflow progression; content gates already ran upstream.
    if version.status == "needs_revision":
        if quality and not quality.passed and _quality_allows_human_soft_acceptance(quality) and _is_human_reviewer(reviewer):
            quality = _record_human_soft_acceptance_quality(
                session,
                quality=quality,
                reviewer=reviewer,
                reason="硬门槛通过，规则软质检未满；作者/人工确认小瑕疵不影响整体阅读，准许采用。",
            )
        if not quality or not quality.passed:
            raise ValueError("当前版本仍未通过质检，不能采用。")
        version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    for brief in session.scalars(
        select(ChapterBrief).where(ChapterBrief.chapter_id == version.chapter_id, ChapterBrief.status == "revision_ready")
    ):
        brief.status = "superseded"
    version.status = move("chapter_version", version.status, "approved", "human_approve")
    if chapter := session.get(Chapter, version.chapter_id):
        chapter.status = "approved"
        chapter.title = version.title or chapter.title
    review = ChapterReview(chapter_version_id=version.id, verdict="approved", reviewer=reviewer, notes="manual approval")
    session.add(review)
    session.flush()
    memory_result: dict[str, int | str] = {"states": 0, "facts": 0, "hooks": 0, "summaries": 0}
    try:
        from app.services.long_term_memory import sync_long_term_memory_for_version
        memory_result = sync_long_term_memory_for_version(session, chapter_version_id=version.id)
    except Exception as exc:
        memory_result = {"states": 0, "facts": 0, "hooks": 0, "summaries": 0, "error": exc.__class__.__name__}
    review.notes = "manual approval; long_term_memory=" + json.dumps(memory_result, ensure_ascii=False, sort_keys=True)
    session.flush()
    return version


def _is_human_reviewer(reviewer: str) -> bool:
    value = (reviewer or "").strip().lower()
    if not value:
        return False
    return not value.startswith(("auto", "system", "pipeline", "cron", "worker"))


def _record_human_soft_acceptance_quality(
    session: Session,
    *,
    quality: QualityReport,
    reviewer: str,
    reason: str,
) -> QualityReport:
    try:
        report_data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        report_data = {"raw_report": quality.report or ""}
    if not isinstance(report_data, dict):
        report_data = {"raw_report": str(report_data)}
    report_data["status"] = "PASS"
    report_data["passed"] = True
    report_data["human_acceptance"] = {
        "schema": "human_acceptance_v1",
        "reviewer": reviewer,
        "decision": "pass",
        "reason": reason,
        "based_on_quality_report_id": quality.id,
    }
    final_verdict = report_data.setdefault("final_verdict", {})
    if isinstance(final_verdict, dict):
        final_verdict.update(
            {
                "status": "reviewed_pass",
                "label": "人工确认通过",
                "reason": reason,
                "source": "human_acceptance_v1",
            }
        )
    accepted = QualityReport(
        chapter_version_id=quality.chapter_version_id,
        score=quality.score,
        passed=True,
        report=json.dumps(report_data, ensure_ascii=False),
    )
    session.add(accepted)
    session.add(
        ChapterReview(
            chapter_version_id=quality.chapter_version_id,
            verdict="pass",
            reviewer=reviewer,
            notes=reason,
        )
    )
    session.flush()
    return accepted


def _quality_allows_human_soft_acceptance(quality: QualityReport) -> bool:
    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    hard_gate = data.get("hard_gate") if isinstance(data.get("hard_gate"), dict) else {}
    if not bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS"):
        return False
    issues = [str(item) for item in data.get("issues", []) if item]
    hard_prefixes = (
        "bias_blocker",
        "forbidden_marker",
        "setting_contradiction",
        "system_artifact",
        "platform_risk",
        "too_short",
        "too_long",
    )
    if any(issue.startswith(hard_prefixes) for issue in issues):
        return False
    chapter_type_gate = data.get("chapter_type_gate") if isinstance(data.get("chapter_type_gate"), dict) else {}
    if chapter_type_gate and not bool(chapter_type_gate.get("passed") or chapter_type_gate.get("soft_pass")):
        return False
    stratification = data.get("editorial_stratification") if isinstance(data.get("editorial_stratification"), dict) else {}
    tier = str(stratification.get("tier") or "")
    if tier in {"A_near_final", "B_solid_draft"}:
        return True
    guidance = data.get("editorial_guidance") if isinstance(data.get("editorial_guidance"), dict) else {}
    level = str(guidance.get("level") or "")
    return level in {"准定稿", "合格底稿"}


def _quality_has_unresolved_gate_blocker(quality: QualityReport | None) -> bool:
    if not quality:
        return False
    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    chapter_type_gate = data.get("chapter_type_gate") if isinstance(data.get("chapter_type_gate"), dict) else {}
    hard_gate = data.get("hard_gate") if isinstance(data.get("hard_gate"), dict) else {}
    hard_gate_ok = bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS")
    # A 方案（2026-07-23）· type_gate 越权否决清理（精准边界）：
    # 常规连载推进章(serial_progress) → quality 层已放行(hard_gate过)时 type_gate
    # 不阻塞。生死线章型(strict=True: opening/early_serial/turning_point) → 保留
    # type_gate 否决权，即使 hard_gate 过、type_gate 未过仍阻塞。soft_pass 逃生阀保留。
    is_strict_chapter = bool(chapter_type_gate.get("strict"))
    quality_layer_cleared = hard_gate_ok and not is_strict_chapter
    soft_pass_active = bool(chapter_type_gate.get("soft_pass")) or quality_layer_cleared
    issues = [str(item) for item in data.get("issues") or []]
    if any(item.startswith("chapter_type_gate_failed") for item in issues) and not soft_pass_active:
        return True
    if chapter_type_gate and not bool(chapter_type_gate.get("passed")) and not soft_pass_active:
        return True
    if hard_gate and not hard_gate_ok:
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


def current_chapter_version(session: Session, *, chapter_id: int) -> ChapterVersion | None:
    approved = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.status == "approved")
        .order_by(ChapterVersion.id.desc())
    )
    if approved:
        return approved
    reviewed = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.status == "reviewed_pass")
        .order_by(ChapterVersion.id.desc())
    )
    if reviewed:
        return reviewed
    return latest_chapter_version(session, chapter_id=chapter_id)
