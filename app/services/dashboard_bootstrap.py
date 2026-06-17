from __future__ import annotations

from sqlalchemy import select

from app.models.entities import Character, EvidenceSource, MarketSignal, PlotThread, PowerSystem, StoryArc, Volume, WorldRule
from app.services.aesthetic_profile import merge_style_with_aesthetic_profile, story_bible_display_fields
from app.services.canon import add_character, add_plot_thread, add_power_system, add_world_rule
from app.services.evidence import add_evidence_source, add_market_signal
from app.services.planning import create_chapter_plan, upgrade_chapter_briefs_production_standards
from app.services.production import seed_prompts
from app.services.story import create_story_arc, create_volume, get_story_bible, upsert_story_bible
from app.services.story_dna import story_dna_display_fields


def bootstrap_book_production(
    session,
    *,
    book_id: int,
    title: str,
    genre: str,
    premise: str,
    reader_promise: str,
    world_engine: str,
    protagonist_engine: str,
    conflict_engine: str,
    only_missing: bool = False,
) -> dict:
    seed_prompts(session)
    source, source_created = _ensure_dashboard_evidence_source(session, book_id=book_id, title=title, only_missing=only_missing)
    signal, signal_created = _ensure_dashboard_market_signal(session, source=source, genre=genre, only_missing=only_missing)
    bible, bible_created = _ensure_dashboard_story_bible(
        session,
        book_id=book_id,
        premise=premise,
        reader_promise=reader_promise,
        world_engine=world_engine,
        protagonist_engine=protagonist_engine,
        conflict_engine=conflict_engine,
        only_missing=only_missing,
    )
    protagonist, protagonist_created = _ensure_dashboard_character(
        session,
        book_id=book_id,
        premise=premise,
        protagonist_engine=protagonist_engine,
        only_missing=only_missing,
    )
    world_rule, world_rule_created = _ensure_dashboard_world_rule(
        session,
        book_id=book_id,
        world_engine=world_engine,
        only_missing=only_missing,
    )
    power, power_created = _ensure_dashboard_power_system(session, book_id=book_id, only_missing=only_missing)
    thread, thread_created = _ensure_dashboard_plot_thread(
        session,
        book_id=book_id,
        conflict_engine=conflict_engine,
        only_missing=only_missing,
    )
    volume, volume_created = _ensure_dashboard_volume(session, book_id=book_id, only_missing=only_missing)
    arc, arc_created = _ensure_dashboard_story_arc(session, book_id=book_id, only_missing=only_missing)
    briefs = create_chapter_plan(
        session,
        book_id=book_id,
        start=1,
        count=5,
        goal_prefix="开局破局",
        required_beats="具体压力,主角主动选择,能力收益,可见代价,信息增量,章末钩子",
        constraints=f"遵守读者承诺:{reader_promise}；不要写系统说明或作者解释；设定必须嵌入场景、动作、对话和后果。",
    )
    upgraded_briefs = upgrade_chapter_briefs_production_standards(session, book_id=book_id)
    return {
        "prompt_templates": {"status": "ensured"},
        "story_bible_id": _bootstrap_item(bible.id, bible_created),
        "evidence_source_id": _bootstrap_item(source.id, source_created),
        "market_signal_id": _bootstrap_item(signal.id, signal_created),
        "character_id": _bootstrap_item(protagonist.id, protagonist_created),
        "world_rule_id": _bootstrap_item(world_rule.id, world_rule_created),
        "power_system_id": _bootstrap_item(power.id, power_created),
        "plot_thread_id": _bootstrap_item(thread.id, thread_created),
        "volume_id": _bootstrap_item(volume.id, volume_created),
        "story_arc_id": _bootstrap_item(arc.id, arc_created),
        "chapter_briefs": {
            "created_count": len(briefs),
            "upgraded_count": upgraded_briefs,
            "status": "created" if briefs else ("upgraded" if upgraded_briefs else "existing"),
        },
    }


def _ensure_dashboard_evidence_source(session, *, book_id: int, title: str, only_missing: bool) -> tuple[EvidenceSource, bool]:
    source_id = f"dashboard-bootstrap-{book_id}"
    existing = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == source_id))
    if existing and only_missing:
        return existing, False
    source = add_evidence_source(
        session,
        source_id=source_id,
        title=f"{title} dashboard bootstrap evidence",
        url="",
        reliability=3,
        status="verified",
    )
    return source, existing is None


def _ensure_dashboard_market_signal(session, *, source: EvidenceSource, genre: str, only_missing: bool) -> tuple[MarketSignal, bool]:
    existing = session.scalar(
        select(MarketSignal).where(
            MarketSignal.source_id == source.id,
            MarketSignal.genre == genre,
        )
    )
    if existing and only_missing:
        return existing, False
    signal = add_market_signal(
        session,
        source_key=source.source_id,
        genre=genre,
        signal_text=f"{genre}连载首章需要快速进入具体压力，突出主角主动选择、能力代价、信息增量和章末钩子。",
        confidence=70,
    )
    return signal, True


