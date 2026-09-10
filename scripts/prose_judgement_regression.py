"""成文判据判卷 (prose_judgement J1-J5) 隔离 DB 回归 — 2026-09-10 第 3 步。

验证口径（对齐 prose_judgement_v1 的定位）：
1. review_chapter(prose_judge=True) 的报告 JSON 含 prose_judgement 节，dry-run 下 status=completed；
2. 缺口表结构合法：criterion ∈ J1-J5、anchor 非空、gap_count 与 gaps 长度一致；
3. 判卷不改变判定：同内容对照章节（不开判卷）与判卷章节的 passed/score 完全一致
   （成文判据无自动 FAIL、不自动拦稿）；
4. 判卷留有审计轨迹：generation_tasks 存在 task_type=prose_judgement 的 completed 行。

种子章节正文埋入 r5_1 式已知缺口句（无来源任务词/黑屏报时间/拍起球/压缩句），
dry-run 提供方返回罐头缺口表，真实判卷需 --live-llm 另行人工触发。
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, CanonAuthorityProfile, Chapter, ChapterVersion, GenerationTask
from app.services.production_reviewing import review_chapter
from regression_db import isolated_database

# r5_1 式已知缺口：J1 无来源任务词、J4 黑屏报时间/拍起球、J5 压缩句。
SEED_CONTENT = (
    "沈渡在山道上睁开眼，第一反应是单子黄了就是真的黄了。九点前跑不完入门流程，违约扣全款，"
    "房租押金全指这单。他摸出手机，黑屏按不亮，左上角时间停在昨晚十一点四十七，信号格空着。"
    "他站起来，拍掉冲锋衣前襟的起球，沿石阶往下走。谁家的钟点他不知道，但这院子里的活是赶时间的——"
    "东西朝着天亮那个点在赶。庙门虚掩，香灰积了半寸，烛影在殿内晃。他喊了一声，没人应。"
    "供桌上摊着一册黄纸，毛笔搁在镇纸边，墨还没干。他想起客户昨晚催单时说的话，又想起平台规则里"
    "那行小字。山风穿堂，吹得经幡猎猎作响。他攥紧背包带，跨过门槛，决定先把名字找到再说。"
    "殿后传来极轻的一声咳嗽，像有人，又像只是梁上灰落。他停住脚，等了三息，那声音没有再响。"
    "他把册子往回翻了一页，纸页刮过指腹，留下一道浅白的痕。"
    * 6
)


def _seed_book(session, *, title: str) -> Book:
    book = Book(title=title, genre="仙侠", target_platform="番茄")
    session.add(book)
    session.flush()
    # human_confirmed 的 active profile → 生产门禁直接放行（见 production_gate）
    session.add(
        CanonAuthorityProfile(
            book_id=book.id,
            status="active",
            source="human_confirmed_regression",
            profile_json="{}",
        )
    )
    session.flush()
    return book


def _seed_chapter(session, *, book_id: int, chapter_number: int) -> None:
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=f"第{chapter_number}章")
    session.add(chapter)
    session.flush()
    session.add(
        ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title=f"第{chapter_number}章",
            content=SEED_CONTENT,
            status="draft",
            source="manual",
        )
    )
    session.flush()


def main() -> int:
    isolated_database("prose-judgement-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = _seed_book(session, title="Prose Judgement Regression")
        _seed_chapter(session, book_id=book.id, chapter_number=1)  # 对照：不开判卷
        _seed_chapter(session, book_id=book.id, chapter_number=2)  # 判卷组

        control = review_chapter(session, book_id=book.id, chapter_number=1, review_dry_run=True)
        judged = review_chapter(
            session,
            book_id=book.id,
            chapter_number=2,
            review_dry_run=True,
            prose_judge=True,
        )

        control_data = json.loads(control.report or "{}")
        judged_data = json.loads(judged.report or "{}")

        # 1. 判卷节存在且完成
        pj = judged_data.get("prose_judgement")
        if not isinstance(pj, dict):
            failures.append("prose_judgement_section_missing")
            pj = {}
        elif pj.get("status") != "completed":
            failures.append(f"prose_judgement_status:{pj.get('status')}")

        # 2. 缺口表结构合法
        gaps = pj.get("gaps")
        if not isinstance(gaps, list) or not gaps:
            failures.append("prose_judgement_gaps_missing")
            gaps = []
        for gap in gaps:
            if gap.get("criterion") not in ("J1", "J2", "J3", "J4", "J5"):
                failures.append(f"bad_criterion:{gap.get('criterion')}")
            if not str(gap.get("anchor") or "").strip():
                failures.append("empty_anchor")
        if pj.get("gap_count") != len(gaps):
            failures.append("gap_count_mismatch")
        if not str(pj.get("summary") or "").strip():
            failures.append("summary_missing")

        # 3. 判卷不改变判定（无自动 FAIL）：同内容对照组 passed/score 必须一致
        if judged.passed != control.passed:
            failures.append(f"passed_changed:{control.passed}->{judged.passed}")
        if int(judged.score or 0) != int(control.score or 0):
            failures.append(f"score_changed:{control.score}->{judged.score}")
        if "prose_judgement" in control_data:
            failures.append("control_unexpectedly_has_prose_judgement")

        # 4. 审计轨迹
        task = session.scalar(
            select(GenerationTask)
            .where(GenerationTask.book_id == book.id, GenerationTask.task_type == "prose_judgement")
            .order_by(GenerationTask.id.desc())
        )
        if task is None:
            failures.append("generation_task_missing")
        elif task.status != "completed":
            failures.append(f"generation_task_status:{task.status}")

        # session 关闭前取出所需值（对象随 session_scope 结束后 detached）
        result_control = {"passed": bool(control.passed), "score": int(control.score or 0)}
        result_judged = {
            "passed": bool(judged.passed),
            "score": int(judged.score or 0),
            "prose_judgement": judged_data.get("prose_judgement"),
        }

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "control": result_control,
                "judged": result_judged,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
