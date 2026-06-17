from __future__ import annotations

import re
from dataclasses import dataclass, asdict

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.entities import (
    Book,
    Character,
    CharacterState,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    FeedbackAdjustment,
    Foreshadow,
    KnowledgeEmbedding,
    PlatformFeedback,
    PlotThread,
    PowerSystem,
    StoryBible,
    StoryArc,
    StoryFoundation,
    Volume,
    WorldRule,
)
from app.services.canon import add_character, add_plot_thread, add_power_system, add_world_rule
from app.services.dashboard_production_actions import restart_production_from_chapter
from app.services.planning import create_chapter_plan


@dataclass(frozen=True)
class SkeletonContextResetResult:
    status: str
    book_id: int
    start_chapter: int
    backup_path: str
    deleted: dict[str, int]
    created: dict[str, int]
    sanitized_skeleton: dict[str, str]
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def reset_context_after_skeleton_approval(
    session: Session,
    *,
    book_id: int,
    skeleton: dict[str, str],
    start_chapter: int = 1,
    plan_count: int = 5,
) -> SkeletonContextResetResult:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    start_chapter = max(1, int(start_chapter or 1))
    plan_count = max(1, int(plan_count or 1))
    deprecated_terms = _deprecated_terms_from_existing_context(session, book_id=book_id, new_skeleton=skeleton)
    sanitized_skeleton = _sanitize_deprecated_terms(skeleton, deprecated_terms)

    deleted: dict[str, int] = {
        "characters": 0,
        "character_states": 0,
        "world_rules": 0,
        "power_systems": 0,
        "plot_threads": 0,
        "foreshadows": 0,
        "knowledge_embeddings": 0,
        "feedback_adjustments_superseded": 0,
        "skeleton_feedback_deleted": 0,
        "old_foundations_deleted": 0,
    }
    created: dict[str, int] = {"characters": 0, "world_rules": 0, "power_systems": 0, "plot_threads": 0, "chapter_briefs": 0}

    # One restart backup covers both the chapter cleanup and the context reset in this transaction.
    restart = restart_production_from_chapter(session, book_id=book_id, start_chapter=start_chapter)
    for key, value in (restart.get("deleted") or {}).items():
        deleted[key] = int(value or 0)

    character_ids = [item.id for item in session.scalars(select(Character).where(Character.book_id == book_id))]
    if character_ids:
        states = list(session.scalars(select(CharacterState).where(CharacterState.character_id.in_(character_ids))))
        deleted["character_states"] = len(states)
        for state in states:
            session.delete(state)
    for model, key in [
        (Character, "characters"),
        (WorldRule, "world_rules"),
        (PowerSystem, "power_systems"),
        (PlotThread, "plot_threads"),
        (Foreshadow, "foreshadows"),
    ]:
        rows = list(session.scalars(select(model).where(model.book_id == book_id)))
        deleted[key] = len(rows)
        for row in rows:
            session.delete(row)

    embedding_result = session.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id))
    deleted["knowledge_embeddings"] = int(embedding_result.rowcount or 0)

    feedback_rows = list(
        session.scalars(
            select(PlatformFeedback).where(
                PlatformFeedback.book_id == book_id,
                PlatformFeedback.metric_name.in_(["skeleton_approval", "revision_suggestion"]),
            )
        )
    )
    deleted["skeleton_feedback_deleted"] = len(feedback_rows)
    for row in feedback_rows:
        session.delete(row)

    for adjustment in session.scalars(select(FeedbackAdjustment).where(FeedbackAdjustment.book_id == book_id)):
        if adjustment.status != "superseded":
            adjustment.status = "superseded"
            deleted["feedback_adjustments_superseded"] += 1

    latest_foundation = session.scalar(
        select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc())
    )
    if latest_foundation:
        old_foundations = list(
            session.scalars(
                select(StoryFoundation).where(
                    StoryFoundation.book_id == book_id,
                    StoryFoundation.id != latest_foundation.id,
                )
            )
        )
        deleted["old_foundations_deleted"] = len(old_foundations)
        for row in old_foundations:
            session.delete(row)

    _sanitize_persisted_skeleton_sources(
        session,
        book_id=book_id,
        sanitized_skeleton=sanitized_skeleton,
        deprecated_terms=deprecated_terms,
    )

    premise = str(sanitized_skeleton.get("premise") or "").strip()
    world_engine = str(sanitized_skeleton.get("world_engine") or "").strip()
    protagonist_engine = str(sanitized_skeleton.get("protagonist_engine") or "").strip()
    conflict_engine = str(sanitized_skeleton.get("conflict_engine") or "").strip()

    add_character(
        session,
        book_id=book_id,
        name="主角",
        role="protagonist",
        personality=protagonist_engine,
        ability="以新版作品设定为准；能力收益、边界、失败条件和代价必须在正文中可见。",
        background=premise,
    )
    created["characters"] += 1
    add_world_rule(
        session,
        book_id=book_id,
        category="新版作品设定",
        rule_text=world_engine or premise or "后续生产只遵守已确认的新版作品设定。",
        status="active",
    )
    created["world_rules"] += 1
    add_power_system(
        session,
        book_id=book_id,
        name="核心能力",
        rules=protagonist_engine or premise,
        costs="以新版作品设定中的代价、失败条件和边界为准；不得沿用旧稿能力表现。",
        limits="旧章节、旧质检、旧修订合同和旧语义记忆均不再作为生产依据。",
        status="active",
    )
    created["power_systems"] += 1
    add_plot_thread(
        session,
        book_id=book_id,
        name="新版主线压力",
        description=conflict_engine or premise,
        status="open",
    )
    created["plot_threads"] += 1

    briefs = create_chapter_plan(
        session,
        book_id=book_id,
        start=start_chapter,
        count=plan_count,
        goal_prefix="按新版作品设定重新开局",
        required_beats="本轮为新版骨架确认后的清洁生产；不得引用旧稿、旧质检、旧修订合同、旧主角名、旧世界名或旧桥段。",
        constraints="以已确认的新版 Story Foundation、Story Bible、Canon 和作品 DNA 为唯一生产依据。",
    )
    created["chapter_briefs"] = len(briefs)
    session.flush()

    return SkeletonContextResetResult(
        status="reset",
        book_id=book_id,
        start_chapter=start_chapter,
        backup_path=str(restart.get("backup_path") or ""),
        deleted=deleted,
        created=created,
        sanitized_skeleton=sanitized_skeleton,
        message=f"已按新版骨架清理旧生产上下文，并从第 {start_chapter} 章重建清洁生产说明。",
    )


