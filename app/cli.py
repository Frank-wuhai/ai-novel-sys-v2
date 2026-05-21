from __future__ import annotations

import argparse
import sys
import time

from app.db.init import current_sqlite_path, init_db, reset_db
from app.db.session import configure_database, session_scope
from app.services.budget import check_token_budget
from app.services.audit import (
    compare_versions,
    get_generation_task,
    get_version_audit,
    list_chapter_versions,
    list_generation_tasks,
    pretty_json,
    task_summary,
)
from app.services.canon import (
    add_character,
    add_character_state,
    add_foreshadow,
    add_plot_thread,
    add_power_system,
    add_world_rule,
    format_canon_context,
)
from app.services.continuity import record_chapter_continuity
from app.services.dashboard import build_project_dashboard
from app.services.llm_queue import (
    enqueue_draft_chapter,
    enqueue_revise_chapter,
    list_generation_queue,
    retry_generation_queue_task,
    run_generation_queue,
    run_generation_queue_task,
)
from app.services.production import (
    approve_chapter,
    create_book,
    create_chapter_brief,
    create_foundation,
    create_manual_chapter_version,
    create_publish_job,
    create_revision_brief,
    draft_chapter,
    get_book,
    latest_chapter_version,
    list_books,
    list_chapters,
    list_publish_jobs,
    mark_publish_job,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    revise_chapter,
    review_chapter,
    seed_prompts,
)
from app.services.planning import (
    build_human_decision_package,
    create_arc_chapter_plan,
    create_chapter_plan,
    plan_chapters,
    run_book_cycle,
    run_next_action,
)
from app.services.evidence import (
    add_evidence_source,
    add_market_signal,
    audit_market_evidence,
    format_market_evidence_context,
    list_evidence_sources,
    list_market_signals,
)
from app.services.feedback import (
    convert_feedback_to_market_signal,
    list_platform_feedback,
    record_platform_feedback,
    summarize_platform_feedback,
)
from app.services.prompts import get_prompt_template
from app.services.readiness import check_production_readiness
from app.services.story import (
    create_story_arc,
    create_volume,
    format_outline,
    format_story_control_context,
    get_story_bible,
    list_story_arcs,
    list_volumes,
    upsert_story_bible,
)


def _parse_id_text(value: str, *, field_name: str) -> tuple[int, str]:
    raw_id, sep, text = value.partition(":")
    if not sep or not raw_id.isdigit() or not text:
        raise ValueError(f"{field_name} must use ID:TEXT format")
    return int(raw_id), text


