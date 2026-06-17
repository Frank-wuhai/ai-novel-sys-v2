from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterVersion
from app.services.planning import run_next_action
from app.services.production_control import build_production_control_report
from app.services.production import create_book, create_chapter_brief, create_foundation, seed_prompts
from app.services.chapter_drafting import draft_chapter
from app.services.llm_queue import enqueue_draft_chapter
from app.services.story import create_story_arc, create_volume, upsert_story_bible
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-gate-regression")
    with session_scope() as session:
        seed_prompts(session)
        book = create_book(session, title="Production Gate Regression", genre="网游武侠", platform="番茄小说")
        create_foundation(
            session,
            book_id=book.id,
            premise="主角获得虚拟现实武侠网游内测资格，游戏实力开始同步现实。",
            reader_promise="看主角在武侠网游与现实同步之间主动破局。",
            world_engine="游戏收益同步现实，但受身体承载和人物因果限制。",
            protagonist_engine="主角主动试错，靠观察、行动和代价换收益。",
            conflict_engine="玩家竞争、门派因果和现实异常逐步升级。",
        )
        upsert_story_bible(
            session,
            book_id=book.id,
            positioning="主角获得内测资格并激活演绎能力。",
            reader_promise="桥段复刻、收益兑现、现实同步。",
            main_plot="游戏与现实融合升级。",
            protagonist_arc="从投机复刻到尊重真实因果。",
            power_curve="收益必须匹配现场条件和代价。",
            forbidden_rules="不要默认机构追杀。",
            style_guide="明快热闹。",
            status="draft",
        )
        create_volume(session, book_id=book.id, volume_number=1, title="第一卷", summary="内测入局。")
        create_story_arc(
            session,
            book_id=book.id,
            arc_number=1,
            title="开局破局",
            start_chapter=1,
            end_chapter=5,
            goal="建立演绎机制和现实同步异常。",
            climax="主角复刻桥段救场。",
            turn="他发现游戏不是普通服务器。",
            volume_number=1,
        )
        create_chapter_brief(
            session,
            book_id=book.id,
            chapter_number=1,
            goal="第1章建立入局和能力触发。",
            required_beats="内测资格，首次入局，能力触发，章末异常",
            constraints="按最新骨架写。",
        )
        result = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=True)
        if result.status != "blocked" or "生产门禁未通过" not in result.message:
            print("production gate did not block unapproved skeleton")
            print(result)
            return 1
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book.id, Chapter.chapter_number == 1))
        versions = session.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id)).all() if chapter else []
        if versions:
            print("blocked production still created versions")
            print([(version.id, version.status, version.source) for version in versions])
            return 1
        preview = run_next_action(session, book_id=book.id, chapter_number=1, preview_only=True)
        if preview.status != "blocked":
            print("preview should still report blocked gate")
            print(preview)
            return 1
        for label, call in [
            ("direct_draft", lambda: draft_chapter(session, book_id=book.id, chapter_number=1, dry_run=True)),
            ("enqueue_draft", lambda: enqueue_draft_chapter(session, book_id=book.id, chapter_number=1, dry_run=True)),
        ]:
            try:
                call()
            except ValueError as exc:
                if "生产门禁未通过" not in str(exc):
                    print(f"{label} failed with wrong error")
                    print(exc)
                    return 1
            else:
                print(f"{label} bypassed production gate")
                return 1
        control = build_production_control_report(session, book_id=book.id, start=1, count=5).to_dict()
        metrics = control.get("metrics") or {}
        if control.get("status") != "blocked" or metrics.get("auto_ready") != 0 or metrics.get("planned_auto_ready") != 5:
            print("blocked production control exposed runnable chapters")
            print(control)
            return 1

    print("production-gate-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
