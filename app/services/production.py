from __future__ import annotations

import json
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.automation.openclaw_ops import OpenClawPublishingOperator
from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError, parse_draft_output, parse_review_output
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterReview,
    ChapterVersion,
    GenerationTask,
    LLMRequestLog,
    PublishJob,
    PublishExecution,
    PublishingTarget,
    PromptTemplate,
    QualityReport,
    StoryFoundation,
)
from app.services.canon import format_canon_context
from app.services.evidence import format_market_evidence_context
from app.services.llm_errors import classify_exception
from app.services.llm_audit import record_llm_request
from app.services.quality import evaluate_chapter
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.workflows.state_machine import WorkflowError, move


def _llm_usage_payload(response, *, prompt: str) -> dict:
    actual_prompt, actual_response, actual_total = _actual_usage_tokens(response.usage)
    return {
        "prompt_chars": len(prompt),
        "response_chars": len(response.text),
        "estimated_prompt_tokens": response.estimated_prompt_tokens,
        "estimated_response_tokens": response.estimated_response_tokens,
        "estimated_total_tokens": response.estimated_prompt_tokens + response.estimated_response_tokens,
        "actual_prompt_tokens": actual_prompt,
        "actual_response_tokens": actual_response,
        "actual_total_tokens": actual_total,
        "elapsed_ms": response.elapsed_ms,
        "usage": response.usage,
        "request_id": response.request_id,
    }


def _llm_parameter_snapshot(*, dry_run: bool, max_tokens: int, temperature: float | None) -> dict:
    return {
        "provider_mode": "dry_run" if dry_run else "live",
        "requested_model": settings.model_name,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }


def _record_generation_llm_log(
    session: Session,
    *,
    task: GenerationTask,
    response,
    prompt_template: str,
    prompt: str,
    status: str,
    error_category: str = "",
) -> LLMRequestLog:
    actual_prompt, actual_response, actual_total = _actual_usage_tokens(response.usage)
    return record_llm_request(
        session,
        book_id=task.book_id,
        task_type=task.task_type,
        generation_task_id=task.id,
        provider=response.provider,
        model=response.model,
        request_id=response.request_id,
        prompt_template=prompt_template,
        prompt_chars=len(prompt),
        response_chars=len(response.text),
        estimated_prompt_tokens=response.estimated_prompt_tokens,
        estimated_response_tokens=response.estimated_response_tokens,
        actual_prompt_tokens=actual_prompt,
        actual_response_tokens=actual_response,
        actual_total_tokens=actual_total,
        elapsed_ms=response.elapsed_ms,
        status=status,
        error_category=error_category,
    )


def _actual_usage_tokens(usage: dict | None) -> tuple[int, int, int]:
    if not usage:
        return 0, 0, 0
    prompt = int(usage.get("prompt_tokens") or 0)
    response = int(usage.get("completion_tokens") or usage.get("response_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + response)
    return prompt, response, total


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


def get_or_create_chapter(session: Session, *, book_id: int, chapter_number: int, title: str = "") -> Chapter:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if chapter:
        return chapter
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=title or f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    return chapter


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
    brief = ChapterBrief(chapter_id=chapter.id, goal=goal, required_beats=required_beats, constraints=constraints, status="ready")
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


def _latest_foundation(session: Session, book_id: int) -> StoryFoundation | None:
    return session.scalar(
        select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc())
    )


def _latest_brief(session: Session, chapter_id: int) -> ChapterBrief | None:
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))


def _next_version(session: Session, chapter_id: int) -> int:
    current = session.scalar(select(func.max(ChapterVersion.version_number)).where(ChapterVersion.chapter_id == chapter_id))
    return int(current or 0) + 1


