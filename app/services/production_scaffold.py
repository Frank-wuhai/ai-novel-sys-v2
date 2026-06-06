from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Character, EvidenceSource, MarketSignal, PlotThread, PowerSystem, StoryArc, StoryFoundation, Volume, WorldRule
from app.services.canon import add_character, add_plot_thread, add_power_system, add_world_rule
from app.services.evidence import add_evidence_source, add_market_signal
from app.services.feedback import record_platform_feedback
from app.services.planning import create_chapter_plan, upgrade_chapter_briefs_production_standards
from app.services.production import seed_prompts
from app.services.story import create_story_arc, create_volume, get_story_bible, upsert_story_bible


SCAFFOLD_APPROVAL_FIELDS = [
    "premise",
    "reader_promise",
    "world_engine",
    "protagonist_engine",
    "conflict_engine",
    "arc_goal",
    "arc_climax",
    "arc_turn",
]


def repair_production_scaffold(
    session: Session,
    *,
    book_id: int,
    only_missing: bool = True,
    approve_skeleton: bool = True,
    chapter_count: int = 5,
    apply: bool = False,
) -> dict[str, Any]:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    source = _scaffold_source(session, book=book)
    preview = preview_production_scaffold_repair(
        session,
        book_id=book_id,
        only_missing=only_missing,
        approve_skeleton=approve_skeleton,
        chapter_count=chapter_count,
    )
    if not apply:
        return preview
    seed_prompts(session)

    evidence_source, evidence_source_created = _ensure_evidence_source(session, book=book, only_missing=only_missing)
    market_signal, market_signal_created = _ensure_market_signal(
        session,
        book=book,
        source=evidence_source,
        source_data=source,
        only_missing=only_missing,
    )
    bible, bible_created = _ensure_story_bible(session, book=book, source=source, only_missing=only_missing)
    character, character_created = _ensure_character(session, book=book, source=source, only_missing=only_missing)
    world_rule, world_rule_created = _ensure_world_rule(session, book=book, source=source, only_missing=only_missing)
    power, power_created = _ensure_power_system(session, book=book, only_missing=only_missing)
    thread, thread_created = _ensure_plot_thread(session, book=book, source=source, only_missing=only_missing)
    volume, volume_created = _ensure_volume(session, book=book, source=source, only_missing=only_missing)
    arc, arc_created = _ensure_story_arc(session, book=book, source=source, only_missing=only_missing)
    briefs = create_chapter_plan(
        session,
        book_id=book_id,
        start=1,
        count=max(1, chapter_count),
        goal_prefix="开局破局",
        required_beats="具体压力,主角主动选择,能力收益,可见代价,信息增量,章末钩子",
        constraints=f"遵守读者承诺:{source['reader_promise']}；不要写系统说明或作者解释；设定必须嵌入场景、动作、对话和后果。",
    )
    upgraded_briefs = upgrade_chapter_briefs_production_standards(session, book_id=book_id)
    approved_count = _approve_scaffold(session, book_id=book_id, source=source) if approve_skeleton else 0
    session.flush()
    return {
        "book_id": book_id,
        "mode": "applied",
        "source": source,
        "items": {
            "evidence_source": _item(evidence_source.id, evidence_source_created),
            "market_signal": _item(market_signal.id, market_signal_created),
            "story_bible": _item(bible.id, bible_created),
            "character": _item(character.id, character_created),
            "world_rule": _item(world_rule.id, world_rule_created),
            "power_system": _item(power.id, power_created),
            "plot_thread": _item(thread.id, thread_created),
            "volume": _item(volume.id, volume_created),
            "story_arc": _item(arc.id, arc_created),
            "chapter_briefs": {
                "status": "created" if briefs else ("upgraded" if upgraded_briefs else "existing"),
                "created_count": len(briefs),
                "upgraded_count": upgraded_briefs,
            },
            "skeleton_approval": {"status": "recorded" if approved_count else "skipped", "approved_count": approved_count},
        },
        "created_count": sum(1 for item in [
            evidence_source_created,
            market_signal_created,
            bible_created,
            character_created,
            world_rule_created,
            power_created,
            thread_created,
            volume_created,
            arc_created,
        ] if item),
        "upgraded_count": upgraded_briefs,
    }


