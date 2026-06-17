from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief
from app.services.production import create_book, seed_prompts
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action


def main() -> int:
    database_url = isolated_database("production-packet-brief-self-heal-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Packet Self Heal", genre="网游武侠", platform="番茄小说")
        _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "大学生林北获得全真武侠网游《万象江湖》内测资格，激活桥段复刻能力；主要压力来自NPC因果、人情债、玩家竞争和现实同步副作用。",
                "reader_promise": "看林北在热闹江湖里观察现场条件、自然复刻经典桥段，拿到招式、人情和现实同步回报；失败会损失好感并现实出糗。",
                "world_engine": "《万象江湖》从武侠向仙侠升维，NPC有独立欲望和好感度；桥段复刻不能强迫人物配合，必须服从人物因果、现场证据和现实同步副作用。",
                "protagonist_engine": "林北熟悉经典桥段但不万能，必须主动观察、选择、试错和承担代价；复刻失败会损失好感、错过奖励并造成现实动作失控。",
                "conflict_engine": "长期冲突来自NPC关系债、玩家竞争、现实同步失控和世界升维后的规则变硬；每次破局都会带来新线索、新代价或新关系变化。",
                "forbidden_rules": "禁止旧主角名、旧世界名和机械面板解题。",
                "style_guide": "明快热闹，有江湖烟火气。",
                "volume_title": "第一卷",
                "volume_summary": "内测、桥段复刻、现实同步。",
                "arc_title": "入局",
                "arc_goal": "建立复刻机制和第一笔人情债。",
                "arc_climax": "复刻成功让现实身体出现同步反应。",
                "arc_turn": "林北意识到游戏规则正在升维。",
                "approve_after_save": True,
            },
        )
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == 1))
        for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id)):
            brief.status = "superseded"
        stale = ChapterBrief(
            chapter_id=chapter.id,
            goal="继续写旧稿。",
            required_beats="当前世界/作品锚点:《已废弃旧主角名志》\n修复质检问题：场景不够。",
            constraints="不得改动旧世界名。",
            status="ready",
        )
        session.add(stale)
        session.flush()
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal=stale.goal,
            required_beats=stale.required_beats,
            constraints=stale.constraints,
            mode="draft",
            chapter_id=chapter.id,
            chapter_brief_id=stale.id,
        )
        contamination = packet.audit.get("context_contamination") or {}
        if not contamination.get("passed"):
            print("production packet did not self-heal contaminated brief")
            print(contamination)
            return 1
        latest = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
        text = "\n".join([latest.goal or "", latest.required_beats or "", latest.constraints or ""])
        if "万象江湖" not in text or "已废弃" in text:
            print("self-healed brief is not clean")
            print(text)
            return 1

    print("production-packet-brief-self-heal-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
