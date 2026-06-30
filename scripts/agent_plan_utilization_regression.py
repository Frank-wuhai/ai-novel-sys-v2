from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterVersion, KnowledgeEmbedding, ProductionRunReview, QualityReport, StoryArc
from app.services.agent_plan_intelligence import index_book_knowledge, summarize_semantic_memory
from app.services.agent_plan_utilization import build_agent_plan_utilization_report
from app.services.feedback import record_platform_feedback
from app.services.production import create_book, create_foundation
from regression_db import isolated_database


def main() -> int:
    isolated_database("agent-plan-utilization-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = create_book(session, title="Agent Plan 利用率回归", genre="网游武侠升仙侠", platform="番茄小说")
        foundation = create_foundation(
            session,
            book_id=book.id,
            premise="主角进入江湖游戏，通过桥段演绎让游戏和现实同步升维。",
            reader_promise="热闹江湖、主动破局、收益与代价同场落地。",
            world_engine="游戏江湖逐步升维到仙侠秩序。",
            protagonist_engine="主角靠观察、交易、演绎和行动破局。",
            conflict_engine="桥段误判、现实同步和江湖规矩形成长期冲突。",
        )
        arc = StoryArc(
            book_id=book.id,
            arc_number=1,
            title="初入江湖",
            start_chapter=1,
            end_chapter=12,
            goal="确认桥段演绎规则。",
            climax="用有代价的桥段复刻破局。",
            turn="现实副作用证明双线同步。",
        )
        session.add(arc)
        session.flush()
        for key, value in {
            "premise": foundation.premise,
            "reader_promise": foundation.reader_promise,
            "world_engine": foundation.world_engine,
            "protagonist_engine": foundation.protagonist_engine,
            "conflict_engine": foundation.conflict_engine,
            "arc_goal": arc.goal,
            "arc_climax": arc.climax,
            "arc_turn": arc.turn,
        }.items():
            record_platform_feedback(
                session,
                book_id=book.id,
                platform="regression",
                metric_name="skeleton_approval",
                metric_value=key,
                raw_text=value,
            )
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", summary="主角第一次遇到桥段门槛。", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第1章",
            content="主角在外部压力下主动选择，收益和代价同时落地。" * 120,
            status="needs_revision",
            source="regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=66,
            passed=False,
            report=json.dumps(
                {
                    "issues": ["主角主动性不足", "章末代价不具体"],
                    "warnings": ["旧开篇结构不可沿用"],
                    "llm_review": {
                        "revision_suggestions": ["改用利益交换开篇", "500字内给出主动选择"],
                        "risk_flags": ["不要用系统面板直接解题"],
                    },
                    "dimensions": {"protagonist_agency": 45, "payoff_grounding": 50},
                    "reading_assessment": {"action": "auto_rebuild", "level": "需重建"},
                },
                ensure_ascii=False,
            ),
        )
        session.add(quality)
        session.flush()
        session.add(
            ProductionRunReview(
                book_id=book.id,
                chapter_id=chapter.id,
                chapter_version_id=version.id,
                status="recorded",
                review_json=json.dumps(
                    {
                        "headline": "单稿重建失败，需改多候选策略",
                        "lessons": ["旧问路结构不要复用", "章末副作用必须落到现实动作"],
                        "recommendations": ["生成三个不同开篇压力候选"],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.flush()

        index_book_knowledge(session, book_id=book.id, dry_run=True, reset=True)
        summary = summarize_semantic_memory(session, book_id=book.id)
        source_types = set(summary.get("source_types") or [])
        if "quality_lesson" not in source_types:
            failures.append(f"quality_lesson_missing:{source_types}")
        if "production_review" not in source_types:
            failures.append(f"production_review_missing:{source_types}")
        quality_memory = session.scalar(
            select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book.id, KnowledgeEmbedding.source_type == "quality_lesson")
        )
        if not quality_memory or "主角主动性不足" not in quality_memory.text or "不要用系统面板直接解题" not in quality_memory.text:
            failures.append("quality_lesson_content_missing")
        report = build_agent_plan_utilization_report(session, book_id=book.id)
        names = {section["name"] for section in report.get("sections") or []}
        if {"configuration", "model_routing", "market_evidence", "semantic_memory", "lesson_memory", "visual_assets"} - names:
            failures.append(f"utilization_sections_missing:{names}")
        if not report.get("next_actions"):
            failures.append("utilization_next_actions_missing")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("agent-plan-utilization-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
