from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, ChapterVersion
from app.services.canon import add_character, add_power_system, add_world_rule
from app.services.evidence import add_evidence_source, add_market_signal
from app.services.production import create_book, seed_prompts
from app.services.production_router import prepare_production
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action


def main() -> int:
    database_url = isolated_database("production-router-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="路由器回归", genre="网游武侠", platform="番茄小说")
        add_evidence_source(session, source_id="router-regression", title="router regression", reliability=4, status="verified")
        add_market_signal(
            session,
            genre="网游武侠",
            signal_text="网游武侠开篇要快速展示核心能力、爽点回报、失败代价和章末钩子。",
            confidence=80,
            source_key="router-regression",
        )
        add_character(session, book_id=book.id, name="林默", role="主角", personality="机灵但容易得意", ability="识别经典桥段")
        add_world_rule(session, book_id=book.id, category="桥段复刻", rule_text="桥段复刻必须符合现场证据、人物关系和环境条件。")
        add_power_system(
            session,
            book_id=book.id,
            name="桥段复刻",
            rules="经典桥段只有在人物因果吻合时才会触发奖励。",
            costs="失败会降低参演人员好感并制造信息壁垒。",
            limits="不能凭空选择桥段，不能靠系统面板直接解题。",
        )
        saved = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "主角林默获得全真武侠网游《江湖志》内测资格，激活桥段复刻能力；主要压力来自NPC因果、玩家竞争和现实同步副作用，游戏收益会延迟同步到现实。",
                "reader_promise": "看主角林默在热闹江湖里主动识别并自然演绎经典桥段，每章有招式、人情、资源或现实同步爽点回报；失败则搞砸关系、付出代价并现实出糗。",
                "world_engine": "《江湖志》从武侠向仙侠升维；NPC有独立欲望和好感度。桥段复刻有明确规则、禁止凭空触发，必须服从现场证据、人物关系和环境条件，失败后果会改变关系网。",
                "protagonist_engine": "主角林默熟悉经典武侠桥段，但必须主动选择、观察证据、承担误判代价并阶段性成长；每次复刻都可能造成好感下降、信息壁垒和现实动作失控。",
                "conflict_engine": "长期冲突来自玩家竞争、NPC关系债、桥段错账、现实生活失衡和世界升维后的规则变硬，压力会逐卷升级并改变主角资源和身份。",
                "style_guide": "轻松明快带网感，对话有烟火气，打斗有画面感，桥段复刻像即兴演出。",
                "forbidden_rules": "不能写成系统任务面板；不能主角一用能力就被追杀或官方盯上；不能把NPC写成工具人。",
                "volume_title": "第一卷",
                "volume_summary": "内测资格、桥段复刻、现实同步和第一笔人情债。",
                "arc_title": "入江湖",
                "arc_goal": "建立复刻机制、收益规则、失败代价和现实同步副作用。",
                "arc_climax": "一次复刻成功让现实身体出现延迟同步，同时欠下关键人情。",
                "arc_turn": "林默意识到游戏规则正在升维。",
                "approve_after_save": True,
            },
        )
        if saved.get("status") != "saved":
            print("skeleton save failed")
            print(saved)
            return 1

        created = prepare_production(session, book_id=book.id, chapter_number=1, platform="番茄小说")
        if created.status not in {"ready", "repaired_ready"} or not created.can_continue:
            print("prepare should create missing brief and become ready")
            print(created.to_dict())
            return 1
        chapter1 = session.query(Chapter).filter_by(book_id=book.id, chapter_number=1).first()
        brief1 = session.query(ChapterBrief).filter_by(chapter_id=chapter1.id).order_by(ChapterBrief.id.desc()).first() if chapter1 else None
        if not brief1:
            print("missing brief was not available after prepare")
            print(created.to_dict())
            return 1
        versions = session.query(ChapterVersion).count()
        if versions:
            print("prepare_production should not generate chapter versions")
            print(versions)
            return 1
        from app.services.planning import run_next_action

        preview = run_next_action(session, book_id=book.id, chapter_number=1, preview_only=True)
        if preview.status != "preview" or preview.action not in {"draft_chapter", "create_chapter_brief", "generate_chapter_samples"}:
            print("preview_only should report next action without executing")
            print(preview)
            return 1
        versions_after_preview = session.query(ChapterVersion).count()
        if versions_after_preview:
            print("preview_only created chapter versions")
            print(versions_after_preview)
            return 1

        chapter2 = session.query(Chapter).filter_by(book_id=book.id, chapter_number=2).first()
        chapter1 = session.query(Chapter).filter_by(book_id=book.id, chapter_number=1).first()
        if chapter1:
            stable1 = ChapterVersion(
                chapter_id=chapter1.id,
                version_number=1,
                title="第一章",
                content="第一章稳定正文。" * 600,
                status="reviewed_pass",
                source="regression:stable_previous",
            )
            session.add(stable1)
            session.flush()
        if not chapter2:
            chapter2 = Chapter(book_id=book.id, chapter_number=2, title="第二章", status="planned")
            session.add(chapter2)
            session.flush()
        session.add(
            ChapterBrief(
                chapter_id=chapter2.id,
                goal="第2章：写陈默确认《大江湖》不是机械游戏。",
                required_beats="修订合同:\n- 修复质检问题：visual_underdeveloped\n- 原始机器修订建议：不要改结构",
                constraints="依据质检报告补足动作。",
                status="revision_ready",
            )
        )
        session.flush()

        repaired = prepare_production(session, book_id=book.id, chapter_number=2, platform="番茄小说")
        if repaired.status != "repaired_ready" or not repaired.auto_fixed or not repaired.can_continue:
            print("stale brief should be auto repaired and ready")
            print(repaired.to_dict())
            return 1
        latest = session.query(ChapterBrief).filter_by(chapter_id=chapter2.id).order_by(ChapterBrief.id.desc()).first()
        text = "\n".join([latest.goal, latest.required_beats, latest.constraints])
        if any(marker in text for marker in ("陈默", "大江湖", "修订合同:", "依据质检报告", "原始机器修订建议")):
            print("repaired brief retained legacy text")
            print(text)
            return 1
        if not all(marker in text for marker in ("林默", "江湖志", "桥段复刻", "现实同步")):
            print("repaired brief did not use current book context")
            print(text)
            return 1

    print("production-router-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
