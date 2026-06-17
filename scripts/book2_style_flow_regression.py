from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief
from app.services.aesthetic_profile import PROFILE_START
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.planning import run_next_action
from app.services.production import create_book, seed_prompts
from app.services.production_packet import build_chapter_production_packet
from app.services.story_alignment import build_story_alignment_audit
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action, _story_skeleton_payload


LEGACY_MARKERS = ("陈默", "大江湖", "修订合同:", "依据质检报告", "原始人工意见", "修复质检问题")


def main() -> int:
    database_url = isolated_database("book2-style-flow-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="我的武侠游戏存档，正在现实同步加载", genre="网游武侠", platform="番茄小说")
        result = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "大学生林默获得全真武侠网游《江湖志》内测资格，激活桥段复刻能力，游戏收益会延迟同步到现实。",
                "reader_promise": "看林默在热闹江湖里识别并自然演绎经典桥段，成功拿招式、人情和现实同步回报，失败则搞砸关系并现实出糗。",
                "world_engine": "《江湖志》从武侠向仙侠升维；NPC有独立欲望和好感度，桥段复刻必须服从现场证据、人物关系和环境条件。",
                "protagonist_engine": "林默熟悉经典武侠桥段，但容易用力过猛；每次复刻都要承担好感下降、信息壁垒和现实动作失控。",
                "conflict_engine": "长期冲突来自玩家竞争、NPC关系债、桥段错账、现实生活失衡和世界升维后的规则变硬。",
                "style_guide": "轻松明快带网感，对话有烟火气，打斗有画面感，桥段复刻像即兴演出。",
                "forbidden_rules": "不能写成系统任务面板；不能主角一用能力就被追杀或官方盯上；不能把NPC写成工具人。",
                "aesthetic_profile": "\n".join(
                    [
                        PROFILE_START,
                        "- 笔触: 轻松明快带网感，对话有烟火气，打斗有画面感。",
                        "- 氛围: 热闹开荒、江湖奇遇、现实出糗，后期武侠向仙侠升维。",
                        "- 路线: 桥段复刻、好感错账、现实同步和世界升维逐步推进。",
                        "- 必须保留: 每章有见识、招式、关系、资源或主动权回报。",
                        "- 禁止惯性: 不要机构盯上、门派追杀或资本实验当主线推进。",
                        "【作品审美画像结束】",
                    ]
                ),
                "volume_title": "第一卷",
                "volume_summary": "内测资格、桥段复刻、现实同步和第一笔人情债。",
                "arc_title": "入江湖",
                "arc_goal": "建立复刻机制、收益规则、失败代价和现实同步副作用。",
                "arc_climax": "一次复刻成功让现实身体出现延迟同步，同时欠下关键人情。",
                "arc_turn": "林默意识到游戏规则正在向仙侠升维。",
                "approve_after_save": True,
            },
        )
        if result.get("status") != "saved":
            print("skeleton update failed")
            print(result)
            return 1

        skeleton = _story_skeleton_payload(session, book_id=book.id)
        story_dna = ((skeleton.get("story_bible") or {}).get("story_dna") or "")
        if PROFILE_START in story_dna:
            print("story dna embedded full aesthetic profile")
            print(story_dna)
            return 1
        if any(marker in story_dna for marker in ("机构盯上", "资本实验", "科技公司")):
            print("story dna retained stale cliche route")
            print(story_dna)
            return 1

        action = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=True)
        if action.action not in {"create_chapter_brief", "draft_chapter"}:
            print("unexpected first action")
            print(action)
            return 1
        chapter1 = session.query(Chapter).filter_by(book_id=book.id, chapter_number=1).first()
        brief1 = session.query(ChapterBrief).filter_by(chapter_id=chapter1.id).order_by(ChapterBrief.id.desc()).first()
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal=brief1.goal,
            required_beats=brief1.required_beats,
            constraints=brief1.constraints,
            chapter_id=chapter1.id,
            chapter_brief_id=brief1.id,
        )
        if packet.director_sheet.count(PROFILE_START) != 1:
            print("director sheet should contain one aesthetic profile block")
            print(packet.director_sheet.count(PROFILE_START))
            return 1
        if any(marker in packet.director_sheet for marker in LEGACY_MARKERS):
            print("director sheet retained legacy markers")
            return 1

        chapter2 = session.query(Chapter).filter_by(book_id=book.id, chapter_number=2).first()
        if not chapter2:
            chapter2 = Chapter(book_id=book.id, chapter_number=2, title="第二章", status="planned")
            session.add(chapter2)
            session.flush()
        session.add(
            ChapterBrief(
                chapter_id=chapter2.id,
                goal="第2章：写陈默确认《大江湖》不是机械游戏。",
                required_beats="修订合同:\n- 修复质检问题：visual_underdeveloped\n- 原始人工意见：不要改结构",
                constraints="依据质检报告补足动作。",
                status="revision_ready",
            )
        )
        session.flush()
        repaired = repair_chapter_brief(session, book_id=book.id, chapter_number=2)
        repaired_text = "\n".join([repaired.goal, repaired.required_beats, repaired.constraints])
        if any(marker in repaired_text for marker in LEGACY_MARKERS):
            print("repaired brief retained legacy markers")
            print(repaired_text)
            return 1
        for marker in ("林默", "江湖志", "桥段复刻", "现实同步"):
            if marker not in repaired_text:
                print("repaired brief missed current book marker")
                print(marker)
                print(repaired_text)
                return 1
        alignment = build_story_alignment_audit(session, book_id=book.id)
        if any("旧质检/旧修订合同残留" in blocker for blocker in alignment.blockers):
            print("alignment still sees stale brief blockers")
            print(alignment.blockers)
            return 1

    print("book2-style-flow-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