def main() -> None:
    parser = argparse.ArgumentParser(prog="novel-v2")
    parser.add_argument("--database-url", default="", help="Override DATABASE_URL for this command.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init-db")

    p = sub.add_parser("reset-dev-db")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("create-book")
    p.add_argument("--title", required=True)
    p.add_argument("--genre", default="")
    p.add_argument("--platform", default="")

    p = sub.add_parser("create-foundation")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--premise", required=True)
    p.add_argument("--reader-promise", default="")
    p.add_argument("--world-engine", default="")
    p.add_argument("--protagonist-engine", default="")
    p.add_argument("--conflict-engine", default="")

    p = sub.add_parser("upsert-story-bible")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--positioning", default="")
    p.add_argument("--reader-promise", default="")
    p.add_argument("--main-plot", default="")
    p.add_argument("--protagonist-arc", default="")
    p.add_argument("--relationship-arc", default="")
    p.add_argument("--power-curve", default="")
    p.add_argument("--forbidden-rules", default="")
    p.add_argument("--style-guide", default="")
    p.add_argument("--status", default="draft")

    p = sub.add_parser("show-story-bible")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("create-volume")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--volume-number", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--status", default="planning")

    p = sub.add_parser("list-volumes")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("create-story-arc")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--arc-number", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--start-chapter", type=int, required=True)
    p.add_argument("--end-chapter", type=int, required=True)
    p.add_argument("--goal", default="")
    p.add_argument("--climax", default="")
    p.add_argument("--turn", default="")
    p.add_argument("--volume-number", type=int, default=0)
    p.add_argument("--status", default="planning")

    p = sub.add_parser("list-story-arcs")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("show-outline")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("show-story-context")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, default=0)

    p = sub.add_parser("create-chapter-brief")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")

    p = sub.add_parser("create-chapter-plan")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, required=True)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--goal-prefix", required=True)
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")

    p = sub.add_parser("create-arc-chapter-plan")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--arc-number", type=int, required=True)
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")

    p = sub.add_parser("plan-chapters")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=10)

    p = sub.add_parser("run-next-action")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--goal-prefix", default="自动规划")
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")
    p.add_argument("--platform", default="manual")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--queue-generation", action="store_true")

    p = sub.add_parser("run-book-cycle")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--goal-prefix", default="自动规划")
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")
    p.add_argument("--platform", default="manual")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--queue-generation", action="store_true")

    p = sub.add_parser("human-decision-package")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=10)

    p = sub.add_parser("production-readiness")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--live-llm", action="store_true")

    p = sub.add_parser("project-dashboard")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--recent-tasks", type=int, default=10)

    p = sub.add_parser("budget-check")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--token-budget", type=int, required=True)

    p = sub.add_parser("draft-chapter")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("enqueue-draft")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--live-llm", action="store_true")
    p.add_argument("--max-attempts", type=int, default=3)

    p = sub.add_parser("enqueue-revision")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--live-llm", action="store_true")
    p.add_argument("--max-attempts", type=int, default=3)

    p = sub.add_parser("list-generation-queue")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("run-generation-task")
    p.add_argument("--task-id", type=int, default=0)

    p = sub.add_parser("run-generation-queue")
    p.add_argument("--max-tasks", type=int, default=1)

    p = sub.add_parser("run-generation-worker")
    p.add_argument("--max-loops", type=int, default=1)
    p.add_argument("--sleep-seconds", type=float, default=5.0)
    p.add_argument("--max-tasks-per-loop", type=int, default=1)
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--token-budget", type=int, default=0)

    p = sub.add_parser("retry-generation-task")
    p.add_argument("--task-id", type=int, required=True)

    p = sub.add_parser("create-manual-chapter-version")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--source", default="manual")

    p = sub.add_parser("review-chapter")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)

    p = sub.add_parser("create-revision-brief")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)

    p = sub.add_parser("revise-chapter")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("approve-chapter")
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--reviewer", default="human")

    p = sub.add_parser("create-publish-job")
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--platform", required=True)

    sub.add_parser("list-books")

    p = sub.add_parser("show-book")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("list-chapters")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("show-chapter")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)

    p = sub.add_parser("list-versions")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)

    p = sub.add_parser("show-version")
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--content", action="store_true")

    p = sub.add_parser("compare-chapter-versions")
    p.add_argument("--left-version-id", type=int, required=True)
    p.add_argument("--right-version-id", type=int, required=True)

    p = sub.add_parser("list-generation-tasks")
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--task-type", default="")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("show-generation-task")
    p.add_argument("--task-id", type=int, required=True)

    p = sub.add_parser("list-publish-jobs")
    p.add_argument("--status", default="")

    p = sub.add_parser("publish-job-dry-run")
    p.add_argument("--job-id", type=int, required=True)

    p = sub.add_parser("queue-publish-job")
    p.add_argument("--job-id", type=int, required=True)

    p = sub.add_parser("mark-publish-job")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--status", choices=["published", "failed"], required=True)
    p.add_argument("--report", default="")

    p = sub.add_parser("retry-publish-job")
    p.add_argument("--job-id", type=int, required=True)

    sub.add_parser("seed-prompts")

    sub.add_parser("list-prompts")

    p = sub.add_parser("show-prompt")
    p.add_argument("--name", required=True)
    p.add_argument("--version", default="v1")

    p = sub.add_parser("add-evidence-source")
    p.add_argument("--source-id", required=True)
    p.add_argument("--title", default="")
    p.add_argument("--url", default="")
    p.add_argument("--reliability", type=int, default=0)
    p.add_argument("--status", default="candidate")

    p = sub.add_parser("list-evidence-sources")
    p.add_argument("--status", default="")
    p.add_argument("--min-reliability", type=int, default=0)

    p = sub.add_parser("add-market-signal")
    p.add_argument("--source-id", default="")
    p.add_argument("--genre", required=True)
    p.add_argument("--signal", required=True)
    p.add_argument("--confidence", type=int, default=0)

    p = sub.add_parser("list-market-signals")
    p.add_argument("--genre", default="")
    p.add_argument("--usable-only", action="store_true")
    p.add_argument("--min-confidence", type=int, default=0)

    p = sub.add_parser("show-evidence-context")
    p.add_argument("--genre", required=True)
    p.add_argument("--limit", type=int, default=5)

    p = sub.add_parser("audit-evidence")
    p.add_argument("--genre", default="")
    p.add_argument("--min-confidence", type=int, default=0)

    p = sub.add_parser("record-feedback")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--metric-name", required=True)
    p.add_argument("--metric-value", default="")
    p.add_argument("--raw-text", default="")
    p.add_argument("--chapter-number", type=int, default=0)

    p = sub.add_parser("list-feedback")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--platform", default="")
    p.add_argument("--metric-name", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("feedback-summary")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("feedback-to-market-signal")
    p.add_argument("--feedback-id", type=int, required=True)
    p.add_argument("--genre", required=True)
    p.add_argument("--signal", required=True)
    p.add_argument("--confidence", type=int, default=65)
    p.add_argument("--source-status", default="verified")
    p.add_argument("--source-reliability", type=int, default=3)

    p = sub.add_parser("add-character")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="")
    p.add_argument("--personality", default="")
    p.add_argument("--ability", default="")
    p.add_argument("--background", default="")

    p = sub.add_parser("add-character-state")
    p.add_argument("--character-id", type=int, required=True)
    p.add_argument("--state", required=True)
    p.add_argument("--chapter-id", type=int, default=0)
    p.add_argument("--source", default="manual")

    p = sub.add_parser("add-world-rule")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--category", default="")
    p.add_argument("--rule", required=True)
    p.add_argument("--status", default="active")

    p = sub.add_parser("add-power-system")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--rules", default="")
    p.add_argument("--costs", default="")
    p.add_argument("--limits", default="")
    p.add_argument("--status", default="active")

    p = sub.add_parser("add-plot-thread")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--description", default="")
    p.add_argument("--status", default="open")

    p = sub.add_parser("add-foreshadow")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--setup", required=True)
    p.add_argument("--payoff", default="")
    p.add_argument("--status", default="open")

    p = sub.add_parser("show-canon-context")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=8)

    p = sub.add_parser("record-chapter-continuity")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--character-state", action="append", default=[], help="Repeatable. Format: CHARACTER_ID:STATE_TEXT")
    p.add_argument("--new-foreshadow", action="append", default=[])
    p.add_argument("--payoff", action="append", default=[], help="Repeatable. Format: FORESHADOW_ID:PAYOFF_TEXT")
    p.add_argument("--plot-thread-status", action="append", default=[], help="Repeatable. Format: THREAD_ID:STATUS")

    args = parser.parse_args()
    if args.database_url:
        configure_database(args.database_url)

    if args.cmd == "init-db":
        init_db()
        print("init-db: PASS")
        return
    if args.cmd == "reset-dev-db":
        if not args.yes:
            print("ERROR: reset-dev-db requires --yes", file=sys.stderr)
            raise SystemExit(1)
        db_path = current_sqlite_path()
        if db_path is None:
            print("ERROR: reset-dev-db only supports sqlite databases", file=sys.stderr)
            raise SystemExit(1)
        reset_db()
        print("reset-dev-db: PASS")
        print(f"database={db_path}")
        return

    try:
        with session_scope() as session:
            if args.cmd == "create-book":
                book = create_book(session, title=args.title, genre=args.genre, platform=args.platform)
                print(f"book_id={book.id}")
            elif args.cmd == "create-foundation":
                foundation = create_foundation(
                    session,
                    book_id=args.book_id,
                    premise=args.premise,
                    reader_promise=args.reader_promise,
                    world_engine=args.world_engine,
                    protagonist_engine=args.protagonist_engine,
                    conflict_engine=args.conflict_engine,
                )
                print(f"foundation_id={foundation.id}")
            elif args.cmd == "upsert-story-bible":
                bible = upsert_story_bible(
                    session,
                    book_id=args.book_id,
                    positioning=args.positioning,
                    reader_promise=args.reader_promise,
                    main_plot=args.main_plot,
                    protagonist_arc=args.protagonist_arc,
                    relationship_arc=args.relationship_arc,
                    power_curve=args.power_curve,
                    forbidden_rules=args.forbidden_rules,
                    style_guide=args.style_guide,
                    status=args.status,
                )
                print(f"story_bible_id={bible.id}")
                print(f"status={bible.status}")
            elif args.cmd == "show-story-bible":
                bible = get_story_bible(session, book_id=args.book_id)
                if not bible:
                    raise ValueError("story bible not found")
                print(f"id={bible.id}")
                print(f"book_id={bible.book_id}")
                print(f"status={bible.status}")
                print(f"positioning={bible.positioning}")
                print(f"reader_promise={bible.reader_promise}")
                print(f"main_plot={bible.main_plot}")
                print(f"protagonist_arc={bible.protagonist_arc}")
                print(f"relationship_arc={bible.relationship_arc}")
                print(f"power_curve={bible.power_curve}")
                print(f"forbidden_rules={bible.forbidden_rules}")
                print(f"style_guide={bible.style_guide}")
            elif args.cmd == "create-volume":
                volume = create_volume(
                    session,
                    book_id=args.book_id,
                    volume_number=args.volume_number,
                    title=args.title,
                    summary=args.summary,
                    status=args.status,
                )
                print(f"volume_id={volume.id}")
                print(f"volume_number={volume.volume_number}")
                print(f"status={volume.status}")
            elif args.cmd == "list-volumes":
                for volume in list_volumes(session, book_id=args.book_id):
                    print(f"{volume.id}\tvolume={volume.volume_number}\t{volume.title}\t{volume.status}\t{volume.summary}")
            elif args.cmd == "create-story-arc":
                arc = create_story_arc(
                    session,
                    book_id=args.book_id,
                    arc_number=args.arc_number,
                    title=args.title,
                    start_chapter=args.start_chapter,
                    end_chapter=args.end_chapter,
                    goal=args.goal,
                    climax=args.climax,
                    turn=args.turn,
                    volume_number=args.volume_number or None,
                    status=args.status,
                )
                print(f"story_arc_id={arc.id}")
                print(f"arc_number={arc.arc_number}")
                print(f"chapters={arc.start_chapter}-{arc.end_chapter}")
                print(f"status={arc.status}")
            elif args.cmd == "list-story-arcs":
                for arc in list_story_arcs(session, book_id=args.book_id):
                    print(
                        f"{arc.id}\tarc={arc.arc_number}\tchapters={arc.start_chapter}-{arc.end_chapter}\t{arc.title}\t{arc.status}\t{arc.goal}"
                    )
            elif args.cmd == "show-outline":
                print(format_outline(session, book_id=args.book_id))
            elif args.cmd == "show-story-context":
                context, refs = format_story_control_context(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number or None,
                )
                print(f"story_bible_ids={','.join(str(item) for item in refs['story_bible_ids'])}")
                print(f"story_arc_ids={','.join(str(item) for item in refs['story_arc_ids'])}")
                print(context)
            elif args.cmd == "create-chapter-brief":
                brief = create_chapter_brief(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    goal=args.goal,
                    required_beats=args.required_beats,
                    constraints=args.constraints,
                )
                print(f"brief_id={brief.id}")
            elif args.cmd == "create-chapter-plan":
                briefs = create_chapter_plan(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                    goal_prefix=args.goal_prefix,
                    required_beats=args.required_beats,
                    constraints=args.constraints,
                )
                print(f"created_brief_count={len(briefs)}")
                for brief in briefs:
                    print(f"brief_id={brief.id}\tchapter_id={brief.chapter_id}\tstatus={brief.status}")
            elif args.cmd == "create-arc-chapter-plan":
                briefs = create_arc_chapter_plan(
                    session,
                    book_id=args.book_id,
                    arc_number=args.arc_number,
                    required_beats=args.required_beats,
                    constraints=args.constraints,
                )
                print(f"created_brief_count={len(briefs)}")
                for brief in briefs:
                    print(f"brief_id={brief.id}\tchapter_id={brief.chapter_id}\tstatus={brief.status}")
            elif args.cmd == "plan-chapters":
                for item in plan_chapters(session, book_id=args.book_id, start=args.start, count=args.count):
                    print(
                        "\t".join(
                            [
                                f"chapter={item.chapter_number}",
                                f"chapter_id={item.chapter_id or ''}",
                                f"brief_id={item.brief_id or ''}",
                                f"version_id={item.latest_version_id or ''}",
                                f"version_status={item.latest_version_status}",
                                f"quality_passed={item.latest_quality_passed}",
                                f"publish_job_id={item.publish_job_id or ''}",
                                f"publish_status={item.publish_job_status}",
                                f"next_action={item.next_action}",
                                f"reason={item.reason}",
                            ]
                        )
                    )
            elif args.cmd == "run-next-action":
                result = run_next_action(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    goal_prefix=args.goal_prefix,
                    required_beats=args.required_beats,
                    constraints=args.constraints,
                    platform=args.platform,
                    dry_run=args.dry_run,
                    queue_generation=args.queue_generation,
                )
                print(f"chapter_number={result.chapter_number}")
                print(f"action={result.action}")
                print(f"status={result.status}")
                print(f"message={result.message}")
                if result.object_id is not None:
                    print(f"object_id={result.object_id}")
            elif args.cmd == "run-book-cycle":
                result = run_book_cycle(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                    max_steps=args.max_steps,
                    goal_prefix=args.goal_prefix,
                    required_beats=args.required_beats,
                    constraints=args.constraints,
                    platform=args.platform,
                    dry_run=args.dry_run,
                    queue_generation=args.queue_generation,
                )
                print(f"executed_count={len(result.executed)}")
                for item in result.executed:
                    object_part = f"\tobject_id={item.object_id}" if item.object_id is not None else ""
                    print(
                        f"executed\tchapter={item.chapter_number}\taction={item.action}\tstatus={item.status}\tmessage={item.message}{object_part}"
                    )
                print(f"blocked_count={len(result.blocked)}")
                for item in result.blocked:
                    print(f"blocked\tchapter={item.chapter_number}\tnext_action={item.next_action}\treason={item.reason}")
                print(f"done_count={len(result.done)}")
                for item in result.done:
                    print(f"done\tchapter={item.chapter_number}\treason={item.reason}")
            elif args.cmd == "human-decision-package":
                package = build_human_decision_package(session, book_id=args.book_id, start=args.start, count=args.count)
                print(f"decision_count={len(package.items)}")
                print(f"continuity_count={package.continuity_count}")
                print(f"approval_count={package.approval_count}")
                print(f"publish_count={package.publish_count}")
                print(f"inspect_count={package.inspect_count}")
                for item in package.items:
                    print(
                        "\t".join(
                            [
                                "decision",
                                f"type={item.decision_type}",
                                f"chapter={item.chapter_number}",
                                f"chapter_id={item.chapter_id or ''}",
                                f"version_id={item.version_id or ''}",
                                f"publish_job_id={item.publish_job_id or ''}",
                                f"reason={item.reason}",
                            ]
                        )
                    )
                    print(f"command\t{item.command_hint}")
            elif args.cmd == "production-readiness":
                report = check_production_readiness(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                    live_llm=args.live_llm,
                )
                print(f"passed={report.passed}")
                for check in report.checks:
                    print(f"check\t{check.name}\tpassed={check.passed}\tdetail={check.detail}")
            elif args.cmd == "project-dashboard":
                report = build_project_dashboard(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                    recent_tasks=args.recent_tasks,
                )
                for line in report.lines:
                    print(line)
            elif args.cmd == "budget-check":
                report = check_token_budget(session, book_id=args.book_id, token_budget=args.token_budget)
                print(f"passed={report.passed}")
                print(f"book_id={report.book_id}")
                print(f"token_budget={report.token_budget}")
                print(f"used_tokens={report.used_tokens}")
                print(f"remaining_tokens={report.remaining_tokens}")
                print(f"task_count={report.task_count}")
            elif args.cmd == "draft-chapter":
                version = draft_chapter(session, book_id=args.book_id, chapter_number=args.chapter_number, dry_run=args.dry_run)
                print(f"version_id={version.id}")
                print(f"status={version.status}")
            elif args.cmd == "enqueue-draft":
                task = enqueue_draft_chapter(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    dry_run=not args.live_llm,
                    max_attempts=args.max_attempts,
                )
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
                print(f"task_type={task.task_type}")
            elif args.cmd == "enqueue-revision":
                task = enqueue_revise_chapter(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    dry_run=not args.live_llm,
                    max_attempts=args.max_attempts,
                )
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
                print(f"task_type={task.task_type}")
            elif args.cmd == "list-generation-queue":
                for task in list_generation_queue(session, status=args.status, limit=args.limit):
                    print(task_summary(task))
            elif args.cmd == "run-generation-task":
                result = run_generation_queue_task(session, task_id=args.task_id or None)
                print(f"generation_task_id={result.task.id}")
                print(f"status={result.task.status}")
                print(f"version_id={result.version_id or ''}")
                print(f"child_generation_task_id={result.child_generation_task_id or ''}")
                print(f"output_json={result.task.output_json}")
            elif args.cmd == "run-generation-queue":
                batch = run_generation_queue(session, max_tasks=args.max_tasks)
                print(f"executed_count={len(batch.results)}")
                for result in batch.results:
                    print(
                        "\t".join(
                            [
                                "executed",
                                f"generation_task_id={result.task.id}",
                                f"status={result.task.status}",
                                f"version_id={result.version_id or ''}",
                                f"child_generation_task_id={result.child_generation_task_id or ''}",
                            ]
                        )
                    )
            elif args.cmd == "run-generation-worker":
                if args.max_loops < 1:
                    raise ValueError("max-loops must be >= 1")
                if args.max_tasks_per_loop < 1:
                    raise ValueError("max-tasks-per-loop must be >= 1")
                total = 0
                idle_loops = 0
                budget_stopped = False
                for loop_index in range(1, args.max_loops + 1):
                    if args.token_budget:
                        if not args.book_id:
                            raise ValueError("--book-id is required when --token-budget is set")
                        budget = check_token_budget(session, book_id=args.book_id, token_budget=args.token_budget)
                        print(
                            f"budget\tbook_id={budget.book_id}\tused_tokens={budget.used_tokens}\tremaining_tokens={budget.remaining_tokens}\tpassed={budget.passed}"
                        )
                        if not budget.passed:
                            budget_stopped = True
                            break
                    batch = run_generation_queue(session, max_tasks=args.max_tasks_per_loop)
                    total += len(batch.results)
                    print(f"worker_loop={loop_index}\texecuted_count={len(batch.results)}")
                    for result in batch.results:
                        print(
                            "\t".join(
                                [
                                    "executed",
                                    f"generation_task_id={result.task.id}",
                                    f"status={result.task.status}",
                                    f"version_id={result.version_id or ''}",
                                    f"child_generation_task_id={result.child_generation_task_id or ''}",
                                ]
                            )
                        )
                    if not batch.results:
                        idle_loops += 1
                    if loop_index < args.max_loops and args.sleep_seconds > 0:
                        time.sleep(args.sleep_seconds)
                print(f"worker_done\ttotal_executed={total}\tidle_loops={idle_loops}\tbudget_stopped={budget_stopped}")
            elif args.cmd == "retry-generation-task":
                task = retry_generation_queue_task(session, task_id=args.task_id)
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
            elif args.cmd == "create-manual-chapter-version":
                version = create_manual_chapter_version(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    title=args.title,
                    content=args.content,
                    source=args.source,
                )
                print(f"version_id={version.id}")
                print(f"status={version.status}")
            elif args.cmd == "review-chapter":
                report = review_chapter(session, book_id=args.book_id, chapter_number=args.chapter_number)
                print(f"quality_report_id={report.id}")
                print(f"passed={report.passed}")
                print(f"score={report.score}")
                print(f"report={report.report}")
            elif args.cmd == "create-revision-brief":
                brief = create_revision_brief(session, book_id=args.book_id, chapter_number=args.chapter_number)
                print(f"revision_brief_id={brief.id}")
                print(f"status={brief.status}")
                print(f"goal={brief.goal}")
                print(f"required_beats={brief.required_beats}")
                print(f"constraints={brief.constraints}")
            elif args.cmd == "revise-chapter":
                version = revise_chapter(session, book_id=args.book_id, chapter_number=args.chapter_number, dry_run=args.dry_run)
                print(f"version_id={version.id}")
                print(f"status={version.status}")
            elif args.cmd == "approve-chapter":
                version = approve_chapter(session, version_id=args.version_id, reviewer=args.reviewer)
                print(f"version_id={version.id}")
                print(f"status={version.status}")
            elif args.cmd == "create-publish-job":
                job = create_publish_job(session, version_id=args.version_id, platform=args.platform)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "list-books":
                for book in list_books(session):
                    print(f"{book.id}\t{book.title}\t{book.genre}\t{book.target_platform}\t{book.status}")
            elif args.cmd == "show-book":
                book = get_book(session, book_id=args.book_id)
                print(f"id={book.id}")
                print(f"title={book.title}")
                print(f"genre={book.genre}")
                print(f"target_platform={book.target_platform}")
                print(f"status={book.status}")
            elif args.cmd == "list-chapters":
                for chapter in list_chapters(session, book_id=args.book_id):
                    latest = latest_chapter_version(session, chapter_id=chapter.id)
                    latest_status = latest.status if latest else "no_version"
                    latest_id = latest.id if latest else ""
                    print(f"{chapter.chapter_number}\t{chapter.title}\t{chapter.status}\tlatest_version={latest_id}\t{latest_status}")
            elif args.cmd == "show-chapter":
                chapters = [item for item in list_chapters(session, book_id=args.book_id) if item.chapter_number == args.chapter_number]
                if not chapters:
                    raise ValueError("chapter not found")
                chapter = chapters[0]
                latest = latest_chapter_version(session, chapter_id=chapter.id)
                print(f"id={chapter.id}")
                print(f"chapter_number={chapter.chapter_number}")
                print(f"title={chapter.title}")
                print(f"status={chapter.status}")
                if latest:
                    print(f"latest_version_id={latest.id}")
                    print(f"latest_version_number={latest.version_number}")
                    print(f"latest_version_status={latest.status}")
                    print(f"latest_version_source={latest.source}")
                    print(f"latest_content_chars={len(latest.content)}")
                else:
                    print("latest_version_id=")
            elif args.cmd == "list-versions":
                for version in list_chapter_versions(session, book_id=args.book_id, chapter_number=args.chapter_number):
                    print(
                        f"{version.id}\tversion={version.version_number}\tstatus={version.status}\tsource={version.source}\tchars={len(version.content)}\ttitle={version.title}"
                    )
            elif args.cmd == "show-version":
                audit = get_version_audit(session, version_id=args.version_id)
                version = audit.version
                print(f"id={version.id}")
                print(f"chapter_id={version.chapter_id}")
                print(f"chapter_number={audit.chapter.chapter_number}")
                print(f"version_number={version.version_number}")
                print(f"title={version.title}")
                print(f"status={version.status}")
                print(f"source={version.source}")
                print(f"content_chars={len(version.content)}")
                if audit.latest_quality:
                    print(f"quality_report_id={audit.latest_quality.id}")
                    print(f"quality_passed={audit.latest_quality.passed}")
                    print(f"quality_score={audit.latest_quality.score}")
                    print(f"quality_report={audit.latest_quality.report}")
                else:
                    print("quality_report_id=")
                print("generation_task_ids=" + ",".join(str(task.id) for task in audit.generation_tasks))
                if args.content:
                    print("content:")
                    print(version.content)
            elif args.cmd == "compare-chapter-versions":
                print(compare_versions(session, left_version_id=args.left_version_id, right_version_id=args.right_version_id))
            elif args.cmd == "list-generation-tasks":
                for task in list_generation_tasks(
                    session,
                    book_id=args.book_id or None,
                    task_type=args.task_type,
                    status=args.status,
                    limit=args.limit,
                ):
                    print(task_summary(task))
            elif args.cmd == "show-generation-task":
                task = get_generation_task(session, task_id=args.task_id)
                print(f"id={task.id}")
                print(f"book_id={task.book_id}")
                print(f"task_type={task.task_type}")
                print(f"status={task.status}")
                print("input_json=")
                print(pretty_json(task.input_json))
                print("output_json=")
                print(pretty_json(task.output_json))
            elif args.cmd == "list-publish-jobs":
                for job in list_publish_jobs(session, status=args.status):
                    print(f"{job.id}\tversion={job.chapter_version_id}\t{job.platform}\t{job.status}")
            elif args.cmd == "publish-job-dry-run":
                job = publish_job_dry_run(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
                print(f"report={job.result_report}")
            elif args.cmd == "queue-publish-job":
                job = queue_publish_job(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "mark-publish-job":
                job = mark_publish_job(session, job_id=args.job_id, status=args.status, report=args.report)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "retry-publish-job":
                job = retry_publish_job(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "seed-prompts":
                templates = seed_prompts(session)
                for template in templates:
                    print(f"{template.id}\t{template.name}@{template.version}\t{template.status}")
            elif args.cmd == "list-prompts":
                templates = seed_prompts(session)
                for template in templates:
                    print(f"{template.id}\t{template.name}\t{template.version}\t{template.status}")
            elif args.cmd == "show-prompt":
                template = get_prompt_template(session, name=args.name, version=args.version)
                print(f"id={template.id}")
                print(f"name={template.name}")
                print(f"version={template.version}")
                print(f"status={template.status}")
                print(template.template)
            elif args.cmd == "add-evidence-source":
                source = add_evidence_source(
                    session,
                    source_id=args.source_id,
                    title=args.title,
                    url=args.url,
                    reliability=args.reliability,
                    status=args.status,
                )
                print(f"evidence_source_id={source.id}")
                print(f"source_id={source.source_id}")
                print(f"status={source.status}")
            elif args.cmd == "list-evidence-sources":
                for source in list_evidence_sources(session, status=args.status, min_reliability=args.min_reliability):
                    print(f"{source.id}\t{source.source_id}\treliability={source.reliability}\t{source.status}\t{source.title}")
            elif args.cmd == "add-market-signal":
                signal = add_market_signal(
                    session,
                    source_key=args.source_id,
                    genre=args.genre,
                    signal_text=args.signal,
                    confidence=args.confidence,
                )
                print(f"market_signal_id={signal.id}")
                print(f"genre={signal.genre}")
                print(f"confidence={signal.confidence}")
            elif args.cmd == "list-market-signals":
                for signal in list_market_signals(
                    session,
                    genre=args.genre,
                    usable_only=args.usable_only,
                    min_confidence=args.min_confidence,
                ):
                    print(f"{signal.id}\tsource={signal.source_id or ''}\t{signal.genre}\tconfidence={signal.confidence}\t{signal.signal_text}")
            elif args.cmd == "show-evidence-context":
                context, signal_ids = format_market_evidence_context(session, genre=args.genre, limit=args.limit)
                print(f"market_signal_ids={','.join(str(item) for item in signal_ids)}")
                print(context)
            elif args.cmd == "audit-evidence":
                for item in audit_market_evidence(session, genre=args.genre, min_confidence=args.min_confidence):
                    reliability = "" if item.source_reliability is None else str(item.source_reliability)
                    reasons = ",".join(item.reasons) if item.reasons else "usable"
                    print(
                        "\t".join(
                            [
                                f"signal_id={item.signal_id}",
                                f"genre={item.genre}",
                                f"confidence={item.confidence}",
                                f"source={item.source_key}",
                                f"source_status={item.source_status}",
                                f"source_reliability={reliability}",
                                f"usable={item.usable}",
                                f"reasons={reasons}",
                                f"signal={item.signal_text}",
                            ]
                        )
                    )
            elif args.cmd == "record-feedback":
                feedback = record_platform_feedback(
                    session,
                    book_id=args.book_id,
                    platform=args.platform,
                    metric_name=args.metric_name,
                    metric_value=args.metric_value,
                    raw_text=args.raw_text,
                    chapter_number=args.chapter_number or None,
                )
                print(f"feedback_id={feedback.id}")
                print(f"book_id={feedback.book_id}")
                print(f"chapter_id={feedback.chapter_id or ''}")
                print(f"platform={feedback.platform}")
                print(f"metric_name={feedback.metric_name}")
            elif args.cmd == "list-feedback":
                for feedback in list_platform_feedback(
                    session,
                    book_id=args.book_id,
                    platform=args.platform,
                    metric_name=args.metric_name,
                    limit=args.limit,
                ):
                    print(
                        "\t".join(
                            [
                                str(feedback.id),
                                f"book={feedback.book_id}",
                                f"chapter={feedback.chapter_id or ''}",
                                f"platform={feedback.platform}",
                                f"metric={feedback.metric_name}",
                                f"value={feedback.metric_value}",
                                f"raw={feedback.raw_text}",
                            ]
                        )
                    )
            elif args.cmd == "feedback-summary":
                summary = summarize_platform_feedback(session, book_id=args.book_id, limit=args.limit)
                by_metric = ",".join(f"{key}={value}" for key, value in sorted(summary.by_metric.items()))
                by_platform = ",".join(f"{key}={value}" for key, value in sorted(summary.by_platform.items()))
                print(f"total={summary.total}")
                print(f"by_metric={by_metric}")
                print(f"by_platform={by_platform}")
                for feedback in summary.latest:
                    print(
                        "\t".join(
                            [
                                "latest",
                                f"feedback_id={feedback.id}",
                                f"chapter={feedback.chapter_id or ''}",
                                f"platform={feedback.platform}",
                                f"metric={feedback.metric_name}",
                                f"value={feedback.metric_value}",
                            ]
                        )
                    )
            elif args.cmd == "feedback-to-market-signal":
                source_id, signal_id = convert_feedback_to_market_signal(
                    session,
                    feedback_id=args.feedback_id,
                    genre=args.genre,
                    signal_text=args.signal,
                    confidence=args.confidence,
                    source_status=args.source_status,
                    source_reliability=args.source_reliability,
                )
                print(f"evidence_source_id={source_id}")
                print(f"market_signal_id={signal_id}")
            elif args.cmd == "add-character":
                character = add_character(
                    session,
                    book_id=args.book_id,
                    name=args.name,
                    role=args.role,
                    personality=args.personality,
                    ability=args.ability,
                    background=args.background,
                )
                print(f"character_id={character.id}")
                print(f"name={character.name}")
            elif args.cmd == "add-character-state":
                state = add_character_state(
                    session,
                    character_id=args.character_id,
                    state_text=args.state,
                    chapter_id=args.chapter_id or None,
                    source=args.source,
                )
                print(f"character_state_id={state.id}")
            elif args.cmd == "add-world-rule":
                rule = add_world_rule(
                    session,
                    book_id=args.book_id,
                    category=args.category,
                    rule_text=args.rule,
                    status=args.status,
                )
                print(f"world_rule_id={rule.id}")
            elif args.cmd == "add-power-system":
                power = add_power_system(
                    session,
                    book_id=args.book_id,
                    name=args.name,
                    rules=args.rules,
                    costs=args.costs,
                    limits=args.limits,
                    status=args.status,
                )
                print(f"power_system_id={power.id}")
            elif args.cmd == "add-plot-thread":
                thread = add_plot_thread(
                    session,
                    book_id=args.book_id,
                    name=args.name,
                    description=args.description,
                    status=args.status,
                )
                print(f"plot_thread_id={thread.id}")
            elif args.cmd == "add-foreshadow":
                foreshadow = add_foreshadow(
                    session,
                    book_id=args.book_id,
                    setup_text=args.setup,
                    payoff_text=args.payoff,
                    status=args.status,
                )
                print(f"foreshadow_id={foreshadow.id}")
            elif args.cmd == "show-canon-context":
                context, refs = format_canon_context(session, book_id=args.book_id, limit=args.limit)
                print(f"canon_refs={refs}")
                print(context)
            elif args.cmd == "record-chapter-continuity":
                result = record_chapter_continuity(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    summary=args.summary,
                    character_states=[
                        _parse_id_text(item, field_name="--character-state") for item in args.character_state
                    ],
                    new_foreshadows=args.new_foreshadow,
                    payoffs=[_parse_id_text(item, field_name="--payoff") for item in args.payoff],
                    plot_thread_updates=[
                        _parse_id_text(item, field_name="--plot-thread-status") for item in args.plot_thread_status
                    ],
                )
                print(f"chapter_id={result.chapter_id}")
                print(f"character_state_ids={','.join(str(item) for item in result.character_state_ids)}")
                print(f"new_foreshadow_ids={','.join(str(item) for item in result.new_foreshadow_ids)}")
                print(f"paid_foreshadow_ids={','.join(str(item) for item in result.paid_foreshadow_ids)}")
                print(f"updated_plot_thread_ids={','.join(str(item) for item in result.updated_plot_thread_ids)}")
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
