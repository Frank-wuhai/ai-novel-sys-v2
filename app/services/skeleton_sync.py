from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, PlatformFeedback, StoryArc, StoryFoundation, Volume
from app.services.agent_plan_intelligence import index_book_knowledge
from app.services.aesthetic_profile import story_bible_display_fields
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.feedback import record_platform_feedback
from app.services.production_scaffold import repair_production_scaffold
from app.services.story import get_story_bible
from app.services.story_dna import story_dna_display_fields, story_dna_for_book


SKELETON_VERSION_FIELDS = [
    "premise",
    "reader_promise",
    "world_engine",
    "protagonist_engine",
    "conflict_engine",
    "forbidden_rules",
    "style_guide",
    "aesthetic_profile",
    "story_dna",
    "volume_summary",
    "arc_goal",
    "arc_climax",
    "arc_turn",
]


@dataclass(frozen=True)
class SkeletonSyncResult:
    status: str
    book_id: int
    version_id: int
    approved_count: int
    repaired_briefs: list[int]
    indexed_count: int
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def current_skeleton_values(session: Session, *, book_id: int) -> dict[str, str]:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = get_story_bible(session, book_id=book_id)
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    dna_display = story_dna_display_fields(style_guide=bible.style_guide if bible else "", forbidden_rules=bible.forbidden_rules if bible else "")
    bible_display = story_bible_display_fields(style_guide=dna_display["style_guide"], forbidden_rules=dna_display["forbidden_rules"])
    return {
        "premise": foundation.premise if foundation else (bible.positioning if bible else ""),
        "reader_promise": foundation.reader_promise if foundation else (bible.reader_promise if bible else ""),
        "world_engine": foundation.world_engine if foundation else (bible.power_curve if bible else ""),
        "protagonist_engine": foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else ""),
        "conflict_engine": foundation.conflict_engine if foundation else (bible.main_plot if bible else ""),
        "forbidden_rules": bible_display["forbidden_rules"],
        "style_guide": bible_display["style_guide"],
        "aesthetic_profile": bible_display["aesthetic_profile"],
        "story_dna": dna_display["story_dna"] or story_dna_for_book(session, book_id=book_id),
        "volume_summary": volume.summary if volume else "",
        "arc_goal": arc.goal if arc else "",
        "arc_climax": arc.climax if arc else "",
        "arc_turn": arc.turn if arc else "",
    }


def propagate_core_term_changes(previous_values: dict[str, str], new_values: dict[str, str]) -> dict[str, str]:
    updated = {key: str(value or "") for key, value in (new_values or {}).items()}
    previous_titles = _world_titles("\n".join(str(value or "") for value in (previous_values or {}).values()))
    new_core_text = "\n".join(str(updated.get(key) or "") for key in ("premise", "world_engine", "reader_promise"))
    new_titles = _world_titles(new_core_text)
    replacements: dict[str, str] = {}
    if len(new_titles) == 1:
        new_title = next(iter(new_titles))
        for old_title in previous_titles:
            if old_title != new_title and old_title not in new_core_text:
                replacements[f"《{old_title}》"] = f"《{new_title}》"
                replacements[old_title] = new_title
    if not replacements:
        return updated
    for key, value in list(updated.items()):
        text = str(value or "")
        for old, new in replacements.items():
            text = text.replace(old, new)
        updated[key] = text
    return updated


def synchronize_skeleton_derivatives(
    session: Session,
    *,
    book_id: int,
    approve_keys: list[str] | str | None = None,
    chapter_count: int = 5,
    reason: str = "skeleton_sync",
) -> SkeletonSyncResult:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    values = current_skeleton_values(session, book_id=book_id)
    approved_count = _approve_keys(session, book_id=book_id, values=values, approve_keys=approve_keys)
    repair_production_scaffold(
        session,
        book_id=book_id,
        only_missing=True,
        approve_skeleton=False,
        chapter_count=chapter_count,
        apply=True,
    )
    repaired = _repair_existing_briefs(session, book_id=book_id, chapter_count=chapter_count)
    memory = index_book_knowledge(session, book_id=book_id, dry_run=True, reset=True)
    snapshot = record_skeleton_version(session, book_id=book_id, reason=reason, values=current_skeleton_values(session, book_id=book_id))
    session.flush()
    return SkeletonSyncResult(
        status="synced",
        book_id=book_id,
        version_id=snapshot.id,
        approved_count=approved_count,
        repaired_briefs=repaired,
        indexed_count=int(memory.get("indexed_count") or 0),
        message=f"作品设定已全域同步：确认 {approved_count} 项，修复 brief {len(repaired)} 个，重建语义记忆 {int(memory.get('indexed_count') or 0)} 条。",
    )


def record_skeleton_version(session: Session, *, book_id: int, reason: str, values: dict[str, str] | None = None) -> PlatformFeedback:
    values = values or current_skeleton_values(session, book_id=book_id)
    previous = session.scalar(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_version")
        .order_by(PlatformFeedback.id.desc())
    )
    previous_payload = _loads_json(previous.raw_text if previous else "")
    version_number = int(previous_payload.get("version_number") or 0) + 1
    payload = {
        "version_number": version_number,
        "reason": reason,
        "created_at": datetime.utcnow().isoformat(),
        "values": {key: str(values.get(key) or "") for key in SKELETON_VERSION_FIELDS},
    }
    return record_platform_feedback(
        session,
        book_id=book_id,
        platform="system",
        metric_name="skeleton_version",
        metric_value=str(version_number),
        raw_text=json.dumps(payload, ensure_ascii=False),
    )


def list_skeleton_versions(session: Session, *, book_id: int, limit: int = 8) -> list[dict]:
    rows = session.scalars(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_version")
        .order_by(PlatformFeedback.id.desc())
        .limit(limit)
    )
    versions = []
    for row in rows:
        payload = _loads_json(row.raw_text)
        values = payload.get("values") if isinstance(payload.get("values"), dict) else {}
        versions.append(
            {
                "id": row.id,
                "version_number": payload.get("version_number") or row.metric_value,
                "reason": payload.get("reason") or "",
                "created_at": payload.get("created_at") or "",
                "premise": str(values.get("premise") or "")[:160],
                "world_engine": str(values.get("world_engine") or "")[:160],
            }
        )
    return versions


def _approve_keys(session: Session, *, book_id: int, values: dict[str, str], approve_keys: list[str] | str | None) -> int:
    if not approve_keys:
        return 0
    keys = SKELETON_VERSION_FIELDS if approve_keys == "all" else list(approve_keys)
    approved = 0
    for key in keys:
        value = str(values.get(key) or "").strip()
        if not value:
            continue
        record_platform_feedback(
            session,
            book_id=book_id,
            platform="dashboard",
            metric_name="skeleton_approval",
            metric_value=key,
            raw_text=value,
        )
        approved += 1
    return approved


def _repair_existing_briefs(session: Session, *, book_id: int, chapter_count: int) -> list[int]:
    repaired: list[int] = []
    chapters = list(
        session.scalars(
            select(Chapter)
            .where(Chapter.book_id == book_id, Chapter.chapter_number <= max(1, chapter_count))
            .order_by(Chapter.chapter_number)
        )
    )
    for chapter in chapters:
        brief = repair_chapter_brief(session, book_id=book_id, chapter_number=chapter.chapter_number)
        repaired.append(brief.id)
    return repaired


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _world_titles(text: str) -> set[str]:
    return {item.strip() for item in re.findall(r"《([^》]{2,24})》", text or "") if item.strip()}