def draft_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = get_or_create_chapter(session, book_id=book_id, chapter_number=chapter_number)
    foundation = _latest_foundation(session, book_id)
    brief = _latest_brief(session, chapter.id)
    if not foundation:
        raise ValueError("story foundation is required before drafting")
    if not brief:
        raise ValueError("chapter brief is required before drafting")
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="draft_chapter", version="v3")
    market_evidence, market_signal_ids = format_market_evidence_context(session, genre=book.genre)
    canon_context, canon_refs = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        market_evidence=market_evidence,
        canon_context=canon_context,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
        chapter_number=chapter_number,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
    )
    provider = get_provider(dry_run)
    llm_parameters = _llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=settings.llm_temperature,
    )
    response = provider.generate(
        prompt,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=settings.llm_temperature,
    )
    try:
        draft = parse_draft_output(response.text)
    except StructuredOutputError as exc:
        task = GenerationTask(
            book_id=book_id,
            task_type="draft_chapter",
            status="failed",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "dry_run": dry_run,
                    "prompt_template": f"{template.name}@{template.version}",
                    "llm_parameters": llm_parameters,
                    "market_signal_ids": market_signal_ids,
                    "canon_refs": canon_refs,
                },
                ensure_ascii=False,
            ),
            output_json=json.dumps(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "llm_parameters": llm_parameters,
                    "error": str(exc),
                    "raw": response.text[:2000],
                    **_llm_usage_payload(response, prompt=prompt),
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        _record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="structured_output",
        )
        raise
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=_next_version(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",
        source=response.provider,
    )
    session.add(version)
    session.flush()
    task = GenerationTask(
        book_id=book_id,
        task_type="draft_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
                "market_signal_ids": market_signal_ids,
                "market_evidence_count": len(market_signal_ids),
                "canon_refs": canon_refs,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                **_llm_usage_payload(response, prompt=prompt),
                "self_check": draft.self_check,
                "used_brief_points": draft.used_brief_points,
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    _record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return version


def seed_prompts(session: Session) -> list[PromptTemplate]:
    return seed_prompt_templates(session)


def review_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    llm_review: bool = False,
    review_dry_run: bool = True,
) -> QualityReport:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    brief = _latest_brief(session, chapter.id)
    canon_context, _ = format_canon_context(session, book_id=book_id)
    result = evaluate_chapter(
        version.content,
        goal=brief.goal if brief else "",
        required_beats=brief.required_beats if brief else "",
        constraints=brief.constraints if brief else "",
        canon_context=canon_context,
    )
    report_data = json.loads(result.report)
    if llm_review:
        report_data["llm_review"] = _run_llm_chapter_review(
            session,
            book=session.get(Book, book_id),
            version=version,
            chapter_number=chapter_number,
            goal=brief.goal if brief else "",
            required_beats=brief.required_beats if brief else "",
            constraints=brief.constraints if brief else "",
            canon_context=canon_context,
            rule_report=result.report,
            dry_run=review_dry_run,
        )
    quality = QualityReport(
        chapter_version_id=version.id,
        score=result.score,
        passed=result.passed,
        report=json.dumps(report_data, ensure_ascii=False),
    )
    review = ChapterReview(
        chapter_version_id=version.id,
        verdict="pass" if result.passed else "needs_revision",
        notes=result.report,
        reviewer="system-quality-gate",
    )
    session.add(quality)
    session.add(review)
    target = "reviewed_pass" if result.passed else "needs_revision"
    action = "quality_pass" if result.passed else "quality_fail"
    version.status = move("chapter_version", version.status, target, action)
    session.flush()
    return quality


