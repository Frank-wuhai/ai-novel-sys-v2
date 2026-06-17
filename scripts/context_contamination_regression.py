from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, Character, PowerSystem, WorldRule
from app.services.canon import add_character, add_power_system, add_world_rule
from app.services.production import create_book, create_foundation, seed_prompts
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database


def main() -> int:
    database_url = isolated_database("context-contamination-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="污染闸门回归", genre="网游武侠", platform="番茄小说")
        create_foundation(
            session,
            book_id=book.id,
            premise="大学生林默获得全真武侠网游《江湖志》内测资格，激活“桥段复刻”能力。",
            reader_promise="看林默在《江湖志》里用桥段复刻破局，并承受现实同步副作用。",
            world_engine="《江湖志》是从武侠向仙侠升维的真实江湖。",
            protagonist_engine="林默必须观察现场证据、人物关系和环境条件，才能自然复刻经典桥段。",
            conflict_engine="长期冲突来自玩家竞争、NPC关系债、现实同步失控和世界升维。",
        )
        add_character(session, book_id=book.id, name="陈默", role="protagonist", ability="套路触发器")
        add_power_system(
            session,
            book_id=book.id,
            name="套路触发器",
            rules="陈默在《大江湖》中复刻套路获得奇遇。",
            costs="失败降低好感。",
            limits="不能机械刷。",
        )
        add_world_rule(session, book_id=book.id, category="旧世界", rule_text="《大江湖》不是机械网游。")
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第1章：写林默进入《江湖志》并首次触碰桥段复刻。",
            required_beats="林默、江湖志、桥段复刻、现实同步必须进入场景。",
            constraints="禁止陈默、大江湖、套路触发器等旧稿锚点。",
            status="ready",
        )
        session.add(brief)
        session.flush()

        blocked = False
        try:
            build_chapter_production_packet(
                session,
                book=book,
                chapter_number=1,
                goal=brief.goal,
                required_beats=brief.required_beats,
                constraints=brief.constraints,
                chapter_id=chapter.id,
                chapter_brief_id=brief.id,
            )
        except ValueError as exc:
            blocked = "生产上下文污染未通过" in str(exc) and "陈默" in str(exc) and "大江湖" in str(exc)
        if not blocked:
            print("contaminated production packet was not blocked")
            return 1

        # Fix the stale Canon and verify the same packet can be built.
        for item in session.query(Character).filter_by(book_id=book.id):
            item.name = "林默"
            item.ability = "桥段复刻"
        for item in session.query(PowerSystem).filter_by(book_id=book.id):
            item.name = "桥段复刻"
            item.rules = "林默在《江湖志》中自然复刻经典桥段获得奖励。"
        for item in session.query(WorldRule).filter_by(book_id=book.id):
            item.category = "江湖志规则"
            item.rule_text = "《江湖志》不是机械网游。"
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            chapter_id=chapter.id,
            chapter_brief_id=brief.id,
        )
        if not packet.audit.get("context_contamination", {}).get("passed"):
            print("clean production packet did not pass contamination audit")
            print(packet.audit.get("context_contamination"))
            return 1

    print("context-contamination-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
