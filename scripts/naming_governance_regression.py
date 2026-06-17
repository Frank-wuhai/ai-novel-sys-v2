from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Character, PowerSystem, StoryBible, StoryFoundation, WorldRule
from app.services.naming_governance import (
    allowed_naming_terms,
    build_naming_governance_block,
    evaluate_naming_governance,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.quality import evaluate_chapter
from regression_db import isolated_database


def main() -> int:
    isolated_database("naming-governance-regression")
    failures: list[str] = []
    cleanup_ids: list[int] = []
    with session_scope() as session:
        book = Book(title=f"命名治理回归-{datetime.utcnow().timestamp()}", genre="真实武侠", target_platform="test")
        session.add(book)
        session.flush()
        cleanup_ids.append(book.id)
        session.add(
            StoryFoundation(
                book_id=book.id,
                premise="陈默进入真实武侠江湖，卷入梅家镖局和旧药王谷旧案。",
                reader_promise="看主角用现场证据和代价活下去。",
                world_engine="梅家镖局、旧药王谷、青河剑派都必须按真实势力写。",
                protagonist_engine="陈默靠观察和承担后果推进。",
                conflict_engine="梅家血印牵出旧账。",
            )
        )
        session.add(
            StoryBible(
                book_id=book.id,
                positioning="真实江湖悬疑。",
                main_plot="围绕梅家血印查清旧药王谷灭门案。",
                power_curve="锈铜铃只能提示危险，不能解决冲突。",
                forbidden_rules="不得生造一堆玄幻名词。",
                style_guide="名称朴素、可记、和利益关系绑定。",
            )
        )
        session.add(Character(book_id=book.id, name="陈默", role="主角", personality="谨慎", ability="观察"))
        session.add(WorldRule(book_id=book.id, category="江湖势力", rule_text="青河剑派欠梅家镖局一笔旧债。"))
        session.add(PowerSystem(book_id=book.id, name="锈铜铃", rules="只在危险临近时发冷", costs="误判会伤身", limits="不能给答案"))
        session.flush()

        allowed = allowed_naming_terms(session, book_id=book.id)
        for expected in ("梅家镖局", "旧药王谷", "青河剑派", "梅家血印", "锈铜铃"):
            if expected not in allowed:
                failures.append(f"allowed_term_missing:{expected}")

        block = build_naming_governance_block(session, book_id=book.id, chapter_number=1)
        if "不要临时生造人名、地名、物品名" not in block:
            failures.append("naming_prompt_rule_missing")

        packet = build_chapter_production_packet(
            session,
            book=book,
            chapter_number=1,
            goal="第1章让陈默发现梅家血印。",
            required_beats="药铺见血印；青河剑派旧债露出；章末锈铜铃发冷。",
            constraints="不要生造新门派、新法器或新地名。",
            mode="draft",
        )
        if "命名治理" not in packet.director_sheet:
            failures.append("production_packet_missing_naming_governance")

        bad_text = (
            "陈默走进玄幽天魄城，掌柜递来冥灵古神令，又说寒魄圣魔桥下藏着青玄幽灵符。"
            "他还没问来历，旁边的人又提到天古神魄秘卷和幽冥寒灵法器。"
        )
        naming = evaluate_naming_governance(bad_text, allowed_terms=allowed)
        if naming.score >= 60:
            failures.append("ungrounded_names_not_penalized")
        if not any(item.startswith("ungrounded_new_names") for item in naming.issues):
            failures.append("ungrounded_name_issue_missing")

        quality = evaluate_chapter(
            bad_text * 20,
            min_chars=100,
            max_chars=8000,
            canon_context="\n".join(allowed),
        )
        quality_report = json.loads(quality.report)
        if "naming_governance" not in quality.dimensions:
            failures.append("quality_dimension_missing_naming_governance")
        if not quality_report.get("naming_governance_report"):
            failures.append("quality_report_missing_naming_governance")

        ordinary_text = "陈默低头看了看账册，又从后巷绕到旧铺门外，桥下雨水很深，这里没有新手村。"
        ordinary = evaluate_naming_governance(ordinary_text, allowed_terms=allowed)
        if ordinary.new_terms:
            failures.append("ordinary_terms_misread_as_names:" + ",".join(ordinary.new_terms[:6]))

        for item in session.scalars(select(PowerSystem).where(PowerSystem.book_id == book.id)):
            session.delete(item)
        for item in session.scalars(select(WorldRule).where(WorldRule.book_id == book.id)):
            session.delete(item)
        for item in session.scalars(select(Character).where(Character.book_id == book.id)):
            session.delete(item)
        for item in session.scalars(select(StoryBible).where(StoryBible.book_id == book.id)):
            session.delete(item)
        for item in session.scalars(select(StoryFoundation).where(StoryFoundation.book_id == book.id)):
            session.delete(item)
        session.delete(book)

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0
if __name__ == "__main__":
    raise SystemExit(main())
