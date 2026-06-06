from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Character, CharacterState, Chapter, ChapterVersion, Foreshadow, PlotThread


@dataclass(frozen=True)
class ContinuityResult:
    chapter_id: int
    character_state_ids: list[int]
    new_foreshadow_ids: list[int]
    paid_foreshadow_ids: list[int]
    updated_plot_thread_ids: list[int]


def record_chapter_continuity(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    summary: str,
    character_states: list[tuple[int, str]] | None = None,
    new_foreshadows: list[str] | None = None,
    payoffs: list[tuple[int, str]] | None = None,
    plot_thread_updates: list[tuple[int, str]] | None = None,
) -> ContinuityResult:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not latest:
        raise ValueError("chapter version not found")
    if latest.status not in {"reviewed_pass", "approved"}:
        raise ValueError("continuity can only be recorded after chapter quality pass or approval")

    chapter.summary = summary
    chapter.status = "continuity_recorded"

    character_state_ids: list[int] = []
    for character_id, state_text in character_states or []:
        character = session.get(Character, character_id)
        if not character or character.book_id != book_id:
            raise ValueError(f"character does not belong to book: {character_id}")
        state = CharacterState(
            character_id=character_id,
            chapter_id=chapter.id,
            state_text=state_text,
            source="continuity",
        )
        session.add(state)
        session.flush()
        character_state_ids.append(state.id)

    new_foreshadow_ids: list[int] = []
    for setup_text in new_foreshadows or []:
        foreshadow = Foreshadow(book_id=book_id, setup_text=setup_text, status="open")
        session.add(foreshadow)
        session.flush()
        new_foreshadow_ids.append(foreshadow.id)

    paid_foreshadow_ids: list[int] = []
    for foreshadow_id, payoff_text in payoffs or []:
        foreshadow = session.get(Foreshadow, foreshadow_id)
        if not foreshadow or foreshadow.book_id != book_id:
            raise ValueError(f"foreshadow does not belong to book: {foreshadow_id}")
        foreshadow.payoff_text = payoff_text
        foreshadow.status = "paid_off"
        paid_foreshadow_ids.append(foreshadow.id)

    updated_plot_thread_ids: list[int] = []
    for thread_id, status in plot_thread_updates or []:
        thread = session.get(PlotThread, thread_id)
        if not thread or thread.book_id != book_id:
            raise ValueError(f"plot thread does not belong to book: {thread_id}")
        thread.status = status
        updated_plot_thread_ids.append(thread.id)

    session.flush()
    return ContinuityResult(
        chapter_id=chapter.id,
        character_state_ids=character_state_ids,
        new_foreshadow_ids=new_foreshadow_ids,
        paid_foreshadow_ids=paid_foreshadow_ids,
        updated_plot_thread_ids=updated_plot_thread_ids,
    )


def latest_version_for_chapter(session: Session, *, book_id: int, chapter_number: int) -> ChapterVersion:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    return version


def default_chapter_continuity_summary(session: Session, *, book_id: int, chapter_number: int) -> str:
    version = latest_version_for_chapter(session, book_id=book_id, chapter_number=chapter_number)
    excerpt = " ".join(version.content.split())[:160]
    return f"第{chapter_number}章已通过质检，最新版本《{version.title}》进入连续性记录。{excerpt}"
