from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief
from app.services.aesthetic_profile import PROFILE_START
from app.services.planning import run_next_action
from app.services.production import create_book, seed_prompts
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action, _story_skeleton_payload


DNA = """【作品DNA】
- 题材主味: 全真虚拟现实武侠网游向仙侠升维，游戏和现实互相挤压。
- 笔触: 明快、热闹、有烟火气和玩家吐槽。
- 氛围: 江湖奇遇感压过阴冷悬疑。
- 故事路线: 复刻经典桥段、获得收益、承担门派因果和现实监测副作用。
- 核心钩子: 剧情演绎触发任务，复刻越像奖励越好。
- 金手指机制: 演绎相似度来自人物因果与场景条件，不是机械刷分。
- 世界收益规则: 收益对应好感、人情债、神经反馈和现实异常。
- 长线压力: 玩家竞争、门派追查、现实采样和仙侠升维逐层升级。
- 必须保留: 每章有招式、关系、收益或现实异常中的至少一项回报。
- 禁止滑坡: 不要默认阴冷旧案追查，不要机械任务链。
- 章节发动机库: 桥段复刻变形；门派规矩试炼；玩家竞争；现实异常外泄；资源交易
- 执行要求: 先选章节发动机，再安排目标、阻碍、动作、代价、收益和章末钩子。
【作品DNA结束】"""


def main() -> int:
    database_url = isolated_database("story-dna-workflow-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Story DNA Regression", genre="网游武侠", platform="番茄小说")
        book_id = book.id
        saved = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book_id,
                "premise": "主角获得全真虚拟现实武侠网游内测资格，激活剧情演绎机制，游戏收益开始同步现实。",
                "reader_promise": "看主角用经典桥段复刻破局，在热闹江湖里拿收益、欠人情、撞上现实异常。",
                "world_engine": "网游从武侠向仙侠升维；奖励、好感、现实同步和服务器监测互相牵制。",
                "protagonist_engine": "主角主动选择桥段复刻路线，但必须处理复刻失败、好感下降和现实副作用。",
                "conflict_engine": "玩家竞争、门派因果、现实监测和升维灾变逐卷升级。",
                "style_guide": "明快热闹，有招式身法、门派场面和轻松吐槽。",
                "forbidden_rules": "禁止阴冷悬疑默认化，禁止机械任务链。",
                "story_dna": DNA,
                "volume_title": "第一卷",
                "volume_summary": "内测资格、桥段演绎、现实同步异常和第一笔门派因果债。",
                "arc_title": "内测入江湖",
                "arc_goal": "前五章建立演绎机制、奖励逻辑、失败代价和现实异常。",
                "arc_climax": "主角复刻桥段救场，却让现实身体出现可检测异常。",
                "arc_turn": "他意识到游戏不是普通服务器，而是升维入口。",
                "approve_after_save": True,
            },
        )
        if saved.get("status") != "saved":
            print("skeleton save failed")
            print(saved)
            return 1
        payload = _story_skeleton_payload(session, book_id=book_id)
        story_dna = ((payload.get("story_bible") or {}).get("story_dna") or "")
        if "章节发动机库" not in story_dna or "桥段复刻变形" not in story_dna:
            print("story dna missing from skeleton payload")
            print(payload.get("story_bible"))
            return 1
        if PROFILE_START in story_dna:
            print("story dna should not embed the full aesthetic profile block")
            print(story_dna)
            return 1
        result = run_next_action(session, book_id=book_id, chapter_number=1, dry_run=True)
        if result.action not in {"create_chapter_brief", "draft_chapter", "generate_chapter_samples"}:
            print("unexpected first action")
            print(result)
            return 1
        chapter = session.query(Chapter).filter_by(book_id=book_id, chapter_number=1).first()
        brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id).order_by(ChapterBrief.id.desc()).first()
        brief_text = "\n".join([brief.goal, brief.required_beats, brief.constraints])
        if "本章章节发动机" not in brief_text or "桥段复刻变形" not in brief_text:
            print("chapter brief did not include story dna engine")
            print(brief_text)
            return 1
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
        if not packet.audit.get("story_dna") or "本章优先发动机" not in packet.director_sheet:
            print("production packet missing story dna")
            print(packet.audit)
            return 1
        restarted = _perform_action(
            session,
            {"action": "restart_production_from_chapter", "book_id": book_id, "start_chapter": 1},
        )
        if restarted.get("status") != "restarted" or not restarted.get("backup_path"):
            print("restart action failed")
            print(restarted)
            return 1
        remaining = session.query(Chapter).filter_by(book_id=book_id).count()
        if remaining:
            print("restart did not clear chapters")
            print(restarted)
            return 1

    print("story-dna-workflow-regression: PASS")
    print(f"database={database_url}")
    print(f"book_id={book_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
