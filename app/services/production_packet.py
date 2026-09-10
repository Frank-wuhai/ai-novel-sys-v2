from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Book, Chapter, ChapterVersion
from app.models.entities import StoryBible
from app.services.aesthetic_profile import profile_from_story_text
from app.services.agent_plan_intelligence import format_semantic_memory_context
from app.services.author_workbench import build_author_workbench_report
from app.services.bias import build_bias_guard_block
from app.services.brief_sanitizer import sanitize_prompt_contract_text
from app.services.book_aesthetic_standard import build_book_aesthetic_standard
from app.services.canon import format_canon_context
from app.services.chapter_unit_plans import ensure_chapter_unit_plan, format_chapter_unit_plan
from app.services.chapter_standards import ensure_chapter_production_standard, _resolve_chapter_type
from app.services.context_contamination import assert_context_not_contaminated, audit_context_contamination
from app.services.continuity import ensure_chapter_exit_state_table
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.director import build_chapter_director_sheet
from app.services.evidence import format_market_evidence_context
from app.services.feedback import format_author_preference_context, format_chapter_sample_adoption_context
from app.services.naming_governance import build_naming_governance_block
from app.services.production_context import ProductionContext, build_production_context
from app.services.production_contract import assert_contract_snapshot, build_production_contract_snapshot, sanitize_production_contract_text
from app.services.production_cache import cached_production_value
from app.services.production_blueprint import ProductionBlueprint, build_production_blueprint
from app.services.prompt_isolation import isolate_generation_inputs
from app.services.production_optimization import optimization_prompt_block
from app.services.production_run_review import build_production_pattern_memory, format_production_pattern_memory
from app.services.reality_logic import build_reality_logic_prompt_for_context
from app.services.reference_craft import build_reference_craft_block
from app.services.story_dna import chapter_engine_for_number, story_dna_for_book
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
    full_director_sheet: str
    blueprint: ProductionBlueprint
    bias_guard: str
    writing_intelligence: WritingIntelligenceContext
    writer_craft: WriterCraftContext
    writer_loop: WriterLoopPlan
    chapter_unit_plan_id: int | None
    chapter_unit_plan: dict
    production_pattern_memory: dict
    book_aesthetic_standard: dict
    market_signal_ids: list[int]
    canon_refs: dict[str, list[int]]
    semantic_memory_ids: list[int]
    audit: dict

    @property
    def prompt_values(self) -> dict:
        return {
            "market_evidence": _prompt_clip(self.context.market_evidence, _prompt_budget("market_evidence")),
            "canon_context": _prompt_clip(self.context.canon_context, _prompt_budget("canon_context")),
            "author_preferences": _prompt_clip(self.context.author_preferences, _prompt_budget("author_preferences")),
            "previous_chapter_context": _prompt_clip(self.context.previous_chapter_context, _prompt_budget("previous_chapter_context"), tail=True),
            "director_sheet": _prompt_clip(self.director_sheet, _prompt_budget("director_sheet")),
            "bias_guard": _prompt_clip(self.bias_guard, _prompt_budget("bias_guard")),
        }

    @property
    def task_payload(self) -> dict:
        return {
            "market_signal_ids": self.market_signal_ids,
            "market_evidence_count": len(self.market_signal_ids),
            "canon_refs": self.canon_refs,
            "semantic_memory_ids": self.semantic_memory_ids,
            "director_sheet": self.full_director_sheet,
            "production_blueprint": self.blueprint.to_dict(),
            "writing_intelligence": self.writing_intelligence.to_dict(),
            "writer_craft": self.writer_craft.to_dict(),
            "writer_loop": self.writer_loop.to_dict(),
            "chapter_unit_plan_id": self.chapter_unit_plan_id,
            "chapter_unit_plan": self.chapter_unit_plan,
            "production_pattern_memory": self.production_pattern_memory,
            "book_aesthetic_standard": self.book_aesthetic_standard,
            "production_context_audit": self.context.audit,
            "production_packet_audit": self.audit,
            "production_contract": self.audit.get("production_contract"),
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
    isolated_inputs = isolate_generation_inputs(
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        previous_chapter_context="",
        canon_context="",
        strict_authority_fields=False,
    )
    goal = isolated_inputs.goal
    required_beats = isolated_inputs.required_beats
    constraints = isolated_inputs.constraints
    if revision_goal or revision_required_beats or revision_constraints:
        isolated_revision = isolate_generation_inputs(
            goal=revision_goal,
            required_beats=revision_required_beats,
            constraints=revision_constraints,
            previous_chapter_context="",
            canon_context="",
            strict_authority_fields=False,
        )
        revision_goal = isolated_revision.goal
        revision_required_beats = isolated_revision.required_beats
        revision_constraints = isolated_revision.constraints
        prompt_isolation_warnings = [*isolated_inputs.warnings, *isolated_revision.warnings]
    else:
        prompt_isolation_warnings = isolated_inputs.warnings
    story_bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book.id).order_by(StoryBible.id.desc()))
    aesthetic_profile = profile_from_story_text(
        style_guide=story_bible.style_guide if story_bible else "",
        forbidden_rules=story_bible.forbidden_rules if story_bible else "",
    )
    story_dna = story_dna_for_book(session, book_id=book.id)
    chapter_engine = chapter_engine_for_number(story_dna, chapter_number)
    market_evidence, market_signal_ids = cached_production_value(
        ("market_evidence", book.genre or "", settings.production_profile),
        lambda: format_market_evidence_context(session, genre=book.genre),
    )
    canon_context, canon_refs = format_canon_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
    )
    semantic_memory_context, semantic_memory_ids = format_semantic_memory_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        query=sanitize_prompt_contract_text(
            "\n".join([goal, required_beats, constraints, revision_goal, revision_required_beats, revision_constraints])
        ),
    )
    if semantic_memory_context:
        canon_context = "\n\n".join([canon_context, semantic_memory_context])
    brief_text = "\n".join(
        item
        for item in [
            goal,
            required_beats,
            constraints,
            revision_goal,
            revision_required_beats,
            revision_constraints,
        ]
        if item
    )
    contamination = audit_context_contamination(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        brief_text=brief_text,
        canon_context=canon_context,
        semantic_memory_context=semantic_memory_context,
        previous_content=previous_content,
        fresh_rewrite=fresh_rewrite,
    )
    if _brief_contamination_is_repairable(contamination.blockers):
        repaired_brief = repair_chapter_brief(session, book_id=book.id, chapter_number=chapter_number)
        goal = repaired_brief.goal
        required_beats = repaired_brief.required_beats
        constraints = repaired_brief.constraints
        revision_goal = repaired_brief.goal if revision_goal else ""
        revision_required_beats = repaired_brief.required_beats if revision_required_beats else ""
        revision_constraints = repaired_brief.constraints if revision_constraints else ""
        brief_text = "\n".join(
            item
            for item in [
                goal,
                required_beats,
                constraints,
                revision_goal,
                revision_required_beats,
                revision_constraints,
            ]
            if item
        )
        semantic_memory_context, semantic_memory_ids = format_semantic_memory_context(
            session,
            book_id=book.id,
            chapter_number=chapter_number,
            query=sanitize_prompt_contract_text(
                "\n".join([goal, required_beats, constraints, revision_goal, revision_required_beats, revision_constraints])
            ),
        )
        canon_context, canon_refs = format_canon_context(
            session,
            book_id=book.id,
            chapter_number=chapter_number,
        )
        if semantic_memory_context:
            canon_context = "\n\n".join([canon_context, semantic_memory_context])
        contamination = audit_context_contamination(
            session,
            book_id=book.id,
            chapter_number=chapter_number,
            brief_text=brief_text,
            canon_context=canon_context,
            semantic_memory_context=semantic_memory_context,
            previous_content=previous_content,
            fresh_rewrite=fresh_rewrite,
        )
    assert_context_not_contaminated(contamination)
    # 全局写作风格护栏：只约束读感，不强行模仿某一本书。
    # 之前这里把“第1句≤6字、短句快切、所有章节必须按斩神写”注入到每章，
    # 会把模型稳定推向冷硬电报句，和 prose_naturalness 门禁互相打架。
    natural_webnovel_style_block = """【自然网文正文约束·最高优先级】:
本章要像真人中文作者写出的男频网文正文，不要像提纲、质检清单、短视频分镜或翻译腔。

【短段不等于电报句】:
- 段落可以短，但句子必须有自然承接。不要把每个动作都拆成 2-6 字孤句。
- 连续 3 段以上不得都只有一个极短判断句；每 3-5 段至少有一句 18-45 字的自然复合句，用动作、反应、环境和后果连起来。
- 可以单独成段的短句只用于重击、转折、对白停顿；不能把全章写成“他看。门响。风冷。”这种冷硬切片。

【自然语气和虚词】:
- AI 味的核心风险之一是缺少自然语气词、虚词和句间胶水。正文要允许“了、着、就、还、也、倒、可、吧、啊、呢、嘛、嗯”等自然出现。
- 对白不能只报功能信息；每个关键人物至少说出半句立场、试探、遮掩、急躁、犹豫或找补。
- 允许口语里的停顿、反问、半截话和临场改口，但不要堆网络口癖。

【画面和因果】:
- 开篇要进入具体处境，可以有外部压力、异常细节、交易催促、人物动作或关系盘问，但不要为了“反差”硬造无关怪画面。
- 穷、病、债、罚款可以写，但必须落到现场动作和选择，不要写成苦难说明书。
- 每个场景要有空间边界、人物站位、关键物件、动作轨迹和动作带来的后果。

【禁忌】:
- 禁止冷硬装酷式短句；禁止只用抽象判断替代场景；禁止系统面板替代人物行动；禁止连续功能对白。
- 禁止为了模仿所谓爆款，把本书既有题材、人物处境和用户修订方向改成无关套路。
"""
    _chapter_type = _resolve_chapter_type(chapter_number)
    prompt_goal = natural_webnovel_style_block + "\n" + sanitize_production_contract_text(
        sanitize_prompt_contract_text(revision_goal or goal) or (revision_goal or goal),
        chapter_type=_chapter_type,
    )
    prompt_required_beats = sanitize_production_contract_text(
        sanitize_prompt_contract_text(revision_required_beats or required_beats),
        chapter_type=_chapter_type,
    )
    prompt_constraints = sanitize_production_contract_text(
        sanitize_prompt_contract_text(revision_constraints or constraints),
        chapter_type=_chapter_type,
    )
    base_goal = sanitize_production_contract_text(sanitize_prompt_contract_text(goal) or goal, chapter_type=_chapter_type)
    base_required_beats = sanitize_production_contract_text(sanitize_prompt_contract_text(required_beats), chapter_type=_chapter_type)
    sample_adoption_context = format_chapter_sample_adoption_context(session, book_id=book.id, chapter_number=chapter_number)
    base_constraints_source = sanitize_production_contract_text(
        sanitize_prompt_contract_text(_merge_author_direction_blocks(revision_constraints or constraints, sample_adoption_context))
        or _merge_author_direction_blocks(revision_constraints or constraints, sample_adoption_context),
        chapter_type=_chapter_type,
    )
    if _chapter_type == "opening" and book.id == 7:
        base_constraints_source = _merge_author_direction_blocks(
            base_constraints_source,
            """【Book7 第1章阶段锁·最高优先级】
- 第1章只写现实底座、进入写实蜀山世界、凡人求活和一次写实武学/行动尝试；不得提前进入修真阶段。
- 禁止出现师父传功、散修传法、真气灌体、内力洗髓、丹田、经脉、热流、掌心发热、气感、符咒显形、修真觉醒。
- 凡人阶段的“变强反馈”只能写成可见动作结果：脚步更稳、出拳更准、借力更顺、打中木桩/逼退对手/旁观者态度变化；不得用修真体感解释。
- 章末钩子只能来自游戏内未解入口、下一次探索机会、现实压力或数据壁垒异常提示；不得写游戏能力进入现实身体。""",
        )
    author_preferences = _merge_author_direction_blocks(
        sample_adoption_context,
        format_author_preference_context(session, book_id=book.id),
    )
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
    book_aesthetic_standard = build_book_aesthetic_standard(session, book_id=book.id)
    relation_scene_context = "\n".join(
        item
        for item in [
            base_goal,
            prompt_required_beats or base_required_beats,
            base_constraints_source,
            previous_chapter_context,
        ]
        if item
    )
    reference_craft_block = build_reference_craft_block(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        scene_context=relation_scene_context,
    )
    reality_logic_block = build_reality_logic_prompt_for_context(
        "\n".join(
            item
            for item in [
                base_goal,
                prompt_required_beats or base_required_beats,
                base_constraints_source,
                previous_chapter_context,
                previous_content,
            ]
            if item
        )
    )
    naming_governance_block = build_naming_governance_block(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
    )
    base_constraints = ensure_chapter_production_standard(
        base_constraints_source,
        chapter_number=chapter_number,
        chapter_type=_chapter_type,
    )
    dna_block = _chapter_dna_block(story_dna=story_dna, chapter_engine=chapter_engine, chapter_number=chapter_number)
    profiled_constraints = "\n\n".join(item for item in [base_constraints, reality_logic_block, dna_block, reference_craft_block] if item)
    effective_required_beats = prompt_required_beats or sanitize_prompt_contract_text(required_beats)
    optimization_block = optimization_prompt_block(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        goal=prompt_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
    )
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
        goal=base_goal,
        required_beats=base_required_beats,
        constraints=base_constraints,
        previous_chapter_context=context.previous_chapter_context,
        canon_context=context.canon_context,
        author_preferences=context.author_preferences,
        revision_goal=prompt_goal if revision_goal else "",
        revision_required_beats=prompt_required_beats,
        revision_constraints=base_constraints if revision_constraints else "",
        mode=mode,
    )
    writing_intelligence = build_writing_intelligence_context(
        session,
        book_id=book.id,
        chapter_number=chapter_number,
        goal=prompt_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
        previous_chapter_context=context.previous_chapter_context,
        mode=mode,
    )
    writer_craft = build_writer_craft_context(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=prompt_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
        previous_chapter_context=context.previous_chapter_context,
        canon_context=context.canon_context,
    )
    writer_loop = build_writer_loop_plan(
        chapter_number=chapter_number,
        goal=prompt_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
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
            goal=base_goal,
            required_beats=prompt_required_beats or base_required_beats,
            constraints=profiled_constraints,
            previous_chapter_context=context.previous_chapter_context,
            mode=mode,
            source="production_packet",
            pattern_memory=production_pattern_memory,
            aesthetic_standard=book_aesthetic_standard.to_dict(),
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
            aesthetic_profile,
            book_aesthetic_standard.prompt_block(),
            reference_craft_block,
            dna_block,
            chapter_unit_plan_block,
            format_production_pattern_memory(production_pattern_memory),
            optimization_block,
            naming_governance_block,
            writer_loop.prompt_block,
            writer_craft.prompt_block,
            writing_intelligence.prompt_block,
        ]
        if item
    )
    full_director_sheet = director_sheet
    bias_guard = build_bias_guard_block(
        constraints=profiled_constraints,
        author_preferences=context.author_preferences,
        story_context="\n".join([context.canon_context, context.previous_chapter_context]),
    )
    blueprint = build_production_blueprint(
        chapter_number=chapter_number,
        mode=mode,
        goal=base_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
        previous_chapter_context=context.previous_chapter_context,
        canon_context=context.canon_context,
        author_preferences=context.author_preferences,
        chapter_unit_plan=chapter_unit_plan_payload,
        book_aesthetic_standard=book_aesthetic_standard.to_dict(),
        style_contract={
            "aesthetic_profile": aesthetic_profile,
            "book_aesthetic_standard": book_aesthetic_standard.to_dict(),
            "reference_craft_block": reference_craft_block,
            "story_dna": story_dna,
            "chapter_engine": chapter_engine,
            "naming_governance": naming_governance_block,
        },
        quality_report=quality_report,
        previous_content=previous_content,
        fresh_rewrite=fresh_rewrite,
        rewrite_mode=rewrite_mode,
    )
    writer_optimization_block = _compact_writer_optimization_block(optimization_block)
    director_sheet = "\n\n".join(item for item in [blueprint.prompt_block, reality_logic_block, writer_optimization_block] if item)
    contract_snapshot = build_production_contract_snapshot(
        chapter_type=_chapter_type,
        goal=base_goal,
        required_beats=effective_required_beats,
        constraints=profiled_constraints,
        director_sheet=director_sheet,
        canon_context=context.canon_context,
        previous_chapter_context=context.previous_chapter_context,
    )
    assert_contract_snapshot(contract_snapshot)
    audit = {
        "packet_version": "chapter_production_packet_v4_blueprint",
        "mode": mode,
        "revision_context_mode": revision_context_mode,
        "chapter_number": chapter_number,
        "effective_required_beats_chars": len(effective_required_beats or ""),
        "effective_constraints_chars": len(base_constraints or ""),
        "profiled_constraints_chars": len(profiled_constraints or ""),
        "director_sheet_chars": len(director_sheet or ""),
        "full_director_sheet_chars": len(full_director_sheet or ""),
        "production_blueprint": blueprint.to_dict(),
        "semantic_memory_count": len(semantic_memory_ids),
        "chapter_unit_plan_id": chapter_unit_plan_id,
        "chapter_unit_plan_units": len(chapter_unit_plan_payload.get("units") or []),
        "production_pattern_memory_reviews": production_pattern_memory.get("source_review_count", 0),
        "book_aesthetic_standard": book_aesthetic_standard.status,
        "book_taste_memory_count": len(book_aesthetic_standard.taste_memory),
        "reference_craft_chars": len(reference_craft_block or ""),
        "reality_logic_chars": len(reality_logic_block or ""),
        "production_optimization": bool(optimization_block),
        "aesthetic_profile": bool(aesthetic_profile),
        "story_dna": bool(story_dna),
        "chapter_engine": chapter_engine,
        "fresh_rewrite": fresh_rewrite,
        "rewrite_mode": rewrite_mode,
        "policy": "single_packet_for_prompt_context",
        "prompt_policy": "compressed_blueprint_for_generation",
        "prompt_isolation": {
            "cleaned": bool(prompt_isolation_warnings),
            "warnings": prompt_isolation_warnings[:12],
        },
        "context_contamination": contamination.to_dict(),
        "production_contract": contract_snapshot.to_dict(),
    }
    clipped_constraints = _prompt_clip(
        "\n\n".join(item for item in [reality_logic_block, base_constraints] if item),
        _prompt_budget("constraints"),
    )
    return ChapterProductionPacket(
        mode=mode,
        context=context,
        constraints=clipped_constraints,
        effective_required_beats=_prompt_clip(effective_required_beats, _prompt_budget("required_beats")),
        director_sheet=director_sheet,
        full_director_sheet=full_director_sheet,
        blueprint=blueprint,
        bias_guard=bias_guard,
        writing_intelligence=writing_intelligence,
        writer_craft=writer_craft,
        writer_loop=writer_loop,
        chapter_unit_plan_id=chapter_unit_plan_id,
        chapter_unit_plan=chapter_unit_plan_payload,
        production_pattern_memory=production_pattern_memory,
        book_aesthetic_standard=book_aesthetic_standard.to_dict(),
        market_signal_ids=market_signal_ids,
        canon_refs=canon_refs,
        semantic_memory_ids=semantic_memory_ids,
        audit=audit,
    )


