from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import parse_review_output
from app.models.entities import Book, Chapter, ChapterBrief, ChapterReview, ChapterVersion, GenerationTask, QualityReport
from app.services.canon import format_canon_context
from app.services.chapter_standards import extract_min_chars
from app.services.llm_errors import classify_exception
from app.services.editorial_stratification import maybe_apply_editorial_stratification, maybe_rollback_failed_elevation
from app.services.production_llm import (
    llm_parameter_snapshot,
    llm_usage_payload,
    record_generation_llm_log,
)
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.services.quality import evaluate_chapter
from app.services.production_gate import assert_production_gate
from app.services.revision_comparison import compare_and_restore_if_regressed
from app.services.review_decision import ReviewRuleResult, apply_review_decision, soft_override_blockers
from app.workflows.state_machine import move


def review_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    llm_review: bool = False,
    review_dry_run: bool = True,
    auto_revision_brief: bool = False,
) -> QualityReport:
    assert_production_gate(session, book_id=book_id, action="review_chapter")
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
        min_chars=extract_min_chars(
            brief.goal if brief else "",
            brief.required_beats if brief else "",
            brief.constraints if brief else "",
        ),
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
        _apply_editorial_gate(result, report_data)
    quality = QualityReport(
        chapter_version_id=version.id,
        score=int(report_data.get("score") or result.score),
        passed=bool(report_data.get("passed", result.passed)),
        report=json.dumps(report_data, ensure_ascii=False),
    )
    review = ChapterReview(
        chapter_version_id=version.id,
        verdict="pass" if quality.passed else "needs_revision",
        notes=result.report,
        reviewer="system-quality-gate",
    )
    session.add(quality)
    session.add(review)
    target = "approved" if quality.passed and version.status == "approved" else ("reviewed_pass" if quality.passed else "needs_revision")
    action = "quality_pass" if quality.passed else "quality_fail"
    version.status = move("chapter_version", version.status, target, action)
    session.flush()
    maybe_apply_editorial_stratification(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        quality=quality,
    )
    maybe_rollback_failed_elevation(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        failed_version=version,
        quality=quality,
    )
    compare_and_restore_if_regressed(session, current_version=version, current_quality=quality)
    if auto_revision_brief and not quality.passed:
        from app.services.production import create_revision_brief

        create_revision_brief(session, book_id=book_id, chapter_number=chapter_number)
    return quality


def reconcile_existing_quality_report(
    session: Session,
    *,
    version: ChapterVersion,
    quality: QualityReport,
) -> bool:
    try:
        report_data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    if bool(report_data.get("passed")) and quality.passed and version.status == "reviewed_pass":
        return True
    llm_review = report_data.get("llm_review") if isinstance(report_data.get("llm_review"), dict) else {}
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    if llm_review.get("status") != "completed" or llm_review.get("verdict") != "pass":
        return False
    if int(llm_review.get("score") or 0) < 75 or not bool(hard_gate.get("passed")):
        return False
    rule_result = ReviewRuleResult(passed=bool(report_data.get("passed")), score=int(report_data.get("score") or quality.score or 0))
    _apply_editorial_gate(rule_result, report_data)
    if not bool(report_data.get("passed")):
        return False
    quality.passed = True
    quality.score = int(report_data.get("score") or quality.score or 0)
    quality.report = json.dumps(report_data, ensure_ascii=False)
    if version.status == "needs_revision":
        version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    session.flush()
    return True


def _apply_editorial_gate(rule_result, report_data: dict) -> None:
    if not isinstance(rule_result, ReviewRuleResult):
        rule_result = ReviewRuleResult(passed=bool(rule_result.passed), score=int(rule_result.score))
    apply_review_decision(rule_result, report_data)


def _soft_override_blockers(dimensions: dict) -> list[str]:
    return soft_override_blockers(dimensions)


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
    template = get_prompt_template(session, name="review_chapter", version="v2")
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
    model = settings.llm_review_model
    temperature = settings.llm_review_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_review_max_tokens,
        temperature=temperature,
        model=model,
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
            temperature=temperature,
            model=model,
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
                **llm_usage_payload(response, prompt=prompt),
                "review": review.to_dict(),
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    record_generation_llm_log(
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


def _latest_brief(session: Session, chapter_id: int) -> ChapterBrief | None:
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))
