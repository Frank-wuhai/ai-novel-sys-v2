from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import session_scope
from app.services.canon import add_power_system, add_world_rule
from app.services.context_contamination import audit_context_contamination
from app.services.production import create_book, create_foundation, seed_prompts
from regression_db import isolated_database


def main() -> int:
    database_url = isolated_database("context-generic-power-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Generic Power Regression", genre="网游武侠", platform="番茄小说")
        create_foundation(
            session,
            book_id=book.id,
            premise="大学生陈默获得全真虚拟现实武侠网游《江湖》内测资格，激活剧情演绎能力。",
            reader_promise="看陈默用剧情演绎能力复刻经典桥段，获得奖励并承担失败代价。",
            world_engine="《江湖》是武侠向仙侠逐步升维的真实网游世界。",
            protagonist_engine="陈默必须观察现场、判断人物关系，再决定是否演绎桥段。",
            conflict_engine="长期压力来自游戏升维、现实同步、人物关系债和玩家竞争。",
        )
        add_power_system(
            session,
            book_id=book.id,
            name="核心能力",
            rules="剧情演绎能力按最新骨架执行。",
            costs="失败降低好感度且无奖励。",
            limits="不能强迫参演人员配合。",
        )
        add_world_rule(session, book_id=book.id, category="新版作品设定", rule_text="《江湖》规则以最新骨架为准。")
        canon = "主角陈默。\n核心能力：剧情演绎能力按最新骨架执行。\n《江湖》规则以最新骨架为准。"
        brief = "第1章写陈默获得《江湖》内测资格，并首次触发剧情演绎能力。"
        report = audit_context_contamination(
            session,
            book_id=book.id,
            chapter_number=1,
            brief_text=brief,
            canon_context=canon,
        )
        if not report.passed:
            print("generic power name should not be treated as stale")
            print(report.to_dict())
            return 1

        add_power_system(session, book_id=book.id, name="旧外挂", rules="旧外挂不在新版骨架中。")
        contaminated = audit_context_contamination(
            session,
            book_id=book.id,
            chapter_number=1,
            brief_text=brief,
            canon_context=canon + "\n旧外挂",
        )
        if contaminated.passed or "旧外挂" not in "；".join(contaminated.blockers):
            print("real stale power name should still be blocked")
            print(contaminated.to_dict())
            return 1

    print("context-generic-power-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
