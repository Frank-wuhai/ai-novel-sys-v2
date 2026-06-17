from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import MarketSignal, PlatformFeedback, StoryBible, StoryFoundation
from app.services.agent_plan_intelligence import index_book_knowledge
from app.services.production import create_book, seed_prompts
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action, _story_skeleton_payload


BAD_SKELETON = {
    "premise": "主角是横店龙套演员，靠演技进入真实武侠世界骗取奇遇。",
    "reader_promise": "看主角靠演员经验和表演让NPC配合他刷奇遇。",
    "world_engine": "大江湖是真实武侠世界，但主角可以制造坠崖桥段刷奖励。",
    "protagonist_engine": "主角靠演技和龙套经验解决所有危机。",
    "conflict_engine": "不断制造桥段骗取资源。",
    "forbidden_rules": "",
    "style_guide": "",
    "volume_title": "第一卷",
    "volume_summary": "制造桥段。",
    "arc_title": "开局破局",
    "arc_goal": "刷奇遇。",
    "arc_climax": "坠崖奇遇。",
    "arc_turn": "继续刷。",
}

AESTHETIC_IDEA = (
    "本书不是悬疑武侠，不要继续用阴冷、压抑、旧案追查、血迹盘问、旧债悬疑路线。"
    "我要有真实质感的武侠爽文/奇遇冒险：热闹江湖、烟火气、门派场面、招式身法、轻松吐槽和人情交锋。"
)


