from __future__ import annotations

import json

from app.db.session import session_scope
from app.services.aesthetic_profile import PROFILE_START, apply_aesthetic_profile, profile_from_story_text, story_bible_display_fields, strip_aesthetic_profile_blocks
from app.services.production import create_book, create_chapter_brief, create_foundation
from app.services.production_packet import build_chapter_production_packet
from app.services.story import get_story_bible
from regression_db import isolated_database


def main() -> int:
    isolated_database("aesthetic-profile-regression")
    failures: list[str] = []
    variant_profile = """## 审美画像
- 笔触: 明快、有画面、有角色声线。
- 氛围: 热闹江湖、烟火气和轻松吐槽。
- 路线: 奇遇冒险、主角主动破局。
文风指南：动作过程要写具体。"""
    extracted_variant = profile_from_story_text(style_guide=variant_profile, forbidden_rules="")
    stripped_variant = strip_aesthetic_profile_blocks("普通文风。\n\n" + variant_profile + "\n\n禁忌规则：不要阴冷悬疑。")
    if PROFILE_START not in extracted_variant or "热闹江湖" not in extracted_variant:
        failures.append("variant_profile_not_extracted")
    if "审美画像" in stripped_variant or "热闹江湖" in stripped_variant:
        failures.append("variant_profile_not_stripped")
    if "普通文风" not in stripped_variant or "文风指南" not in stripped_variant or "禁忌规则" not in stripped_variant:
        failures.append("variant_profile_strip_removed_non_profile_text")
    with session_scope() as session:
        book = create_book(session, title="Aesthetic Profile Regression", genre="玄幻脑洞", platform="manual")
        create_foundation(
            session,
            book_id=book.id,
            premise="主角进入真实武侠游戏世界，用机敏和武侠套路知识争取奇遇。",
            reader_promise="热闹江湖、奇遇冒险、主角主动破局。",
            world_engine="江湖有门派、集市、武馆、镖局和山门规矩。",
            protagonist_engine="主角嘴硬、会吐槽、敢试探，靠观察和话术破局。",
            conflict_engine="冲突来自门派场面、人情交锋和奇遇选择，不靠阴冷旧案。",
        )
        block = apply_aesthetic_profile(
            session,
            book_id=book.id,
            prose_style="明快、有画面、有角色声线，不长期冷硬克制。",
            atmosphere="热闹江湖、烟火气、门派场面、轻松吐槽。",
            story_route="奇遇冒险、主角主动破局、招式身法和人情交锋给爽点回报。",
            must_have="每章有正向回报：见识、招式、资源、关系或主动权。",
            must_not="不要默认阴冷悬疑、旧案追查、血迹盘问、旧债逼迫。",
        )
        brief = create_chapter_brief(
            session,
            book_id=book.id,
            chapter_number=1,
            goal="第1章用热闹江湖场面建立主角和奇遇入口。",
            required_beats="集市或武馆开场；主角主动试探；小爽点回报；章末新机会",
            constraints="不要写成旧案悬疑。",
        )
        bible = get_story_bible(session, book_id=book.id)
        bible_style = bible.style_guide if bible else ""
        bible_forbidden = bible.forbidden_rules if bible else ""
        display = story_bible_display_fields(
            style_guide=(bible_style or "") + "\n\n" + variant_profile,
            forbidden_rules=(bible_forbidden or "") + "\n\n审美画像：题材主味热闹，不能压成冷硬悬疑。\n禁忌规则：保留爽点回报。",
        )
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
    if PROFILE_START not in block:
        failures.append("profile_block_missing_marker")
    if PROFILE_START not in (bible_style or ""):
        failures.append("bible_style_missing_profile")
    if PROFILE_START in strip_aesthetic_profile_blocks(bible_forbidden or ""):
        failures.append("bible_forbidden_should_not_store_profile")
    if "审美画像" in display["style_guide"] or "热闹江湖" in display["style_guide"]:
        failures.append("display_style_should_not_duplicate_profile")
    if "审美画像" in display["forbidden_rules"] or "题材主味热闹" in display["forbidden_rules"]:
        failures.append("display_forbidden_should_not_duplicate_profile")
    if "热闹江湖" not in display["aesthetic_profile"]:
        failures.append("display_missing_profile")
    if "热闹江湖" in packet.constraints:
        failures.append("packet_constraints_should_not_duplicate_aesthetic_profile")
    if "奇遇冒险" not in packet.director_sheet:
        failures.append("packet_missing_aesthetic_profile")
    if packet.director_sheet.count(PROFILE_START) != 1:
        failures.append("director_sheet_should_include_profile_once")
    if not packet.audit.get("aesthetic_profile"):
        failures.append("packet_audit_missing_profile_flag")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "packet_audit": packet.audit,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
