from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, KnowledgeEmbedding, PlatformFeedback
from app.services.context_contamination import audit_context_contamination, context_anchor_lines
from app.services.production import create_book, seed_prompts
from app.services.production_gate import pending_skeleton_approval_labels
from app.services.readiness import check_production_readiness
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action, _story_skeleton_payload


def main() -> int:
    database_url = isolated_database("skeleton-global-sync-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Global Sync Regression", genre="网游武侠", platform="番茄小说")
        saved = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "沈砚获得全真武侠网游《云梯志》内测资格，桥段复刻奖励会延迟同步到现实。",
                "reader_promise": "看沈砚在热闹江湖里用桥段复刻破局，拿到招式、人情和现实同步回报。",
                "world_engine": "《云梯志》从武侠向仙侠升维，桥段复刻必须服从NPC因果和现实同步副作用。",
                "protagonist_engine": "沈砚熟悉经典桥段，但每次复刻都要承担好感下降、关系误读和现实动作失控。",
                "conflict_engine": "长期冲突来自玩家竞争、NPC关系债、现实同步失控和世界升维后的规则变硬。",
                "style_guide": "明快热闹，有江湖烟火气和玩家吐槽。",
                "forbidden_rules": "不能写成系统任务面板；不能主角一用能力就被追杀或官方盯上。",
                "volume_title": "第一卷",
                "volume_summary": "内测资格、桥段复刻、现实同步和第一笔人情债。",
                "arc_title": "入局",
                "arc_goal": "建立复刻机制和失败代价。",
                "arc_climax": "一次复刻成功让现实身体出现延迟同步。",
                "arc_turn": "沈砚意识到游戏规则正在升维。",
                "approve_after_save": True,
            },
        )
        if saved.get("status") != "saved":
            print("initial skeleton save failed")
            print(saved)
            return 1

        payload = _story_skeleton_payload(session, book_id=book.id)
        current = _current_form(payload)
        for key in ("premise", "world_engine"):
            current[key] = current[key].replace("云梯志", "万象江湖")
        approved = _perform_action(
            session,
            {
                "action": "approve_skeleton_item",
                "book_id": book.id,
                "key": "all",
                "current_skeleton": current,
            },
        )
        if approved.get("status") != "approved":
            print("approve all failed")
            print(approved)
            return 1

        if pending_skeleton_approval_labels(session, book_id=book.id):
            print("pending approvals survived global sync")
            print(pending_skeleton_approval_labels(session, book_id=book.id))
            return 1
        readiness = check_production_readiness(session, book_id=book.id, start=1, count=5)
        if not readiness.passed:
            print("readiness failed after global sync")
            print([(item.name, item.detail) for item in readiness.checks if not item.passed])
            return 1

        anchors = "\n".join(context_anchor_lines(session, book_id=book.id))
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == 1))
        brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
        brief_text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if "万象江湖" not in brief_text or "云梯志" in brief_text:
            print("brief did not globally sync world title")
            print(brief_text)
            return 1
        contamination = audit_context_contamination(
            session,
            book_id=book.id,
            chapter_number=1,
            brief_text=brief_text,
            canon_context=anchors,
            previous_content="",
        )
        if not contamination.passed:
            print("contamination audit failed after global sync")
            print(contamination.to_dict())
            return 1
        versions = session.scalars(
            select(PlatformFeedback).where(PlatformFeedback.book_id == book.id, PlatformFeedback.metric_name == "skeleton_version")
        ).all()
        if len(versions) < 2:
            print("skeleton versions were not recorded")
            print(len(versions))
            return 1
        embedding_count = session.query(KnowledgeEmbedding).filter_by(book_id=book.id).count()
        if embedding_count < 1:
            print("semantic memory was not rebuilt")
            return 1

    print("skeleton-global-sync-regression: PASS")
    print(f"database={database_url}")
    return 0


def _current_form(payload: dict) -> dict[str, str]:
    foundation = payload.get("foundation") or {}
    bible = payload.get("story_bible") or {}
    volume = payload.get("volume") or {}
    arc = payload.get("story_arc") or {}
    return {
        "premise": foundation.get("premise") or bible.get("positioning") or "",
        "reader_promise": foundation.get("reader_promise") or bible.get("reader_promise") or "",
        "world_engine": foundation.get("world_engine") or bible.get("power_curve") or "",
        "protagonist_engine": foundation.get("protagonist_engine") or bible.get("protagonist_arc") or "",
        "conflict_engine": foundation.get("conflict_engine") or bible.get("main_plot") or "",
        "forbidden_rules": bible.get("forbidden_rules") or "",
        "style_guide": bible.get("style_guide") or "",
        "aesthetic_profile": bible.get("aesthetic_profile") or "",
        "story_dna": bible.get("story_dna") or "",
        "volume_title": volume.get("title") or "第一卷",
        "volume_summary": volume.get("summary") or "",
        "arc_title": arc.get("title") or "开局",
        "arc_goal": arc.get("goal") or "",
        "arc_climax": arc.get("climax") or "",
        "arc_turn": arc.get("turn") or "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
