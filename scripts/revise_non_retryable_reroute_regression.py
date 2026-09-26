"""卡点②改路回归（2026-09-26, 用户批准方案a）: 修订任务 retryable=false 失败 →
_revise_non_retryable_failure_pending 信号翻转, kernel 改路 generate_rebuild_candidates。

覆盖:
1. 失败修订 + retryable=false        → True  (改路)
2. 失败修订 + retryable=true         → False (可重试, 不干预)
3. 失败修订之后已有重建任务           → False (信号失效, 防永久跳修订)
4. 进行中(pending)修订               → False (在飞工作不干预)
5. 最新为已完成修订                   → False
6. 无队列任务                        → False
7. 别章的失败修订                     → False (章节过滤)
8. 路由层: 信号置位时即使策略层说「继续修订当选候选」也改路重建 (ch4 task 76 撞车场景)
9. 路由层: 信号未置位时策略分支照旧走修订 (不误伤)
"""
from __future__ import annotations

import json
from datetime import datetime

from app.db.session import session_scope
from app.models.entities import Book, GenerationTask
from app.services.llm_queue import QUEUE_REBUILD_CANDIDATES, QUEUE_REVISE
from app.services.planning import _revise_non_retryable_failure_pending
from app.services.production_orchestrator import ProductionSituation, decide_production_route
from regression_db import isolated_database


def _task(book_id: int, task_type: str, status: str, chapter_number: int, output: dict | None = None) -> GenerationTask:
    return GenerationTask(
        book_id=book_id,
        task_type=task_type,
        status=status,
        input_json=json.dumps({"chapter_number": chapter_number}, ensure_ascii=False),
        output_json=json.dumps(output or {}, ensure_ascii=False),
    )


def main() -> int:
    isolated_database("revise-non-retryable-reroute-regression")
    failures: list[str] = []
    created: list[object] = []
    with session_scope() as session:
        book = Book(title=f"revise-reroute-regression-{datetime.utcnow().timestamp()}", genre="test", target_platform="test")
        session.add(book)
        session.flush()
        created.append(book)

        def check(label: str, expected: bool, tasks: list[GenerationTask], chapter_number: int = 4) -> None:
            for t in tasks:
                session.add(t)
                created.append(t)
            session.flush()
            actual = _revise_non_retryable_failure_pending(session, book_id=book.id, chapter_number=chapter_number)
            if actual != expected:
                failures.append(f"{label}: expected={expected} actual={actual}")
            for t in tasks:
                session.delete(t)
                created.remove(t)
            session.flush()

        check(
            "failed_revise_retryable_false",
            True,
            [_task(book.id, QUEUE_REVISE, "failed", 4, {"retryable": False, "error": "unit_flow revision failed"})],
        )
        check(
            "failed_revise_retryable_true",
            False,
            [_task(book.id, QUEUE_REVISE, "failed", 4, {"retryable": True})],
        )
        check(
            "rebuild_after_failed_revise_disarms",
            False,
            [
                _task(book.id, QUEUE_REVISE, "failed", 4, {"retryable": False}),
                _task(book.id, QUEUE_REBUILD_CANDIDATES, "completed", 4),
            ],
        )
        check(
            "pending_revise_untouched",
            False,
            [_task(book.id, QUEUE_REVISE, "pending", 4)],
        )
        check(
            "completed_revise_latest",
            False,
            [
                _task(book.id, QUEUE_REVISE, "failed", 4, {"retryable": False}),
                _task(book.id, QUEUE_REVISE, "completed", 4),
            ],
        )
        check("no_tasks", False, [])
        check(
            "other_chapter_filtered",
            False,
            [_task(book.id, QUEUE_REVISE, "failed", 9, {"retryable": False})],
        )

    with session_scope() as session:
        for obj in reversed(created):
            session.delete(obj)

    # 路由层: 信号必须抢在 continue_selected_rebuild_candidate 策略分支之前
    base = dict(
        chapter_number=4,
        chapter_status="in_production",
        has_brief=True,
        latest_version_status="needs_revision",
        latest_quality_passed=False,
        has_revision_brief=True,
        strategy_action="revise_chapter",
        strategy_intent="continue_selected_rebuild_candidate",
    )
    rerouted = decide_production_route(ProductionSituation(**base, revise_non_retryable_failure_pending=True))
    if rerouted.action != "generate_rebuild_candidates":
        failures.append(f"route_prempts_strategy: expected generate_rebuild_candidates, got {rerouted.action}")
    untouched = decide_production_route(ProductionSituation(**base, revise_non_retryable_failure_pending=False))
    if untouched.action != "revise_chapter":
        failures.append(f"route_strategy_untouched: expected revise_chapter, got {untouched.action}")

    if failures:
        print("FAIL")
        for failure in failures:
            print(" -", failure)
        return 1
    print("PASS: revise_non_retryable_reroute_regression 9/9")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