def _deprecated_terms_from_existing_context(session: Session, *, book_id: int, new_skeleton: dict[str, str]) -> list[str]:
    positive_text = "\n".join(
        str(new_skeleton.get(key) or "")
        for key in [
            "premise",
            "reader_promise",
            "world_engine",
            "protagonist_engine",
            "conflict_engine",
            "style_guide",
            "aesthetic_profile",
            "story_dna",
            "volume_summary",
            "arc_goal",
            "arc_climax",
            "arc_turn",
        ]
    )
    old_text_parts: list[str] = []
    terms: set[str] = set()
    for character in session.scalars(select(Character).where(Character.book_id == book_id)):
        if character.name and character.name not in positive_text:
            terms.add(character.name)
        old_text_parts.extend([character.name, character.personality, character.ability, character.background])
    for model, fields in [
        (WorldRule, ("category", "rule_text")),
        (PowerSystem, ("name", "rules", "costs", "limits")),
        (PlotThread, ("name", "description")),
    ]:
        for row in session.scalars(select(model).where(model.book_id == book_id)):
            for field in fields:
                old_text_parts.append(str(getattr(row, field, "") or ""))
    chapters = list(session.scalars(select(Chapter).where(Chapter.book_id == book_id)))
    chapter_ids = [chapter.id for chapter in chapters]
    old_text_parts.extend([f"{chapter.title}\n{chapter.summary}" for chapter in chapters])
    if chapter_ids:
        for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id.in_(chapter_ids))):
            old_text_parts.extend([brief.goal, brief.required_beats, brief.constraints])
        for version in session.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id.in_(chapter_ids))):
            old_text_parts.extend([version.title, version.content[:3000]])
    for embedding in session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id)):
        old_text_parts.append(embedding.text)
    for adjustment in session.scalars(select(FeedbackAdjustment).where(FeedbackAdjustment.book_id == book_id)):
        old_text_parts.append(adjustment.adjustment_text)
    for feedback in session.scalars(select(PlatformFeedback).where(PlatformFeedback.book_id == book_id)):
        old_text_parts.extend([feedback.metric_value, feedback.raw_text])
    old_text = "\n".join(old_text_parts)
    for term in re.findall(r"《[^》]{2,20}》", old_text):
        if term not in positive_text:
            terms.add(term)
            terms.add(term.strip("《》"))
    for term in re.findall(r"[\u4e00-\u9fff]{0,8}旧[\u4e00-\u9fff]{1,10}", old_text):
        if 2 <= len(term) <= 14 and term not in positive_text:
            terms.add(term)
    return sorted(terms, key=len, reverse=True)


