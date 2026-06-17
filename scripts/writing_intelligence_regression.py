from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book
from app.services.production import create_book
from app.services.chapter_standards import build_chapter_production_standard
from app.services.quality import evaluate_chapter
from app.services.writing_intelligence import build_writing_intelligence_context
from regression_db import isolated_database


def main() -> int:
    isolated_database("writing-intelligence-regression")
    with session_scope() as session:
        book = session.scalar(select(Book).order_by(Book.id.desc()))
        if not book:
            book = create_book(session, title="Writing Intelligence Regression", genre="真实武侠", platform="manual")
        ctx = build_writing_intelligence_context(
            session,
            book_id=book.id,
            chapter_number=2,
            goal="承接上一章后果，让主角通过观察和交易换来新线索。",
            required_beats="承接前章后果；人物试探；利益交换；付出代价；章末出现新机会",
            constraints=build_chapter_production_standard(chapter_number=2),
            previous_chapter_context="上一章结尾，门外脚步停住，有人低声报出主角名字。",
            mode="draft",
        )
    required_sections = ["开篇策略", "反雷同", "小单元导演表", "人物反应链", "低成本开篇/章末备选"]
    missing = [section for section in required_sections if section not in ctx.prompt_block]
    quality = evaluate_chapter(
        _sample_chapter(),
        min_chars=800,
        goal="主角通过观察和交易换来新线索",
        required_beats="承接后果；试探；利益交换；代价；章末新机会",
        constraints=build_chapter_production_standard(chapter_number=2),
    )
    dimensions = quality.dimensions
    required_dimensions = ["opening_variety", "causal_scene_chain", "reaction_chain", "earned_payoff"]
    missing_dimensions = [name for name in required_dimensions if name not in dimensions]
    status = "pass" if not missing and not missing_dimensions and len(ctx.low_cost_variants) >= 3 and len(ctx.scene_plan) >= 5 else "fail"
    print(
        json.dumps(
            {
                "status": status,
                "opening_strategy": ctx.opening_strategy,
                "scene_plan_count": len(ctx.scene_plan),
                "variant_count": len(ctx.low_cost_variants),
                "missing_sections": missing,
                "missing_dimensions": missing_dimensions,
                "quality_dimensions": {name: dimensions.get(name) for name in required_dimensions},
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if status == "pass" else 1


def _sample_chapter() -> str:
    paragraphs = [
        "门外的脚步停住时，陈默先以为是客栈伙计送错了热水。",
        "他没有立刻开门，只把掌心贴在门闩上，听见对方的呼吸比普通人沉得多，像是压着伤，也像是压着怒气。",
        "按江湖规矩，夜半报人姓名不是拜访，是提醒屋里的人还有一口气可以谈。",
        "陈默把油灯挑亮，故意咳了一声：“银子在桌上，消息得先验货。”",
        "门外那人沉默片刻，递进来半枚碎玉。玉边沾着血，血里有一种淡淡的药味，正和他下午在药铺闻到的一样。",
        "他原本只想套出追兵来路，现在却知道药铺、碎玉和昨夜的黑衣人是一条线。",
        "这条线给了他机会，也把他推到更深的麻烦里：若接下碎玉，他欠的是人情；若不接，门外的人会死在这里。",
        "陈默最终伸手接住碎玉，指尖一凉，屋外立刻响起第二个人的笑声。",
        "“他收了。”那人说，“按规矩，明早之前，他就是我们的人。”",
    ]
    return "\n".join(paragraphs * 10)


if __name__ == "__main__":
    raise SystemExit(main())
