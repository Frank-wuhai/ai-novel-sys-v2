from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.production import create_book, seed_prompts
from app.services.story_alignment import build_story_alignment_audit
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action


def main() -> int:
    database_url = isolated_database("preflight-brief-repair-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="云梯志", genre="网游武侠", platform="番茄小说")
        saved = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "沈砚获得全真武侠网游《云梯志》内测资格，桥段复刻奖励会延迟同步到现实。",
                "reader_promise": "看沈砚在热闹江湖里用桥段复刻破局，拿到招式、人情和现实同步的小回报。",
                "world_engine": "游戏从武侠向仙侠升维，桥段复刻必须服从NPC因果、玩家竞争和现实同步副作用。",
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
            print("skeleton save failed")
            print(saved)
            return 1

        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == 2))
        if not chapter:
            chapter = Chapter(book_id=book.id, chapter_number=2, title="第二章", status="planned")
            session.add(chapter)
            session.flush()
        stale = ChapterBrief(
            chapter_id=chapter.id,
            goal="第2章：承接旧稿，写陈默确认《大江湖》不是机械游戏。",
            required_beats="修订合同:\n- 修复质检问题：visual_underdeveloped\n- 原始人工意见：不要改结构\n",
            constraints="依据质检报告补足动作；《大江湖》必须写成真实存在的武侠世界。",
            status="revision_ready",
        )
        session.add(stale)
        session.flush()

        repaired = repair_chapter_brief(session, book_id=book.id, chapter_number=2)
        text = "\n".join([repaired.goal, repaired.required_beats, repaired.constraints])
        forbidden = ("陈默", "大江湖", "修订合同:", "依据质检报告", "原始人工意见")
        if any(marker in text for marker in forbidden):
            print("repaired brief retained stale or wrong-book text")
            print(text)
            return 1
        required = ("云梯志", "沈砚", "桥段复刻", "现实同步")
        if not all(marker in text for marker in required):
            print("repaired brief did not use current book context")
            print(text)
            return 1
        alignment = build_story_alignment_audit(session, book_id=book.id)
        stale_blockers = [blocker for blocker in alignment.blockers if "brief 仍含旧质检/旧修订合同残留" in blocker]
        if stale_blockers:
            print("story alignment should not retain stale brief blockers after repair")
            print(stale_blockers)
            return 1
        latest = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
        if latest.id != repaired.id or stale.status != "superseded":
            print("brief replacement status incorrect")
            return 1

    print("preflight-brief-repair-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
