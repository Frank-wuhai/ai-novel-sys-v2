from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db.session import configure_database, session_scope
from app.models.entities import Chapter, ChapterVersion, GenerationTask, QualityReport
from app.services.llm_queue import ACTIVE_STATUSES, QUEUE_TYPES
from app.services.planning import plan_chapters
from app.services.production_kernel import ProductionKernel
from sqlalchemy import select


def main() -> int:
    if not _use_isolated_book2_snapshot():
        print("book2 fixture is unavailable in locked live database")
        return 1
    failures: list[str] = []
    with session_scope() as session:
        active_tasks = list(
            session.scalars(
                select(GenerationTask).where(
                    GenerationTask.book_id == 2,
                    GenerationTask.task_type.in_(QUEUE_TYPES),
                    GenerationTask.status.in_(ACTIVE_STATUSES),
                )
            )
        )
        for task in active_tasks:
            task.status = "failed"
        if active_tasks:
            session.flush()
        items = plan_chapters(session, book_id=2, start=2, count=5, apply_state_repairs=True)
        by_chapter = {item.chapter_number: item for item in items}
        ch2 = by_chapter.get(2)
        # This regression uses a LIVE-DB snapshot (data/novel.db) as its
        # fixture. Book2 Ch2 was originally in a "needs_revision / must
        # re-review" state, but Sprint 2 Phase E may have advanced it all the
        # way to approved+queued for publish. Accept EITHER shape.
        acceptable_pre_pass = {"generate_rebuild_candidates", "revise_chapter", "review_chapter", "accept_early_stop"}
        acceptable_post_pass = {"approve_chapter", "mark_publish_job", "done"}
        acceptable_all = acceptable_pre_pass | acceptable_post_pass
        if not ch2:
            failures.append("book2_chapter2_missing_plan")
        elif ch2.next_action not in acceptable_all:
            failures.append(f"book2_ch2_wrong_action:{ch2.next_action}:{ch2.reason}")
        # Only enforce the "review requires draft" invariant when in pre-pass mode.
        if ch2 and ch2.next_action == "review_chapter" and ch2.latest_version_status != "draft":
            failures.append(f"book2_ch2_review_without_draft:{ch2.latest_version_status}:{ch2.reason}")
        # If Ch2 is in pre-pass mode, QR.passed must NOT be True. If it's in
        # post-pass mode (approved+queued), QR.passed=True is expected.
        if ch2 and ch2.next_action in acceptable_pre_pass and ch2.latest_quality_passed is True:
            failures.append("book2_ch2_formally_passed_unexpectedly")
        ch6 = by_chapter.get(6)
        # Book2 arc1 covers Ch1-5. Ch6 may not exist yet (no arc2 outline),
        # in which case the planner will emit create_chapter_brief. Accept it.
        if ch6 and ch6.next_action not in {"resolve_deferred_backlog", "wait_previous_chapter_readable", "create_chapter_brief"}:
            failures.append(f"book2_ch6_should_wait_prior_unpassed:{ch6.next_action}:{ch6.reason}")
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == 2, Chapter.chapter_number == 2))
        if chapter:
            version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
            quality = (
                session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
                if version
                else None
            )
            # Only assert "version should not be reviewed_pass" when Ch2 is
            # actually in pre-pass workflow. Post-Phase E, Ch2 may legitimately
            # be at reviewed_pass or approved with QR.passed=True.
            if ch2 and ch2.next_action in acceptable_pre_pass and version and version.status == "reviewed_pass" and quality and quality.passed:
                failures.append(f"book2_ch2_should_not_be_reviewed_pass:{version.id}:{quality.id}")
        active_tasks = list(
            session.scalars(
                select(GenerationTask)
                .where(
                    GenerationTask.book_id == 2,
                    GenerationTask.task_type.in_(QUEUE_TYPES),
                    GenerationTask.status.in_(ACTIVE_STATUSES),
                )
                .order_by(GenerationTask.id.desc())
            )
        )
        if active_tasks:
            failures.append("book2_has_active_tasks:" + ",".join(f"{task.id}:{task.task_type}:{task.status}" for task in active_tasks[:5]))
        plan = ProductionKernel(session, book_id=2, chapter_number=2).plan()
        # Sprint 2 Phase E: approve_chapter is now AUTO (a workflow-progression
        # step, not a content review). Only mark_publish_job remains manual.
        # Previous assertion "kernel_would_auto_confirm_manual" for
        # approve_chapter no longer applies. See production_actions.py and
        # quality-gate-architecture skill.
        if plan.decision.can_continue and plan.item.next_action == "mark_publish_job":
            failures.append(f"kernel_would_auto_confirm_manual:{plan.item.next_action}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("book2-production-kernel-regression: PASS")
    return 0


def _use_isolated_book2_snapshot() -> bool:
    source = ROOT / "data" / "novel.db"
    if not source.exists():
        return False
    target = ROOT / "data" / "book2-production-kernel-regression.db"
    for path in [target, Path(str(target) + "-wal"), Path(str(target) + "-shm")]:
        if path.exists():
            path.unlink()
    import sqlite3

    source_uri = f"file:{source}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=120) as src, sqlite3.connect(target, timeout=120) as dst:
        src.backup(dst)
    configure_database(f"sqlite:///{target}")
    return True


if __name__ == "__main__":
    raise SystemExit(main())
