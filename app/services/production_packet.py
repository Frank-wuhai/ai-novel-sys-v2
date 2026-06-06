from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterVersion
from app.services.agent_plan_intelligence import format_semantic_memory_context
from app.services.author_workbench import build_author_workbench_report
from app.services.bias import build_bias_guard_block
from app.services.canon import format_canon_context
from app.services.chapter_unit_plans import ensure_chapter_unit_plan, format_chapter_unit_plan
from app.services.chapter_standards import ensure_chapter_production_standard
from app.services.director import build_chapter_director_sheet
from app.services.evidence import format_market_evidence_context
from app.services.feedback import format_author_preference_context
from app.services.production_context import ProductionContext, build_production_context
from app.services.production_run_review import build_production_pattern_memory, format_production_pattern_memory
from app.services.writing_intelligence import WritingIntelligenceContext, build_writing_intelligence_context
from app.services.writer_craft import WriterCraftContext, build_writer_craft_context
from app.services.writer_loop import WriterLoopPlan, build_writer_loop_plan


@dataclass(frozen=True)
class ChapterProductionPacket:
    mode: str
    context: ProductionContext
    constraints: str
    effective_required_beats: str
    director_sheet: str
    bias_guard: str
    writing_intelligence: WritingIntelligenceContext
    writer_craft: WriterCraftContext
    writer_loop: WriterLoopPlan
    chapter_unit_plan_id: int | None
    chapter_unit_plan: dict
    production_pattern_memory: dict
    market_signal_ids: list[int]
    canon_refs: dict[str, list[int]]
    semantic_memory_ids: list[int]
    audit: dict

    @property
    def prompt_values(self) -> dict:
        return {
            "market_evidence": self.context.market_evidence,
            "canon_context": self.context.canon_context,
            "author_preferences": self.context.author_preferences,
            "previous_chapter_context": self.context.previous_chapter_context,
            "director_sheet": self.director_sheet,
            "bias_guard": self.bias_guard,
        }

    @property
    def task_payload(self) -> dict:
        return {
            "market_signal_ids": self.market_signal_ids,
            "market_evidence_count": len(self.market_signal_ids),
            "canon_refs": self.canon_refs,
            "semantic_memory_ids": self.semantic_memory_ids,
            "director_sheet": self.director_sheet,
            "writing_intelligence": self.writing_intelligence.to_dict(),
            "writer_craft": self.writer_craft.to_dict(),
            "writer_loop": self.writer_loop.to_dict(),
            "chapter_unit_plan_id": self.chapter_unit_plan_id,
            "chapter_unit_plan": self.chapter_unit_plan,
            "production_pattern_memory": self.production_pattern_memory,
            "production_context_audit": self.context.audit,
            "production_packet_audit": self.audit,
        }