def _ensure_dashboard_story_bible(
    session,
    *,
    book_id: int,
    premise: str,
    reader_promise: str,
    world_engine: str,
    protagonist_engine: str,
    conflict_engine: str,
    only_missing: bool,
):
    existing = get_story_bible(session, book_id=book_id)
    if existing and only_missing:
        return existing, False
    dna_display = story_dna_display_fields(style_guide=existing.style_guide if existing else "", forbidden_rules=existing.forbidden_rules if existing else "")
    display = story_bible_display_fields(style_guide=dna_display["style_guide"], forbidden_rules=dna_display["forbidden_rules"])
    bible = upsert_story_bible(
        session,
        book_id=book_id,
        positioning=premise,
        reader_promise=reader_promise,
        main_plot=conflict_engine or premise,
        protagonist_arc=protagonist_engine,
        power_curve=world_engine,
        forbidden_rules="避免系统提示词、作者说明、元叙事泄露到正文。",
        style_guide=merge_style_with_aesthetic_profile("番茄小说节奏：开篇快，冲突明确，章末留钩子。", display["aesthetic_profile"]),
        status="draft",
    )
    return bible, existing is None


def _ensure_dashboard_character(
    session,
    *,
    book_id: int,
    premise: str,
    protagonist_engine: str,
    only_missing: bool,
) -> tuple[Character, bool]:
    existing = session.scalar(select(Character).where(Character.book_id == book_id, Character.name == "主角"))
    if existing and only_missing:
        return existing, False
    character = add_character(
        session,
        book_id=book_id,
        name="主角",
        role="protagonist",
        personality=protagonist_engine,
        ability="核心能力必须带来收益、限制和可见代价。",
        background=premise,
    )
    return character, existing is None


def _ensure_dashboard_world_rule(session, *, book_id: int, world_engine: str, only_missing: bool) -> tuple[WorldRule, bool]:
    existing = session.scalar(select(WorldRule).where(WorldRule.book_id == book_id, WorldRule.category == "生产底线"))
    if existing and only_missing:
        return existing, False
    rule = add_world_rule(
        session,
        book_id=book_id,
        category="生产底线",
        rule_text=world_engine or "世界规则必须通过场景和后果呈现，不能用设定说明替代剧情。",
    )
    return rule, True


def _ensure_dashboard_power_system(session, *, book_id: int, only_missing: bool) -> tuple[PowerSystem, bool]:
    existing = session.scalar(select(PowerSystem).where(PowerSystem.book_id == book_id, PowerSystem.name == "核心能力"))
    if existing and only_missing:
        return existing, False
    power = add_power_system(
        session,
        book_id=book_id,
        name="核心能力",
        rules="能力只能推动短期选择和局部破局，不能无条件解决所有问题。",
        costs="每次使用都必须付出资源、身体、关系、信息或处境上的可见代价。",
        limits="不能跳过冲突、不能直接获得最终答案、不能推翻已登记 Canon。",
    )
    return power, existing is None


def _ensure_dashboard_plot_thread(session, *, book_id: int, conflict_engine: str, only_missing: bool):
    existing = session.scalar(select(PlotThread).where(PlotThread.book_id == book_id, PlotThread.name == "主线压力"))
    if existing and only_missing:
        return existing, False
    thread = add_plot_thread(
        session,
        book_id=book_id,
        name="主线压力",
        description=conflict_engine or "主角在身边危机和更高层压力中不断夺回主动权。",
    )
    return thread, True


def _ensure_dashboard_volume(session, *, book_id: int, only_missing: bool) -> tuple[Volume, bool]:
    existing = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    if existing and only_missing:
        return existing, False
    volume = create_volume(
        session,
        book_id=book_id,
        volume_number=1,
        title="第一卷",
        summary="建立主角处境、核心能力代价、第一轮外部压力和持续追读钩子。",
    )
    return volume, existing is None


def _ensure_dashboard_story_arc(session, *, book_id: int, only_missing: bool) -> tuple[StoryArc, bool]:
    existing = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    if existing and only_missing:
        return existing, False
    arc = create_story_arc(
        session,
        book_id=book_id,
        arc_number=1,
        title="开局破局",
        start_chapter=1,
        end_chapter=5,
        goal="让主角在具体危机中发现能力、付出代价，并主动踏入更大的主线压力。",
        climax="主角用能力赢下一次局部胜利，但暴露更大危险或更高层关注。",
        turn="主角意识到眼前事件不是偶然，必须主动追查或反击。",
        volume_number=1,
    )
    return arc, existing is None


def _bootstrap_item(item_id: int, created: bool) -> dict:
    return {"id": item_id, "status": "created" if created else "existing"}
