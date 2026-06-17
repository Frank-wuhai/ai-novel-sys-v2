from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, ChapterVersion
from app.services.context_contamination import audit_context_contamination, context_anchor_lines
from app.services.feedback import submit_revision_suggestion
from app.services.production import create_book, create_chapter_brief, seed_prompts
from regression_db import isolated_database
from scripts.run_local_dashboard import _perform_action


def main() -> int:
    database_url = isolated_database("brief-write-sanitizer-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Brief Write Sanitizer", genre="网游武侠", platform="番茄小说")
        _perform_action(
            session,
            {
                "action": "update_story_skeleton",
                "book_id": book.id,
                "premise": "林北获得全真武侠网游《万象江湖》内测资格，激活桥段复刻能力，现实同步会带来动作失控和关系代价。",
                "reader_promise": "看林北在热闹江湖里观察现场条件、自然复刻经典桥段，拿到招式、人情和现实同步回报。",
                "world_engine": "《万象江湖》从武侠向仙侠升维，NPC有独立欲望；桥段复刻必须服从人物因果、现场证据和现实同步副作用。",
                "protagonist_engine": "林北熟悉经典桥段但不万能，必须主动观察、选择、试错和承担代价；复刻失败会损失好感并现实动作失控。",
                "conflict_engine": "长期冲突来自NPC关系债、玩家竞争、现实同步失控和世界升维后的规则变硬。",
                "forbidden_rules": "禁止旧主角名、旧世界名、机械面板解题、机构关注和门派追杀模板。",
                "style_guide": "明快热闹，有江湖烟火气。",
                "volume_title": "第一卷",
                "volume_summary": "内测、桥段复刻、现实同步和第一笔人情债。",
                "arc_title": "入局",
                "arc_goal": "建立复刻机制和第一笔人情债。",
                "arc_climax": "复刻成功让现实身体出现同步反应。",
                "arc_turn": "林北意识到游戏规则正在升维。",
                "approve_after_save": True,
            },
        )
        brief = create_chapter_brief(
            session,
            book_id=book.id,
            chapter_number=1,
            goal="第1章进入旧世界《江湖志》。",
            required_beats="当前世界/作品锚点:《已废弃旧主角名志》",
            constraints="不得改旧桥段。",
        )
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == 1))
        version = ChapterVersion(chapter_id=chapter.id, version_number=1, title="第一章", content="旧稿", status="draft")
        session.add(version)
        session.flush()
        _feedback, _adjustment, revision_brief, _version = submit_revision_suggestion(
            session,
            book_id=book.id,
            chapter_number=1,
            suggestion_text="保留《江湖志》这个旧世界名，并继续旧桥段。",
            platform="regression",
            revision_mode="targeted",
        )
        anchors = "\n".join(context_anchor_lines(session, book_id=book.id))
        for item in (brief, revision_brief):
            latest = session.get(ChapterBrief, item.id)
            text = "\n".join([latest.goal or "", latest.required_beats or "", latest.constraints or ""])
            if "万象江湖" not in text or any(marker in text for marker in ("江湖志", "已废弃", "旧主角名", "旧桥段")):
                print("brief write sanitizer failed")
                print(text)
                return 1
            report = audit_context_contamination(
                session,
                book_id=book.id,
                chapter_number=1,
                brief_text=text,
                canon_context=anchors,
                previous_content="",
            )
            if not report.passed:
                print("sanitized brief failed contamination audit")
                print(report.to_dict())
                return 1

    print("brief-write-sanitizer-regression: PASS")
    print(f"database={database_url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
