from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError
from app.models.entities import Book, ChapterVersion, GenerationTask
from app.services.chapter_standards import extract_min_chars
from app.services.chapter_unit_plans import align_chapter_unit_plan
from app.services.production_llm import (
    expand_short_draft_output,
    llm_parameter_snapshot,
    llm_usage_payload,
    parse_or_repair_draft_output,
    record_generation_llm_log,
    repair_humanized_unit_flow,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.production_gate import assert_production_gate
from app.services.production_run_review import record_production_run_review
from app.services.production_state import get_or_create_chapter, latest_brief, latest_foundation, next_version_number
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates


def draft_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    assert_production_gate(session, book_id=book_id, action="draft_chapter")
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = get_or_create_chapter(session, book_id=book_id, chapter_number=chapter_number)
    foundation = latest_foundation(session, book_id)
    brief = latest_brief(session, chapter.id)
    if not foundation:
        raise ValueError("story foundation is required before drafting")
    if not brief:
        raise ValueError("chapter brief is required before drafting")
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="draft_chapter", version="v4")
    packet = build_chapter_production_packet(
        session,
        book=book,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
        chapter_number=chapter_number,
        mode="draft",
        chapter_id=chapter.id,
        chapter_brief_id=brief.id,
    )
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        **packet.prompt_values,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
        chapter_number=chapter_number,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=packet.constraints,
    )
    provider = get_provider(dry_run)
    model = settings.llm_draft_model
    temperature = settings.llm_draft_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=temperature,
        model=model,
    )
    response = provider.generate(
        prompt,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    try:
        draft = parse_or_repair_draft_output(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            max_tokens=settings.llm_draft_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节生成",
        )
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
                    **packet.task_payload,
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
                    **llm_usage_payload(response, prompt=prompt),
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
            status="failed",
            error_category="structured_output",
        )
        raise
    min_chars = extract_min_chars(brief.goal, brief.required_beats, packet.constraints)
    draft, length_repair = expand_short_draft_output(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节生成",
    )
    draft, unit_flow_repair = repair_humanized_unit_flow(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_draft_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节生成",
    )
    unit_report = (unit_flow_repair.get("after") if unit_flow_repair.get("accepted") else None) or unit_flow_repair.get("before")
    unit_plan_alignment = align_chapter_unit_plan(packet.chapter_unit_plan, unit_report)
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",
        source=response.provider,
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
        task_type="draft_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
                "min_chars": min_chars,
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
