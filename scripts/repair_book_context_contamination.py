from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import (
    Character,
    Chapter,
    ChapterBrief,
    KnowledgeEmbedding,
    PlotThread,
    PowerSystem,
    WorldRule,
)
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.db_ops import create_database_backup


REPLACEMENTS = {
    "林默": "陈默",
    "《江湖志》": "《江湖》",
    "江湖志": "江湖",
}


def _clean_text(value: str | None) -> str:
    text = value or ""
    for old, new in REPLACEMENTS.items():
        text = text.replace(old, new)
    return text


def _has_stale(value: str | None) -> bool:
    text = value or ""
    return any(old in text for old in REPLACEMENTS)


def main() -> int:
    book_id = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    report: dict[str, object] = {"book_id": book_id}
    with session_scope() as session:
        backup = create_database_backup(session, label=f"repair-context-contamination-book-{book_id}")
        report["backup_path"] = backup.backup_path

        changed = {"characters": 0, "world_rules": 0, "power_systems": 0, "plot_threads": 0}
        for character in session.scalars(select(Character).where(Character.book_id == book_id)):
            before = (character.name, character.personality, character.ability, character.background)
            character.name = _clean_text(character.name)
            character.personality = _clean_text(character.personality)
            character.ability = _clean_text(character.ability)
            character.background = _clean_text(character.background)
            if before != (character.name, character.personality, character.ability, character.background):
                changed["characters"] += 1

        for rule in session.scalars(select(WorldRule).where(WorldRule.book_id == book_id)):
            before = (rule.category, rule.rule_text)
            rule.category = _clean_text(rule.category)
            rule.rule_text = _clean_text(rule.rule_text)
            if before != (rule.category, rule.rule_text):
                changed["world_rules"] += 1

        for power in session.scalars(select(PowerSystem).where(PowerSystem.book_id == book_id)):
            before = (power.name, power.rules, power.costs, power.limits)
            power.name = _clean_text(power.name)
            power.rules = _clean_text(power.rules)
            power.costs = _clean_text(power.costs)
            power.limits = _clean_text(power.limits)
            if before != (power.name, power.rules, power.costs, power.limits):
                changed["power_systems"] += 1

        for thread in session.scalars(select(PlotThread).where(PlotThread.book_id == book_id)):
            before = (thread.name, thread.description)
            thread.name = _clean_text(thread.name)
            thread.description = _clean_text(thread.description)
            if before != (thread.name, thread.description):
                changed["plot_threads"] += 1

        stale_embeddings = list(
            session.scalars(
                select(KnowledgeEmbedding).where(
                    KnowledgeEmbedding.book_id == book_id,
                    KnowledgeEmbedding.text.like("%林默%") | KnowledgeEmbedding.text.like("%江湖志%"),
                )
            )
        )
        for item in stale_embeddings:
            session.delete(item)

        repaired_briefs = []
        for chapter in session.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number)):
            if chapter.status not in {"briefing", "queued", "drafting", "needs_revision", "continuity_recorded"}:
                continue
            brief = repair_chapter_brief(session, book_id=book_id, chapter_number=chapter.chapter_number)
            repaired_briefs.append({"chapter_number": chapter.chapter_number, "brief_id": brief.id, "status": brief.status})

        session.flush()
        stale_briefs = session.scalars(
            select(ChapterBrief)
            .join(Chapter, Chapter.id == ChapterBrief.chapter_id)
            .where(Chapter.book_id == book_id)
        )
        remaining_brief_ids = [
            brief.id
            for brief in stale_briefs
            if brief.status != "superseded"
            and (_has_stale(brief.goal) or _has_stale(brief.required_beats) or _has_stale(brief.constraints))
        ]
        report.update(
            {
                "changed": changed,
                "deleted_stale_embeddings": len(stale_embeddings),
                "repaired_briefs": repaired_briefs,
                "remaining_active_stale_brief_ids": remaining_brief_ids,
            }
        )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
