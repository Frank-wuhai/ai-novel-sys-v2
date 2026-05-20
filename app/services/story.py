from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, StoryArc, StoryBible, Volume


def upsert_story_bible(
    session: Session,
    *,
    book_id: int,
    positioning: str = "",
    reader_promise: str = "",
    main_plot: str = "",
    protagonist_arc: str = "",
    relationship_arc: str = "",
    power_curve: str = "",
    forbidden_rules: str = "",
    style_guide: str = "",
    status: str = "draft",
) -> StoryBible:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    if not bible:
        bible = StoryBible(book_id=book_id)
        session.add(bible)
    bible.positioning = positioning or bible.positioning
    bible.reader_promise = reader_promise or bible.reader_promise
    bible.main_plot = main_plot or bible.main_plot
    bible.protagonist_arc = protagonist_arc or bible.protagonist_arc
    bible.relationship_arc = relationship_arc or bible.relationship_arc
    bible.power_curve = power_curve or bible.power_curve
    bible.forbidden_rules = forbidden_rules or bible.forbidden_rules
    bible.style_guide = style_guide or bible.style_guide
    bible.status = status
    session.flush()
    return bible


def get_story_bible(session: Session, *, book_id: int) -> StoryBible | None:
    return session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))


def create_volume(
    session: Session,
    *,
    book_id: int,
    volume_number: int,
    title: str,
    summary: str = "",
    status: str = "planning",
) -> Volume:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    existing = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == volume_number))
    if existing:
        existing.title = title or existing.title
        existing.summary = summary or existing.summary
        existing.status = status
        session.flush()
        return existing
    volume = Volume(book_id=book_id, volume_number=volume_number, title=title, summary=summary, status=status)
    session.add(volume)
    session.flush()
    return volume


def list_volumes(session: Session, *, book_id: int) -> list[Volume]:
    return list(session.scalars(select(Volume).where(Volume.book_id == book_id).order_by(Volume.volume_number)))


def create_story_arc(
    session: Session,
    *,
    book_id: int,
    arc_number: int,
    title: str,
    start_chapter: int,
    end_chapter: int,
    goal: str = "",
    climax: str = "",
    turn: str = "",
    volume_number: int | None = None,
    status: str = "planning",
) -> StoryArc:
    if start_chapter < 1 or end_chapter < start_chapter:
        raise ValueError("chapter range must be valid")
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    volume_id = None
    if volume_number is not None:
        volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == volume_number))
        if not volume:
            raise ValueError(f"volume not found: {volume_number}")
        volume_id = volume.id
    existing = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == arc_number))
    if existing:
        existing.volume_id = volume_id
        existing.title = title or existing.title
        existing.start_chapter = start_chapter
        existing.end_chapter = end_chapter
        existing.goal = goal or existing.goal
        existing.climax = climax or existing.climax
        existing.turn = turn or existing.turn
        existing.status = status
        session.flush()
        return existing
    arc = StoryArc(
        book_id=book_id,
        volume_id=volume_id,
        arc_number=arc_number,
        title=title,
        start_chapter=start_chapter,
        end_chapter=end_chapter,
        goal=goal,
        climax=climax,
        turn=turn,
        status=status,
    )
    session.add(arc)
    session.flush()
    return arc


def list_story_arcs(session: Session, *, book_id: int) -> list[StoryArc]:
    return list(session.scalars(select(StoryArc).where(StoryArc.book_id == book_id).order_by(StoryArc.arc_number)))


def arcs_for_chapter(session: Session, *, book_id: int, chapter_number: int, limit: int = 3) -> list[StoryArc]:
    return list(
        session.scalars(
            select(StoryArc)
            .where(
                StoryArc.book_id == book_id,
                StoryArc.start_chapter <= chapter_number,
                StoryArc.end_chapter >= chapter_number,
            )
            .order_by(StoryArc.arc_number)
            .limit(limit)
        )
    )


def format_story_control_context(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
) -> tuple[str, dict[str, list[int]]]:
    refs: dict[str, list[int]] = {"story_bible_ids": [], "story_arc_ids": []}
    sections: list[str] = []
    bible = get_story_bible(session, book_id=book_id)
    if bible:
        refs["story_bible_ids"].append(bible.id)
        lines = _non_empty_lines(
            [
                ("作品定位", bible.positioning),
                ("读者承诺", bible.reader_promise),
                ("主线", bible.main_plot),
                ("主角弧光", bible.protagonist_arc),
                ("关系线", bible.relationship_arc),
                ("能力曲线", bible.power_curve),
                ("禁区规则", bible.forbidden_rules),
                ("文风指南", bible.style_guide),
            ]
        )
        if lines:
            sections.append("创作圣经：\n" + "\n".join(lines))

    arcs = arcs_for_chapter(session, book_id=book_id, chapter_number=chapter_number) if chapter_number else list_story_arcs(session, book_id=book_id)
    if arcs:
        refs["story_arc_ids"] = [arc.id for arc in arcs]
        sections.append(
            "剧情段：\n"
            + "\n".join(
                f"- arc#{arc.id} 第{arc.start_chapter}-{arc.end_chapter}章 {arc.title}｜目标：{arc.goal}｜高潮：{arc.climax}｜转折：{arc.turn}"
                for arc in arcs
            )
        )

    if not sections:
        return "未登记 Story Bible/Arc；只能依赖已登记 Canon。", refs
    return "\n\n".join(sections), refs


def format_outline(session: Session, *, book_id: int) -> str:
    volumes = list_volumes(session, book_id=book_id)
    arcs = list_story_arcs(session, book_id=book_id)
    if not volumes and not arcs:
        return "未登记分卷或剧情段。"
    lines: list[str] = []
    for volume in volumes:
        lines.append(f"volume#{volume.id} 第{volume.volume_number}卷 {volume.title} status={volume.status}")
        if volume.summary:
            lines.append(f"  summary={volume.summary}")
        for arc in [item for item in arcs if item.volume_id == volume.id]:
            lines.append(_format_arc_line(arc, prefix="  "))
    for arc in [item for item in arcs if item.volume_id is None]:
        lines.append(_format_arc_line(arc))
    return "\n".join(lines)


def _format_arc_line(arc: StoryArc, *, prefix: str = "") -> str:
    return (
        f"{prefix}arc#{arc.id} arc_number={arc.arc_number} chapters={arc.start_chapter}-{arc.end_chapter} "
        f"title={arc.title} status={arc.status} goal={arc.goal}"
    )


def _non_empty_lines(items: list[tuple[str, str]]) -> list[str]:
    return [f"- {label}: {value}" for label, value in items if value]
