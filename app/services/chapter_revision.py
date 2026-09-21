from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, FeedbackAdjustment, GenerationTask, QualityReport
from app.services.bias import apply_model_drift_local_patch, evaluate_generation_bias
from app.services.brief_sanitizer import sanitize_chapter_brief_fields, sanitize_prompt_contract_text
from app.services.chapter_standards import ensure_chapter_production_standard, _resolve_chapter_type
from app.services.chapter_unit_plans import align_chapter_unit_plan
from app.services.feedback import REVISION_MODE_FRESH, REVISION_MODE_LOCAL_PATCH, build_rewrite_contract
from app.services.production_llm import (
    compress_overlong_draft_output,
    expand_short_draft_output,
    llm_parameter_snapshot,
    llm_usage_payload,
    parse_or_repair_json_object,
    parse_or_repair_draft_output,
    record_generation_llm_log,
    repair_humanized_unit_flow,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.production_gate import assert_production_gate
from app.services.production_optimization import apply_skeleton_preflight_to_brief
from app.services.production_run_review import record_production_run_review
from app.services.production_state import brief_has_revision_artifacts, latest_foundation, latest_story_brief, next_version_number
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.services.quality import chinese_chars
from app.services.chapter_units import split_chapter_units
from app.services.reference_craft import build_reference_craft_block, evaluate_reference_craft
from app.services.world_logic import evaluate_world_logic, game_world_meta_leak_reasons
from app.services.paragraph_aesthetic import format_paragraph_aesthetic_contract
from app.services.revision_success_boost import apply_revision_success_boost
from app.services.revision_contract_manager import normalize_active_revision_contract, prepare_new_revision_contract
from app.services.writer_loop import build_writer_loop_plan, local_revision_brief_lines


def create_revision_brief(session: Session, *, book_id: int, chapter_number: int) -> ChapterBrief:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )
    if not version:
        raise ValueError("chapter version not found")
    if version.status != "needs_revision":
        raise ValueError("revision brief requires latest chapter version to be needs_revision")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc())
    )
    if not quality:
        raise ValueError("quality report is required before revision brief")
    prepare_new_revision_contract(session, chapter_id=chapter.id)
    try:
        quality_data = json.loads(quality.report)
    except json.JSONDecodeError:
        quality_data = {"raw_report": quality.report}
    dimensions = quality_data.get("dimensions", {}) if isinstance(quality_data, dict) else {}
    issues = quality_data.get("issues", []) if isinstance(quality_data, dict) else []
    llm_review = quality_data.get("llm_review", {}) if isinstance(quality_data, dict) else {}
    base_brief = _latest_story_brief_for_revision(session, chapter=chapter)
    goal = _revision_story_goal(chapter_number=chapter_number, base_brief=base_brief)
    failure_class = quality_data.get("production_failure_classification") if isinstance(quality_data.get("production_failure_classification"), dict) else {}
    if failure_class.get("category") == "structure_rewrite":
        required = _structural_rewrite_required_beats(
            chapter_number=chapter_number,
            base_brief=base_brief,
            quality_data=quality_data,
            failure_class=failure_class,
        )
    else:
        weak_dimensions = [name for name, score in dimensions.items() if isinstance(score, int) and score < 70]
        required = "；".join(
            [
                _revision_story_intent(chapter_number=chapter_number, base_brief=base_brief),
                *_revision_dimension_beats(weak_dimensions),
                *_revision_issue_beats(issues),
                *_chapter_unit_beats(quality_data),
                *_llm_review_diagnostic_beats(llm_review),
                *_revision_trend_beats(session, chapter=chapter, current_dimensions=dimensions),
                *_editor_in_chief_beats(quality_data),
                *_paragraph_aesthetic_beats(quality_data),
                *local_revision_brief_lines(quality_data, chapter_number=chapter_number),
            ]
        )
    feedback_requirements = _latest_feedback_requirements(session, book_id=book_id, chapter_number=chapter_number)
    if feedback_requirements:
        required = "；".join([item for item in [required, *feedback_requirements] if item])
    if not required:
        required = "根据质量报告补足章节完整度、连续性和平台可发布性。"
    constraints = ensure_chapter_production_standard(
        _revision_story_constraints(base_brief=base_brief),
        chapter_number=chapter_number,
        chapter_type=_resolve_chapter_type(chapter_number),
    )
    if failure_class.get("category") != "structure_rewrite":
        writer_loop = build_writer_loop_plan(
            chapter_number=chapter_number,
            goal=goal,
            required_beats=required,
            constraints=constraints,
            quality_report=quality_data,
            previous_content=version.content or "",
            mode="revision_brief",
        )
        required = "；".join([required, *writer_loop.rewrite_directives, *writer_loop.acceptance_checks])
    else:
        constraints = "；".join(
            [
                constraints,
                "revision_mode:rewrite",
                "结构性失败必须整章重构：按 6-8 个连续小单元重写，禁止继续局部补丁。",
                "正文必须控制在1800-2500中文字符；超过2800直接视为膨胀失败。",
            ]
        )
    goal, required, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required,
        constraints=constraints,
    )
    brief = ChapterBrief(chapter_id=chapter.id, goal=goal, required_beats=required, constraints=constraints, status="revision_ready")
    session.add(brief)
    session.flush()
    normalize_active_revision_contract(session, chapter_id=chapter.id, quality=quality)
    return brief


