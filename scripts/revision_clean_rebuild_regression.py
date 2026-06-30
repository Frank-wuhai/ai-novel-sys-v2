from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief
from app.services.brief_sanitizer import sanitize_prompt_contract_text
from app.services.production import create_book, create_foundation, seed_prompts
from app.services.production_llm import _unit_flow_rejection_reason
from app.services.production_packet import build_chapter_production_packet
from app.services.quality import evaluate_chapter
from regression_db import isolated_database


def main() -> int:
    isolated_database("revision-clean-rebuild-regression")
    failures: list[str] = []

    raw_contract = "\n".join(
        [
            "clean_rebuild_contract@v1",
            "阅读评估重建第1章：以当前作品剧情承诺为准，旧稿 v195 只保留可用素材。",
            "触发原因：连续多轮修订均低于恢复底稿，切断旧合同循环",
            "重建素材来源：v193；允许替换失败开场、场景顺序和行动链。",
            "本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；核心能力必须通过行动触发并产生明确回报；章末出现改变下一章局面的具体变化。",
            "剧情基线：大学生林北获得全真武侠网游《万象江湖》内测资格，进入后激活桥段复刻。",
            "本轮只解决：删除重复拼接；前300字进入具体处境；章末钩子具体到动作。",
            "合同当前底稿：v193",
        ]
    )
    clean_contract = sanitize_prompt_contract_text(raw_contract)
    if "clean_rebuild_contract" in clean_contract or "触发原因" in clean_contract or "v193" in clean_contract:
        failures.append("prompt_contract_kept_recovery_metadata")
    if "本章剧情承诺" not in clean_contract or "修订目标" not in clean_contract:
        failures.append("prompt_contract_removed_story_commitments")

    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="干净重建回归", genre="网游武侠", platform="番茄小说")
        create_foundation(
            session,
            book_id=book.id,
            premise="大学生林北获得全真武侠网游《万象江湖》内测资格，激活桥段复刻能力。",
            reader_promise="看林北在真实江湖里用经典桥段破局，并把武功记忆带回现实。",
            world_engine="《万象江湖》像真实武侠世界，人物有利益、恐惧和门派关系。",
            protagonist_engine="林北靠观察、话术和临场表演触发桥段复刻。",
            conflict_engine="游戏规则、真实江湖和现实同步不断冲突。",
        )
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第1章干净重建：按当前作品剧情承诺重新组织可读正文，旧稿 v193 只作素材参考。",
            required_beats=raw_contract,
            constraints="revision_mode:rewrite\n禁止复刻失败稿的重复开头。\n必须输出完整小说正文。",
            status="revision_ready",
        )
        session.add(brief)
        session.flush()
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            mode="revision",
            revision_goal=brief.goal,
            revision_required_beats=brief.required_beats,
            revision_constraints=brief.constraints,
            rewrite_mode=True,
            chapter_id=chapter.id,
            chapter_brief_id=brief.id,
        )
        forbidden = ("clean_rebuild_contract", "触发原因", "合同当前底稿", "v193", "恢复底稿")
        if any(marker in packet.director_sheet for marker in forbidden):
            failures.append("director_sheet_contains_recovery_metadata")
        if "本章剧情承诺" not in packet.director_sheet:
            failures.append("director_sheet_missing_story_commitment")

    rejection = _unit_flow_rejection_reason(
        before={"score": 63, "unit_count": 8},
        after={"score": 64, "unit_count": 9},
        before_chars=4200,
        after_chars=4300,
        min_chars=3000,
        threshold=70,
        content="林北醒来。" * 300,
    )
    if "below threshold" not in rejection:
        failures.append("low_unit_flow_repair_was_not_rejected")
    repeated_unit = (
        "林北听见树林里传来打斗声，他扶住树干，先观察地形，再决定怎样开口诈住对方。"
        "他已经看清了三个人的站位，却又一次告诉自己先看清局面，再想怎么活过这一炷香。"
    )
    repeated_rejection = _unit_flow_rejection_reason(
        before={"score": 72, "unit_count": 8},
        after={"score": 74, "unit_count": 8},
        before_chars=3600,
        after_chars=3700,
        min_chars=3000,
        threshold=70,
        content=f"{repeated_unit}\n\n{repeated_unit}",
    )
    if "repeated" not in repeated_rejection:
        failures.append("repeated_segment_repair_was_not_rejected")

    ui_text = "林北盯着视野右上角，那里像系统提示的边框一样闪了一下，但没有替他解决眼前的刀光。"
    ui_quality = evaluate_chapter(ui_text * 8, min_chars=40)
    if any(str(issue).startswith("forbidden_marker: 系统提示") for issue in ui_quality.issues):
        failures.append("game_ui_system_hint_was_hard_blocked")
    meta_quality = evaluate_chapter("系统提示词：你是一个写作模型。" * 8, min_chars=20)
    if not any(str(issue).startswith("forbidden_marker: 系统提示") for issue in meta_quality.issues):
        failures.append("meta_system_prompt_leak_not_blocked")

    if failures:
        print(json.dumps({"status": "FAIL", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("revision-clean-rebuild-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