def build_chapter_production_packet(
    session: Session,
    *,
    book: Book,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    mode: str = "draft",
    revision_goal: str = "",
    revision_required_beats: str = "",
    revision_constraints: str = "",
    quality_report: str | None = None,
    previous_content: str = "",
    revision_context_mode: str = "draft",
    fresh_rewrite: bool = False,
    rewrite_mode: bool = False,
    chapter_id: int | None = None,
    chapter_brief_id: int | None = None,
) -> ChapterProductionPacket:
    market_evidence, market_signal_ids = format_market_evidence_context(session, genre=book.genre)
    canon_context, canon_refs = format_canon_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
    )
    semantic_memory_context, semantic_memory_ids = format_semantic_memory_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        query="\n".join([goal, required_beats, constraints, revision_goal, revision_required_beats, revision_constraints]),
    )
    if semantic_memory_context:
        canon_context = "\n\n".join([canon_context, semantic_memory_context])
    author_preferences = format_author_preference_context(session, book_id=book.id)
    previous_chapter_context = build_previous_chapter_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
    )
    production_pattern_memory = build_production_pattern_memory(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        limit=8,
    )
    effective_constraints = ensure_chapter_production_standard(
        revision_constraints or constraints,
        chapter_number=chapter_number,
    )
    effective_required_beats = revision_required_beats or required_beats
    context = build_production_context(
        market_evidence=market_evidence,
        canon_context=canon_context,
        author_preferences=author_preferences,
        previous_chapter_context=previous_chapter_context,
        quality_report=quality_report,
        previous_content=previous_content,
        revision_mode=revision_context_mode,
        fresh_rewrite=fresh_rewrite,
        rewrite_mode=rewrite_mode,
    )
    director_sheet = build_chapter_director_sheet(
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=effective_constraints,
        previous_chapter_context=context.previous_chapter_context,
        canon_context=context.canon_context,
        author_preferences=context.author_preferences,
        revision_goal=revision_goal,
        revision_required_beats=revision_required_beats,
        revision_constraints=effective_constraints if revision_constraints else "",
        mode=mode,
    )
    writing_intelligence = build_writing_intelligence_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        goal=revision_goal or goal,
        required_beats=effective_required_beats,
        constraints=effective_constraints,
        previous_chapter_context=context.previous_chapter_context,
        mode=mode,
    )
    writer_craft = build_writer_craft_context(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=revision_goal or goal,
        required_beats=effective_required_beats,
        constraints=effective_constraints,
        previous_chapter_context=context.previous_chapter_context,
        canon_context=context.canon_context,
    )
    writer_loop = build_writer_loop_plan(
        chapter_number=chapter_number,
        goal=revision_goal or goal,
        required_beats=effective_required_beats,
        constraints=effective_constraints,
        quality_report=quality_report,
        previous_content=previous_content,
        mode=mode,
    )
    chapter_unit_plan_id: int | None = None
    chapter_unit_plan_payload: dict = {}
    chapter_unit_plan_block = ""
    if chapter_id:
        chapter_unit_plan = ensure_chapter_unit_plan(
            session,
            chapter_id=chapter_id,
            chapter_brief_id=chapter_brief_id,
            chapter_number=chapter_number,
            goal=revision_goal or goal,
            required_beats=effective_required_beats,
            constraints=effective_constraints,
            previous_chapter_context=context.previous_chapter_context,
            mode=mode,
            source="production_packet",
            pattern_memory=production_pattern_memory,
        )
        chapter_unit_plan_id = chapter_unit_plan.id
        try:
            loaded_plan = json.loads(chapter_unit_plan.plan_json or "{}")
        except json.JSONDecodeError:
            loaded_plan = {}
        chapter_unit_plan_payload = loaded_plan if isinstance(loaded_plan, dict) else {}
        chapter_unit_plan_block = format_chapter_unit_plan(chapter_unit_plan)
    director_sheet = "\n\n".join(
        item
        for item in [
            director_sheet,
            chapter_unit_plan_block,
            format_production_pattern_memory(production_pattern_memory),
            writer_loop.prompt_block,
            writer_craft.prompt_block,
            writing_intelligence.prompt_block,
        ]
        if item
    )
    bias_guard = build_bias_guard_block(
        constraints=effective_constraints,
        author_preferences=context.author_preferences,
        story_context="\n".join([context.canon_context, context.previous_chapter_context]),
    )
    audit = {
        "packet_version": "chapter_production_packet_v3_writer_loop",
        "mode": mode,
        "revision_context_mode": revision_context_mode,
        "chapter_number": chapter_number,
        "effective_required_beats_chars": len(effective_required_beats or ""),
        "effective_constraints_chars": len(effective_constraints or ""),
        "director_sheet_chars": len(director_sheet or ""),
        "semantic_memory_count": len(semantic_memory_ids),
        "chapter_unit_plan_id": chapter_unit_plan_id,
        "chapter_unit_plan_units": len(chapter_unit_plan_payload.get("units") or []),
        "production_pattern_memory_reviews": production_pattern_memory.get("source_review_count", 0),
        "fresh_rewrite": fresh_rewrite,
        "rewrite_mode": rewrite_mode,
        "policy": "single_packet_for_prompt_context",
    }
    return ChapterProductionPacket(
        mode=mode,
        context=context,
        constraints=effective_constraints,
        effective_required_beats=effective_required_beats,
        director_sheet=director_sheet,
        bias_guard=bias_guard,
        writing_intelligence=writing_intelligence,
        writer_craft=writer_craft,
        writer_loop=writer_loop,
        chapter_unit_plan_id=chapter_unit_plan_id,
        chapter_unit_plan=chapter_unit_plan_payload,
        production_pattern_memory=production_pattern_memory,
        market_signal_ids=market_signal_ids,
        canon_refs=canon_refs,
        semantic_memory_ids=semantic_memory_ids,
        audit=audit,
    )


def build_previous_chapter_context(session: Session, *, book_id: int, chapter_number: int) -> str:
    workbench = build_author_workbench_report(session, book_id=book_id, chapter_number=chapter_number)
    continuity_text = workbench.prompt_text
    if chapter_number <= 1:
        return "\n\n".join(
            [
                "本章是第1章：直接建立可读场景、主角处境、核心钩子和章末期待。",
                continuity_text,
            ]
        ).strip()
    previous = session.scalar(
        select(Chapter)
        .where(Chapter.book_id == book_id, Chapter.chapter_number < chapter_number)
        .order_by(Chapter.chapter_number.desc())
    )
    if not previous:
        return "\n\n".join(
            [
                "未找到上一章；按本章 brief 写，但不要与已登记 Canon 冲突。",
                continuity_text,
            ]
        ).strip()
    preferred = session.scalar(
        select(ChapterVersion)
        .where(
            ChapterVersion.chapter_id == previous.id,
            ChapterVersion.status.in_(["approved", "reviewed_pass"]),
        )
        .order_by(ChapterVersion.id.desc())
    )
    latest = preferred or session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == previous.id)
        .order_by(ChapterVersion.id.desc())
    )
    if not latest:
        return "\n\n".join(
            [
                f"上一章是第{previous.chapter_number}章，但尚未生成正文；按本章 brief 写，并保持剧情承接意识。",
                continuity_text,
            ]
        ).strip()
    content = latest.content or ""
    ending = content[-900:] if len(content) > 900 else content
    lines = [
        f"上一章：第{previous.chapter_number}章《{latest.title or previous.title}》 status={latest.status}",
    ]
    if previous.summary:
        lines.append(f"连续性摘要：{previous.summary}")
    lines.append("上一章结尾/最新可用正文片段（本章必须承接其后果、情绪和未解决压力，不要另起炉灶）：")
    lines.append(ending)
    if continuity_text:
        lines.append("")
        lines.append(continuity_text)
    return "\n".join(lines)