def revise_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    assert_production_gate(session, book_id=book_id, action="revise_chapter")
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    source_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )
    if not source_version:
        raise ValueError("chapter version not found")
    if source_version.status != "needs_revision":
        raise ValueError("latest chapter version must be needs_revision before revise")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == source_version.id).order_by(QualityReport.id.desc())
    )
    revision_brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not revision_brief:
        raise ValueError("revision brief is required before revise")
    if not quality:
        quality = _fallback_quality_for_recovery_revision(
            session,
            source_version=source_version,
            revision_brief=revision_brief,
        )
    if not quality and not _brief_has_feedback_marker(revision_brief) and not _brief_has_actionable_revision_plan(revision_brief):
        raise ValueError("quality report is required before revise")
    # 2026-09-20 第 4.5 步: 版本回退恢复从 QC 评审收归修订入口。进入修订流时若
    # 最新稿是相对其源稿的回退修订(revision: 链), 先恢复源稿再修订, 避免沿更差
    # 版本继续修; 含用户裁决指令的修订稿受 compare_and_restore 保护, 不会被逆转。
    if quality and str(source_version.source or "").startswith("revision:"):
        from app.services.revision_comparison import compare_and_restore_if_regressed

        comparison = compare_and_restore_if_regressed(
            session, current_version=source_version, current_quality=quality
        )
        if comparison.restored_version_id is not None:
            source_version = session.get(ChapterVersion, comparison.restored_version_id) or source_version
            quality = session.scalar(
                select(QualityReport)
                .where(QualityReport.chapter_version_id == source_version.id)
                .order_by(QualityReport.id.desc())
            ) or quality
    boost = apply_revision_success_boost(session, book_id=book_id, chapter_number=chapter_number)
    if boost.applied:
        revision_brief = session.get(ChapterBrief, revision_brief.id) or revision_brief
    preflight_anchor_cleanup = _try_author_anchor_cleanup_revision(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        dry_run=dry_run,
    )
    if preflight_anchor_cleanup:
        return preflight_anchor_cleanup
    apply_skeleton_preflight_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=revision_brief)
    foundation = latest_foundation(session, book_id)
    if not foundation:
        raise ValueError("story foundation is required before revising")
    seed_prompt_templates(session)
    fresh_rewrite = _revision_is_fresh_rewrite(revision_brief)
    forced_world_logic_rewrite = _source_requires_current_world_logic_rewrite(source_version)
    rewrite_mode = fresh_rewrite or forced_world_logic_rewrite or _revision_requires_rewrite(revision_brief)
    revision_required_beats = _revision_required_beats(revision_brief, rewrite_mode=rewrite_mode, fresh_rewrite=fresh_rewrite)
    world_logic_constraints = _revision_world_logic_prompt_constraints(book=book, brief=revision_brief, source_version=source_version)
    if world_logic_constraints:
        revision_required_beats = "\n".join([revision_required_beats, world_logic_constraints]).strip()
    revision_constraints = "\n".join([revision_brief.constraints or "", world_logic_constraints]).strip()
    revision_prompt_goal = sanitize_prompt_contract_text(revision_brief.goal) or revision_brief.goal
    revision_context_mode = (
        "fresh"
        if fresh_rewrite
        else ("rewrite" if rewrite_mode else ("local_patch" if _revision_is_local_patch(revision_brief) else "targeted"))
    )
    packet = build_chapter_production_packet(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=revision_brief.goal,
        required_beats=revision_brief.required_beats,
        constraints=revision_constraints,
        mode="fresh" if fresh_rewrite else "revision",
        revision_goal=revision_prompt_goal,
        revision_required_beats=revision_required_beats,
        revision_constraints=revision_constraints,
        quality_report=quality.report if quality else None,
        previous_content=source_version.content,
        revision_context_mode=revision_context_mode,
        fresh_rewrite=fresh_rewrite,
        rewrite_mode=rewrite_mode,
        chapter_id=chapter.id,
        chapter_brief_id=revision_brief.id,
    )
    local_patch_version = _try_local_patch_revision(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        canon_context=packet.context.canon_context,
        dry_run=dry_run,
        rewrite_mode=rewrite_mode,
    )
    if local_patch_version:
        return local_patch_version
    if _revision_specialty(revision_brief) == "unit_flow" and not rewrite_mode:
        raise ValueError("unit_flow revision failed; refusing full-chapter fallback")
    if _revision_specialty(revision_brief) == "ending_hook" and not rewrite_mode:
        raise ValueError("ending_hook revision failed; refusing full-chapter fallback")
    if _brief_is_prose_targeted(revision_brief) and not forced_world_logic_rewrite:
        raise ValueError("prose-targeted revision requires narrow local patch; full rewrite fallback is blocked")
    template = get_prompt_template(session, name="revise_chapter", version="v5" if rewrite_mode else "v3")
    prompt_values = dict(
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        previous_content=packet.context.previous_content,
        quality_report=packet.context.quality_report,
        revision_goal=packet.blueprint.goal,
        revision_required_beats=packet.blueprint.required_beats,
        revision_constraints=packet.blueprint.constraints,
        **packet.prompt_values,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
    )
    prompt = render_template(template, **prompt_values)
    prompt, prompt_budget = _trim_revision_prompt_to_budget(template, prompt_values, prompt)
    provider = get_provider(dry_run)
    model = settings.llm_revision_model
    temperature = settings.llm_revision_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
    )
    llm_parameters["prompt_budget"] = prompt_budget
    response = provider.generate(
        prompt,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    try:
        draft = parse_or_repair_draft_output(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            max_tokens=settings.llm_revision_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节修订",
        )
    except StructuredOutputError as exc:
        task = _record_revision_generation_failure(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            source_version=source_version,
            quality=quality,
            revision_brief=revision_brief,
            template_label=f"{template.name}@{template.version}",
            llm_parameters=llm_parameters,
            packet_payload=packet.task_payload,
            response=response,
            prompt=prompt,
            error_category="structured_output",
            error=str(exc),
            extra={"raw": response.text[:2000]},
            rewrite_mode=rewrite_mode,
            fresh_rewrite=fresh_rewrite,
            terminal=True,
        )
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="structured_output",
        )
        session.commit()
        raise
    min_chars = packet.blueprint.target_min_chars
    draft, length_repair = expand_short_draft_output(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节修订",
    )
    draft, unit_flow_repair = repair_humanized_unit_flow(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节修订",
    )
    unit_report = (unit_flow_repair.get("after") if unit_flow_repair.get("accepted") else None) or unit_flow_repair.get("before")
    unit_plan_alignment = align_chapter_unit_plan(packet.chapter_unit_plan, unit_report)
    meta_leak_repair = {"attempted": False, "accepted": False}
    leak_reasons = game_world_meta_leak_reasons(draft.content or "")
    if leak_reasons:
        draft, meta_leak_repair = _repair_revision_game_world_meta_leak(
            provider,
            draft=draft,
            original_prompt=prompt,
            leak_reasons=leak_reasons,
            max_tokens=settings.llm_revision_max_tokens,
            temperature=temperature,
            model=model,
            dry_run=dry_run,
        )
        repaired_leak_reasons = game_world_meta_leak_reasons(draft.content or "")
        if repaired_leak_reasons:
            task = _record_revision_generation_failure(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                source_version=source_version,
                quality=quality,
                revision_brief=revision_brief,
                template_label=f"{template.name}@{template.version}",
                llm_parameters=llm_parameters,
                packet_payload=packet.task_payload,
                response=response,
                prompt=prompt,
                error_category="game_world_meta_leakage",
                error=(
                    "revision contains game-world meta leakage before persistence after repair: "
                    + "；".join(repaired_leak_reasons[:4])
                ),
                extra={"leak_reasons": leak_reasons[:8], "repaired_leak_reasons": repaired_leak_reasons[:8], "meta_leak_repair": meta_leak_repair},
                rewrite_mode=rewrite_mode,
                fresh_rewrite=fresh_rewrite,
                min_chars=min_chars,
                max_chars=packet.blueprint.target_max_chars,
                terminal=True,
            )
            record_generation_llm_log(
                session,
                task=task,
                response=response,
                prompt_template=f"{template.name}@{template.version}",
                prompt=prompt,
                status="failed",
                error_category="game_world_meta_leakage",
            )
            session.commit()
            raise StructuredOutputError(
                "revision contains game-world meta leakage before persistence after repair: "
                + "；".join(repaired_leak_reasons[:4])
            )
        length_repair["meta_leak_repair"] = meta_leak_repair
    if dry_run and _same_revision_content(source_version.content, draft.content):
        draft.content = "\n\n".join(
            [
                draft.content,
                "【dry-run修订验证段】主角重新审视刚才的选择，意识到章末线索已经把下一步压力推到眼前；他必须主动承担代价，而不是等待局面自行解决。",
            ]
        )
        draft.self_check = [*draft.self_check, "dry-run detected duplicate output and appended a deterministic revision delta."]
    if _same_revision_content(source_version.content, draft.content):
        task = _record_revision_generation_failure(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            source_version=source_version,
            quality=quality,
            revision_brief=revision_brief,
            template_label=f"{template.name}@{template.version}",
            llm_parameters=llm_parameters,
            packet_payload=packet.task_payload,
            response=response,
            prompt=prompt,
            error_category="duplicate_revision_output",
            error="revision output is identical to source version",
            extra={"self_check": draft.self_check},
            rewrite_mode=rewrite_mode,
            fresh_rewrite=fresh_rewrite,
            min_chars=min_chars,
            max_chars=packet.blueprint.target_max_chars,
        )
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="duplicate_revision_output",
        )
        raise ValueError("revision output is identical to source version; blocked duplicate version creation")
    max_chars = max(packet.blueprint.target_max_chars or 0, 2800)
    overlong_repair = {"attempted": False, "accepted": True}
    if not dry_run:
        draft, overlong_repair = compress_overlong_draft_output(
            provider,
            draft=draft,
            original_prompt=prompt,
            min_chars=min_chars,
            max_chars=max_chars,
            max_tokens=settings.llm_revision_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节修订",
        )
        length_repair["overlong_repair"] = overlong_repair
    draft_chars = chinese_chars(draft.content or "")
    if not dry_run and draft_chars > max_chars:
        task = _record_revision_generation_failure(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            source_version=source_version,
            quality=quality,
            revision_brief=revision_brief,
            template_label=f"{template.name}@{template.version}",
            llm_parameters=llm_parameters,
            packet_payload=packet.task_payload,
            response=response,
            prompt=prompt,
            error_category="overlong_revision_output",
            error=f"revision output exceeds max_chars before persistence: {draft_chars} > {max_chars}",
            extra={
                "content_chars": draft_chars,
                "self_check": draft.self_check,
                "length_repair": length_repair,
                "unit_flow_repair": unit_flow_repair,
                "unit_plan_alignment": unit_plan_alignment,
            },
            rewrite_mode=rewrite_mode,
            fresh_rewrite=fresh_rewrite,
            min_chars=min_chars,
            max_chars=max_chars,
            terminal=True,
        )
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="overlong_revision_output",
        )
        session.commit()
        raise ValueError(f"revision output exceeds max_chars before persistence: {draft_chars} > {max_chars}")
    anchor_rejection = _revision_author_sample_anchor_rejection(revision_brief, draft.title or "", draft.content or "")
    if anchor_rejection:
        task = _record_revision_generation_failure(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            source_version=source_version,
            quality=quality,
            revision_brief=revision_brief,
            template_label=f"{template.name}@{template.version}",
            llm_parameters=llm_parameters,
            packet_payload=packet.task_payload,
            response=response,
            prompt=prompt,
            error_category="author_sample_anchor_violation",
            error=anchor_rejection,
            extra={
                "content_chars": draft_chars,
                "title": draft.title,
                "self_check": draft.self_check,
                "length_repair": length_repair,
                "unit_flow_repair": unit_flow_repair,
            },
            rewrite_mode=rewrite_mode,
            fresh_rewrite=fresh_rewrite,
            min_chars=min_chars,
            max_chars=max_chars,
            terminal=True,
        )
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="author_sample_anchor_violation",
        )
        session.commit()
        raise StructuredOutputError(f"revision violates author sample anchors before persistence: {anchor_rejection}")
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",
        source=f"revision:{response.provider}",
    )
    session.add(version)
    session.flush()
    output_data = {
        "version_id": version.id,
        "provider": response.provider,
        "model": response.model,
        "llm_parameters": llm_parameters,
        **llm_usage_payload(response, prompt=prompt),
        "self_check": draft.self_check,
        "used_brief_points": draft.used_brief_points,
        "length_repair": length_repair,
        "unit_flow_repair": unit_flow_repair,
        "unit_plan_alignment": unit_plan_alignment,
    }
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
                "quality_report_id": quality.id if quality else None,
                "revision_brief_id": revision_brief.id,
                "rewrite_mode": rewrite_mode,
                "fresh_rewrite": fresh_rewrite,
                "min_chars": min_chars,
                "max_chars": packet.blueprint.target_max_chars,
                **packet.task_payload,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(output_data, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    record_production_run_review(
        session,
        book_id=book_id,
        chapter_id=chapter.id,
        chapter_number=chapter_number,
        version=version,
        task=task,
        output_data=output_data,
    )
    record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return version


def _trim_revision_prompt_to_budget(template, prompt_values: dict, prompt: str) -> tuple[str, dict]:
    max_chars = max(3000, int(settings.llm_revision_prompt_max_chars or 9000))
    audit = {
        "max_chars": max_chars,
        "original_chars": len(prompt),
        "final_chars": len(prompt),
        "trimmed": False,
        "policy": "revision_previous_content_quality_report_budget",
    }
    if len(prompt) <= max_chars:
        return prompt, audit
    for previous_limit, quality_limit in ((1800, 900), (1200, 700), (700, 500)):
        values = dict(prompt_values)
        values["previous_content"] = _clip_prompt_field(values.get("previous_content", ""), previous_limit, tail=True)
        values["quality_report"] = _clip_prompt_field(values.get("quality_report", ""), quality_limit)
        candidate = render_template(template, **values)
        if len(candidate) <= max_chars or (len(candidate) < len(prompt) and previous_limit == 700):
            audit.update(
                {
                    "final_chars": len(candidate),
                    "trimmed": True,
                    "previous_content_limit": previous_limit,
                    "quality_report_limit": quality_limit,
                }
            )
            return candidate, audit
    return prompt, audit


def _clip_prompt_field(value: object, limit: int, *, tail: bool = False) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if tail:
        return "...\n" + text[-limit:]
    return text[:limit] + "\n..."


def _record_revision_generation_failure(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    source_version: ChapterVersion,
    quality: QualityReport | None,
    revision_brief: ChapterBrief,
    template_label: str,
    llm_parameters: dict,
    packet_payload: dict,
    response,
    prompt: str,
    error_category: str,
    error: str,
    extra: dict | None = None,
    rewrite_mode: bool,
    fresh_rewrite: bool,
    min_chars: int | None = None,
    max_chars: int | None = None,
    terminal: bool = False,
) -> GenerationTask:
    # 2026-09-21 第 4.5 步验收腿修复: 终态失败(记完即 raise)的记录原先挂在注定被
    # session_scope 回滚的事务里——ch3 重建两连败在 DB 里零痕迹。terminal=True 时先
    # 回滚清掉在途写入(与外层传播后回滚等效), 失败记录由调用方随后 commit 独立落库。
    # 非终态调用点(记完继续流程, 如 duplicate_revision_output)保持原行为。
    if terminal:
        session.rollback()
    input_payload = {
        "chapter_number": chapter_number,
        "dry_run": response.provider == "dry_run",
        "prompt_template": template_label,
        "llm_parameters": llm_parameters,
        "source_version_id": source_version.id,
        "quality_report_id": quality.id if quality else None,
        "revision_brief_id": revision_brief.id,
        "rewrite_mode": rewrite_mode,
        "fresh_rewrite": fresh_rewrite,
        **packet_payload,
    }
    if min_chars is not None:
        input_payload["min_chars"] = min_chars
    if max_chars is not None:
        input_payload["max_chars"] = max_chars
    task = GenerationTask(
        book_id=book_id,
        task_type="revise_chapter",
        status="failed",
        input_json=json.dumps(input_payload, ensure_ascii=False),
        output_json=json.dumps(
            {
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                "error_category": error_category,
                "error": error,
                **(extra or {}),
                **llm_usage_payload(response, prompt=prompt),
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    return task


def _repair_revision_game_world_meta_leak(
    provider,
    *,
    draft,
    original_prompt: str,
    leak_reasons: list[str],
    max_tokens: int,
    temperature: float | None,
    model: str,
    dry_run: bool,
) -> tuple[object, dict]:
    repair_prompt = "\n".join(
        [
            "你是长篇网文定向修订的世界内化修复器。",
            "任务：只修复正文里的玩家层/系统层元概念泄漏，保持当前底稿的场景链、人物压力、对白目的和章末后果。",
            "禁止把清虚观现场改成回现实刷论坛、查攻略、解释规则；不得另起炉灶。",
            "",
            "硬禁词：内测、论坛、玩家、NPC、GM、新手村、任务栏、任务面板、系统分配、系统提示、界面、调出任务。",
            "清虚观/山门/拜师/盘问/试炼/门派交涉现场，正文、对白、内心独白都不得出现这些词。",
            "替代方式：木牌、拜帖、山门规矩、旧衣着误判、拂尘压迫、捏骨试探、挑水/扫院/入门规矩、江湖话套问。",
            "修订目标如果要求补对白/画面/回报，只能在现有清虚观现场里补，不得跳到论坛、现实攻略或面板说明。",
            "",
            "坏例：顾晚回到现实刷论坛，看到玩家说这个NPC会卡任务。",
            "改法：顾晚盯着老道茶杯缺口，听出他在等自己露怯，只好拿木牌背面的拜帖再试一句江湖话。",
            "坏例：内测期间系统提示任务完成。",
            "改法：掌心热痕沿腕骨一跳，木剑上的裂纹亮了一瞬，老道脸色随之变了。",
            "",
            "已检测到的问题：",
            "；".join(leak_reasons[:8]),
            "",
            "输出严格 JSON：{\"title\": \"...\", \"content\": \"...\", \"self_check\": [\"...\"], \"used_brief_points\": [\"...\"]}",
            "不得输出说明文字，不得保留任何硬禁词。",
            "",
            "原始修订要求：",
            original_prompt,
            "",
            "待修复修订稿 JSON：",
            json.dumps(
                {
                    "title": getattr(draft, "title", "") or "",
                    "content": getattr(draft, "content", "") or "",
                    "self_check": getattr(draft, "self_check", []) or [],
                    "used_brief_points": getattr(draft, "used_brief_points", []) or [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    repair_temperature = min(0.3, float(temperature or 0.5))
    response = provider.generate(
        repair_prompt,
        max_tokens=max(max_tokens, 4500),
        temperature=repair_temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    repaired = parse_or_repair_draft_output(
        provider,
        response_text=response.text,
        original_prompt=repair_prompt,
        max_tokens=max(max_tokens, 4500),
        temperature=repair_temperature,
        model=model,
        task_label="章节修订元概念修复",
    )
    return repaired, {
        "attempted": True,
        "accepted": True,
        "before": leak_reasons[:8],
        **llm_usage_payload(response, prompt=repair_prompt),
    }


def _revision_world_logic_prompt_constraints(*, book: Book, brief: ChapterBrief, source_version: ChapterVersion) -> str:
    context = "\n".join(
        [
            str(book.title or ""),
            str(book.genre or ""),
            str(brief.goal or ""),
            str(brief.required_beats or ""),
            str(brief.constraints or ""),
            str(source_version.title or ""),
            str(source_version.source or ""),
            str(source_version.content or "")[:2200],
        ]
    )
    if not any(marker in context for marker in ("入梦", "清虚观", "网游", "游戏", "内测", "NPC", "玩家", "论坛", "木牌", "山门")):
        return ""
    return "\n".join(
        [
            "章节修订硬禁区：不得在游戏内/清虚观现场引入玩家层、系统层、论坛攻略层解释。",
            "若底稿已经用木牌、拜帖、山门规矩、拂尘盘问、捏骨试探解决任务来源，修订必须保留这条认知边界，不得改成论坛、内测、玩家、NPC、任务面板、系统提示或界面说明。",
            "正文、对白、内心独白都不得出现：内测、论坛、玩家、NPC、GM、新手村、任务栏、任务面板、系统分配、系统提示、界面、调出任务。",
            "补对白承载时，只能让人物多说立场、怀疑、威胁、找补和江湖规矩；补画面颗粒时，只能补站位、光线、物件、动作轨迹；补奖励/代价时，只能用身体变化、物证变化或人物反应落地。",
            "禁止把定向修订扩写成主角回现实查攻略、刷论坛、解释系统规则或用面板解题。",
        ]
    )


def _revision_dimension_beats(dimensions: list[str]) -> list[str]:
    beats: list[str] = []
    if "brief_coverage" in dimensions:
        beats.append("补足本章核心承诺，让读者能在正文里看见人物目标、场景阻碍和局面变化")
    if "reader_momentum" in dimensions:
        beats.append("开场尽快进入具体处境，可用人物欲望、关系张力、异常细节、利益交换、行动后果或悬念建立阅读牵引")
    if "conflict_pressure" in dimensions:
        beats.append("增加可见阻碍、利益冲突、误判或逼近风险，让主角必须做出回应")
    if "choice_and_cost" in dimensions:
        beats.append("主角必须做出选择，并付出清晰代价或承担后果")
    if "hook_strength" in dimensions:
        beats.append(
            "只重写最后300-500字章末钩子：必须由本章已发生的拜师、站桩、数据壁垒异常自然引出，"
            "落成一个具体动作、物证、异常后果或下一次探索压力"
        )
    if "prose_density" in dimensions:
        beats.append("减少解释和重复，增加动作、感官、对话和信息增量")
    if "arc_alignment" in dimensions:
        beats.append("修订必须服务本章阶段目标，结尾要推动主线进入下一步")
    if "production_standard" in dimensions:
        beats.append("必须按通用章节生产标准重写成完整章节：3000字以上、开篇有牵引、主角行动链完整、场景推进清楚、章末钩子由本章行动引发")
    return beats


def _revision_issue_beats(issues: list[str]) -> list[str]:
    beats: list[str] = []
    for issue in issues:
        if issue.startswith("forbidden_marker"):
            beats.append("把弹窗式奖励、UI播报或面板提醒改成角色可感知的身体变化、物证、人物怀疑或江湖传闻；不要让机械提示替代场景因果")
        elif "hook_strength" in issue:
            beats.append(
                "只重写章末最后300-500字：保留前文拜师/站桩结果，让最后一幕出现具体异常后果、下一次登录压力或可追查线索；"
                "不得新增追杀、现实机构关注或门派通缉"
            )
        elif "too_short" in issue:
            beats.append("补足关键场景，使正文字数达到最低要求")
        elif "too_long" in issue:
            beats.append("压缩冗余段落，使正文不超过平台长度上限")
        else:
            beats.append(f"修复质检问题：{issue}")
    return beats


def _chapter_unit_beats(quality_data: dict) -> list[str]:
    if not isinstance(quality_data, dict):
        return []
    report = quality_data.get("chapter_unit_report")
    if not isinstance(report, dict):
        return []
    rows: list[str] = []
    score = int(report.get("score") or 0)
    unit_count = int(report.get("unit_count") or 0)
    if score < 70:
        rows.append(
            f"拟人化小单元修复：当前单元流评分 {score}，共 {unit_count} 个单元；"
            "修订必须按 300-700 字小单元重建目标、阻碍、动作后果和承接点"
        )
    for item in (report.get("repair_contract") or [])[:3]:
        if item:
            rows.append(str(item))
    for unit in report.get("units") or []:
        if not isinstance(unit, dict) or int(unit.get("score") or 0) >= 70:
            continue
        issues = "、".join(str(item) for item in (unit.get("issues") or [])[:4])
        summary = str(unit.get("summary") or "").strip()
        detail = f"第{unit.get('index')}单元验收：修复 {issues or '单元推进不足'}，保留有效信息但补清承接和后果"
        if summary:
            detail += f"；当前片段：{summary}"
        rows.append(detail)
    return list(dict.fromkeys(rows))[:5]


def _llm_review_diagnostic_beats(llm_review: dict) -> list[str]:
    if not isinstance(llm_review, dict) or llm_review.get("status") != "completed":
        return []
    beats: list[str] = []
    if llm_review.get("verdict") in {"needs_revision", "fail"}:
        beats.append("参考主编二审的抽象失败原因修复读者体验，但不得继承二审里的具体旧桥段、旧名词、旧场景要求")
    if llm_review.get("risk_flags"):
        beats.append("重新检查连续性、平台可读性、爽点节奏和章末钩子风险；具体处理以最新生产骨架和修订方向为准")
    return beats


def _revision_trend_beats(
    session: Session,
    *,
    chapter: Chapter,
    current_dimensions: dict,
) -> list[str]:
    """D5d 失败记忆量化（2026-07-29）：喂回"这次比上次涨/跌几分"。

    现有回填只告诉生成端"哪些维度弱、怎么改"，但不告诉它"上一稿的
    修改方向是对是错"——于是同 brief 反复重写时容易原地打转（改动
    没让分数动，甚至改错方向让分数退步，系统却毫无察觉）。

    本函数取该章倒数第二版的质检维度分，与当前版逐维度对比，生成
    明确的方向反馈：
      * 退步维度 → 警告"上次改动方向错了，换一种写法"
      * 停滞维度 → "反复修没效果，改用结构性重写而非局部补丁"
    这把"失败记忆"从定性(哪弱)升级为定量闭环(改对没/往哪改)。
    """
    if not isinstance(current_dimensions, dict) or not current_dimensions:
        return []
    prev_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.id.desc())
        .offset(1)
    )
    if prev_version is None:
        return []  # 首稿，无上一稿可比
    prev_quality = session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == prev_version.id)
        .order_by(QualityReport.id.desc())
    )
    if prev_quality is None:
        return []
    try:
        prev_data = json.loads(prev_quality.report)
    except (json.JSONDecodeError, TypeError):
        return []
    prev_dims = prev_data.get("dimensions") if isinstance(prev_data.get("dimensions"), dict) else {}
    if not prev_dims:
        return []

    regressed: list[str] = []
    stalled: list[str] = []
    for name, cur_raw in current_dimensions.items():
        prev_raw = prev_dims.get(name)
        if prev_raw is None:
            continue
        try:
            cur = int(cur_raw)
            prev = int(prev_raw)
        except (TypeError, ValueError):
            continue
        delta = cur - prev
        if delta <= -3:
            regressed.append(f"{name}({prev}→{cur}·退{-delta}分)")
        elif abs(delta) <= 1 and cur < 65:
            stalled.append(f"{name}({cur}分·连续两稿几乎没动)")

    beats: list[str] = []
    if regressed:
        beats.append(
            "【上稿方向复盘·退步维度】" + "、".join(regressed[:4])
            + "。上一次的改法让这些维度不升反降，说明修改方向错了——"
            "换一种完全不同的写法处理它们，不要沿用上稿思路。"
        )
    if stalled:
        beats.append(
            "【上稿方向复盘·停滞维度】" + "、".join(stalled[:4])
            + "。连续两稿反复局部修补都没能让分数移动，改用结构性重写"
            "（整段推倒重来、换叙事视角或场景切入点），别再做同类小修补。"
        )
    return beats


def _editor_in_chief_beats(quality_data: dict) -> list[str]:
    chief = quality_data.get("editor_in_chief") if isinstance(quality_data.get("editor_in_chief"), dict) else {}
    if not chief:
        return []
    rows: list[str] = []
    decision = str(chief.get("decision") or "").strip()
    largest = str(chief.get("largest_problem") or "").strip()
    if decision:
        rows.append(f"主编裁决：{decision}")
    if largest:
        rows.append(f"最大问题：{largest}")
    for item in (chief.get("minimum_effective_revision") or [])[:4]:
        rows.append(f"最小有效修法：{item}")
    forbidden = "；".join(str(item) for item in (chief.get("forbidden_revision") or [])[:5])
    if forbidden:
        rows.append(f"禁止修法：{forbidden}")
    for item in (chief.get("acceptance_checks") or [])[:3]:
        rows.append(f"主编验收：{item}")
    return rows


def _paragraph_aesthetic_beats(quality_data: dict) -> list[str]:
    report = quality_data.get("paragraph_aesthetic_report") if isinstance(quality_data.get("paragraph_aesthetic_report"), dict) else {}
    contract = format_paragraph_aesthetic_contract(report)
    return [line for line in contract.splitlines() if line.strip()][:8]


def _latest_story_brief_for_revision(session: Session, *, chapter: Chapter) -> ChapterBrief | None:
    return latest_story_brief(session, chapter.id)


def _brief_is_diagnostic(text: str) -> bool:
    return brief_has_revision_artifacts(text)


def _revision_story_goal(*, chapter_number: int, base_brief: ChapterBrief | None) -> str:
    if base_brief and base_brief.goal and not _brief_is_diagnostic(base_brief.goal):
        return base_brief.goal
    return f"第{chapter_number}章：按最新生产骨架重修为可读章节，重点写真实场景、人物动机、江湖因果和主角主动选择。"


def _revision_story_intent(*, chapter_number: int, base_brief: ChapterBrief | None) -> str:
    if base_brief and base_brief.required_beats and not _brief_is_diagnostic(base_brief.required_beats):
        return base_brief.required_beats
    return (
        f"第{chapter_number}章必须像真实作者重写章节：先承接人物处境，再推进人物欲望、阻碍、误判或冲突；"
        "游戏世界要像真实武侠世界，人物有欲望、顾虑、利益和误判；"
        "主角成长来自观察规则、修炼、人情、冒险和承担后果，不靠打怪升级或机械任务。"
    )


def _structural_rewrite_required_beats(
    *,
    chapter_number: int,
    base_brief: ChapterBrief | None,
    quality_data: dict,
    failure_class: dict,
) -> str:
    base_intent = _revision_story_intent(chapter_number=chapter_number, base_brief=base_brief)
    dimensions = quality_data.get("dimensions") if isinstance(quality_data.get("dimensions"), dict) else {}
    issues = [str(item) for item in quality_data.get("issues") or []]
    reasons = [str(item) for item in failure_class.get("structural_reasons") or []]
    rows = [
        "revision_mode:rewrite",
        "本轮不是局部润色；按稳定生产蓝图整章重构。",
        "正文硬目标：1800-2500中文字符（上限2800），5-6个连续小单元；不得膨胀成多支线长章。",
        base_intent,
        "开头必须承接上一章结尾的具体后果、人物状态或未解决压力。",
        "每个小单元只完成一个清晰动作：目标、阻碍、反应、信息增量、后果承接必须可见。",
        "章末只留一个由本章行动自然引发的具体钩子，不再额外开新线。",
    ]
    if "length_out_of_range" in reasons or "over_target_max_chars" in reasons or any(item.startswith("too_long") for item in issues):
        rows.append("压缩策略：删除旁支解释、重复对话和未兑现专名；保留主角行动链、关键交易/冲突和章末物证。")
    if "unit_count_exploded" in reasons or "unit_flow_structural" in reasons:
        rows.append("结构策略：合并碎片场景，按承接/试探/受阻/转圜/反压/变局/代价/钩子推进，不要写成14个散片。")
    if int(dimensions.get("brief_coverage") or 100) < 60:
        rows.append("覆盖策略：只兑现本章最关键的3-5个承诺；每个承诺必须落到场景、动作、对白或后果。")
    if int(dimensions.get("dialogue_fullness") or 100) < 60:
        rows.append("对白策略：对白必须承担试探、遮掩、交易、威胁或情绪变化，不能只解释设定。")
    if int(dimensions.get("imageable_paragraphs") or 100) < 60:
        rows.append("画面策略：每个主要场景交代空间边界、关键物件、人物站位和动作轨迹。")
    return "；".join(list(dict.fromkeys(row for row in rows if row)))[:1800]


def _revision_story_constraints(*, base_brief: ChapterBrief | None) -> str:
    base = ""
    if base_brief and base_brief.constraints and not _brief_is_diagnostic(base_brief.constraints):
        base = base_brief.constraints
    additions = [
        "保留已登记 Canon，不引入无代价能力。",
        "不要输出模型说明、JSON、数据库、任务链路、质检报告、修订合同或作者说明。",
        "少量玩家感知层信息只能点到为止，不能替代真实江湖人物、因果和现场反应。",
        "如果前一版因弹窗式奖励或机械系统提示失败，下一版要改成身体反应、环境异象、物证、误会或人物怀疑。",
        "修订后必须重新走 review-chapter。",
    ]
    return "；".join([item for item in [base, *additions] if item])


def _latest_feedback_requirements(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    limit: int = 1,
) -> list[str]:
    adjustments = session.scalars(
        select(FeedbackAdjustment)
        .where(
            FeedbackAdjustment.book_id == book_id,
            FeedbackAdjustment.target_chapter_number == chapter_number,
            FeedbackAdjustment.status == "applied",
        )
        .order_by(FeedbackAdjustment.id.desc())
        .limit(limit)
    )
    requirements: list[str] = []
    for adjustment in adjustments:
        text = adjustment.adjustment_text.strip()
        if not text:
            continue
        requirements.append(build_rewrite_contract(text, chapter_number=chapter_number))
    return requirements


def _brief_has_feedback_marker(brief: ChapterBrief) -> bool:
    marker = "反馈调整#"
    return marker in brief.goal or marker in brief.required_beats or marker in brief.constraints


def _brief_has_actionable_revision_plan(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if len(text.strip()) < 120:
        return False
    required_markers = ("第", "核心设定", "主角", "1800-2500")
    if not all(marker in text for marker in required_markers):
        return False
    stale_markers = ("修订合同:", "原始修订方向:", "验收清单:", "质检报告 #")
    return not any(marker in text for marker in stale_markers)


def _brief_has_budget_recovery_marker(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        "system_revision_budget_recovery" in text
        or "persistent_revision_budget:" in text
        or "自动修订预算触顶" in text
    )


def _source_version_id(source: str | None) -> int | None:
    prefix, _, raw_id = str(source or "").partition(":v")
    if prefix not in {"revision_budget_recovery", "revision_budget_readable_restore"} or not raw_id:
        return None
    try:
        return int(raw_id.split(":", 1)[0])
    except ValueError:
        return None


def _fallback_quality_for_recovery_revision(
    session: Session,
    *,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
) -> QualityReport | None:
    source_id = _source_version_id(source_version.source)
    if not source_id or not _brief_has_budget_recovery_marker(revision_brief):
        return None
    quality = session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == source_id)
        .order_by(QualityReport.id.desc())
    )
    if quality:
        return quality
    return session.scalar(
        select(QualityReport)
        .join(ChapterVersion, QualityReport.chapter_version_id == ChapterVersion.id)
        .where(ChapterVersion.chapter_id == source_version.chapter_id, QualityReport.passed.is_(False))
        .order_by(QualityReport.score.desc(), QualityReport.id.desc())
    )


def _source_requires_current_world_logic_rewrite(source_version: ChapterVersion) -> bool:
    source = str(source_version.source or "")
    if not source.startswith("rebuild_candidate_incumbent_restore:"):
        return False
    report = evaluate_world_logic(source_version.content or "")
    return (
        report.score < 60
        or report.checks.get("player_layer_intrusion", 100) < 60
        or report.checks.get("character_knowledge_boundary", 100) < 60
    )


def _revision_requires_rewrite(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    primary_mode = _primary_revision_mode(text)
    if primary_mode in {REVISION_MODE_FRESH, "rewrite"}:
        return True
    if primary_mode in {REVISION_MODE_LOCAL_PATCH, "polish"}:
        return False
    modes = _revision_modes(text)
    if _revision_specialty(brief) in {"unit_flow", "ending_hook"} and not (
        REVISION_MODE_FRESH in modes or "rewrite" in modes
    ):
        return False
    if primary_mode == "targeted" and (REVISION_MODE_FRESH in modes or "rewrite" in modes):
        return True
    if primary_mode == "targeted":
        return False
    markers = (
        "修订模式:rewrite",
        "修订模式:fresh",
        "revision_mode:rewrite",
        "revision_mode:fresh",
        "重写",
        "重做",
        "重新组织",
        "最新生产骨架",
        "不要只做局部润色",
        "替换无效桥段",
    )
    return any(marker in text for marker in markers)


def _revision_is_fresh_rewrite(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return _primary_revision_mode(text) == REVISION_MODE_FRESH


def _revision_is_local_patch(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return _primary_revision_mode(text) == REVISION_MODE_LOCAL_PATCH


def _revision_modes(text: str) -> set[str]:
    modes: set[str] = set()
    normalized = (text or "").replace("：", ":")
    for match in re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", normalized):
        modes.add(match.group(1).strip().lower())
    return modes


def _primary_revision_mode(text: str) -> str:
    normalized = (text or "").replace("：", ":")
    current_text = normalized.split("历史合同残留", 1)[0]
    matches = list(re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", current_text))
    if not matches:
        matches = list(re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", normalized))
    return matches[-1].group(1).strip().lower() if matches else ""


def _same_revision_content(source: str | None, revised: str | None) -> bool:
    def normalize(value: str | None) -> str:
        return "".join(str(value or "").split())

    source_text = normalize(source)
    revised_text = normalize(revised)
    if not source_text or not revised_text:
        return False
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest() == hashlib.sha256(revised_text.encode("utf-8")).hexdigest()


def _try_local_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    canon_context: str,
    dry_run: bool,
    rewrite_mode: bool = False,
) -> ChapterVersion | None:
    # Phase 2/2: local_patch is now the *default* revise path. We only fall
    # through to the full rewrite branch when:
    #   * the brief explicitly asked for fresh/rewrite via ``修订模式:fresh``,
    #     or
    #   * the brief carries a rewrite marker (_revision_requires_rewrite),
    #     both of which are captured by the caller in ``rewrite_mode``.
    # Callers pass ``rewrite_mode=True`` to force the classic full-rewrite
    # path; otherwise local_patch is attempted first. If the local patcher
    # produces nothing meaningful (no drift hits + LLM produced identical
    # content) it returns None and the caller falls back to a full rewrite.
    if rewrite_mode:
        return None
    # Legacy: if the brief explicitly opts into local_patch, that still wins.
    # Otherwise, the default is local_patch anyway.
    bias = evaluate_generation_bias(
        content=source_version.content or "",
        goal=revision_brief.goal or "",
        required_beats=revision_brief.required_beats or "",
        constraints=revision_brief.constraints or "",
        canon_context=canon_context,
    )
    if not bias.model_bias_hits:
        return _try_llm_local_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    patched_content, replacements = apply_model_drift_local_patch(source_version.content or "", bias.model_bias_hits)
    if not replacements or patched_content == (source_version.content or ""):
        return _try_llm_local_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    return _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="deterministic_local_patch",
        output_extra={"replacements": replacements, "bias_report": bias.to_dict()},
        dry_run=dry_run,
    )


def _try_llm_local_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    if chinese_chars(source_content) > 9000:
        return None
    provider = get_provider(dry_run)
    import os as _os
    model = _os.environ.get("B_PIPELINE_MODEL") or settings.llm_revision_model
    temperature = min(settings.llm_revision_temperature, 0.35)
    prose_targeted = _brief_is_prose_targeted(revision_brief)
    specialty = _revision_specialty(revision_brief)
    author_anchor_cleanup = _try_author_anchor_cleanup_revision(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        dry_run=dry_run,
    )
    if author_anchor_cleanup:
        return author_anchor_cleanup
    if specialty == "prose_voice" and _revision_targets_fanqie_paragraph_gate(revision_brief):
        paragraph_patch = _try_paragraph_hard_gate_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
        if paragraph_patch:
            return paragraph_patch
    if specialty == "ending_hook":
        return _try_ending_hook_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    if specialty == "unit_flow":
        return _try_unit_flow_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    specialty_contract = _revision_specialty_contract(specialty)
    if prose_targeted:
        prompt = f"""
你是小说主笔，只做“表达层窄修订”，目标是降低 AI 味。不要重写整章，不要扩写剧情。

专项修订类型：{specialty}
{specialty_contract}

请严格输出 JSON：{{"title":"章节标题","content":"窄修订后的完整章节正文","patch_note":"说明删减/拆段/微调了哪里"}}

硬性边界：
- 不新增人物、地点、设定、任务、系统提示、现实钩子或章末新事件。
- 不更换开场、主事件、场景顺序、章末事实。
- 只允许做三类动作：删多余比喻/形容词；把长段拆短；把关键对白微调得更自然。
- 可以补少量自然语气词、虚词和句间承接，但不能把对白写成解释设定的台词。
- 总字数不得超过原文；如果必须补一句对白，必须在同段或相邻段删掉等量解释。
- 段落密度要提升：优先把 3 行以上长段拆成 2-3 个短段，每段保留明确动作或反应。
- content 必须是完整章节正文，不要输出说明、Markdown、修订清单或系统信息。

本轮只处理修订单里的文风/对白/段落问题；修订单中任何“候选重建、重建、另起新章、增加新钩子”的字样都视为无效旧噪声。

修订单：
{revision_brief.goal}
{revision_brief.required_beats}
{revision_brief.constraints}

原章节：
{source_content}
""".strip()
    else:
        prompt = f"""
你是主笔，只做局部补丁，不重写整章。

专项修订类型：{specialty}
{specialty_contract}

请严格输出 JSON：{{"title":"章节标题","content":"局部补丁后的完整章节正文","patch_note":"说明改了哪里"}}

局部补丁要求：
- 只能修订修订单命中的句子、词语、短段落或轻微承接问题。
- 不得重排整章结构，不得改变章末事实，不得新增大设定。
- 保留原文已经有效的场景、动作链、人物关系和信息顺序。
- content 必须是完整章节正文，不要输出说明、Markdown 或系统信息。

修订单：
{revision_brief.goal}
{revision_brief.required_beats}
{revision_brief.constraints}

原章节：
{source_content}
""".strip()
    try:
        response = provider.generate(
            prompt,
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        data = parse_or_repair_json_object(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            expected_schema='{"title":"章节标题","content":"局部补丁后的完整章节正文","patch_note":"说明改了哪里"}',
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            task_label="局部补丁修订",
        )
    except Exception:
        return None
    patched_content = str(data.get("content") or "").strip()
    if not patched_content or patched_content == source_content:
        return None
    before_chars = chinese_chars(source_content)
    after_chars = chinese_chars(patched_content)
    if prose_targeted:
        from app.services.chapter_standards import REBUILD_MAX_CHARS

        max_after = min(REBUILD_MAX_CHARS, max(before_chars, int(before_chars * 1.03)))
        if after_chars < max(800, int(before_chars * 0.82)) or after_chars > max_after:
            return None
    elif after_chars < max(800, int(before_chars * 0.75)) or after_chars > min(8000, int(max(before_chars, 1) * 1.18)):
        return None
    version = _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="llm_local_patch",
        output_extra={
            "patch_note": str(data.get("patch_note") or ""),
            "revision_specialty": specialty,
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=prompt),
        },
        dry_run=False,
    )
    task = session.scalar(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    if task and task.task_type == "revise_chapter":
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template="local_patch@v1",
            prompt=prompt,
            status="completed",
        )
    return version


def _try_ending_hook_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    if not source_content.strip():
        return None
    head_context, old_tail = _split_ending_tail(source_content, tail_paragraphs=8)
    if chinese_chars(old_tail) < 120:
        return None
    provider = get_provider(dry_run)
    model = settings.llm_revision_model
    temperature = min(settings.llm_revision_temperature, 0.28)
    contract = _revision_specialty_contract("ending_hook")
    prompt = f"""
你是小说主笔。只修章末钩子，不重写整章。

请严格输出 JSON：{{"replacement_tail":"替换后的章末正文","patch_note":"说明改了哪里"}}

{contract}

硬性要求：
- replacement_tail 必须能直接接在【前文尾部上下文】后面。
- 只写章末最后300-500中文字符，保留“沈渡被武馆收留/开始站桩/数据壁垒异常”这些已成立事实。
- 钩子必须更具体：让异常提示带来下一章必须处理的压力、选择或线索。
- 禁止写现实身体变强、热流、丹田、经脉、真气、内力、掌心发热。
- 禁止用身体印记/掌心红线/灼痛/烙印/胎记/皮肤文字做钩子；本轮钩子只能落在外部物件、提示文字、声音、门窗、柴房物件或下一次登录规则上。
- 禁止新增追杀、现实机构关注、门派通缉、论坛、玩家、NPC。
- 不输出标题、说明、Markdown、质检术语。

修订单：
{revision_brief.goal}
{revision_brief.required_beats}
{revision_brief.constraints}

【前文尾部上下文】
{head_context[-900:]}

【当前失败章末】
{old_tail}
""".strip()
    try:
        response = provider.generate(
            prompt,
            # 2026-09-18: 2200 装不下 thinking 模型推理(~2000)+替换尾(≤650字),
            # kimi-k3 真机空响应; 下限抬到 4500。
            max_tokens=min(settings.llm_revision_max_tokens, 4500),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        data = parse_or_repair_json_object(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            expected_schema='{"replacement_tail":"替换后的章末正文","patch_note":"说明改了哪里"}',
            max_tokens=4500,
            temperature=temperature,
            model=model,
            task_label="章末钩子局部补丁",
        )
    except Exception:
        return None
    replacement_tail = _normalize_patch_paragraphs(str(data.get("replacement_tail") or "").strip())
    if not replacement_tail:
        return None
    tail_chars = chinese_chars(replacement_tail)
    if tail_chars < 180 or tail_chars > 650:
        return None
    if any(marker in replacement_tail for marker in ("热流", "丹田", "经脉", "真气", "内力", "掌心发热", "论坛", "玩家", "NPC", "现实机构", "通缉", "掌心", "灼痛", "红线", "印记", "烙印", "胎记", "皮肤")):
        return None
    patched_content = _replace_ending_tail(source_content, replacement_tail, tail_paragraphs=8)
    if patched_content == source_content:
        return None
    before_chars = chinese_chars(source_content)
    after_chars = chinese_chars(patched_content)
    if after_chars < max(800, int(before_chars * 0.85)) or after_chars > int(before_chars * 1.12):
        return None
    version = _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="ending_hook_patch",
        output_extra={
            "patch_note": str(data.get("patch_note") or ""),
            "revision_specialty": "ending_hook",
            "provider": response.provider,
            "model": response.model,
            "old_tail_chars": chinese_chars(old_tail),
            "replacement_tail_chars": tail_chars,
            **llm_usage_payload(response, prompt=prompt),
        },
        dry_run=dry_run,
    )
    task = session.scalar(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    if task and task.task_type == "revise_chapter":
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template="ending_hook_patch@v1",
            prompt=prompt,
            status="completed",
        )
    return version


def _split_ending_tail(content: str, *, tail_paragraphs: int = 8) -> tuple[str, str]:
    paras = [p.strip() for p in re.split(r"\n\s*\n+", content or "") if p.strip()]
    if len(paras) <= tail_paragraphs:
        return "", "\n\n".join(paras)
    return "\n\n".join(paras[:-tail_paragraphs]), "\n\n".join(paras[-tail_paragraphs:])


def _replace_ending_tail(content: str, replacement_tail: str, *, tail_paragraphs: int = 8) -> str:
    head, _old_tail = _split_ending_tail(content, tail_paragraphs=tail_paragraphs)
    replacement = "\n\n".join(p.strip() for p in re.split(r"\n\s*\n+", replacement_tail or "") if p.strip())
    return "\n\n".join(part for part in (head.strip(), replacement.strip()) if part)


def _normalize_patch_paragraphs(text: str, *, max_chars: int = 120) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", text or "") if p.strip()]
    normalized: list[str] = []
    for paragraph in paragraphs:
        if chinese_chars(paragraph) <= max_chars:
            normalized.append(paragraph)
            continue
        current = ""
        for sentence in _split_cn_sentences(paragraph):
            if current and chinese_chars(current + sentence) > max_chars:
                normalized.append(current.strip())
                current = sentence
            else:
                current += sentence
        if current.strip():
            normalized.append(current.strip())
    return "\n\n".join(normalized)


def _split_cn_sentences(text: str) -> list[str]:
    parts = re.split(r"([。！？!?；;])", text or "")
    sentences: list[str] = []
    for index in range(0, len(parts), 2):
        body = parts[index].strip()
        punct = parts[index + 1] if index + 1 < len(parts) else ""
        sentence = f"{body}{punct}".strip()
        if sentence:
            sentences.append(sentence)
    return sentences



def _try_unit_flow_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    units = split_chapter_units(source_content, target_min=300, target_max=700)
    target_indexes = _unit_flow_target_indexes(revision_brief)
    expected_count = _unit_flow_expected_count(revision_brief)
    if expected_count and expected_count != len(units):
        print(f"unit_flow_patch_skip=unit_count_mismatch expected={expected_count} actual={len(units)}")
        return None
    if not units or not target_indexes:
        print(f"unit_flow_patch_skip=missing_units_or_targets units={len(units)} targets={sorted(target_indexes)}")
        return None
    target_units = [unit for unit in units if unit.index in target_indexes]
    if not target_units:
        print(f"unit_flow_patch_skip=no_target_units targets={sorted(target_indexes)}")
        return None

    before_chars = chinese_chars(source_content)
    max_chars = _unit_flow_max_chars(revision_brief)
    provider = get_provider(dry_run)
    import os as _os
    model = _os.environ.get("B_PIPELINE_MODEL") or settings.llm_revision_model
    temperature = min(settings.llm_revision_temperature, 0.28)
    unit_payload = [
        {
            "index": unit.index,
            "chars": unit.chars,
            "max_replacement_chars": _unit_flow_replacement_max_chars(unit.chars, before_chars=before_chars, max_chars=max_chars),
            "text": unit.text,
        }
        for unit in target_units[:3]
    ]
    craft_targets = _unit_flow_craft_targets(revision_brief)
    craft_target_block = _unit_flow_craft_target_block(craft_targets)
    relation_scene_context = "\n".join(
        item
        for item in [
            revision_brief.goal,
            revision_brief.required_beats,
            revision_brief.constraints,
            craft_target_block,
        ]
        if item
    )
    reference_craft_block = build_reference_craft_block(
        None,
        chapter_number=chapter.chapter_number,
        scene_context=relation_scene_context,
    )
    prompt = f"""
你是小说主笔。只修失败小单元，不重写整章。

请严格输出 JSON：{{"units":[{{"index":1,"replacement_text":"替换后的该单元正文"}}],"patch_note":"说明改了哪些单元"}}
强制要求：units 必须包含下面【待替换小单元】里的每一个 index；replacement_text 不能为空、不能照抄原文、不能写说明，必须是可直接入正文的小说段落。

【本轮必须优先修复的技法弱项】
{craft_target_block}

{reference_craft_block}

【修订单 · 最高优先级】
{revision_brief.goal}
{revision_brief.required_beats}
{revision_brief.constraints}

硬性边界：
- 只能替换指定 index 的小单元；不得改其它单元、章末钩子、人物关系、场景顺序或已成立事实。
- replacement_text 必须能原地替换对应小单元，前后邻接自然；禁止空字符串、占位符、摘要、解释、自检。
- 必须返回且只返回 target index={sorted(target_indexes)} 的 replacement_text；缺任一目标 index 本轮视为失败。
- 每个 replacement_text 必须遵守待替换小单元里的 max_replacement_chars，最低 220 中文字符；不得靠扩写堆字数；补清“主角目标→现场阻碍→连续动作→可见后果/代价→人物反应→下一单元交接”。
- 补丁后整章中文字符数不得超过 {max_chars or "原文长度上限"}，小单元数量必须保持 {expected_count or len(units)} 个。
- 若本轮目标包含“心理链”，replacement_text 必须出现身体反应→判断/误判→迟疑→选择动作，不能只写“震惊/害怕/复杂”。
- 若本轮目标包含“动作反应链”，主角每个关键动作后必须接对方反应、环境后果或局面变化。
- 若本轮目标包含“场景描绘”，必须补空间边界、光源/声音/气味、人物站位和可互动物件。
- 若本轮目标包含“修辞用词”，比喻只能来自当场物象，禁止空泛气氛判断替代描写。
- 不新增追杀、现实机构关注、门派通缉、现实身体变强、热流、丹田、经脉、真气、内力、论坛、玩家、NPC。
- 不输出标题、说明、Markdown 或质检术语。

【前后文摘要】
开头邻接：{source_content[:700]}
章末邻接：{source_content[-700:]}

【待替换小单元】
{json.dumps(unit_payload, ensure_ascii=False, indent=2)}
""".strip()
    try:
        response = provider.generate(
            prompt,
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        data = parse_or_repair_json_object(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            expected_schema='{"units":[{"index":1,"replacement_text":"替换后的该单元正文"}],"patch_note":"说明改了哪些单元"}',
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            task_label="小单元定点补丁",
        )
    except Exception as exc:
        print(f"unit_flow_patch_skip=llm_or_json_error error={type(exc).__name__}:{exc}")
        return None

    replacements: dict[int, str] = {}
    rejected: list[str] = []
    for item in data.get("units") or []:
        if not isinstance(item, dict):
            rejected.append("non_dict_item")
            continue
        try:
            index = int(item.get("index") or 0)
        except (TypeError, ValueError):
            rejected.append("bad_index")
            continue
        if index not in target_indexes:
            rejected.append(f"unexpected_index:{index}")
            continue
        replacement = _normalize_patch_paragraphs(str(item.get("replacement_text") or "").strip(), max_chars=140)
        replacement_chars = chinese_chars(replacement)
        if replacement_chars < 220 or replacement_chars > 820:
            rejected.append(f"bad_length:{index}:{replacement_chars}")
            continue
        if any(marker in replacement for marker in ("热流", "丹田", "经脉", "真气", "内力", "论坛", "玩家", "NPC", "现实机构", "通缉")):
            rejected.append(f"forbidden_marker:{index}")
            continue
        original_unit = next((unit for unit in target_units if unit.index == index), None)
        if original_unit:
            unit_max_chars = _unit_flow_replacement_max_chars(original_unit.chars, before_chars=before_chars, max_chars=max_chars)
            if replacement_chars > unit_max_chars:
                rejected.append(f"unit_length_expanded:{index}:{original_unit.chars}->{replacement_chars},max={unit_max_chars}")
                continue
        craft_rejection = _unit_flow_craft_rejection(
            original_unit.text if original_unit else "",
            replacement,
            targets=craft_targets,
            index=index,
        )
        if craft_rejection:
            rejected.append(craft_rejection)
            continue
        replacements[index] = replacement
    missing_indexes = sorted(index for index in target_indexes if index not in replacements)
    if missing_indexes:
        rejected.append(f"missing_replacement_indexes:{missing_indexes}")
        print(f"unit_flow_patch_skip=no_valid_replacements rejected={rejected}")
        return None
    if not replacements:
        print(f"unit_flow_patch_skip=no_valid_replacements rejected={rejected}")
        return None

    patched_content = _replace_chapter_units(source_content, replacements)
    if patched_content == source_content:
        print("unit_flow_patch_skip=unchanged_content")
        return None
    boundary_rejection = _unit_flow_patch_boundary_rejection(
        source_content,
        patched_content,
        expected_count=expected_count or len(units),
        max_chars=max_chars,
    )
    if boundary_rejection:
        print(f"unit_flow_patch_skip={boundary_rejection}")
        return None
    version = _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="unit_flow_patch",
        output_extra={
            "patch_note": str(data.get("patch_note") or ""),
            "revision_specialty": "unit_flow",
            "target_indexes": sorted(replacements),
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=prompt),
        },
        dry_run=dry_run,
    )
    task = session.scalar(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    if task and task.task_type == "revise_chapter":
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template="unit_flow_patch@v1",
            prompt=prompt,
            status="completed",
        )
    return version


def _unit_flow_max_chars(brief: ChapterBrief) -> int:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    candidates: list[int] = []
    patterns = (
        r"(?:max_chars|硬上限|系统上限|目标上限|不得超过|不能超过|超过|控制在)[^0-9]{0,12}(\d{4})",
        r"(\d{4})\s*(?:中文字符|字)[^。；\n]{0,12}(?:以内|以下|上限)",
    )
    for pattern in patterns:
        for match in re.findall(pattern, text):
            value = int(match)
            if 1200 <= value <= 6000:
                candidates.append(value)
    return min(candidates) if candidates else 0


def _unit_flow_replacement_max_chars(original_chars: int, *, before_chars: int, max_chars: int) -> int:
    extra_budget = 40
    if max_chars:
        extra_budget = min(extra_budget, max(0, max_chars - before_chars))
    return max(220, min(820, original_chars + extra_budget))


def _unit_flow_patch_boundary_rejection(source_content: str, patched_content: str, *, expected_count: int, max_chars: int) -> str:
    before_chars = chinese_chars(source_content)
    after_chars = chinese_chars(patched_content)
    if max_chars and after_chars > max_chars:
        return f"chapter_max_chars before={before_chars} after={after_chars} max={max_chars}"
    if after_chars < max(800, int(before_chars * 0.88)) or after_chars > int(before_chars * 1.05):
        return f"chapter_length_bounds before={before_chars} after={after_chars}"
    after_count = len(split_chapter_units(patched_content, target_min=300, target_max=700))
    if expected_count and after_count != expected_count:
        return f"unit_count_changed expected={expected_count} after={after_count}"
    return ""


def _unit_flow_target_indexes(brief: ChapterBrief) -> set[int]:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    indexes: set[int] = set()
    cn_digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

    def add_indexes(segment: str) -> None:
        target_part = re.split(r"(?:其他|其余|剩余|保留|保持不动|不动)", segment, maxsplit=1)[0]
        for match in re.findall(r"(?:第\s*(\d+)\s*(?:个\s*单元|单元|个)?|(?<![A-Za-z])\b(\d+)\s*(?:个\s*单元|单元))", target_part):
            value = match[0] or match[1]
            indexes.add(int(value))
        for match in re.findall(r"(?:第\s*([一二三四五六七八九十])\s*(?:个\s*单元|单元|个)?|([一二三四五六七八九十])\s*(?:个\s*单元|单元))", target_part):
            value = match[0] or match[1]
            indexes.add(cn_digits[value])

    for segment in re.findall(r"只(?:修|重写|改写|改|替换|动|处理|重做)([^。；\n]+)", text):
        add_indexes(segment)
    for segment in re.findall(
        r"(第\s*(?:\d+|[一二三四五六七八九十])\s*(?:个\s*)?单元[^。；\n]*(?:局部重修|局部修复|重修|修复|补丁|替换|改写|重写))",
        text,
    ):
        add_indexes(segment)
    return {index for index in indexes if 1 <= index <= 20}


def _unit_flow_expected_count(brief: ChapterBrief) -> int:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    match = re.search(r"共\s*(\d+)\s*个单元", text)
    if not match:
        return 0
    return int(match.group(1))


def _unit_flow_craft_targets(brief: ChapterBrief) -> set[str]:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    targets: set[str] = set()
    markers = {
        "scene_craft": ("scene_craft", "场景描绘", "场景描写", "空间边界", "光源", "声音", "气味", "人物站位"),
        "psychological_chain": ("psychological_chain", "心理链", "身体反应", "误判", "判断", "迟疑", "选择动作"),
        "action_reaction_chain": ("action_reaction_chain", "动作反应链", "对方反应", "环境后果", "局面变化", "人物反应"),
        "rhetoric_specificity": ("rhetoric", "修辞", "修辞用词", "当场物象", "具体比喻"),
        "diction_vividness": ("diction", "用词", "具体动词", "空泛判断"),
    }
    for key, needles in markers.items():
        if any(needle in text for needle in needles):
            targets.add(key)
    if "reference_craft" in text or "范文技法" in text:
        targets.update({"scene_craft", "psychological_chain", "action_reaction_chain"})
    return targets


def _unit_flow_craft_target_block(targets: set[str]) -> str:
    labels = {
        "scene_craft": "- 场景描绘：补空间边界、光源/声音/气味、人物站位、可互动物件。",
        "psychological_chain": "- 心理链：补身体反应 → 判断/误判 → 迟疑 → 选择动作。",
        "action_reaction_chain": "- 动作反应链：主角动作后必须有对方反应、环境后果或局面变化。",
        "rhetoric_specificity": "- 修辞：只从当场物象生成具体比喻，不写空泛气氛判断。",
        "diction_vividness": "- 用词：把“震惊/复杂/压迫感”等概括词换成能改变局面的具体动作。",
    }
    if not targets:
        return "- 补清目标、阻碍、动作后果、人物反应和下一单元承接。"
    return "\n".join(labels[key] for key in labels if key in targets)


def _unit_flow_craft_rejection(original: str, replacement: str, *, targets: set[str], index: int) -> str:
    if not targets:
        return ""
    before = evaluate_reference_craft(original)
    after = evaluate_reference_craft(replacement)
    mapping = {
        "scene_craft": "scene_craft",
        "psychological_chain": "psychological_chain",
        "action_reaction_chain": "action_reaction_chain",
        "rhetoric_specificity": "rhetoric_specificity",
        "diction_vividness": "diction_vividness",
    }
    failures: list[str] = []
    improvements: list[str] = []
    for target in sorted(targets):
        check = mapping.get(target)
        if not check:
            continue
        before_score = int(before.checks.get(check) or 0)
        after_score = int(after.checks.get(check) or 0)
        if after_score < before_score - 5:
            failures.append(f"{check}_regressed:{before_score}->{after_score}")
        if after_score >= 55 or after_score >= before_score + 6:
            improvements.append(f"{check}:{before_score}->{after_score}")
    if failures:
        return f"craft_not_improved:{index}:{','.join(failures[:4])}"
    required_improvements = 2 if len(targets) >= 3 else 1
    if len(improvements) < required_improvements:
        return f"craft_not_improved:{index}:insufficient_improvement:{';'.join(improvements[:4]) or 'none'}"
    return ""


def _replace_chapter_units(content: str, replacements: dict[int, str]) -> str:
    units = split_chapter_units(content, target_min=300, target_max=700)
    if not units or not replacements:
        return content
    parts = [replacements.get(unit.index, unit.text).strip() for unit in units]
    return "\n\n".join(part for part in parts if part)


def _revision_specialty(brief: ChapterBrief) -> str:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]).lower()
    if _revision_targets_fanqie_paragraph_gate(brief):
        return "prose_voice"
    if any(marker in text for marker in ("小单元", "chapter_unit", "unit_flow", "单元流", "动作后果", "信息增量")):
        return "unit_flow"
    if any(marker in text for marker in ("hook_strength", "章末钩子", "章末压力", "ending_pull", "earned_hook")):
        return "ending_hook"
    if any(marker in text for marker in ("承接", "连续", "因果", "上一章", "前章", "hook", "钩子", "causal_continuity")):
        return "continuity"
    if any(marker in text for marker in ("对白", "声线", "character_voice", "dialogue", "说话", "功能化")):
        return "dialogue_voice"
    if any(marker in text for marker in ("文风", "ai味", "太ai", "翻译腔", "啰嗦", "表达", "段落", "prose", "paragraph")):
        return "prose_voice"
    if any(marker in text for marker in ("brief_coverage", "必须项", "承诺", "兑现", "爽点", "奖励", "代价", "章末")):
        return "beat_coverage"
    return "craft"


def _revision_specialty_contract(specialty: str) -> str:
    contracts = {
        "ending_hook": (
            "专项边界：只修章末最后300-500字，不重写开篇和中段，不改变拜师、站桩、武馆收留等已成立事实。"
            "章末钩子必须由本章行动自然导致：陈松鹤的收留/站桩结果/数据壁垒异常三者至少扣住一个；"
            "必须落成具体物象或动作后果（异常小字、头盔信号、坐标锁定、下一次登录代价、某个可追查线索）。"
            "禁止新增追杀、现实机构关注、门派通缉、现实身体变强或游戏修为外溢。"
        ),
        "continuity": "专项边界：只修场景承接、因果链和前后状态错位；不得借修连续性新增大事件或替换主线目标。每处新增必须回答：从哪里来、为什么现在发生、造成什么后果。",
        "dialogue_voice": "专项边界：只修关键对白和人物反应。对白必须带身份、立场、试探、遮掩、威胁或利益算盘；禁止把设定解释塞进台词，禁止让所有角色同一种口气。",
        "prose_voice": "专项边界：只修语言自然度、段落密度和AI味。删抽象判断、空泛形容词和翻译腔；用可见动作、物件、停顿和身体反应替代解释。",
        "beat_coverage": "专项边界：只补本章修订单明确要求的兑现点，例如爽点、奖励、代价、章末压力。补充必须落在现有场景内，不得另开支线。",
        "unit_flow": "专项边界：只修失败小单元。按审核指出的第N单元原地替换，补清目标、阻碍、动作后果、人物反应和下一单元交接；不得改其它单元或章末事实。",
        "craft": "专项边界：只做最小有效修订。优先保留已成立的场景链和人物选择，用少量动作、对白、后果增强读感。",
    }
    return contracts.get(specialty, contracts["craft"])


def _revision_targets_fanqie_paragraph_gate(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]).lower()
    return any(
        marker in text
        for marker in (
            "fanqie_para_avg_too_long",
            "fanqie_para_density_too_low",
            "段均长",
            "段落密度",
            "段均过长",
            "段落过长",
        )
    )


def _revision_author_sample_anchor_rejection(brief: ChapterBrief, title: str, content: str) -> str:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if "用户样稿不可漂移锚点" not in text and "用户作者样稿约束" not in text:
        return ""
    title_value = (title or "").strip().strip("#《》 ")
    if title_value and title_value != "旧盔":
        return f"title_drift:{title_value}"
    body = content or ""
    forbidden = [
        term
        for term in (
            "道观",
            "道童",
            "清虚观",
            "铁牌",
            "身份牌",
            "外门执事",
            "投师",
            "拜师",
            "收徒",
            "不是VR",
            "不是什么VR",
            "论坛",
            "玩家",
            "NPC",
        )
        if term in body
    ]
    if forbidden:
        return "forbidden_author_anchor_drift:" + ",".join(forbidden[:8])
    required_groups = [
        ("旧盔", ("旧盔", "二手头盔", "头盔")),
        ("雪屏", ("雪花", "雪屏", "满屏的雪")),
        ("物理坠落", ("摔", "掉", "坠", "滚下", "砸上")),
        ("山地痛感", ("山", "血", "疼")),
        ("药味人烟", ("药味", "草药", "窝棚", "灯")),
        ("求伤药", ("伤药", "拿活抵", "什么都干")),
        ("第二个压力", ("第二个", "上一个", "这个月")),
    ]
    missing = [label for label, options in required_groups if not any(option in body for option in options)]
    if missing:
        return "missing_author_anchor:" + ",".join(missing)
    return ""


def _try_author_anchor_cleanup_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    patched_content, replacements = _author_anchor_cleanup_content(source_content)
    if not replacements or patched_content == source_content:
        return None
    if _revision_author_sample_anchor_rejection(revision_brief, source_version.title or "", patched_content):
        return None
    return _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="author_anchor_cleanup",
        output_extra={
            "revision_specialty": "prose_voice",
            "author_anchor_cleanup": replacements,
        },
        dry_run=dry_run,
    )


def _author_anchor_cleanup_content(content: str) -> tuple[str, list[dict[str, str]]]:
    replacements: list[dict[str, str]] = []
    patched = content or ""
    rules = [
        ("论坛翻了三天", "代练群翻了三天"),
        ("这不是什么VR测试，沈渡脑子里刚转过这个念头，后背就狠狠撞在硬邦邦的泥土上，疼得他倒抽一口冷气。", "那单活的说法在脑子里一闪而过，后背就狠狠撞在硬邦邦的泥土上，疼得他倒抽一口冷气。"),
        ("这不是什么VR测试", "那单活不是眼下能解释的事"),
        ("不是VR，不是内测，他好像被那数据异常整个人拽到什么地方来了。", "那单活的说法在脑子里闪了一下，又很快被疼和冷压下去；他像是被那场数据异常整个人拽到什么地方来了。"),
        ("不是VR，不是内测", "那单活不是眼下能解释的事"),
        ("不是VR", "那单活不是眼下能解释的事"),
        ("不是游戏。这个念头刚冒出来，就被疼压下去了。", "那单活的念头刚冒出来，就被疼压下去了。"),
        ("旧T恤牛仔裤", "旧短袖长裤"),
        ("T恤牛仔裤", "短袖长裤"),
    ]
    for old, new in rules:
        if old in patched:
            patched = patched.replace(old, new)
            replacements.append({"old": old, "new": new})
    return patched, replacements


def _paragraph_hard_gate_patch(content: str, *, max_chars: int = 70) -> str:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n+", content or "") if p.strip()]
    if not paragraphs:
        return content
    patched: list[str] = []
    for paragraph in paragraphs:
        if chinese_chars(paragraph) <= max_chars:
            patched.append(paragraph)
            continue
        patched.extend(
            p.strip()
            for p in _normalize_patch_paragraphs(paragraph, max_chars=max_chars).split("\n\n")
            if p.strip()
        )
    return "\n\n".join(patched)


def _try_paragraph_hard_gate_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    patched_content = _paragraph_hard_gate_patch(source_content, max_chars=70)
    if not patched_content.strip() or patched_content == source_content:
        return None
    before_chars = chinese_chars(source_content)
    after_chars = chinese_chars(patched_content)
    if after_chars != before_chars:
        return None
    before_paras = [p for p in re.split(r"\n\s*\n+", source_content) if p.strip()]
    after_paras = [p for p in re.split(r"\n\s*\n+", patched_content) if p.strip()]
    if len(after_paras) <= len(before_paras):
        return None
    return _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="paragraph_hard_gate_repair",
        output_extra={
            "revision_specialty": "prose_voice",
            "paragraph_repair": {
                "before_paragraphs": len(before_paras),
                "after_paragraphs": len(after_paras),
                "max_chars": 70,
                "preserved_chinese_chars": after_chars,
            },
        },
        dry_run=dry_run,
    )


def _brief_is_prose_targeted(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        "当前阅读层级：高分底稿，文风定点修订" in text
        and "revision_mode:targeted" in text
        and "reading_assessment_contract" in text
    )

def _store_local_patch_version(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    patched_content: str,
    strategy: str,
    output_extra: dict,
    dry_run: bool,
) -> ChapterVersion:
    anchor_rejection = _revision_author_sample_anchor_rejection(revision_brief, source_version.title or "", patched_content)
    if anchor_rejection:
        raise ValueError(f"local patch violates author sample anchors before persistence: {anchor_rejection}")
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=source_version.title,
        content=patched_content,
        status="draft",
        source=f"revision:{strategy}",
    )
    session.add(version)
    session.flush()
    task = GenerationTask(
        book_id=book_id,
        task_type="revise_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter.chapter_number,
                "dry_run": dry_run,
                "source_version_id": source_version.id,
                "revision_brief_id": revision_brief.id,
                "revision_mode": REVISION_MODE_LOCAL_PATCH,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "strategy": strategy,
                "content_chars": chinese_chars(patched_content),
                **output_extra,
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    return version


def _revision_required_beats(brief: ChapterBrief, *, rewrite_mode: bool, fresh_rewrite: bool) -> str:
    if not fresh_rewrite and not rewrite_mode:
        return sanitize_prompt_contract_text(brief.required_beats)
    keep: list[str] = []
    for part in brief.required_beats.replace("\n", "；").split("；"):
        item = part.strip()
        if not item:
            continue
        if item.startswith(("采纳二审建议：", "规避风险：", "修复质检问题：")):
            continue
        keep.append(item)
    if rewrite_mode and not fresh_rewrite:
        keep.append("结构重写时二审建议只作为抽象诊断，不得继承具体旧桥段、旧名词或旧场景要求")
    return sanitize_prompt_contract_text("；".join(keep))