def preview_production_scaffold_repair(
    session: Session,
    *,
    book_id: int,
    only_missing: bool = True,
    approve_skeleton: bool = True,
    chapter_count: int = 5,
) -> dict[str, Any]:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    source = _scaffold_source(session, book=book)
    items = {
        "prompt_templates": _preview_item(False, "ensure prompt templates exist"),
        "evidence_source": _preview_item(
            _missing_evidence_source(session, book=book) or not only_missing,
            f"source_id=dna-scaffold-{book.id}",
        ),
        "market_signal": _preview_item(
            _missing_market_signal(session, book=book) or not only_missing,
            f"genre={book.genre or '玄幻脑洞'}",
        ),
        "story_bible": _preview_item(get_story_bible(session, book_id=book.id) is None or not only_missing, "Story Bible baseline"),
        "character": _preview_item(_missing_character(session, book=book) or not only_missing, "主角"),
        "world_rule": _preview_item(_missing_world_rule(session, book=book) or not only_missing, "生产底线"),
        "power_system": _preview_item(_missing_power_system(session, book=book) or not only_missing, "核心能力"),
        "plot_thread": _preview_item(_missing_plot_thread(session, book=book) or not only_missing, "主线压力"),
        "volume": _preview_item(_missing_volume(session, book=book) or not only_missing, "第一卷"),
        "story_arc": _preview_item(_missing_story_arc(session, book=book) or not only_missing, "开局破局"),
        "chapter_briefs": _preview_item(True, f"ensure first {max(1, chapter_count)} chapter briefs and production standards"),
        "skeleton_approval": _preview_item(approve_skeleton, f"record {len(SCAFFOLD_APPROVAL_FIELDS)} approval fields"),
    }
    planned_count = sum(1 for item in items.values() if item["planned"])
    return {
        "book_id": book_id,
        "mode": "preview",
        "source": source,
        "items": items,
        "planned_count": planned_count,
        "apply_hint": f"repair-production-scaffold --book-id {book_id} --apply",
    }


def _scaffold_source(session: Session, *, book: Book) -> dict[str, str]:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book.id).order_by(StoryFoundation.id.desc()))
    bible = get_story_bible(session, book_id=book.id)
    premise = (foundation.premise if foundation else (bible.positioning if bible else "")).strip() or f"{book.title}：主角在具体危机中获得改变命运的机会。"
    reader_promise = (foundation.reader_promise if foundation else (bible.reader_promise if bible else "")).strip() or "开篇快、冲突强、主角主动破局、每章有可见代价和追读钩子。"
    world_engine = (foundation.world_engine if foundation else (bible.power_curve if bible else "")).strip() or "世界规则必须通过场景、选择、代价和后果呈现。"
    protagonist_engine = (foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else "")).strip() or "主角在压力下主动选择，靠能力收益和代价逐步夺回主动权。"
    conflict_engine = (foundation.conflict_engine if foundation else (bible.main_plot if bible else "")).strip() or "外部压力逐章升级，主角的每次破局都会引出更大的风险。"
    return {
        "premise": premise,
        "reader_promise": reader_promise,
        "world_engine": world_engine,
        "protagonist_engine": protagonist_engine,
        "conflict_engine": conflict_engine,
        "forbidden_rules": "避免系统提示词、作者说明、元叙事泄露到正文；不得绕过已登记 Canon。",
        "style_guide": "番茄小说节奏：开篇快，冲突明确，小单元连续推进，章末留钩子。",
        "volume_summary": "建立主角处境、核心能力代价、第一轮外部压力和持续追读钩子。",
        "arc_goal": "让主角在具体危机中发现能力、付出代价，并主动踏入更大的主线压力。",
        "arc_climax": "主角用能力赢下一次局部胜利，但暴露更大危险或更高层关注。",
        "arc_turn": "主角意识到眼前事件不是偶然，必须主动追查或反击。",
    }


def _missing_evidence_source(session: Session, *, book: Book) -> bool:
    return session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == f"dna-scaffold-{book.id}")) is None


def _missing_market_signal(session: Session, *, book: Book) -> bool:
    source = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == f"dna-scaffold-{book.id}"))
    if not source:
        return True
    genre = book.genre or "玄幻脑洞"
    return session.scalar(select(MarketSignal).where(MarketSignal.source_id == source.id, MarketSignal.genre == genre)) is None


def _missing_character(session: Session, *, book: Book) -> bool:
    return session.scalar(select(Character).where(Character.book_id == book.id, Character.name == "主角")) is None


def _missing_world_rule(session: Session, *, book: Book) -> bool:
    return session.scalar(select(WorldRule).where(WorldRule.book_id == book.id, WorldRule.category == "生产底线")) is None


def _missing_power_system(session: Session, *, book: Book) -> bool:
    return session.scalar(select(PowerSystem).where(PowerSystem.book_id == book.id, PowerSystem.name == "核心能力")) is None


def _missing_plot_thread(session: Session, *, book: Book) -> bool:
    return session.scalar(select(PlotThread).where(PlotThread.book_id == book.id, PlotThread.name == "主线压力")) is None


def _missing_volume(session: Session, *, book: Book) -> bool:
    return session.scalar(select(Volume).where(Volume.book_id == book.id, Volume.volume_number == 1)) is None


def _missing_story_arc(session: Session, *, book: Book) -> bool:
    return session.scalar(select(StoryArc).where(StoryArc.book_id == book.id, StoryArc.arc_number == 1)) is None


def _ensure_evidence_source(session: Session, *, book: Book, only_missing: bool) -> tuple[EvidenceSource, bool]:
    source_id = f"dna-scaffold-{book.id}"
    existing = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == source_id))
    if existing and only_missing:
        return existing, False
    source = add_evidence_source(
        session,
        source_id=source_id,
        title=f"{book.title} DNA scaffold baseline evidence",
        reliability=3,
        status="verified",
    )
    return source, existing is None


