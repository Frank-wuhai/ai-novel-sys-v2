from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import ArkOpenAIProvider
from app.models.entities import Book, Character, EvidenceSource, PowerSystem, StoryArc, StoryBible, StoryFoundation, WorldRule
from app.services.evidence import list_market_signals
from app.services.planning import AUTO_ACTIONS, build_human_decision_package, plan_chapters


@dataclass(frozen=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ProductionReadinessReport:
    passed: bool
    checks: list[ReadinessCheck]


def check_production_readiness(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    live_llm: bool = False,
) -> ProductionReadinessReport:
    checks = [
        _foundation_check(session, book_id),
        _story_bible_check(session, book_id, start, count),
        _evidence_check(session, book_id),
        _canon_check(session, book_id),
        _chapter_queue_check(session, book_id, start, count),
        _human_decision_check(session, book_id, start, count),
        _llm_config_check(live_llm=live_llm),
    ]
    return ProductionReadinessReport(passed=all(check.passed for check in checks), checks=checks)


def _foundation_check(session: Session, book_id: int) -> ReadinessCheck:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    if not foundation:
        return ReadinessCheck("foundation", False, "missing story foundation")
    missing = []
    if not foundation.premise:
        missing.append("premise")
    if not foundation.reader_promise:
        missing.append("reader_promise")
    if missing:
        return ReadinessCheck("foundation", False, "missing fields: " + ",".join(missing))
    return ReadinessCheck("foundation", True, f"foundation_id={foundation.id}")


def _story_bible_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    if not bible:
        return ReadinessCheck("story_bible", False, "missing story bible")
    missing = []
    if not bible.positioning:
        missing.append("positioning")
    if not bible.reader_promise:
        missing.append("reader_promise")
    if not bible.main_plot:
        missing.append("main_plot")
    if not bible.forbidden_rules:
        missing.append("forbidden_rules")
    if missing:
        return ReadinessCheck("story_bible", False, "missing fields: " + ",".join(missing))
    end = start + count - 1
    arc_count = session.scalar(
        select(func.count(StoryArc.id)).where(
            StoryArc.book_id == book_id,
            StoryArc.start_chapter <= end,
            StoryArc.end_chapter >= start,
        )
    )
    if not arc_count:
        return ReadinessCheck("story_bible", False, f"no story arcs covering chapters {start}-{end}")
    return ReadinessCheck("story_bible", True, f"story_bible_id={bible.id} covering_arcs={arc_count}")


def _evidence_check(session: Session, book_id: int) -> ReadinessCheck:
    book = session.get(Book, book_id)
    if not book:
        return ReadinessCheck("evidence", False, f"book not found: {book_id}")
    if not book.genre:
        return ReadinessCheck("evidence", False, "book genre is required for market evidence matching")
    signals = list_market_signals(session, genre=book.genre, usable_only=True, min_confidence=60)
    verified_sources = session.scalar(
        select(func.count(EvidenceSource.id)).where(EvidenceSource.status == "verified", EvidenceSource.reliability >= 3)
    )
    if not signals:
        return ReadinessCheck("evidence", False, f"no usable market signals for genre={book.genre}")
    return ReadinessCheck(
        "evidence",
        True,
        f"genre={book.genre} usable_market_signals={len(signals)} verified_sources={verified_sources}",
    )


def _canon_check(session: Session, book_id: int) -> ReadinessCheck:
    character_count = session.scalar(select(func.count(Character.id)).where(Character.book_id == book_id)) or 0
    world_rule_count = session.scalar(select(func.count(WorldRule.id)).where(WorldRule.book_id == book_id, WorldRule.status == "active")) or 0
    power_count = session.scalar(
        select(func.count(PowerSystem.id)).where(PowerSystem.book_id == book_id, PowerSystem.status.in_(["active", "locked"]))
    ) or 0
    missing = []
    if character_count < 1:
        missing.append("character")
    if world_rule_count < 1:
        missing.append("world_rule")
    if power_count < 1:
        missing.append("power_system")
    if missing:
        return ReadinessCheck("canon", False, "missing canon: " + ",".join(missing))
    return ReadinessCheck("canon", True, f"characters={character_count} world_rules={world_rule_count} power_systems={power_count}")


def _chapter_queue_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    items = plan_chapters(session, book_id=book_id, start=start, count=count)
    runnable = [item for item in items if item.next_action in AUTO_ACTIONS]
    waiting = [item for item in items if item.next_action in {"record_chapter_continuity", "approve_chapter", "mark_publish_job"}]
    done = [item for item in items if item.next_action == "done"]
    if not runnable and not waiting:
        return ReadinessCheck("chapter_queue", False, "no runnable or human-waiting chapters in range")
    return ReadinessCheck("chapter_queue", True, f"auto_ready={len(runnable)} human_waiting={len(waiting)} done={len(done)}")


def _human_decision_check(session: Session, book_id: int, start: int, count: int) -> ReadinessCheck:
    package = build_human_decision_package(session, book_id=book_id, start=start, count=count)
    if package.inspect_count:
        return ReadinessCheck("human_decisions", False, f"manual inspection required={package.inspect_count}")
    return ReadinessCheck(
        "human_decisions",
        True,
        f"continuity={package.continuity_count} approval={package.approval_count} publish={package.publish_count}",
    )


def _llm_config_check(*, live_llm: bool) -> ReadinessCheck:
    missing = []
    if not settings.ark_api_key:
        missing.append("ARK_API_KEY")
    if not settings.ark_base_url:
        missing.append("ARK_BASE_URL")
    if not settings.model_name:
        missing.append("MODEL_NAME")
    if missing:
        return ReadinessCheck("llm", False, "missing config: " + ",".join(missing))
    if not live_llm:
        return ReadinessCheck("llm", True, f"configured model={settings.model_name} live_check=skipped")
    try:
        response = ArkOpenAIProvider().generate('只回复 JSON: {"ok": true}', max_tokens=20)
    except Exception as exc:
        return ReadinessCheck("llm", False, f"live_check_failed={type(exc).__name__}: {exc}")
    ok = '"ok"' in response.text or "ok" in response.text.lower()
    return ReadinessCheck("llm", ok, f"live_check_model={response.model} text={response.text[:80]}")
