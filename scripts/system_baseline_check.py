from __future__ import annotations

import subprocess

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterBrief, ChapterVersion
from app.services.author_command_center import build_author_command_center
from app.services.planning import plan_chapters


def main() -> int:
    failures: list[str] = []
    with session_scope() as session:
        book2_ch1 = session.scalar(select(Chapter).where(Chapter.book_id == 2, Chapter.chapter_number == 1))
        if book2_ch1:
            plan = plan_chapters(session, book_id=2, start=1, count=1)[0]
            center = build_author_command_center(session, book_id=2, chapter_number=1, start=1, count=5)
            if plan.next_action not in {
                "approve_chapter",
                "record_chapter_continuity",
                "draft_chapter",
                "revise_chapter",
                "revision_trend_recovery",
                "generate_rebuild_candidates",
                "create_publish_job",
                "publish_job_dry_run",
                "queue_publish_job",
                "retry_publish_job",
                "mark_publish_job",
            }:
                failures.append(f"book2_ch1_unstable_next_action:{plan.next_action}")
            if center.get("status") == "can_produce" and plan.latest_version_status == "reviewed_pass":
                failures.append("book2_readable_still_shows_produce")
            active_revision = list(
                session.scalars(
                    select(ChapterBrief).where(ChapterBrief.chapter_id == book2_ch1.id, ChapterBrief.status == "revision_ready")
                )
            )
            if active_revision:
                if plan.next_action not in {"revise_chapter", "revision_trend_recovery", "generate_rebuild_candidates"}:
                    failures.append(f"book2_active_revision_not_routed:{[brief.id for brief in active_revision]}:{plan.next_action}")
            latest = session.scalar(
                select(ChapterVersion).where(ChapterVersion.chapter_id == book2_ch1.id).order_by(ChapterVersion.id.desc())
            )
            if latest and latest.status == "needs_revision" and not str(latest.source or "").startswith("archived:"):
                if plan.next_action not in {"revise_chapter", "revision_trend_recovery", "generate_rebuild_candidates"}:
                    failures.append(f"book2_latest_revision_not_routed:{latest.id}:{latest.source}:{plan.next_action}")
                latest_source = str(latest.source or "")
                if latest_source.startswith(("revision_budget_recovery:", "revision_budget_readable_restore:")):
                    active_same_source = list(
                        session.scalars(
                            select(ChapterVersion).where(
                                ChapterVersion.chapter_id == book2_ch1.id,
                                ChapterVersion.source == latest_source,
                            )
                        )
                    )
                    if len(active_same_source) > 1:
                        failures.append(
                            "book2_duplicate_active_budget_recovery:"
                            + ",".join(str(version.id) for version in active_same_source)
                        )

    timer = subprocess.run(
        ["systemctl", "--user", "is-enabled", "ai-novel-auto-slim.timer"],
        text=True,
        capture_output=True,
        check=False,
    )
    if timer.returncode != 0 or timer.stdout.strip() != "enabled":
        failures.append("auto_slim_timer_not_enabled")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("system-baseline-check: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