def main() -> int:
    database_url = isolated_database("skeleton-repair-dashboard-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Skeleton Repair Dashboard Regression", genre="玄幻都市", platform="manual")
        book_id = book.id

    with session_scope() as session:
        draft = _perform_action(
            session,
            {"action": "repair_story_skeleton_draft", "book_id": book_id, "current_skeleton": BAD_SKELETON},
        )
        if draft.get("status") != "completed" or not draft.get("skeleton") or not draft.get("passed"):
            print("repair draft action failed")
            print(draft)
            return 1
        if session.query(StoryFoundation).filter_by(book_id=book_id).count() != 0:
            print("repair draft unexpectedly wrote story foundation")
            return 1

    with session_scope() as session:
        preview_skeleton = dict(draft.get("skeleton") or {})
        preview_skeleton["premise"] = "预览稿所见即所得：主角进入真实江湖，能力必须经由选择、代价和人物因果兑现。"
        applied = _perform_action(
            session,
            {
                "action": "apply_story_skeleton_repair",
                "book_id": book_id,
                "current_skeleton": BAD_SKELETON,
                "repaired_skeleton": preview_skeleton,
            },
        )
        if applied.get("status") != "applied" or int(applied.get("approved_count") or 0) < 5:
            print("apply repair action failed")
            print(applied)
            return 1
        foundation = session.query(StoryFoundation).filter_by(book_id=book_id).order_by(StoryFoundation.id.desc()).first()
        approvals = session.query(PlatformFeedback).filter_by(book_id=book_id, metric_name="skeleton_approval").count()
        if not foundation or not foundation.premise or approvals < 5:
            print("apply repair did not persist foundation and approvals")
            return 1
        if foundation.premise != preview_skeleton["premise"]:
            print("apply repair did not persist the exact preview skeleton")
            print(foundation.premise)
            return 1
        research_pack = _perform_action(
            session,
            {
                "action": "create_market_research_pack",
                "book_id": book_id,
                "market_query": "番茄小说 玄幻都市 爆款 开篇 爽点 避雷",
                "platform": "番茄小说",
            },
        )
        if research_pack.get("status") != "created" or not research_pack.get("artifact_path"):
            print("market research pack action failed")
            print(research_pack)
            return 1
        ingested = _perform_action(
            session,
            {
                "action": "ingest_market_research_results",
                "book_id": book_id,
                "result_json": (
                    '{"results":[{"title":"sample","url":"https://example.com/sample",'
                    '"snippet":"玄幻都市开篇需要明确爽点和章末钩子",'
                    '"signals":["番茄小说 玄幻都市 爆款更强调开篇爽点、读者追读钩子和明确避雷。"],'
                    '"reliability":3,"confidence":86}]}'
                ),
            },
        )
        if ingested.get("status") != "ingested" or len(ingested.get("market_signal_ids") or []) < 1:
            print("market research ingest action failed")
            print(ingested)
            return 1
        session.add(
            MarketSignal(
                genre="玄幻都市",
                signal_text="番茄小说 玄幻都市 近期爆款更强调开篇爽点、读者追读钩子和明确避雷。",
                confidence=86,
            )
        )
        session.add(
            MarketSignal(
                genre="玄幻都市",
                signal_text="玄幻都市读者更期待主角主动选择、可见代价和章末新悬念。",
                confidence=82,
            )
        )
        session.add(
            MarketSignal(
                genre="玄幻都市",
                signal_text="开篇需要用场景行动兑现设定，避免长段解释和慢热铺垫。",
                confidence=80,
            )
        )
        index_book_knowledge(session, book_id=book_id, dry_run=True, reset=True)
        readiness_repair = _perform_action(
            session,
            {"action": "repair_readiness_gate", "book_id": book_id, "chapter_number": 1, "platform": "番茄小说"},
        )
        if readiness_repair.get("status") != "completed" or not isinstance(readiness_repair.get("steps"), list):
            print("readiness repair action failed")
            print(readiness_repair)
            return 1
        payload = _story_skeleton_payload(session, book_id=book_id)
        governance = payload.get("governance") or {}
        source_hits = governance.get("source_hits") or {}
        dimensions = governance.get("dimensions") or {}
        evidence_summary = governance.get("evidence_summary") or []
        if not source_hits.get("agent_plan_evidence"):
            print("agent plan evidence was not attached to skeleton governance")
            print(governance)
            return 1
        if "agent_plan_evidence" not in dimensions:
            print("agent plan evidence dimension missing")
            print(governance)
            return 1
        if not any("市场信号" in item for item in evidence_summary):
            print("agent plan market evidence summary missing")
            print(governance)
            return 1
        market_repair = _perform_action(
            session,
            {
                "action": "repair_story_skeleton_draft",
                "book_id": book_id,
                "revision_idea": AESTHETIC_IDEA,
                "current_skeleton": BAD_SKELETON,
            },
        )
        repaired_skeleton = market_repair.get("skeleton") or {}
        market_context = market_repair.get("market_context") or {}
        repair_text = "\n".join(str(value or "") for value in repaired_skeleton.values())
        if int(market_context.get("signal_count") or 0) < 1:
            print("market repair did not read market signals")
            print(market_repair)
            return 1
        if "平台读者预期" not in repair_text or "章末钩子" not in repair_text:
            print("market repair did not apply platform reader expectations")
            print(market_repair)
            return 1
        style_guide = str(repaired_skeleton.get("style_guide") or "")
        aesthetic_profile = str(repaired_skeleton.get("aesthetic_profile") or "")
        if "【作品审美画像】" in style_guide or "【作品DNA】" in style_guide:
            print("repair draft leaked profile/dna blocks into style guide")
            print(market_repair)
            return 1
        if "真实质感的武侠爽文" not in aesthetic_profile or "不要继续用阴冷" not in aesthetic_profile:
            print("repair draft did not preserve author aesthetic idea in aesthetic_profile")
            print(market_repair)
            return 1
        applied_profile = _perform_action(
            session,
            {
                "action": "apply_story_skeleton_repair",
                "book_id": book_id,
                "revision_idea": AESTHETIC_IDEA,
                "current_skeleton": BAD_SKELETON,
            },
        )
        if applied_profile.get("status") != "applied":
            print("apply repair with aesthetic profile failed")
            print(applied_profile)
            return 1
        bible = session.query(StoryBible).filter_by(book_id=book_id).first()
        if not bible or "【作品审美画像】" not in (bible.style_guide or ""):
            print("applied repair did not write aesthetic profile to story bible")
            print(applied_profile)
            return 1
        display_payload = _story_skeleton_payload(session, book_id=book_id)
        display_skeleton = display_payload.get("story_bible") or {}
        if "【作品审美画像】" in str(display_skeleton.get("style_guide") or ""):
            print("story skeleton display leaked aesthetic profile into style guide")
            print(display_skeleton)
            return 1
        if "真实质感的武侠爽文" not in str(display_skeleton.get("aesthetic_profile") or ""):
            print("story skeleton display did not expose aesthetic profile separately")
            print(display_skeleton)
            return 1

    print("skeleton-repair-dashboard-regression: PASS")
    print(f"database={database_url}")
    print(f"book_id={book_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
