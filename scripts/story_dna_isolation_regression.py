from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.models.entities import StoryBible
from app.services.production import create_book, seed_prompts
from app.services.story_dna import extract_story_dna_block, strip_story_dna_blocks
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action, _story_skeleton_payload


DNA_VARIANT = """## 作品 DNA
- 题材主味: 网游武侠向仙侠升维。
- 核心钩子: 剧情演绎触发任务，桥段复刻越像奖励越好。
- 金手指机制: 复刻程度由场景条件、人物因果和临场动作共同判定。
- 章节发动机库: 桥段复刻变形；玩家竞争；现实异常外泄
文风指南：明快热闹，不要冷硬压缩。"""


def main() -> int:
    database_url = isolated_database("story-dna-isolation-regression")
    assert extract_story_dna_block(style_guide=DNA_VARIANT)
    stripped = strip_story_dna_blocks("文风开头\n\n" + DNA_VARIANT + "\n\n禁忌规则：不要机械任务链")
    if "作品 DNA" in stripped or "桥段复刻变形" in stripped:
        print("variant story dna was not stripped")
        print(stripped)
        return 1
    if "文风开头" not in stripped or "文风指南" not in stripped or "禁忌规则" not in stripped:
        print("non-dna text was stripped by mistake")
        print(stripped)
        return 1

    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Story DNA Isolation Regression", genre="网游武侠", platform="番茄小说")
        result = _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "主角获得全真虚拟现实武侠网游内测资格，激活剧情演绎系统。",
                "reader_promise": "看主角复刻经典桥段，在游戏与现实同步里拿收益、还因果。",
                "world_engine": "武侠网游逐步向仙侠升维，游戏收益会以受限形式同步现实。",
                "protagonist_engine": "主角靠剧情演绎破局，但必须处理好感下降和失败空奖。",
                "conflict_engine": "桥段复刻、玩家竞争、现实异常和升维规则逐层升级。",
                "style_guide": "明快热闹，有玩家吐槽。\n\n" + DNA_VARIANT,
                "forbidden_rules": "禁止阴冷悬疑默认化。\n\n作品DNA：不要把系统面板写成万能答案。\n文风指南：保留动作过程。",
                "aesthetic_profile": "审美画像：热闹江湖。\n\n" + DNA_VARIANT,
                "story_dna": "",
                "volume_summary": "内测入局、演绎触发、第一笔现实同步异常。",
                "arc_goal": "建立金手指、收益、代价和现实反馈。",
                "arc_climax": "主角高相似复刻桥段，现实身体出现异常反馈。",
                "arc_turn": "他发现游戏不是普通服务器。",
                "approve_after_save": True,
            },
        )
        if result.get("status") != "saved":
            print("save failed")
            print(result)
            return 1
        payload = _story_skeleton_payload(session, book_id=book.id)
        bible = payload.get("story_bible") or {}
        story_dna = bible.get("story_dna") or ""
        style = bible.get("style_guide") or ""
        forbidden = bible.get("forbidden_rules") or ""
        aesthetic = bible.get("aesthetic_profile") or ""
        if "桥段复刻变形" not in story_dna or "章节发动机库" not in story_dna:
            print("story dna was lost during save/display")
            print(bible)
            return 1
        leaked_fields = {
            "style_guide": style,
            "forbidden_rules": forbidden,
            "aesthetic_profile": aesthetic,
        }
        leaks = {key: value for key, value in leaked_fields.items() if "作品DNA" in value or "作品 DNA" in value or "桥段复刻变形" in value}
        if leaks:
            print("story dna leaked into display fields")
            print(leaks)
            return 1
        raw_bible = session.get(StoryBible, bible["id"])
        raw_forbidden = raw_bible.forbidden_rules or ""
        if "作品DNA" in raw_forbidden or "作品 DNA" in raw_forbidden:
            print("story dna should not be stored in forbidden_rules")
            print(raw_forbidden)
            return 1

    print("story-dna-isolation-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