def _prompt_budget(name: str) -> int:
    standard = {
        "market_evidence": 600,
        "canon_context": 900,
        "author_preferences": 500,
        "previous_chapter_context": 700,
        "director_sheet": 3600,
        "bias_guard": 650,
        "constraints": 900,
        "required_beats": 900,
    }
    fast = {
        "market_evidence": 400,
        "canon_context": 700,
        "author_preferences": 400,
        "previous_chapter_context": 600,
        "director_sheet": 3000,
        "bias_guard": 500,
        "constraints": 700,
        "required_beats": 700,
    }
    deep = {
        "market_evidence": 900,
        "canon_context": 1200,
        "author_preferences": 700,
        "previous_chapter_context": 900,
        "director_sheet": 4200,
        "bias_guard": 800,
        "constraints": 1200,
        "required_beats": 1200,
    }
    profile = settings.production_profile
    table = fast if profile == "fast" else (deep if profile == "deep" else standard)
    return table.get(name, standard.get(name, 1600))


def _merge_author_direction_blocks(*blocks: str) -> str:
    parts = [str(block or "").strip() for block in blocks if str(block or "").strip()]
    return "\n\n".join(parts)


def _compact_writer_optimization_block(block: str) -> str:
    text = str(block or "").strip()
    if not text:
        return ""
    rows = []
    for line in text.splitlines():
        compact = line.strip()
        if not compact:
            continue
        if compact in {"production_optimization@v1", "production_optimization@end"}:
            rows.append(compact)
            continue
        if compact.startswith("章节类型：") or compact.startswith("章节骨架验收："):
            rows.append(compact)
        elif compact.startswith("生成前必须"):
            rows.append("生成前必须把本章目标、阻碍、行动、代价/回报和章末变化写成正文场景，不输出清单。")
    return "\n".join(dict.fromkeys(rows))


