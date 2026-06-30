from __future__ import annotations

from pathlib import Path

from app.db.session import session_scope
from app.models.entities import Book, Chapter
from app.services.self_repair import generate_self_repair_plan, latest_self_repair_report, run_self_repair_regressions
from regression_db import isolated_database


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    isolated_database("self-repair-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="self repair", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        plan = generate_self_repair_plan(
            session,
            issue="生产主线按钮显示刷新状态，点击后没有推进。",
            book_id=book.id,
            chapter_number=1,
            live_model=False,
        )
        if plan.status != "completed":
            failures.append(f"plan_not_completed:{plan}")
        if not (ROOT / plan.report_path).exists():
            failures.append(f"plan_report_missing:{plan.report_path}")
        if "direct_file_write" not in str(plan.payload.get("safety")):
            failures.append("safety_policy_missing")

    regression = run_self_repair_regressions(suite="production")
    if regression.status != "completed":
        failures.append(f"regression_not_completed:{regression.status}:{regression.summary}")
    latest = latest_self_repair_report()
    if latest.get("status") != "found":
        failures.append(f"latest_report_missing:{latest}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("self-repair-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
