from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models.entities import Book, Chapter, KnowledgeEmbedding
from app.services.agent_plan_intelligence import list_visual_assets
from app.services.aesthetic_profile import story_bible_display_fields
from app.services.canon import format_canon_context
from app.services.evidence import audit_market_evidence, format_market_evidence_context
from app.services.story import format_story_control_context, get_story_bible
from app.services.story_dna import story_dna_display_fields, story_dna_for_book
from app.services.skeleton_governance import audit_story_skeleton_with_agent_evidence
from app.services.skeleton_sync import list_skeleton_versions
from app.dashboard_skeleton_constants import SKELETON_APPROVAL_FIELDS


def knowledge_payload(session, *, book_id: int, chapter_number: int, latest_foundation_fn, approval_payload_fn) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    story_context, story_refs = format_story_control_context(session, book_id=book_id, chapter_number=chapter_number)
    canon_context, canon_refs = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    evidence_context, signal_ids = format_market_evidence_context(session, genre=book.genre)
    bible = get_story_bible(session, book_id=book_id)
    embedding_rows = []
    embedding_count = 0
    visual_assets = []
    try:
        embedding_rows = list(session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id).order_by(KnowledgeEmbedding.id.desc()).limit(5)))
        embedding_count = session.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.book_id == book_id).count()
        visual_assets = list_visual_assets(session, book_id=book_id, limit=8)
    except OperationalError:
        session.rollback()
    return {
        "story_bible": {"id": bible.id, "status": bible.status} if bible else None,
        "skeleton": story_skeleton_payload(session, book_id=book_id, latest_foundation_fn=latest_foundation_fn, approval_payload_fn=approval_payload_fn),
        "story_refs": story_refs,
        "canon_refs": canon_refs,
        "story_context": story_context,
        "canon_context": canon_context,
        "evidence_context": evidence_context,
        "market_signal_ids": signal_ids,
        "evidence_audit": [
            {"signal_id": item.signal_id, "usable": item.usable, "reasons": item.reasons, "source": item.source_key, "signal": item.signal_text}
            for item in audit_market_evidence(session, genre=book.genre)
        ],
        "semantic_memory": {
            "count": embedding_count,
            "recent": [
                {"id": item.id, "source_type": item.source_type, "source_label": item.source_label, "model": item.model, "dimensions": item.dimensions}
                for item in embedding_rows
            ],
        },
        "visual_assets": [
            {"id": item.id, "asset_type": item.asset_type, "chapter_id": item.chapter_id, "status": item.status, "model": item.model, "artifact_path": item.artifact_path}
            for item in visual_assets
        ],
    }


def story_skeleton_payload(session, *, book_id: int, latest_foundation_fn, approval_payload_fn) -> dict:
    from app.models.entities import StoryArc, Volume

    foundation = latest_foundation_fn(session, book_id=book_id)
    bible = get_story_bible(session, book_id=book_id)
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    dna_display = story_dna_display_fields(style_guide=bible.style_guide if bible else "", forbidden_rules=bible.forbidden_rules if bible else "")
    bible_display = story_bible_display_fields(style_guide=dna_display["style_guide"], forbidden_rules=dna_display["forbidden_rules"])
    story_dna = dna_display["story_dna"] or story_dna_for_book(session, book_id=book_id)
    skeleton_values = {
        "premise": foundation.premise if foundation else (bible.positioning if bible else ""),
        "reader_promise": foundation.reader_promise if foundation else (bible.reader_promise if bible else ""),
        "world_engine": foundation.world_engine if foundation else (bible.power_curve if bible else ""),
        "protagonist_engine": foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else ""),
        "conflict_engine": foundation.conflict_engine if foundation else (bible.main_plot if bible else ""),
        "forbidden_rules": bible_display["forbidden_rules"],
        "style_guide": bible_display["style_guide"],
        "aesthetic_profile": bible_display["aesthetic_profile"],
        "story_dna": story_dna,
        "volume_summary": volume.summary if volume else "",
        "arc_goal": arc.goal if arc else "",
        "arc_climax": arc.climax if arc else "",
        "arc_turn": arc.turn if arc else "",
    }
    payload = {
        "foundation": {"id": foundation.id, "premise": foundation.premise, "reader_promise": foundation.reader_promise, "world_engine": foundation.world_engine, "protagonist_engine": foundation.protagonist_engine, "conflict_engine": foundation.conflict_engine, "status": foundation.status} if foundation else None,
        "story_bible": {"id": bible.id, "positioning": bible.positioning, "reader_promise": bible.reader_promise, "main_plot": bible.main_plot, "protagonist_arc": bible.protagonist_arc, "relationship_arc": bible.relationship_arc, "power_curve": bible.power_curve, "forbidden_rules": bible_display["forbidden_rules"], "style_guide": bible_display["style_guide"], "aesthetic_profile": bible_display["aesthetic_profile"], "story_dna": story_dna, "status": bible.status} if bible else None,
        "volume": {"id": volume.id, "title": volume.title, "summary": volume.summary, "status": volume.status} if volume else None,
        "story_arc": {"id": arc.id, "title": arc.title, "start_chapter": arc.start_chapter, "end_chapter": arc.end_chapter, "goal": arc.goal, "climax": arc.climax, "turn": arc.turn, "status": arc.status} if arc else None,
    }
    payload["approvals"] = approval_payload_fn(session, book_id=book_id, skeleton=skeleton_values)
    payload["versions"] = list_skeleton_versions(session, book_id=book_id)
    payload["governance"] = audit_story_skeleton_with_agent_evidence(session, book_id=book_id).to_dict()
    return payload