def _prompt_clip(value: str, limit: int, *, tail: bool = False) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if tail:
        return "…\n" + text[-limit:]
    return text[:limit] + "\n…"


def _chapter_dna_block(*, story_dna: str, chapter_engine: str, chapter_number: int) -> str:
    if not story_dna:
        return ""
    return "\n".join(
        [
            "【本书作品DNA / 本章发动机】",
            story_dna,
            f"本章优先发动机: 第{chapter_number}章使用“{chapter_engine}”。",
            "执行: 本章必须围绕该发动机安排目标、阻碍、动作、代价、收益和章末钩子；不要回到通用冷硬悬疑模板。",
            "【本书作品DNA / 本章发动机结束】",
        ]
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
    # P1-exit-state · 优先注入前章"章末状态快照"(结构化 · 稳定 · 不随并行 rebuild 漂移)
    exit_state = _load_chapter_exit_state(session, chapter_version_id=latest.id)
    if exit_state:
        lines.append("")
        lines.append("上一章末状态快照（权威 · 本章必须承接以下状态、不得漂移）：")
        lines.append(f"- 主角状态：{exit_state['main_character_state']}")
        lines.append(f"- 物理位置：{exit_state['physical_location']}")
        lines.append(f"- 时间节点：{exit_state['time_marker']}")
        lines.append(f"- 关系变化：{exit_state['relationship_delta']}")
        lines.append(f"- 章末钩子（本章开局必须直接承接）：{exit_state['plot_hook']}")
        if exit_state.get('hook_keywords'):
            kws = exit_state['hook_keywords']
            if isinstance(kws, list) and kws:
                lines.append(f"- 承接关键词（本章正文必须承接以下具体 hook · 系统摘要词已自动过滤 · 空列表 = 不硬约束）：{' / '.join(kws)}")
        if exit_state.get('new_facts'):
            lines.append(f"- 新增 canon 事实：{exit_state['new_facts']}")
        lines.append("")
    if previous.summary:
        lines.append(f"连续性摘要：{previous.summary}")
    lines.append("上一章结尾/最新可用正文片段（本章必须承接其后果、情绪和未解决压力，不要另起炉灶）：")
    lines.append(ending)
    if continuity_text:
        lines.append("")
        lines.append(continuity_text)
    return "\n".join(lines)


def load_previous_hook_keywords(session: Session, *, book_id: int, chapter_number: int) -> list[str]:
    """加载前章 (chapter_number-1) exit_state.hook_keywords · 供 gate 校验用。
    若前章无 exit_state 或无 keywords · 返回空列表（不触发 hook_missing）。
    """
    if chapter_number <= 1:
        return []
    from app.models.entities import Chapter, ChapterVersion
    prev_chapter = session.query(Chapter).filter_by(book_id=book_id, chapter_number=chapter_number - 1).one_or_none()
    if not prev_chapter:
        return []
    prev_cv = (session.query(ChapterVersion)
               .filter_by(chapter_id=prev_chapter.id)
               .filter(ChapterVersion.status.in_(["approved", "published_review"]))
               .order_by(ChapterVersion.id.desc()).first())
    if not prev_cv:
        return []
    exit_state = _load_chapter_exit_state(session, chapter_version_id=prev_cv.id)
    if not exit_state:
        return []
    kws = exit_state.get("hook_keywords") or []
    return [k for k in kws if k and isinstance(k, str)]


def _load_chapter_exit_state(session: Session, *, chapter_version_id: int) -> dict | None:
    """读 chapter_exit_states · 返回 dict 或 None。"""
    ensure_chapter_exit_state_table(session)
    from sqlalchemy import text as _sql_text
    row = session.execute(
        _sql_text("""SELECT main_character_state, relationship_delta, plot_hook, new_facts,
                              physical_location, time_marker, raw_summary, hook_keywords
                     FROM chapter_exit_states
                     WHERE chapter_version_id=:v
                     ORDER BY id DESC LIMIT 1"""),
        {"v": chapter_version_id},
    ).fetchone()
    if not row:
        return None
    import json as _json
    kws = []
    if row[7]:
        try:
            kws = _json.loads(row[7])
        except Exception:
            kws = []
    return {
        "main_character_state": row[0],
        "relationship_delta": row[1],
        "plot_hook": row[2],
        "new_facts": row[3],
        "physical_location": row[4],
        "time_marker": row[5],
        "raw_summary": row[6],
        "hook_keywords": kws,
    }


def _brief_contamination_is_repairable(blockers: list[str]) -> bool:
    if not blockers:
        return False
    repairable = (
        "brief 未承接当前骨架锚点",
        "brief 含旧设定锚点",
    )
    return all(any(marker in blocker for marker in repairable) for blocker in blockers)