def _sanitize_deprecated_terms(skeleton: dict[str, str], deprecated_terms: list[str]) -> dict[str, str]:
    if not deprecated_terms:
        return {key: str(value or "").strip() for key, value in skeleton.items()}
    sanitized: dict[str, str] = {}
    for key, value in skeleton.items():
        text = str(value or "").strip()
        for term in deprecated_terms:
            if not term:
                continue
            text = text.replace(term, _abstract_deprecated_label(term))
        sanitized[key] = text
    return sanitized


def _sanitize_persisted_skeleton_sources(
    session: Session,
    *,
    book_id: int,
    sanitized_skeleton: dict[str, str],
    deprecated_terms: list[str],
) -> None:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    if foundation:
        for field in ("premise", "reader_promise", "world_engine", "protagonist_engine", "conflict_engine"):
            source = str(sanitized_skeleton.get(field) or getattr(foundation, field, "") or "")
            setattr(foundation, field, _replace_deprecated_terms(source, deprecated_terms).strip())
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    if bible:
        field_sources = {
            "positioning": "premise",
            "reader_promise": "reader_promise",
            "main_plot": "conflict_engine",
            "protagonist_arc": "protagonist_engine",
            "power_curve": "world_engine",
            "forbidden_rules": "forbidden_rules",
        }
        for field, source_key in field_sources.items():
            source = str(sanitized_skeleton.get(source_key) or getattr(bible, field, "") or "")
            if field == "forbidden_rules":
                from app.services.story_dna import strip_story_dna_blocks

                source = strip_story_dna_blocks(source)
            setattr(bible, field, _replace_deprecated_terms(source, deprecated_terms).strip())
        for field in ("relationship_arc", "style_guide"):
            setattr(bible, field, _replace_deprecated_terms(str(getattr(bible, field, "") or ""), deprecated_terms).strip())
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    if volume:
        if str(sanitized_skeleton.get("volume_title") or "").strip():
            volume.title = _replace_deprecated_terms(str(sanitized_skeleton.get("volume_title") or ""), deprecated_terms).strip()
        if str(sanitized_skeleton.get("volume_summary") or "").strip():
            volume.summary = _replace_deprecated_terms(str(sanitized_skeleton.get("volume_summary") or ""), deprecated_terms).strip()
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    if arc:
        arc_updates = {
            "title": "arc_title",
            "goal": "arc_goal",
            "climax": "arc_climax",
            "turn": "arc_turn",
        }
        for field, source_key in arc_updates.items():
            source = str(sanitized_skeleton.get(source_key) or getattr(arc, field, "") or "")
            setattr(arc, field, _replace_deprecated_terms(source, deprecated_terms).strip())


def _replace_deprecated_terms(text: str, deprecated_terms: list[str]) -> str:
    updated = text
    for term in deprecated_terms:
        updated = updated.replace(term, _abstract_deprecated_label(term))
    return updated


def _abstract_deprecated_label(term: str) -> str:
    if term.startswith("《") or term.endswith("志"):
        return "书中游戏"
    if "旧" in term:
        return "桥段事件"
    return "主角"
