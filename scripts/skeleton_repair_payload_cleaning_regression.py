from __future__ import annotations

import json
from datetime import datetime

from app.db.session import session_scope
from app.models.entities import Book
from app.services.aesthetic_profile import build_aesthetic_profile_block
from app.services.story import upsert_story_bible
from app.services.story_dna import build_story_dna_block
from regression_db import isolated_database
from run_local_dashboard import (
    _current_story_skeleton_values,
    _preserve_skeleton_identity_fields_in_payload,
    _sanitize_skeleton_repair_payload,
    _update_story_skeleton,
)


def main() -> int:
    isolated_database("skeleton-repair-payload-cleaning-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title=f"skeleton-cleaning-{datetime.utcnow().timestamp()}", genre="网游武侠", target_platform="test")
        session.add(book)
        session.flush()
        dna = build_story_dna_block(
            genre_flavor="网游武侠",
            prose_style="轻松明快",
            core_hook="桥段复刻",
            goldfinger="剧情演绎",
            world_rule="拟真 NPC",
            conflict="现实同步",
        )
        aesthetic = build_aesthetic_profile_block(
            prose_style="轻松明快，有现场笑点和招式画面",
            atmosphere="热闹江湖、烟火气、游戏奇遇",
            story_route="网游武侠向仙侠升维的爽文冒险",
            must_have="主角主动试错，经典桥段要被真实人物因果重新解释",
            must_not="不要阴冷悬疑，不要门派追杀或现实机构盯上当默认代价",
        )
        upsert_story_bible(
            session,
            book_id=book.id,
            positioning="大学生获得内测资格，在拟真武侠网游里用桥段复刻能力变强。",
            reader_promise="看主角即兴演绎经典桥段，拿到奖励也付出代价。",
            main_plot="现实同步和游戏关系网逐步升级。",
            protagonist_arc="主角从投机复刻变成会尊重真实人物因果。",
            power_curve="桥段复刻必须符合现场证据和人物关系。",
            forbidden_rules="不能机械任务面板；不能 NPC 工具人。",
            style_guide="轻松幽默，市井江湖气。\n\n" + aesthetic + "\n\n" + dna,
            status="draft",
        )
        session.flush()

        current = _current_story_skeleton_values(session, book_id=book.id)
        if "【作品DNA】" in current.get("style_guide", ""):
            failures.append("current_values_style_contains_dna")
        if "【作品DNA】" not in current.get("story_dna", ""):
            failures.append("current_values_missing_story_dna")
        if "热闹江湖" not in current.get("aesthetic_profile", ""):
            failures.append("current_values_missing_aesthetic_profile")

        payload = _sanitize_skeleton_repair_payload(
            {
                "skeleton": {
                    **current,
                    "style_guide": current.get("style_guide", "") + "\n\n" + current.get("story_dna", ""),
                }
            }
        )
        repaired = payload.get("skeleton") or {}
        if "【作品DNA】" in repaired.get("style_guide", ""):
            failures.append("repair_payload_style_contains_dna")

        misplaced_payload = _sanitize_skeleton_repair_payload(
            {
                "skeleton": {
                    **current,
                    "style_guide": "\n".join(
                        [
                            "文风指南：动作要写具体，保留轻松吐槽。",
                            "读者承诺：每章都有桥段复刻带来的收益、误差或关系变化。",
                            "世界规则：游戏收益同步现实，但必须经过身体适应和人物因果校验。",
                            "禁忌规则：不要把现实机构盯上写成默认压力。",
                        ]
                    ),
                    "reader_promise": "",
                    "world_engine": "",
                    "forbidden_rules": "",
                }
            }
        )
        misplaced = misplaced_payload.get("skeleton") or {}
        if "读者承诺" in misplaced.get("style_guide", "") or "世界规则" in misplaced.get("style_guide", "") or "禁忌规则" in misplaced.get("style_guide", ""):
            failures.append("labeled_sections_not_removed_from_source_field")
        if "桥段复刻带来的收益" not in misplaced.get("reader_promise", ""):
            failures.append("reader_promise_not_relocated")
        if "收益同步现实" not in misplaced.get("world_engine", ""):
            failures.append("world_engine_not_relocated")
        if "现实机构盯上" not in misplaced.get("forbidden_rules", ""):
            failures.append("forbidden_rules_not_relocated")
        if "动作要写具体" not in misplaced.get("style_guide", ""):
            failures.append("source_field_own_content_lost")

        preserved_payload = _preserve_skeleton_identity_fields_in_payload(
            {"skeleton": {key: value for key, value in repaired.items() if key != "aesthetic_profile"}},
            current,
        )
        preserved = preserved_payload.get("skeleton") or {}
        if "热闹江湖" not in preserved.get("aesthetic_profile", ""):
            failures.append("repair_payload_lost_aesthetic_profile")

        _update_story_skeleton(
            session,
            book=book,
            payload={
                **repaired,
                "aesthetic_profile": "",
                "story_dna": "",
            },
        )
        reread = _current_story_skeleton_values(session, book_id=book.id)
        if "【作品DNA】" in reread.get("style_guide", ""):
            failures.append("saved_style_display_contains_dna")
        if "热闹江湖" not in reread.get("aesthetic_profile", ""):
            failures.append("saved_lost_existing_aesthetic_profile")
        if "【作品DNA】" not in reread.get("story_dna", ""):
            failures.append("saved_lost_existing_story_dna")

    print(json.dumps({"status": "pass" if not failures else "fail", "failures": failures}, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
