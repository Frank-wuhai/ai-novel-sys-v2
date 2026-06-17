from __future__ import annotations

import json

from app.db.session import session_scope
from app.services.aesthetic_profile import apply_aesthetic_profile
from app.services.book_aesthetic_standard import build_book_aesthetic_standard
from app.services.paragraph_aesthetic import evaluate_paragraph_aesthetic
from app.services.production import create_book, create_chapter_brief, create_foundation
from app.services.production_packet import build_chapter_production_packet
from regression_db import isolated_database


def main() -> int:
    isolated_database("book-aesthetic-standard-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = create_book(session, title="万象江湖", genre="网游武侠仙侠", platform="manual")
        create_foundation(
            session,
            book_id=book.id,
            premise="主角进入全真虚拟现实武侠游戏，桥段演绎能力让江湖桥段和现实同步升维。",
            reader_promise="热闹江湖、桥段复刻、武侠升仙侠、现实科技碰撞，每章有爽点和新门槛。",
            world_engine="游戏世界从武侠江湖逐步升维到仙侠秩序。",
            protagonist_engine="主角靠观察、演绎、试探、人情和代价获得奖励。",
            conflict_engine="冲突来自江湖门槛、桥段复刻偏差、现实同步和升维规则变化。",
        )
        apply_aesthetic_profile(
            session,
            book_id=book.id,
            prose_style="明快、热闹、有画面、有角色声线，不能冷硬装深沉。",
            atmosphere="江湖烟火、门派场面、奇遇冒险和轻松吐槽。",
            story_route="桥段演绎、武侠升维仙侠、主角主动破局。",
            must_have="每章都有招式、关系、见识、资源或主动权回报。",
            must_not="不要阴冷悬疑、机构关注、追杀模板和一句话概括氛围。",
        )
        brief = create_chapter_brief(
            session,
            book_id=book.id,
            chapter_number=1,
            goal="第1章写主角进入万象江湖，遇到第一个桥段演绎门槛。",
            required_beats="热闹开场；主角主动试探；桥段演绎触发；奖励和代价落地；章末升维线索",
            constraints="3000字以上；不要系统面板直接解题；不要冷硬悬疑腔。",
        )
        standard = build_book_aesthetic_standard(session, book_id=book.id)
        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            chapter_id=brief.chapter_id,
            chapter_brief_id=brief.id,
        )
        paragraph_report = evaluate_paragraph_aesthetic(_bad_paragraph_text()).to_dict()

    if "江湖要有烟火" not in standard.prompt_block():
        failures.append("standard_missing_wuxia_flavor")
    if "作品级审美标尺" not in packet.director_sheet:
        failures.append("packet_missing_aesthetic_standard")
    if "好稿记忆" in packet.director_sheet and not packet.book_aesthetic_standard.get("taste_memory"):
        failures.append("packet_rendered_empty_taste_memory")
    plan = packet.chapter_unit_plan
    acceptance = "\n".join(str(item) for item in plan.get("acceptance") or [])
    if "审美密度" not in acceptance or "禁止笔触" not in acceptance:
        failures.append("unit_plan_missing_aesthetic_acceptance")
    if paragraph_report.get("status") != "attention" or not paragraph_report.get("revision_targets"):
        failures.append("paragraph_aesthetic_did_not_flag_abstract_text")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "standard": standard.to_dict(),
                "packet_audit": packet.audit,
                "paragraph_report": paragraph_report,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


def _bad_paragraph_text() -> str:
    paragraph = (
        "夜色很冷，沉默像某种无形的压迫落在所有人心头。林默感觉到一种说不清的危险，"
        "仿佛黑暗深处有什么东西正在逼近。他没有多说，只是意识到局面已经变得复杂。"
        "这种复杂带着冰冷意味，让人本能地不安。"
    )
    return "\n\n".join([paragraph, paragraph, paragraph])


if __name__ == "__main__":
    raise SystemExit(main())
