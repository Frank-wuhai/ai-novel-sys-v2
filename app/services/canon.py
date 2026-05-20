from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Character,
    CharacterState,
    Chapter,
    Foreshadow,
    PlotThread,
    PowerSystem,
    WorldRule,
)
from app.services.story import format_story_control_context


def add_character(
    session: Session,
    *,
    book_id: int,
    name: str,
    role: str = "",
    personality: str = "",
    ability: str = "",
    background: str = "",
) -> Character:
    existing = session.scalar(select(Character).where(Character.book_id == book_id, Character.name == name))
    if existing:
        existing.role = role or existing.role
        existing.personality = personality or existing.personality
        existing.ability = ability or existing.ability
        existing.background = background or existing.background
        session.flush()
        return existing
    character = Character(
        book_id=book_id,
        name=name,
        role=role,
        personality=personality,
        ability=ability,
        background=background,
    )
    session.add(character)
    session.flush()
    return character


def add_character_state(
    session: Session,
    *,
    character_id: int,
    state_text: str,
    chapter_id: int | None = None,
    source: str = "manual",
) -> CharacterState:
    if not session.get(Character, character_id):
        raise ValueError(f"character not found: {character_id}")
    if chapter_id and not session.get(Chapter, chapter_id):
        raise ValueError(f"chapter not found: {chapter_id}")
    state = CharacterState(character_id=character_id, chapter_id=chapter_id, state_text=state_text, source=source)
    session.add(state)
    session.flush()
    return state


def add_world_rule(session: Session, *, book_id: int, category: str, rule_text: str, status: str = "active") -> WorldRule:
    rule = WorldRule(book_id=book_id, category=category, rule_text=rule_text, status=status)
    session.add(rule)
    session.flush()
    return rule


def add_power_system(
    session: Session,
    *,
    book_id: int,
    name: str,
    rules: str = "",
    costs: str = "",
    limits: str = "",
    status: str = "active",
) -> PowerSystem:
    existing = session.scalar(select(PowerSystem).where(PowerSystem.book_id == book_id, PowerSystem.name == name))
    if existing:
        existing.rules = rules or existing.rules
        existing.costs = costs or existing.costs
        existing.limits = limits or existing.limits
        existing.status = status
        session.flush()
        return existing
    power = PowerSystem(book_id=book_id, name=name, rules=rules, costs=costs, limits=limits, status=status)
    session.add(power)
    session.flush()
    return power


def add_plot_thread(session: Session, *, book_id: int, name: str, description: str = "", status: str = "open") -> PlotThread:
    thread = PlotThread(book_id=book_id, name=name, description=description, status=status)
    session.add(thread)
    session.flush()
    return thread


def add_foreshadow(
    session: Session,
    *,
    book_id: int,
    setup_text: str,
    payoff_text: str = "",
    status: str = "open",
) -> Foreshadow:
    foreshadow = Foreshadow(book_id=book_id, setup_text=setup_text, payoff_text=payoff_text, status=status)
    session.add(foreshadow)
    session.flush()
    return foreshadow


def format_canon_context(
    session: Session,
    *,
    book_id: int,
    limit: int = 8,
    chapter_number: int | None = None,
) -> tuple[str, dict[str, list[int]]]:
    refs: dict[str, list[int]] = {
        "story_bible_ids": [],
        "story_arc_ids": [],
        "character_ids": [],
        "character_state_ids": [],
        "world_rule_ids": [],
        "power_system_ids": [],
        "plot_thread_ids": [],
        "foreshadow_ids": [],
    }
    sections: list[str] = []
    story_context, story_refs = format_story_control_context(session, book_id=book_id, chapter_number=chapter_number)
    if "未登记 Story Bible/Arc" not in story_context:
        sections.append(story_context)
        refs["story_bible_ids"] = story_refs["story_bible_ids"]
        refs["story_arc_ids"] = story_refs["story_arc_ids"]

    characters = list(session.scalars(select(Character).where(Character.book_id == book_id).order_by(Character.id).limit(limit)))
    if characters:
        lines: list[str] = []
        for character in characters:
            refs["character_ids"].append(character.id)
            latest_state = session.scalar(
                select(CharacterState)
                .where(CharacterState.character_id == character.id)
                .order_by(CharacterState.id.desc())
            )
            state_text = ""
            if latest_state:
                refs["character_state_ids"].append(latest_state.id)
                state_text = f" 当前状态：{latest_state.state_text}"
            lines.append(
                f"- character#{character.id} {character.name}｜{character.role}｜性格：{character.personality}｜能力：{character.ability}{state_text}"
            )
        sections.append("人物：\n" + "\n".join(lines))

    rules = list(
        session.scalars(
            select(WorldRule)
            .where(WorldRule.book_id == book_id, WorldRule.status == "active")
            .order_by(WorldRule.id)
            .limit(limit)
        )
    )
    if rules:
        refs["world_rule_ids"] = [rule.id for rule in rules]
        sections.append("世界规则：\n" + "\n".join(f"- rule#{rule.id} [{rule.category}] {rule.rule_text}" for rule in rules))

    powers = list(
        session.scalars(
            select(PowerSystem)
            .where(PowerSystem.book_id == book_id, PowerSystem.status.in_(["active", "locked"]))
            .order_by(PowerSystem.id)
            .limit(limit)
        )
    )
    if powers:
        refs["power_system_ids"] = [power.id for power in powers]
        sections.append(
            "力量体系：\n"
            + "\n".join(
                f"- power#{power.id} {power.name}｜规则：{power.rules}｜代价：{power.costs}｜限制：{power.limits}" for power in powers
            )
        )

    threads = list(
        session.scalars(
            select(PlotThread)
            .where(PlotThread.book_id == book_id, PlotThread.status == "open")
            .order_by(PlotThread.id)
            .limit(limit)
        )
    )
    if threads:
        refs["plot_thread_ids"] = [thread.id for thread in threads]
        sections.append("剧情线：\n" + "\n".join(f"- thread#{thread.id} {thread.name}: {thread.description}" for thread in threads))

    foreshadows = list(
        session.scalars(
            select(Foreshadow)
            .where(Foreshadow.book_id == book_id, Foreshadow.status == "open")
            .order_by(Foreshadow.id)
            .limit(limit)
        )
    )
    if foreshadows:
        refs["foreshadow_ids"] = [item.id for item in foreshadows]
        sections.append("伏笔：\n" + "\n".join(f"- foreshadow#{item.id} {item.setup_text}" for item in foreshadows))

    if not sections:
        return "未登记 Canon；不得自行发明长期设定。", refs
    return "\n\n".join(sections), refs