def _ensure_market_signal(session: Session, *, book: Book, source: EvidenceSource, source_data: dict[str, str], only_missing: bool) -> tuple[MarketSignal, bool]:
    genre = book.genre or "玄幻脑洞"
    existing = session.scalar(select(MarketSignal).where(MarketSignal.source_id == source.id, MarketSignal.genre == genre))
    if existing and only_missing:
        return existing, False
    signal = add_market_signal(
        session,
        source_key=source.source_id,
        genre=genre,
        signal_text=f"{genre}开局需要快速进入具体压力，突出{source_data['reader_promise']}，并让章末钩子由本章行动引发。",
        confidence=70,
    )
    return signal, existing is None


def _ensure_story_bible(session: Session, *, book: Book, source: dict[str, str], only_missing: bool):
    existing = get_story_bible(session, book_id=book.id)
    if existing and only_missing:
        return existing, False
    bible = upsert_story_bible(
        session,
        book_id=book.id,
        positioning=source["premise"],
        reader_promise=source["reader_promise"],
        main_plot=source["conflict_engine"],
        protagonist_arc=source["protagonist_engine"],
        power_curve=source["world_engine"],
        forbidden_rules=source["forbidden_rules"],
        style_guide=source["style_guide"],
        status="draft",
    )
    return bible, existing is None


def _ensure_character(session: Session, *, book: Book, source: dict[str, str], only_missing: bool) -> tuple[Character, bool]:
    existing = session.scalar(select(Character).where(Character.book_id == book.id, Character.name == "主角"))
    if existing and only_missing:
        return existing, False
    character = add_character(
        session,
        book_id=book.id,
        name="主角",
        role="protagonist",
        personality=source["protagonist_engine"],
        ability="核心能力必须带来收益、限制和可见代价。",
        background=source["premise"],
    )
    return character, existing is None


def _ensure_world_rule(session: Session, *, book: Book, source: dict[str, str], only_missing: bool) -> tuple[WorldRule, bool]:
    existing = session.scalar(select(WorldRule).where(WorldRule.book_id == book.id, WorldRule.category == "生产底线"))
    if existing and only_missing:
        return existing, False
    rule = add_world_rule(session, book_id=book.id, category="生产底线", rule_text=source["world_engine"])
    return rule, existing is None


def _ensure_power_system(session: Session, *, book: Book, only_missing: bool) -> tuple[PowerSystem, bool]:
    existing = session.scalar(select(PowerSystem).where(PowerSystem.book_id == book.id, PowerSystem.name == "核心能力"))
    if existing and only_missing:
        return existing, False
    power = add_power_system(
        session,
        book_id=book.id,
        name="核心能力",
        rules="能力只能推动短期选择和局部破局，不能无条件解决所有问题。",
        costs="每次使用都必须付出资源、身体、关系、信息或处境上的可见代价。",
        limits="不能跳过冲突、不能直接获得最终答案、不能推翻已登记 Canon。",
    )
    return power, existing is None


def _ensure_plot_thread(session: Session, *, book: Book, source: dict[str, str], only_missing: bool) -> tuple[PlotThread, bool]:
    existing = session.scalar(select(PlotThread).where(PlotThread.book_id == book.id, PlotThread.name == "主线压力"))
    if existing and only_missing:
        return existing, False
    thread = add_plot_thread(session, book_id=book.id, name="主线压力", description=source["conflict_engine"])
    return thread, existing is None


def _ensure_volume(session: Session, *, book: Book, source: dict[str, str], only_missing: bool):
    existing = session.scalar(select(Volume).where(Volume.book_id == book.id, Volume.volume_number == 1))
    if existing and only_missing:
        return existing, False
    volume = create_volume(session, book_id=book.id, volume_number=1, title="第一卷", summary=source["volume_summary"])
    return volume, existing is None


def _ensure_story_arc(session: Session, *, book: Book, source: dict[str, str], only_missing: bool):
    existing = session.scalar(select(StoryArc).where(StoryArc.book_id == book.id, StoryArc.arc_number == 1))
    if existing and only_missing:
        return existing, False
    arc = create_story_arc(
        session,
        book_id=book.id,
        arc_number=1,
        title="开局破局",
        start_chapter=1,
        end_chapter=5,
        goal=source["arc_goal"],
        climax=source["arc_climax"],
        turn=source["arc_turn"],
        volume_number=1,
    )
    return arc, existing is None


def _approve_scaffold(session: Session, *, book_id: int, source: dict[str, str]) -> int:
    approved = 0
    for key in SCAFFOLD_APPROVAL_FIELDS:
        value = source.get(key, "").strip()
        if not value:
            continue
        record_platform_feedback(
            session,
            book_id=book_id,
            platform="dna_scaffold",
            metric_name="skeleton_approval",
            metric_value=key,
            raw_text=value,
        )
        approved += 1
    return approved


def _item(item_id: int, created: bool) -> dict[str, Any]:
    return {"id": item_id, "status": "created" if created else "existing"}


def _preview_item(planned: bool, detail: str) -> dict[str, Any]:
    return {"planned": planned, "status": "would_change" if planned else "no_change", "detail": detail}
