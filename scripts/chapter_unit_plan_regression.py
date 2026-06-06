from __future__ import annotations

import json
from pathlib import Path

from app.db import session as db_session
from app.db.base import Base
from app.db.session import configure_database, session_scope
from app.models.entities import Book, Chapter, ChapterBrief
from app.services.chapter_unit_plans import align_chapter_unit_plan, ensure_chapter_unit_plan, format_chapter_unit_plan
from app.services.chapter_units import evaluate_chapter_units


ROOT = Path(__file__).resolve().parents[1]
TEST_DB = ROOT / "data/chapter-unit-plan-regression.db"


def main() -> int:
    if TEST_DB.exists():
        TEST_DB.unlink()
    configure_database("sqlite:///data/chapter-unit-plan-regression.db")
    Base.metadata.create_all(db_session.engine)
    with session_scope() as session:
        book = Book(title="Unit Plan Regression", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第二章")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="承接上一章追兵逼近，主角通过试探和交易拿到药王谷线索。",
            required_beats="承接前章后果；主角先误判来人身份；用碎玉试探规矩；交易换来线索；追兵逼近；主角付出人情代价；章末出现药王谷旧债",
            constraints="正文3000字以上；真实武侠世界；设定通过动作和对话呈现；章末钩子由本章行动导致",
            status="ready",
        )
        session.add(brief)
        session.flush()
        plan = ensure_chapter_unit_plan(
            session,
            chapter_id=chapter.id,
            chapter_brief_id=brief.id,
            chapter_number=chapter.chapter_number,
            goal=brief.goal,
            required_beats=brief.required_beats,
            constraints=brief.constraints,
            previous_chapter_context="上一章结尾，门外脚步停住，有人报出主角名字。",
            mode="draft",
        )
        payload = json.loads(plan.plan_json)
        prompt_block = format_chapter_unit_plan(plan)
        report = evaluate_chapter_units(_sample_chapter()).to_dict()
        alignment = align_chapter_unit_plan(plan, report)
        plan_id = plan.id
    failures: list[str] = []
    if payload.get("target_unit_count", 0) < 6:
        failures.append("target_unit_count_low")
    if len(payload.get("units") or []) < 6:
        failures.append("units_low")
    if "拟人化小单元计划" not in prompt_block:
        failures.append("missing_prompt_block")
    if int(alignment.get("alignment_score") or 0) < 70:
        failures.append(f"alignment_low:{alignment.get('alignment_score')}")
    result = {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "plan_id": plan_id,
        "target_unit_count": payload.get("target_unit_count"),
        "prompt_chars": len(prompt_block),
        "alignment": alignment,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _sample_chapter() -> str:
    unit = """
陈默先想弄清门外是谁，只能把油灯压低。他听见脚步停在门槛前，却没有立刻开门，先退到桌边摸起那半枚碎玉。门外的人逼得很近，呼吸里带着药味，像受了伤，也像在试探。他原本以为是追兵，却发现碎玉边缘的血正和白日药铺里的气味相同。这让他明白，对方不是来杀他，而是把药王谷的旧债送到他手里。于是他没有逃，反而隔着门问：“你拿什么换命？”

刚才那句话落下，门外的人沉默了一瞬。陈默决定先把价码压住，接着把碎玉推到门缝边，只露出一半。对方伸手来接，指节却在灯下抖了一下，露出青色针孔。陈默看见针孔才知道这不是普通伤势，而是谷中用来催债的青瘴针。门外人低声说规矩不能破，这让陈默必须选择：收下人情，还是把线索推出门外。

于是陈默按住袖中的短刀，没有马上答应。他先问药王谷要找谁，再让对方说出追兵人数。门外人被逼得咳出血，却仍把一张湿纸塞进屋里。纸上只有三个名字，其中一个正是刚才报出陈默姓名的人。陈默终于拿到线索，也欠下一条命债。没等他再问，楼下忽然响起第二道脚步声，下一单元必须承接追兵已经上楼的后果。
""".strip()
    return "\n\n".join([unit, unit, unit])


if __name__ == "__main__":
    raise SystemExit(main())