def _run_llm_chapter_review(
    session: Session,
    *,
    book: Book | None,
    version: ChapterVersion,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    canon_context: str,
    rule_report: str,
    dry_run: bool,
) -> dict:
    if not book:
        return {"status": "failed", "error_category": "validation", "error": "book not found"}
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="review_chapter", version="v1")
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        rule_report=rule_report,
        canon_context=canon_context,
        chapter_content=version.content,
    )
    provider = get_provider(dry_run)
    llm_parameters = _llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_review_max_tokens,
        temperature=settings.llm_temperature,
    )
    input_json = {
        "chapter_number": chapter_number,
        "dry_run": dry_run,
        "prompt_template": f"{template.name}@{template.version}",
        "llm_parameters": llm_parameters,
        "version_id": version.id,
    }
    try:
        response = provider.generate(
            prompt,
            max_tokens=settings.llm_review_max_tokens,
            temperature=settings.llm_temperature,
        )
        review = parse_review_output(response.text)
    except Exception as exc:
        classification = classify_exception(exc)
        task = GenerationTask(
            book_id=book.id,
            task_type="llm_review_chapter",
            status="failed",
            input_json=json.dumps(input_json, ensure_ascii=False),
            output_json=json.dumps(
                {
                    "error_category": classification.category,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "llm_parameters": llm_parameters,
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        return {
            "status": "failed",
            "generation_task_id": task.id,
            "error_category": classification.category,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    task = GenerationTask(
        book_id=book.id,
        task_type="llm_review_chapter",
        status="completed",
        input_json=json.dumps(input_json, ensure_ascii=False),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                **_llm_usage_payload(response, prompt=prompt),
                "review": review.to_dict(),
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    _record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return {
        "status": "completed",
        "generation_task_id": task.id,
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        **review.to_dict(),
    }


def create_revision_brief(session: Session, *, book_id: int, chapter_number: int) -> ChapterBrief:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    if version.status != "needs_revision":
        raise ValueError("revision brief requires latest chapter version to be needs_revision")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc())
    )
    if not quality:
        raise ValueError("quality report is required before revision brief")
    try:
        quality_data = json.loads(quality.report)
    except json.JSONDecodeError:
        quality_data = {"raw_report": quality.report}
    dimensions = quality_data.get("dimensions", {}) if isinstance(quality_data, dict) else {}
    issues = quality_data.get("issues", []) if isinstance(quality_data, dict) else []
    weak_dimensions = [name for name, score in dimensions.items() if isinstance(score, int) and score < 70]
    goal = f"修订第{chapter_number}章，使质量门禁从失败恢复到可复审状态。"
    required = "；".join(
        [
            *(f"提升维度：{name}" for name in weak_dimensions),
            *(f"修复问题：{issue}" for issue in issues),
        ]
    )
    if not required:
        required = "根据质量报告补足章节完整度、连续性和平台可发布性。"
    constraints = "保留已登记 Canon，不引入无代价能力，不输出系统元信息；修订后必须重新走 review-chapter。"
    brief = ChapterBrief(chapter_id=chapter.id, goal=goal, required_beats=required, constraints=constraints, status="revision_ready")
    session.add(brief)
    session.flush()
    return brief


def revise_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    source_version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not source_version:
        raise ValueError("chapter version not found")
    if source_version.status != "needs_revision":
        raise ValueError("latest chapter version must be needs_revision before revise")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == source_version.id).order_by(QualityReport.id.desc())
    )
    if not quality:
        raise ValueError("quality report is required before revise")
    revision_brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not revision_brief:
        raise ValueError("revision brief is required before revise")
    foundation = _latest_foundation(session, book_id)
    if not foundation:
        raise ValueError("story foundation is required before revising")
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="revise_chapter", version="v1")
    market_evidence, market_signal_ids = format_market_evidence_context(session, genre=book.genre)
    canon_context, canon_refs = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        previous_content=source_version.content,
        quality_report=quality.report,
        revision_goal=revision_brief.goal,
        revision_required_beats=revision_brief.required_beats,
        revision_constraints=revision_brief.constraints,
        market_evidence=market_evidence,
        canon_context=canon_context,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
    )
    provider = get_provider(dry_run)
    llm_parameters = _llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=settings.llm_temperature,
    )
    response = provider.generate(
        prompt,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=settings.llm_temperature,
    )
    try:
        draft = parse_draft_output(response.text)
    except StructuredOutputError as exc:
        task = GenerationTask(
            book_id=book_id,
            task_type="revise_chapter",
            status="failed",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "dry_run": dry_run,
                    "prompt_template": f"{template.name}@{template.version}",
                    "llm_parameters": llm_parameters,
                    "source_version_id": source_version.id,
                    "quality_report_id": quality.id,
                    "revision_brief_id": revision_brief.id,
                    "market_signal_ids": market_signal_ids,
                    "canon_refs": canon_refs,
                },
                ensure_ascii=False,
            ),
            output_json=json.dumps(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "llm_parameters": llm_parameters,
                    "error": str(exc),
                    "raw": response.text[:2000],
                    **_llm_usage_payload(response, prompt=prompt),
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        _record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="structured_output",
        )
        raise
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=_next_version(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",
        source=f"revision:{response.provider}",
    )
    session.add(version)
    session.flush()
    task = GenerationTask(
        book_id=book_id,
        task_type="revise_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
                "source_version_id": source_version.id,
                "quality_report_id": quality.id,
                "revision_brief_id": revision_brief.id,
                "market_signal_ids": market_signal_ids,
                "canon_refs": canon_refs,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                **_llm_usage_payload(response, prompt=prompt),
                "self_check": draft.self_check,
                "used_brief_points": draft.used_brief_points,
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    _record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return version


def approve_chapter(session: Session, *, version_id: int, reviewer: str) -> ChapterVersion:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    version.status = move("chapter_version", version.status, "approved", "human_approve")
    session.add(ChapterReview(chapter_version_id=version.id, verdict="approved", reviewer=reviewer, notes="manual approval"))
    session.flush()
    return version


def create_publish_job(session: Session, *, version_id: int, platform: str) -> PublishJob:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    if version.status != "approved":
        raise ValueError("only approved chapter versions can create publish jobs")
    existing = session.scalar(
        select(PublishJob).where(
            PublishJob.chapter_version_id == version_id,
            PublishJob.platform == platform,
            PublishJob.status.in_(["pending", "dry_run_ready", "queued", "published"]),
        )
    )
    if existing:
        raise ValueError(f"active publish job already exists: {existing.id} ({existing.status})")
    target = get_publishing_target(session, platform=platform)
    payload = {}
    if target:
        payload = {
            "publishing_target_id": target.id,
            "account_label": target.account_label,
            "work_identifier": target.work_identifier,
            "automation_mode": target.automation_mode,
            "target_config": _loads_json(target.config_json),
        }
    job = PublishJob(
        chapter_version_id=version_id,
        platform=platform,
        status="pending",
        automation_payload=json.dumps(payload, ensure_ascii=False),
    )
    session.add(job)
    session.flush()
    return job


def upsert_publishing_target(
    session: Session,
    *,
    platform: str,
    account_label: str = "",
    work_identifier: str = "",
    automation_mode: str = "manual",
    status: str = "active",
    config_json: str = "{}",
) -> PublishingTarget:
    if not platform:
        raise ValueError("platform is required")
    _loads_json(config_json)
    target = session.scalar(
        select(PublishingTarget).where(
            PublishingTarget.platform == platform,
            PublishingTarget.account_label == account_label,
            PublishingTarget.work_identifier == work_identifier,
        )
    )
    if not target:
        target = PublishingTarget(platform=platform, account_label=account_label, work_identifier=work_identifier)
        session.add(target)
    target.automation_mode = automation_mode
    target.status = status
    target.config_json = config_json
    session.flush()
    return target


def get_publishing_target(
    session: Session,
    *,
    platform: str,
    account_label: str = "",
    work_identifier: str = "",
) -> PublishingTarget | None:
    stmt = select(PublishingTarget).where(PublishingTarget.platform == platform, PublishingTarget.status == "active")
    if account_label:
        stmt = stmt.where(PublishingTarget.account_label == account_label)
    if work_identifier:
        stmt = stmt.where(PublishingTarget.work_identifier == work_identifier)
    return session.scalar(stmt.order_by(PublishingTarget.id.desc()))


def list_publishing_targets(session: Session, *, platform: str = "", status: str = "") -> list[PublishingTarget]:
    stmt = select(PublishingTarget).order_by(PublishingTarget.id)
    if platform:
        stmt = stmt.where(PublishingTarget.platform == platform)
    if status:
        stmt = stmt.where(PublishingTarget.status == status)
    return list(session.scalars(stmt))


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


def list_publish_jobs(session: Session, *, status: str = "") -> list[PublishJob]:
    stmt = select(PublishJob).order_by(PublishJob.id)
    if status:
        stmt = stmt.where(PublishJob.status == status)
    return list(session.scalars(stmt))


def get_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    return job


def list_publish_executions(session: Session, *, job_id: int | None = None, limit: int = 20) -> list[PublishExecution]:
    stmt = select(PublishExecution).order_by(PublishExecution.id.desc()).limit(limit)
    if job_id is not None:
        stmt = stmt.where(PublishExecution.publish_job_id == job_id)
    return list(session.scalars(stmt))


def publish_job_dry_run(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError("publish job points to missing chapter version")
    if version.status != "approved":
        raise ValueError("publish dry-run requires approved chapter version")
    operator = OpenClawPublishingOperator()
    result = operator.publish_dry_run(platform=job.platform, title=version.title, content=version.content)
    job.status = move("publish_job", job.status, result.status, "dry_run")
    job.result_report = result.report
    session.flush()
    return job


def queue_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    job.status = move("publish_job", job.status, "queued", "queue_for_platform")
    session.flush()
    return job


def mark_publish_job(session: Session, *, job_id: int, status: str, report: str = "") -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    if status == "published":
        action = "mark_published"
    elif status == "failed":
        action = "mark_failed"
    else:
        raise WorkflowError("publish job can only be marked published or failed")
    job.status = move("publish_job", job.status, status, action)
    if report:
        job.result_report = report
    session.flush()
    return job


def retry_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    job.status = move("publish_job", job.status, "queued", "retry")
    session.flush()
    return job


def execute_publish_job(session: Session, *, job_id: int, confirm: bool = False) -> tuple[PublishJob, PublishExecution]:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError("publish job points to missing chapter version")
    if job.status != "queued":
        raise ValueError("publish execution requires queued publish job")
    operator = OpenClawPublishingOperator()
    if not confirm:
        result = operator.publish_dry_run(platform=job.platform, title=version.title, content=version.content)
        execution = PublishExecution(
            publish_job_id=job.id,
            platform=job.platform,
            status="blocked",
            automation_mode="confirmation_required",
            report=f"Final publish confirmation required. {result.report}",
        )
        session.add(execution)
        session.flush()
        return job, execution
    result = operator.publish_confirmed(platform=job.platform, title=version.title, content=version.content)
    target_status = "published" if result.status == "published" else "failed"
    action = "mark_published" if target_status == "published" else "mark_failed"
    job.status = move("publish_job", job.status, target_status, action)
    job.result_report = result.report
    execution = PublishExecution(
        publish_job_id=job.id,
        platform=job.platform,
        status=target_status,
        automation_mode="confirmed",
        report=result.report,
    )
    session.add(execution)
    session.flush()
    return job, execution


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON object is required")
    return data
