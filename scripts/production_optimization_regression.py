from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, ProductionRunReview
from app.services.production_optimization import (
    OPTIMIZATION_MARKER,
    apply_skeleton_preflight_to_brief,
    chapter_type_profile,
    enrich_quality_report_with_optimization,
    predict_revision_pass,
)
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-optimization-regression")
    failures: list[str] = []
    profile = chapter_type_profile(1, goal="第一章开局")
    if profile.code != "opening" or profile.pass_score < 72:
        failures.append("opening_profile_not_strict")

    near_pass_report = {
        "score": 70,
        "passed": True,
        "dimensions": {
            "reader_momentum": 66,
            "hook_strength": 70,
            "author_intent": 66,
            "brief_coverage": 61,
            "chapter_unit_flow": 63,
            "dialogue_fullness": 58,
            "scene_atmosphere": 58,
        },
        "hard_gate": {"passed": True},
        "issues": [],
    }
    enriched = enrich_quality_report_with_optimization(near_pass_report, chapter_number=1, goal="第一章开局")
    if enriched.get("passed"):
        failures.append("chapter_type_gate_did_not_raise_opening_standard")
    if not enriched.get("chapter_type_gate") or not enriched.get("revision_pass_prediction"):
        failures.append("optimization_metadata_missing")

    rebuild = predict_revision_pass(
        {
            "score": 55,
            "dimensions": {"brief_coverage": 40, "author_intent": 42, "arc_alignment": 50},
            "hard_gate": {"passed": True},
            "issues": [],
        },
        chapter_number=2,
    )
    if rebuild.tier != "rebuild" or not rebuild.should_rebuild:
        failures.append(f"rebuild_prediction_failed:{rebuild}")

    with session_scope() as session:
        book = Book(title="production optimization", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第2章：茶棚遇同行",
            required_beats="玩家竞争规则误判",
            constraints="3000-4500中文字符",
            status="ready",
        )
        session.add(brief)
        session.flush()
        session.add(
            ProductionRunReview(
                book_id=book.id,
                chapter_id=chapter.id,
                chapter_version_id=None,
                generation_task_id=None,
                status="pass",
                review_json=json.dumps(
                    {
                        "chapter_number": 1,
                        "headline": "开场承接明确，章末钩子自然。",
                        "recommendations": ["继续保持对话试探推动信息差。"],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.flush()

        preflight = apply_skeleton_preflight_to_brief(session, book_id=book.id, chapter_number=2, brief=brief)
        if preflight.passed or OPTIMIZATION_MARKER not in (brief.required_beats or ""):
            failures.append("skeleton_preflight_did_not_patch_brief")
        if "合格章样本记忆" not in brief.required_beats:
            failures.append("passed_chapter_memory_missing_from_brief")

        version = ChapterVersion(chapter_id=chapter.id, version_number=1, title="第2章", content="正文" * 1000, status="draft")
        session.add(version)
        session.flush()
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=2,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            mode="draft",
            chapter_id=chapter.id,
            chapter_brief_id=brief.id,
        )
        if OPTIMIZATION_MARKER not in packet.director_sheet:
            failures.append("optimization_block_missing_from_packet")
        if not packet.audit.get("production_optimization"):
            failures.append("packet_audit_missing_optimization_flag")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-optimization-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
