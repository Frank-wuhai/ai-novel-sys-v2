"""Regression for accept_early_stop pipeline advancement (Sprint 2 P0-1).

Before the fix, accept_early_stop only flipped chapter.status to
``needs_confirmation``. The best version still had status=``needs_revision``
and quality.passed=False, so the orchestrator kept routing back to
_decide_revision_route → early_stop → accept_early_stop forever, burning
worker loops but never advancing the chapter.

After the fix, accept_early_stop promotes the best-scoring passing version to
status=``reviewed_pass`` and flips its QualityReport.passed=True, so the next
orchestrator tick lands in the reviewed_pass branch and can proceed to
record_continuity → approve_chapter.
"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    QualityReport,
    StoryFoundation,
    StoryArc,
)
from app.services.planning import _execute_accept_early_stop, plan_chapters
from app.services.production_state import collect_version_scores
from regression_db import isolated_database


def _seed(session, chapter_number: int = 1, best_score: int = 78) -> tuple[int, int, int]:
    book = Book(title="早停推进回归书", status="active", genre="都市悬疑", target_platform="番茄小说")
    session.add(book)
    session.flush()
    session.add(StoryFoundation(book_id=book.id, premise="都市悬疑测试", status="approved"))
    session.add(
        StoryArc(
            book_id=book.id,
            arc_number=1,
            title="启示",
            start_chapter=1,
            end_chapter=5,
            goal="发现异能",
            status="planning",
        )
    )
    chapter = Chapter(book_id=book.id, chapter_number=chapter_number, title=f"第{chapter_number}章", status="drafting")
    session.add(chapter)
    session.flush()
    session.add(
        ChapterBrief(
            chapter_id=chapter.id,
            goal="主角发现预知笔记本",
            required_beats="beat1\nbeat2\nbeat3",
            constraints="3000-4500字",
            status="revision_ready",
        )
    )
    session.flush()
    best_v_id = 0
    # v1-v3 fail, v4-v6 pass with improving scores. Avoids linear_revision_exhausted
    # (needs 4+ failing rows) and gives evaluate_early_stop 3 passing candidates
    # with the highest being ``best_score``.
    scores = [50, 55, 60, 75, 76, best_score]
    for i, s in enumerate(scores, start=1):
        v = ChapterVersion(
            chapter_id=chapter.id,
            version_number=i,
            title=f"第{chapter_number}章 v{i}",
            content="正文" * 2000,
            status="needs_revision",
            source="revision:test",
        )
        session.add(v)
        session.flush()
        best_v_id = v.id
        session.add(
            QualityReport(
                chapter_version_id=v.id,
                score=s,
                passed=(s >= 75),
                report=json.dumps({
                    "score": s,
                    "passed": (s >= 75),
                    "verdict": "pass" if s >= 75 else "hard_fail",
                    "hard_gate": {"passed": True, "status": "PASS"},
                }, ensure_ascii=False),
            )
        )
        session.flush()
    return book.id, chapter.id, best_v_id


def _seed_polluted_high_score_history(session) -> tuple[int, int, int]:
    """Book6 regression: a high-scoring polluted historical restore must not
    preempt a lower-scoring clean latest candidate via accept_early_stop.
    """

    book = Book(title="早停污染候选回归书", status="active", genre="武侠网游", target_platform="番茄小说")
    session.add(book)
    session.flush()
    session.add(StoryFoundation(book_id=book.id, premise="入梦清虚观测试", status="approved"))
    session.add(
        StoryArc(
            book_id=book.id,
            arc_number=1,
            title="清虚观",
            start_chapter=1,
            end_chapter=5,
            goal="拜入山门",
            status="planning",
        )
    )
    chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="drafting")
    session.add(chapter)
    session.flush()
    session.add(
        ChapterBrief(
            chapter_id=chapter.id,
            goal="顾晚在清虚观山门被道士盘问，靠木牌和江湖话过关",
            required_beats="山门盘问\n木牌物证\n穴位试探\n代价伏笔",
            constraints="修订合同: 保留清虚观现场认知边界；不得出现内测/论坛/玩家/NPC/系统分配。",
            status="revision_ready",
        )
    )
    safe = "顾晚攥着木牌站在清虚观山门外。老道士拂尘一横，问他从哪儿来。他没有提别的，只说茶摊老道给他指过路。山风压着松针响，木牌墨迹还没干。"
    polluted = "顾晚对道士说系统分配我来的。进游戏前他看过论坛攻略，内测玩家都知道清虚观入门任务要找NPC接。"
    rows = [
        (1, 50, False, safe, "revision:test"),
        (2, 55, False, safe, "revision:test"),
        (3, 60, False, safe, "revision:test"),
        (4, 79, False, polluted, "rebuild_candidate_incumbent_restore:v2168"),
        (5, 69, False, safe, "revision:ark_openai_compatible"),
    ]
    clean_latest_id = 0
    for version_number, score, passed, content, source in rows:
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=version_number,
            title=f"第一章 v{version_number}",
            content=content,
            status="needs_revision",
            source=source,
        )
        session.add(version)
        session.flush()
        if version_number == 5:
            clean_latest_id = version.id
        session.add(
            QualityReport(
                chapter_version_id=version.id,
                score=score,
                passed=passed,
                report=json.dumps({
                    "score": score,
                    "passed": passed,
                    "verdict": "hard_fail",
                    "hard_gate": {"passed": True, "status": "PASS"},
                }, ensure_ascii=False),
            )
        )
        session.flush()
    return book.id, chapter.id, clean_latest_id


def main() -> int:
    isolated_database("accept-early-stop-advance")
    failures: list[str] = []
    with session_scope() as session:
        book_id, chapter_id, best_v_id = _seed(session, chapter_number=1, best_score=78)

        items = plan_chapters(session, book_id=book_id, start=1, count=1, apply_state_repairs=False)
        item = items[0]
        if item.next_action != "accept_early_stop":
            failures.append(f"initial_action={item.next_action!r} expected accept_early_stop; reason={item.reason}")
        else:
            _execute_accept_early_stop(
                session, book_id=book_id, chapter_number=1, item=item
            )

            best = session.get(ChapterVersion, best_v_id)
            if best is None or best.status != "reviewed_pass":
                failures.append(
                    f"best_version_status={best.status if best else None!r} expected reviewed_pass"
                )

            items2 = plan_chapters(session, book_id=book_id, start=1, count=1, apply_state_repairs=False)
            item2 = items2[0]
            if item2.next_action == "accept_early_stop":
                failures.append(
                    f"stuck_loop: next_action still {item2.next_action!r} after accept_early_stop"
                )
            if item2.next_action not in {"record_chapter_continuity", "approve_chapter"}:
                failures.append(
                    f"next_action_after_early_stop={item2.next_action!r} "
                    f"expected record_chapter_continuity or approve_chapter"
                )

    with session_scope() as session:
        book_id, chapter_id, clean_latest_id = _seed_polluted_high_score_history(session)
        scores = collect_version_scores(session, chapter_id)
        score_versions = {score.version_number for score in scores}
        if 4 in score_versions:
            failures.append("polluted_high_score_version_included_in_early_stop_scores")
        items = plan_chapters(session, book_id=book_id, start=1, count=1, apply_state_repairs=False)
        item = items[0]
        if item.next_action == "accept_early_stop":
            failures.append(
                f"polluted_history_preempted_clean_latest: action={item.next_action!r}; reason={item.reason}"
            )
        clean_latest = session.get(ChapterVersion, clean_latest_id)
        if clean_latest is None or clean_latest.status != "needs_revision":
            failures.append(
                f"clean_latest_status_changed={clean_latest.status if clean_latest else None!r} expected needs_revision"
            )

    if failures:
        print("accept-early-stop-advance-regression: FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("accept-early-stop-advance-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
