from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.db.init import current_sqlite_path, init_db, reset_db
from app.db.session import configure_database, session_scope
from app.core.config import settings
from app.models.entities import Chapter, ChapterBrief
from app.services.budget import check_token_budget
from app.services.bias import evaluate_generation_bias
from app.services.audit import (
    compare_versions,
    get_generation_task,
    get_version_audit,
    list_chapter_versions,
    list_generation_tasks,
    pretty_json,
    task_summary,
)
from app.services.agent_plan_intelligence import (
    create_market_research_pack,
    create_visual_asset,
    index_book_knowledge,
    ingest_market_research_results,
    list_visual_assets,
    retrieve_book_knowledge,
    run_agent_plan_enhancement_cycle,
    summarize_semantic_memory,
)
from app.services.agent_plan_utilization import build_agent_plan_utilization_report
from app.services.aesthetic_profile import apply_aesthetic_profile
from app.services.author_command_center import build_author_command_center
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
from app.services.dashboard import build_project_dashboard, build_project_snapshot
from app.services.db_ops import (
    check_database_health,
    check_schema_version,
    create_database_backup,
    list_database_backups,
    restore_database_from_backup,
)
from app.services.llm_audit import list_llm_request_logs, summarize_llm_failures, summarize_llm_usage
from app.services.llm_costs import summarize_llm_cost
from app.services.live_llm import run_live_llm_smoke
from app.services.llm_queue import (
    build_generation_queue_health,
    cancel_generation_queue_task,
    enqueue_draft_chapter,
    enqueue_rebuild_candidates,
    enqueue_revise_chapter,
    list_generation_queue,
    pause_generation_queue_task,
    recover_stale_generation_tasks,
    retry_generation_queue_task,
    resume_generation_queue_task,
    run_generation_queue,
    run_generation_queue_task,
)
from app.services.production import (
    approve_chapter,
    auto_prepare_publish_job,
    create_book,
    create_chapter_brief,
    create_foundation,
    create_manual_chapter_version,
    create_publish_job,
    create_revision_brief,
    draft_chapter,
    execute_publish_job,
    get_publish_job,
    get_book,
    latest_chapter_version,
    list_books,
    list_chapters,
    list_publish_executions,
    list_publish_jobs,
    list_publishing_targets,
    mark_publish_job,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    revise_chapter,
    review_chapter,
    seed_prompts,
    upsert_publishing_target,
)
from app.services.planning import (
    build_human_decision_package,
    create_arc_chapter_plan,
    create_chapter_plan,
    plan_chapters,
    run_book_cycle,
    run_next_action,
)
from app.services.production_kernel import ProductionKernel
from app.services.evidence import (
    add_evidence_source,
    add_market_signal,
    audit_market_evidence,
    format_market_evidence_context,
    list_evidence_sources,
    list_market_signals,
)
from app.services.feedback import (
    apply_feedback_adjustment_to_brief,
    convert_feedback_to_market_signal,
    create_feedback_adjustment,
    list_feedback_adjustments,
    list_platform_feedback,
    record_platform_feedback,
    submit_revision_suggestion,
    summarize_platform_feedback,
)
from app.services.prompts import get_prompt_template
from app.services.quality_insights import build_quality_calibration, build_quality_trends
from app.services.production_control import build_production_control_report
from app.services.production_scaffold import repair_production_scaffold
from app.services.revision_intent import extract_revision_decision
from app.services.data_governance import audit_book_data_governance
from app.services.development_governance import build_development_status
from app.services.model_strategy import build_model_strategy
from app.services.publish_preflight import build_publish_preflight
from app.services.readiness import check_production_readiness
from app.services.story_alignment import build_story_alignment_audit
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


def _print_revision_decision(adjustment_text: str) -> None:
    decision = extract_revision_decision(adjustment_text)
    if not decision:
        return
    print(f"revision_mode={decision.get('处理强度', '')}")
    print(f"revision_confidence={decision.get('置信度', '')}")
    print(f"revision_reason={decision.get('判定理由', '')}")
    print(f"revision_escalation={decision.get('升级规则', '')}")


def _print_debug_entrypoint(recommended_command: str) -> None:
    print("debug_entrypoint=true")
    print(f"recommended_command={recommended_command}")


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
    p.add_argument("--prose-style", default="")
    p.add_argument("--atmosphere", default="")
    p.add_argument("--story-route", default="")
    p.add_argument("--style-must-have", default="")
    p.add_argument("--style-must-not", default="")

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

    p = sub.add_parser("set-aesthetic-profile")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--prose-style", default="")
    p.add_argument("--atmosphere", default="")
    p.add_argument("--story-route", default="")
    p.add_argument("--style-must-have", default="")
    p.add_argument("--style-must-not", default="")

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
    p.add_argument("--no-state-repairs", action="store_true", help="read-only planning; do not apply state repair writes")

    p = sub.add_parser("run-next-action")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--goal-prefix", default="自动规划")
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")
    p.add_argument("--platform", default="manual")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--preview-only", action="store_true", help="only report the next action; do not write versions, reports, or jobs")
    p.add_argument("--queue-generation", action="store_true")

    p = sub.add_parser("production-run-next")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--goal-prefix", default="自动规划")
    p.add_argument("--required-beats", default="")
    p.add_argument("--constraints", default="")
    p.add_argument("--platform", default="manual")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--preview-only", action="store_true", help="only report the next action; do not write versions, reports, or jobs")
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

    p = sub.add_parser("production-run-cycle")
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

    p = sub.add_parser("semantic-memory-status")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("agent-plan-cycle")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, default=0)
    p.add_argument("--market-query", default="")
    p.add_argument("--platform", default="")
    p.add_argument("--live-embedding", action="store_true")
    p.add_argument("--skip-memory", action="store_true")
    p.add_argument("--skip-visuals", action="store_true")

    p = sub.add_parser("agent-plan-utilization")
    p.add_argument("--book-id", type=int, required=True)

    p = sub.add_parser("production-control")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=8)

    p = sub.add_parser("repair-production-scaffold")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--overwrite", action="store_true")
    p.add_argument("--no-approve", action="store_true")
    p.add_argument("--chapter-count", type=int, default=5)

    p = sub.add_parser("data-governance-audit")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-limit", type=int, default=12)

    p = sub.add_parser("development-status")
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=5)

    p = sub.add_parser("author-command-center")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, default=1)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=20)

    sub.add_parser("model-strategy")

    p = sub.add_parser("publish-preflight")
    p.add_argument("--version-id", type=int, required=True)

    p = sub.add_parser("project-dashboard")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--recent-tasks", type=int, default=10)

    p = sub.add_parser("project-snapshot-json")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, default=0)
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
    p.add_argument("--task-timeout-seconds", type=int, default=3600)

    p = sub.add_parser("enqueue-revision")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--live-llm", action="store_true")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--task-timeout-seconds", type=int, default=3600)

    p = sub.add_parser("enqueue-rebuild-candidates")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--live-llm", action="store_true")
    p.add_argument("--candidate-count", type=int, default=3)
    p.add_argument("--max-attempts", type=int, default=2)
    p.add_argument("--task-timeout-seconds", type=int, default=3600)

    p = sub.add_parser("list-generation-queue")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("generation-queue-health")
    p.add_argument("--failure-limit", type=int, default=5)
    p.add_argument("--stale-after-seconds", type=int, default=3600)

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
    p.add_argument("--recover-stale-before-run", action="store_true")
    p.add_argument("--task-timeout-seconds", type=int, default=3600)

    p = sub.add_parser("retry-generation-task")
    p.add_argument("--task-id", type=int, required=True)

    p = sub.add_parser("recover-stale-generation-tasks")
    p.add_argument("--timeout-seconds", type=int, default=3600)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("pause-generation-task")
    p.add_argument("--task-id", type=int, required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("resume-generation-task")
    p.add_argument("--task-id", type=int, required=True)

    p = sub.add_parser("cancel-generation-task")
    p.add_argument("--task-id", type=int, required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("create-manual-chapter-version")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--content", required=True)
    p.add_argument("--source", default="manual")

    p = sub.add_parser("review-chapter")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--llm-review", action="store_true")
    p.add_argument("--live-llm", action="store_true")
    p.add_argument("--auto-revision-brief", action="store_true")

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

    p = sub.add_parser("one-click-publish")
    p.add_argument("--version-id", type=int, required=True)
    p.add_argument("--platform", required=True)
    p.add_argument("--confirm-real-platform", action="store_true")

    p = sub.add_parser("upsert-publishing-target")
    p.add_argument("--platform", required=True)
    p.add_argument("--account-label", default="")
    p.add_argument("--work-identifier", default="")
    p.add_argument("--automation-mode", default="manual")
    p.add_argument("--status", default="active")
    p.add_argument("--config-json", default="{}")

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

    p = sub.add_parser("list-llm-requests")
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("llm-usage-summary")
    p.add_argument("--book-id", type=int, default=0)

    p = sub.add_parser("llm-failure-summary")
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("llm-cost-summary")
    p.add_argument("--book-id", type=int, default=0)
    p.add_argument("--input-price-per-1m", type=float, default=-1.0)
    p.add_argument("--output-price-per-1m", type=float, default=-1.0)

    sub.add_parser("show-llm-config")

    p = sub.add_parser("live-llm-smoke")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--book-id", type=int, default=0)

    p = sub.add_parser("quality-trends")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("quality-calibration")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--max-failure-rate", type=float, default=0.35)
    p.add_argument("--min-average-score", type=float, default=70.0)

    p = sub.add_parser("story-alignment-audit")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-limit", type=int, default=8)

    p = sub.add_parser("chapter-bias-audit")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)

    p = sub.add_parser("list-publish-jobs")
    p.add_argument("--status", default="")

    p = sub.add_parser("show-publish-job")
    p.add_argument("--job-id", type=int, required=True)

    p = sub.add_parser("list-publishing-targets")
    p.add_argument("--platform", default="")
    p.add_argument("--status", default="")

    p = sub.add_parser("publish-job-dry-run")
    p.add_argument("--job-id", type=int, required=True)

    p = sub.add_parser("queue-publish-job")
    p.add_argument("--job-id", type=int, required=True)

    p = sub.add_parser("execute-publish-job")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser("list-publish-executions")
    p.add_argument("--job-id", type=int, default=0)
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("mark-publish-job")
    p.add_argument("--job-id", type=int, required=True)
    p.add_argument("--status", choices=["published", "failed"], required=True)
    p.add_argument("--report", default="")

    p = sub.add_parser("retry-publish-job")
    p.add_argument("--job-id", type=int, required=True)

    sub.add_parser("database-health")
    sub.add_parser("schema-version")

    p = sub.add_parser("backup-database")
    p.add_argument("--label", default="")

    p = sub.add_parser("restore-database")
    p.add_argument("--backup-path", required=True)
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("list-database-backups")
    p.add_argument("--limit", type=int, default=20)

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

    p = sub.add_parser("create-market-research-pack")
    p.add_argument("--genre", required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--platform", default="番茄小说")

    p = sub.add_parser("ingest-market-research-results")
    p.add_argument("--genre", required=True)
    p.add_argument("--result-json", default="")
    p.add_argument("--result-path", default="")
    p.add_argument("--source-prefix", default="agent-search")

    p = sub.add_parser("index-book-knowledge")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--dry-run", action="store_true", help="use deterministic local hash embeddings")
    p.add_argument("--live-embedding", action="store_true", help="call the Agent Plan embedding model")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--limit-chapters", type=int, default=80)

    p = sub.add_parser("retrieve-book-knowledge")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--dry-run", action="store_true", help="use deterministic local hash embeddings")
    p.add_argument("--live-embedding", action="store_true", help="call the Agent Plan embedding model for the query vector")

    p = sub.add_parser("create-visual-asset")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--asset-type", choices=["cover", "chapter_illustration"], required=True)
    p.add_argument("--chapter-number", type=int, default=0)
    p.add_argument("--style", default="")
    p.add_argument("--live", action="store_true")

    p = sub.add_parser("list-visual-assets")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--asset-type", default="")
    p.add_argument("--limit", type=int, default=20)

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

    p = sub.add_parser("create-feedback-adjustment")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--target-chapter-number", type=int, required=True)
    p.add_argument("--feedback-id", type=int, action="append", default=[])
    p.add_argument("--adjustment-text", default="")
    p.add_argument("--apply-to-brief", action="store_true")

    p = sub.add_parser("list-feedback-adjustments")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("apply-feedback-adjustment")
    p.add_argument("--adjustment-id", type=int, required=True)

    p = sub.add_parser("submit-revision-suggestion")
    p.add_argument("--book-id", type=int, required=True)
    p.add_argument("--chapter-number", type=int, required=True)
    p.add_argument("--suggestion", required=True)
    p.add_argument("--platform", default="manual")
    p.add_argument("--revision-mode", choices=["auto", "polish", "local_patch", "targeted", "rewrite", "fresh"], default="auto")

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
    if args.cmd == "restore-database":
        try:
            result = restore_database_from_backup(backup_path=args.backup_path, confirm=args.yes)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            raise SystemExit(1)
        print("restore-database: PASS")
        print(f"database_path={result.database_path}")
        print(f"source_backup_path={result.source_backup_path}")
        print(f"pre_restore_backup_path={result.pre_restore_backup_path}")
        print(f"restored_size_bytes={result.restored_size_bytes}")
        return

    try:
        with session_scope() as session:
            if args.cmd == "create-book":
                book = create_book(session, title=args.title, genre=args.genre, platform=args.platform)
                print(f"book_id={book.id}")
                if any([args.prose_style, args.atmosphere, args.story_route, args.style_must_have, args.style_must_not]):
                    block = apply_aesthetic_profile(
                        session,
                        book_id=book.id,
                        prose_style=args.prose_style,
                        atmosphere=args.atmosphere,
                        story_route=args.story_route,
                        must_have=args.style_must_have,
                        must_not=args.style_must_not,
                    )
                    print("aesthetic_profile=applied")
                    print(block)
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
            elif args.cmd == "set-aesthetic-profile":
                block = apply_aesthetic_profile(
                    session,
                    book_id=args.book_id,
                    prose_style=args.prose_style,
                    atmosphere=args.atmosphere,
                    story_route=args.story_route,
                    must_have=args.style_must_have,
                    must_not=args.style_must_not,
                )
                print("aesthetic_profile=applied")
                print(block)
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
                for item in plan_chapters(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                    apply_state_repairs=not args.no_state_repairs,
                ):
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
            elif args.cmd == "production-run-next":
                if args.preview_only:
                    plan = ProductionKernel(
                        session,
                        book_id=args.book_id,
                        chapter_number=args.chapter_number,
                        platform=args.platform,
                    ).plan()
                    print(f"chapter_number={plan.item.chapter_number}")
                    print(f"action={plan.item.next_action}")
                    print("status=preview")
                    print(f"message={plan.item.reason}")
                    print(f"object_id={plan.item.latest_version_id or plan.item.brief_id or plan.item.publish_job_id or ''}")
                else:
                    run = ProductionKernel(
                        session,
                        book_id=args.book_id,
                        chapter_number=args.chapter_number,
                        platform=args.platform,
                    ).run_until_terminal(dry_run=args.dry_run)
                    latest = run.latest_result
                    print(f"chapter_number={args.chapter_number}")
                    print(f"action={latest.get('action', '')}")
                    print(f"status={latest.get('status', '')}")
                    print(f"message={latest.get('message', '')}")
                    print(f"object_id={latest.get('object_id', '')}")
                    print(f"terminal_status={run.terminal_status}")
                    print(f"terminal_message={run.terminal_message}")
                    print(f"executed_count={len(run.executed)}")
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
                    preview_only=args.preview_only,
                )
                print(f"chapter_number={result.chapter_number}")
                print(f"action={result.action}")
                print(f"status={result.status}")
                print(f"message={result.message}")
                if result.object_id is not None:
                    print(f"object_id={result.object_id}")
            elif args.cmd in {"run-book-cycle", "production-run-cycle"}:
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
            elif args.cmd == "semantic-memory-status":
                try:
                    summary = summarize_semantic_memory(session, book_id=args.book_id)
                except OperationalError:
                    session.rollback()
                    summary = {
                        "book_id": args.book_id,
                        "ready": False,
                        "indexed_count": 0,
                        "stale": False,
                        "attention": "migration missing; run alembic upgrade head",
                    }
                print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "agent-plan-cycle":
                result = run_agent_plan_enhancement_cycle(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number or None,
                    market_query=args.market_query,
                    platform=args.platform,
                    dry_run=not args.live_embedding,
                    rebuild_memory=not args.skip_memory,
                    create_visuals=not args.skip_visuals,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "agent-plan-utilization":
                result = build_agent_plan_utilization_report(session, book_id=args.book_id)
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "production-control":
                report = build_production_control_report(
                    session,
                    book_id=args.book_id,
                    start=args.start,
                    count=args.count,
                )
                for line in report.lines:
                    print(line)
            elif args.cmd == "repair-production-scaffold":
                result = repair_production_scaffold(
                    session,
                    book_id=args.book_id,
                    only_missing=not args.overwrite,
                    approve_skeleton=not args.no_approve,
                    chapter_count=args.chapter_count,
                    apply=args.apply,
                )
                print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "data-governance-audit":
                report = audit_book_data_governance(session, book_id=args.book_id, chapter_limit=args.chapter_limit)
                print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "development-status":
                report = build_development_status(
                    session,
                    book_id=args.book_id or None,
                    start=args.start,
                    count=args.count,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "author-command-center":
                report = build_author_command_center(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    start=args.start,
                    count=args.count,
                )
                print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "model-strategy":
                print(json.dumps(build_model_strategy(), ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "publish-preflight":
                print(json.dumps(build_publish_preflight(session, version_id=args.version_id), ensure_ascii=False, indent=2, sort_keys=True))
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
            elif args.cmd == "project-snapshot-json":
                snapshot = build_project_snapshot(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number or args.start,
                    start=args.start,
                    count=args.count,
                    recent_tasks=args.recent_tasks,
                )
                print(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
            elif args.cmd == "budget-check":
                report = check_token_budget(session, book_id=args.book_id, token_budget=args.token_budget)
                print(f"passed={report.passed}")
                print(f"book_id={report.book_id}")
                print(f"token_budget={report.token_budget}")
                print(f"used_tokens={report.used_tokens}")
                print(f"remaining_tokens={report.remaining_tokens}")
                print(f"task_count={report.task_count}")
            elif args.cmd == "draft-chapter":
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number}"
                )
                version = draft_chapter(session, book_id=args.book_id, chapter_number=args.chapter_number, dry_run=args.dry_run)
                print(f"version_id={version.id}")
                print(f"status={version.status}")
            elif args.cmd == "enqueue-draft":
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number} --queue-generation"
                )
                task = enqueue_draft_chapter(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    dry_run=not args.live_llm,
                    max_attempts=args.max_attempts,
                    timeout_seconds=args.task_timeout_seconds,
                )
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
                print(f"task_type={task.task_type}")
                print(f"task_timeout_seconds={args.task_timeout_seconds}")
            elif args.cmd == "enqueue-revision":
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number} --queue-generation"
                )
                task = enqueue_revise_chapter(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    dry_run=not args.live_llm,
                    max_attempts=args.max_attempts,
                    timeout_seconds=args.task_timeout_seconds,
                )
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
                print(f"task_type={task.task_type}")
                print(f"task_timeout_seconds={args.task_timeout_seconds}")
            elif args.cmd == "enqueue-rebuild-candidates":
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number} --queue-generation"
                )
                task = enqueue_rebuild_candidates(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    dry_run=not args.live_llm,
                    candidate_count=args.candidate_count,
                    max_attempts=args.max_attempts,
                    timeout_seconds=args.task_timeout_seconds,
                )
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
                print(f"task_type={task.task_type}")
                print(f"candidate_count={args.candidate_count}")
                print(f"task_timeout_seconds={args.task_timeout_seconds}")
            elif args.cmd == "list-generation-queue":
                for task in list_generation_queue(session, status=args.status, limit=args.limit):
                    print(task_summary(task))
            elif args.cmd == "generation-queue-health":
                report = build_generation_queue_health(
                    session,
                    failure_limit=args.failure_limit,
                    stale_after_seconds=args.stale_after_seconds,
                )
                print(f"total={report.total}")
                print("counts=" + ",".join(f"{key}={value}" for key, value in report.counts.items()))
                print(f"oldest_pending_id={report.oldest_pending_id or ''}")
                print(f"oldest_pending_chapter={report.oldest_pending_chapter or ''}")
                print(f"running_count={report.running_count}")
                print(f"stale_running_count={report.stale_running_count}")
                for item in report.running_tasks:
                    print(
                        "\t".join(
                            [
                                "running",
                                f"generation_task_id={item.task_id}",
                                f"type={item.task_type}",
                                f"chapter={item.chapter_number or ''}",
                                f"attempt={item.attempt}",
                                f"max_attempts={item.max_attempts}",
                                f"running_age_seconds={item.running_age_seconds}",
                                f"timeout_seconds={item.timeout_seconds}",
                                f"stale={item.stale}",
                                f"recoverable={item.recoverable}",
                            ]
                        )
                    )
                for failure in report.latest_failures:
                    print(
                        "\t".join(
                            [
                                "failure",
                                f"generation_task_id={failure.task_id}",
                                f"type={failure.task_type}",
                                f"chapter={failure.chapter_number or ''}",
                                f"attempt={failure.attempt}",
                                f"max_attempts={failure.max_attempts}",
                                f"error_category={failure.error_category}",
                                f"retryable={failure.retryable}",
                                f"error={failure.error}",
                            ]
                        )
                    )
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
                if args.task_timeout_seconds < 1:
                    raise ValueError("task-timeout-seconds must be >= 1")
                total = 0
                idle_loops = 0
                budget_stopped = False
                total_recovered = 0
                for loop_index in range(1, args.max_loops + 1):
                    if args.recover_stale_before_run:
                        recovered = recover_stale_generation_tasks(
                            session,
                            timeout_seconds=args.task_timeout_seconds,
                        )
                        total_recovered += len(recovered)
                        print(f"worker_recovery_loop={loop_index}\trecovered_count={len(recovered)}")
                        for item in recovered:
                            print(
                                "\t".join(
                                    [
                                        "recovered",
                                        f"generation_task_id={item.task_id}",
                                        f"previous_status={item.previous_status}",
                                        f"status={item.new_status}",
                                        f"chapter={item.chapter_number or ''}",
                                        f"attempt={item.attempt}",
                                        f"max_attempts={item.max_attempts}",
                                        f"age_seconds={item.age_seconds}",
                                        f"error_category={item.error_category}",
                                    ]
                                )
                            )
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
                print(
                    f"worker_done\ttotal_executed={total}\tidle_loops={idle_loops}\tbudget_stopped={budget_stopped}\trecovered_count={total_recovered}"
                )
            elif args.cmd == "retry-generation-task":
                task = retry_generation_queue_task(session, task_id=args.task_id)
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
            elif args.cmd == "recover-stale-generation-tasks":
                recovered = recover_stale_generation_tasks(
                    session,
                    timeout_seconds=args.timeout_seconds,
                    limit=args.limit,
                )
                print(f"recovered_count={len(recovered)}")
                for item in recovered:
                    print(
                        "\t".join(
                            [
                                "recovered",
                                f"generation_task_id={item.task_id}",
                                f"previous_status={item.previous_status}",
                                f"status={item.new_status}",
                                f"chapter={item.chapter_number or ''}",
                                f"attempt={item.attempt}",
                                f"max_attempts={item.max_attempts}",
                                f"age_seconds={item.age_seconds}",
                                f"error_category={item.error_category}",
                            ]
                        )
                    )
            elif args.cmd == "pause-generation-task":
                task = pause_generation_queue_task(session, task_id=args.task_id, reason=args.reason)
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
            elif args.cmd == "resume-generation-task":
                task = resume_generation_queue_task(session, task_id=args.task_id)
                print(f"generation_task_id={task.id}")
                print(f"status={task.status}")
            elif args.cmd == "cancel-generation-task":
                task = cancel_generation_queue_task(session, task_id=args.task_id, reason=args.reason)
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
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number}"
                )
                report = review_chapter(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    llm_review=args.llm_review,
                    review_dry_run=not args.live_llm,
                    auto_revision_brief=args.auto_revision_brief,
                )
                print(f"quality_report_id={report.id}")
                print(f"passed={report.passed}")
                print(f"score={report.score}")
                print(f"report={report.report}")
                if args.auto_revision_brief and not report.passed:
                    chapter = session.scalar(select(Chapter).where(Chapter.book_id == args.book_id, Chapter.chapter_number == args.chapter_number))
                    brief = (
                        session.scalar(
                            select(ChapterBrief)
                            .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
                            .order_by(ChapterBrief.id.desc())
                        )
                        if chapter
                        else None
                    )
                    print(f"revision_brief_id={brief.id if brief else ''}")
            elif args.cmd == "create-revision-brief":
                brief = create_revision_brief(session, book_id=args.book_id, chapter_number=args.chapter_number)
                print(f"revision_brief_id={brief.id}")
                print(f"status={brief.status}")
                print(f"goal={brief.goal}")
                print(f"required_beats={brief.required_beats}")
                print(f"constraints={brief.constraints}")
            elif args.cmd == "revise-chapter":
                _print_debug_entrypoint(
                    f"production-run-next --book-id {args.book_id} --chapter-number {args.chapter_number}"
                )
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
            elif args.cmd == "one-click-publish":
                result = auto_prepare_publish_job(
                    session,
                    version_id=args.version_id,
                    platform=args.platform,
                    confirm_real_platform=args.confirm_real_platform,
                )
                job = result.get("job") or {}
                print(f"status={result['status']}")
                print(f"message={result['message']}")
                print(f"publish_job_id={job.get('id', '')}")
                print(f"publish_job_status={job.get('status', '')}")
                preflight = result.get("preflight") or {}
                print(f"preflight_passed={preflight.get('passed', False)}")
                print("preflight_blockers=" + ";".join(preflight.get("blockers", [])))
                for step in result.get("steps", []):
                    print(
                        "\t".join(
                            [
                                "step",
                                f"action={step.get('action', '')}",
                                f"status={step.get('status', '')}",
                                f"publish_job_id={step.get('publish_job_id', '')}",
                                f"publish_execution_id={step.get('publish_execution_id', '')}",
                            ]
                        )
                    )
            elif args.cmd == "upsert-publishing-target":
                target = upsert_publishing_target(
                    session,
                    platform=args.platform,
                    account_label=args.account_label,
                    work_identifier=args.work_identifier,
                    automation_mode=args.automation_mode,
                    status=args.status,
                    config_json=args.config_json,
                )
                print(f"publishing_target_id={target.id}")
                print(f"platform={target.platform}")
                print(f"account_label={target.account_label}")
                print(f"work_identifier={target.work_identifier}")
                print(f"automation_mode={target.automation_mode}")
                print(f"status={target.status}")
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
            elif args.cmd == "list-llm-requests":
                for log in list_llm_request_logs(
                    session,
                    book_id=args.book_id or None,
                    status=args.status,
                    limit=args.limit,
                ):
                    print(
                        "\t".join(
                            [
                                str(log.id),
                                f"book={log.book_id}",
                                f"task={log.generation_task_id or ''}",
                                f"type={log.task_type}",
                                f"status={log.status}",
                                f"provider={log.provider}",
                                f"model={log.model}",
                                f"request_id={log.request_id}",
                                f"tokens={log.estimated_total_tokens}",
                                f"actual_tokens={log.actual_total_tokens}",
                                f"elapsed_ms={log.elapsed_ms}",
                                f"template={log.prompt_template}",
                                f"error_category={log.error_category}",
                            ]
                        )
                    )
            elif args.cmd == "llm-usage-summary":
                summary = summarize_llm_usage(session, book_id=args.book_id or None)
                print(f"book_id={summary.book_id or ''}")
                print(f"request_count={summary.request_count}")
                print(f"completed_count={summary.completed_count}")
                print(f"failed_count={summary.failed_count}")
                print(f"estimated_total_tokens={summary.estimated_total_tokens}")
                print(f"actual_total_tokens={summary.actual_total_tokens}")
                print(f"billable_prompt_tokens={summary.billable_prompt_tokens}")
                print(f"billable_response_tokens={summary.billable_response_tokens}")
                print(f"billable_total_tokens={summary.billable_total_tokens}")
                print(f"elapsed_ms={summary.elapsed_ms}")
            elif args.cmd == "llm-failure-summary":
                rows = summarize_llm_failures(session, book_id=args.book_id or None, limit=args.limit)
                print(f"failure_bucket_count={len(rows)}")
                for row in rows:
                    print(
                        "\t".join(
                            [
                                "failure_bucket",
                                f"error_category={row.error_category}",
                                f"count={row.count}",
                                f"latest_request_id={row.latest_request_id}",
                                f"task_type={row.latest_task_type}",
                                f"provider={row.latest_provider}",
                                f"model={row.latest_model}",
                                f"elapsed_ms={row.latest_elapsed_ms}",
                                f"suggestion={row.suggestion}",
                            ]
                        )
                    )
            elif args.cmd == "llm-cost-summary":
                cost = summarize_llm_cost(
                    session,
                    book_id=args.book_id or None,
                    input_price_per_1m_tokens=None if args.input_price_per_1m < 0 else args.input_price_per_1m,
                    output_price_per_1m_tokens=None if args.output_price_per_1m < 0 else args.output_price_per_1m,
                )
                print(f"book_id={cost.book_id or ''}")
                print(f"model={cost.model}")
                print(f"request_count={cost.request_count}")
                print(f"billable_prompt_tokens={cost.billable_prompt_tokens}")
                print(f"billable_response_tokens={cost.billable_response_tokens}")
                print(f"billable_total_tokens={cost.billable_total_tokens}")
                print(f"input_price_per_1m_tokens={cost.input_price_per_1m_tokens}")
                print(f"output_price_per_1m_tokens={cost.output_price_per_1m_tokens}")
                print(f"estimated_cost={cost.estimated_cost}")
                print(f"currency={cost.currency}")
            elif args.cmd == "show-llm-config":
                effective_plan = "coding_plan" if settings.llm_require_coding_plan else settings.llm_plan
                print(f"llm_plan={settings.llm_plan}")
                print(f"effective_llm_plan={effective_plan}")
                print(f"model={settings.model_name}")
                print(f"planning_model={settings.llm_planning_model}")
                print(f"draft_model={settings.llm_draft_model}")
                print(f"revision_model={settings.llm_revision_model}")
                print(f"review_model={settings.llm_review_model}")
                print(f"ark_base_url={settings.ark_base_url}")
                print(f"ark_api_key_configured={bool(settings.ark_api_key)}")
                print(f"ark_agent_plan_api_key_configured={bool(settings.ark_agent_plan_api_key)}")
                print(f"ark_search_api_key_configured={bool(settings.ark_search_api_key)}")
                print(f"ark_embedding_model={settings.ark_embedding_model}")
                print(f"ark_vision_model={settings.ark_vision_model}")
                print(f"ark_image_model={settings.ark_image_model}")
                print(f"ark_video_model={settings.ark_video_model}")
                print(f"legacy_require_coding_plan={settings.llm_require_coding_plan}")
                print(f"temperature={settings.llm_temperature}")
                print(f"planning_temperature={settings.llm_planning_temperature}")
                print(f"draft_temperature={settings.llm_draft_temperature}")
                print(f"revision_temperature={settings.llm_revision_temperature}")
                print(f"review_temperature={settings.llm_review_temperature}")
                print(f"draft_max_tokens={settings.llm_draft_max_tokens}")
                print(f"revision_max_tokens={settings.llm_revision_max_tokens}")
                print(f"review_max_tokens={settings.llm_review_max_tokens}")
                print(f"smoke_max_tokens={settings.llm_smoke_max_tokens}")
                print(f"request_timeout_seconds={settings.llm_request_timeout_seconds}")
                print(f"input_price_per_1m_tokens={settings.llm_input_price_per_1m_tokens}")
                print(f"output_price_per_1m_tokens={settings.llm_output_price_per_1m_tokens}")
            elif args.cmd == "live-llm-smoke":
                if not args.yes:
                    raise ValueError("live-llm-smoke requires --yes because it calls the real LLM API")
                result = run_live_llm_smoke(session, book_id=args.book_id or None)
                print(f"passed={result.passed}")
                print(f"provider={result.provider}")
                print(f"model={result.model}")
                print(f"request_id={result.request_id}")
                print(f"llm_request_log_id={result.llm_request_log_id or ''}")
                print(f"estimated_total_tokens={result.estimated_total_tokens}")
                print(f"elapsed_ms={result.elapsed_ms}")
                print(f"error_category={result.error_category}")
                print(f"error={result.error}")
                print(f"text={result.text}")
                if not result.passed:
                    session.commit()
                    raise SystemExit(1)
            elif args.cmd == "quality-trends":
                trend = build_quality_trends(session, book_id=args.book_id, limit=args.limit)
                weak_counts = ",".join(f"{key}={value}" for key, value in trend.weak_dimension_counts.items())
                print(f"book_id={trend.book_id}")
                print(f"report_count={trend.report_count}")
                print(f"passed_count={trend.passed_count}")
                print(f"failed_count={trend.failed_count}")
                print(f"average_score={trend.average_score}")
                print(f"weak_dimensions={weak_counts}")
                for item in trend.snapshots:
                    print(
                        "\t".join(
                            [
                                "quality",
                                f"chapter={item.chapter_number}",
                                f"version_id={item.version_id}",
                                f"quality_report_id={item.quality_report_id}",
                                f"score={item.score}",
                                f"passed={item.passed}",
                                f"issue_count={item.issue_count}",
                                f"weak={','.join(item.weak_dimensions)}",
                            ]
                        )
                    )
            elif args.cmd == "quality-calibration":
                report = build_quality_calibration(
                    session,
                    book_id=args.book_id,
                    limit=args.limit,
                    max_failure_rate=args.max_failure_rate,
                    min_average_score=args.min_average_score,
                )
                weak_counts = ",".join(f"{key}={value}" for key, value in report.weak_dimension_counts.items())
                blockers = ",".join(report.blockers)
                print(f"quality_calibration book_id={report.book_id}")
                print(f"report_count={report.report_count}")
                print(f"passed_count={report.passed_count}")
                print(f"failed_count={report.failed_count}")
                print(f"failure_rate={report.failure_rate}")
                print(f"average_score={report.average_score}")
                print(f"auto_revision_brief_count={report.auto_revision_brief_count}")
                print(f"auto_revision_brief_coverage={report.auto_revision_brief_coverage}")
                print(f"ready_for_trial={report.ready_for_trial}")
                print(f"weak_dimensions={weak_counts}")
                print(f"blockers={blockers}")
            elif args.cmd == "story-alignment-audit":
                audit = build_story_alignment_audit(
                    session,
                    book_id=args.book_id,
                    chapter_limit=args.chapter_limit,
                )
                print(f"story_alignment book_id={audit.book_id}")
                print(f"title={audit.book_title}")
                print(f"status={audit.status}")
                print(f"score={audit.score}")
                print(f"blockers={';'.join(audit.blockers)}")
                for item in audit.recommendations:
                    print(f"recommendation={item}")
                for source in audit.source_summary:
                    print(
                        "\t".join(
                            [
                                "source",
                                f"kind={source.kind}",
                                f"name={source.name}",
                                f"positive={','.join(source.positive_hits)}",
                                f"negative={','.join(source.negative_hits)}",
                                f"stale={','.join(source.stale_hits)}",
                                f"preview={source.preview}",
                            ]
                        )
                    )
                for chapter in audit.chapters:
                    print(
                        "\t".join(
                            [
                                "chapter",
                                f"number={chapter.chapter_number}",
                                f"brief_id={chapter.brief_id or ''}",
                                f"brief_status={chapter.brief_status}",
                                f"brief_positive={','.join(chapter.brief_positive_hits)}",
                                f"brief_negative={','.join(chapter.brief_negative_hits)}",
                                f"brief_stale={','.join(chapter.brief_stale_hits)}",
                                f"version_id={chapter.latest_version_id or ''}",
                                f"version_status={chapter.latest_version_status}",
                                f"content_chars={chapter.content_chars}",
                                f"content_positive={','.join(chapter.content_positive_hits)}",
                                f"content_negative={','.join(chapter.content_negative_hits)}",
                            ]
                        )
                    )
            elif args.cmd == "chapter-bias-audit":
                chapter = session.scalar(
                    select(Chapter).where(Chapter.book_id == args.book_id, Chapter.chapter_number == args.chapter_number)
                )
                if not chapter:
                    raise ValueError("chapter not found")
                brief = session.scalar(
                    select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc())
                )
                version = latest_chapter_version(session, chapter_id=chapter.id)
                canon_context, _ = format_canon_context(session, book_id=args.book_id, chapter_number=args.chapter_number)
                report = evaluate_generation_bias(
                    content=version.content if version else "",
                    goal=brief.goal if brief else "",
                    required_beats=brief.required_beats if brief else "",
                    constraints=brief.constraints if brief else "",
                    canon_context=canon_context,
                )
                print(f"chapter_bias book_id={args.book_id}")
                print(f"chapter_number={args.chapter_number}")
                print(f"version_id={version.id if version else ''}")
                print(f"version_status={version.status if version else ''}")
                print(f"passed={report.passed}")
                print(f"system_bias_hits={','.join(report.system_bias_hits)}")
                print(f"model_bias_hits={','.join(report.model_bias_hits)}")
                print(f"blockers={';'.join(report.blockers)}")
                print(f"warnings={';'.join(report.warnings)}")
            elif args.cmd == "list-publish-jobs":
                for job in list_publish_jobs(session, status=args.status):
                    print(f"{job.id}\tversion={job.chapter_version_id}\t{job.platform}\t{job.status}\tpayload={job.automation_payload}")
            elif args.cmd == "show-publish-job":
                job = get_publish_job(session, job_id=args.job_id)
                print(f"id={job.id}")
                print(f"version_id={job.chapter_version_id}")
                print(f"platform={job.platform}")
                print(f"status={job.status}")
                print(f"automation_payload={pretty_json(job.automation_payload)}")
                print(f"result_report={job.result_report}")
            elif args.cmd == "list-publishing-targets":
                for target in list_publishing_targets(session, platform=args.platform, status=args.status):
                    print(
                        "\t".join(
                            [
                                str(target.id),
                                f"platform={target.platform}",
                                f"account={target.account_label}",
                                f"work={target.work_identifier}",
                                f"mode={target.automation_mode}",
                                f"status={target.status}",
                                f"config={target.config_json}",
                            ]
                        )
                    )
            elif args.cmd == "publish-job-dry-run":
                job = publish_job_dry_run(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
                print(f"report={job.result_report}")
            elif args.cmd == "queue-publish-job":
                job = queue_publish_job(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "execute-publish-job":
                job, execution = execute_publish_job(session, job_id=args.job_id, confirm=args.confirm)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
                print(f"publish_execution_id={execution.id}")
                print(f"execution_status={execution.status}")
                print(f"automation_mode={execution.automation_mode}")
                print(f"artifact_path={execution.artifact_path}")
                print(f"report={execution.report}")
            elif args.cmd == "list-publish-executions":
                for execution in list_publish_executions(
                    session,
                    job_id=args.job_id or None,
                    limit=args.limit,
                ):
                    print(
                        "\t".join(
                            [
                                str(execution.id),
                                f"job={execution.publish_job_id}",
                                f"platform={execution.platform}",
                                f"status={execution.status}",
                                f"mode={execution.automation_mode}",
                                f"artifact={execution.artifact_path}",
                                f"report={execution.report}",
                            ]
                        )
                    )
            elif args.cmd == "mark-publish-job":
                job = mark_publish_job(session, job_id=args.job_id, status=args.status, report=args.report)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "retry-publish-job":
                job = retry_publish_job(session, job_id=args.job_id)
                print(f"publish_job_id={job.id}")
                print(f"status={job.status}")
            elif args.cmd == "database-health":
                health = check_database_health(session)
                print(f"database_url={health.database_url}")
                print(f"sqlite_path={health.sqlite_path}")
                print(f"table_count={health.table_count}")
                print(f"migration_count={health.migration_count}")
                print(f"latest_migration={health.latest_migration}")
                print(f"backup_count={health.backup_count}")
                print("tables=" + ",".join(health.tables))
            elif args.cmd == "schema-version":
                report = check_schema_version(session)
                print(f"database_url={report.database_url}")
                print(f"status={report.status}")
                print("current_versions=" + ",".join(report.current_versions))
                print(f"expected_head={report.expected_head}")
                print(f"migration_count={report.migration_count}")
                print(f"latest_migration={report.latest_migration}")
                print(f"message={report.message}")
            elif args.cmd == "backup-database":
                backup = create_database_backup(session, label=args.label)
                print(f"database_backup_id={backup.id}")
                print(f"status={backup.status}")
                print(f"backup_path={backup.backup_path}")
                print(f"size_bytes={backup.size_bytes}")
            elif args.cmd == "list-database-backups":
                for backup in list_database_backups(session, limit=args.limit):
                    print(
                        "\t".join(
                            [
                                str(backup.id),
                                f"status={backup.status}",
                                f"size_bytes={backup.size_bytes}",
                                f"path={backup.backup_path}",
                            ]
                        )
                    )
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
            elif args.cmd == "create-market-research-pack":
                payload = create_market_research_pack(
                    session,
                    genre=args.genre,
                    query=args.query,
                    platform=args.platform,
                )
                print(f"artifact_path={payload['artifact_path']}")
                print("queries=" + " | ".join(payload["queries"]))
            elif args.cmd == "ingest-market-research-results":
                if args.result_path:
                    raw_result = Path(args.result_path).read_text(encoding="utf-8")
                else:
                    raw_result = args.result_json
                if not raw_result:
                    raise ValueError("provide --result-json or --result-path")
                result = ingest_market_research_results(
                    session,
                    genre=args.genre,
                    result_json=raw_result,
                    source_prefix=args.source_prefix,
                )
                print(f"source_ids={','.join(str(item) for item in result['source_ids'])}")
                print(f"market_signal_ids={','.join(str(item) for item in result['market_signal_ids'])}")
            elif args.cmd == "index-book-knowledge":
                result = index_book_knowledge(
                    session,
                    book_id=args.book_id,
                    dry_run=not args.live_embedding,
                    reset=args.reset,
                    limit_chapters=args.limit_chapters,
                )
                print(f"book_id={result['book_id']}")
                print(f"indexed_count={result['indexed_count']}")
                print(f"embedding_ids={','.join(str(item) for item in result['embedding_ids'])}")
            elif args.cmd == "retrieve-book-knowledge":
                for hit in retrieve_book_knowledge(
                    session,
                    book_id=args.book_id,
                    query=args.query,
                    limit=args.limit,
                    dry_run=not args.live_embedding,
                ):
                    print(
                        "\t".join(
                            [
                                f"embedding_id={hit.embedding_id}",
                                f"score={hit.score:.4f}",
                                f"type={hit.source_type}",
                                f"ref={hit.source_ref_id}",
                                f"label={hit.source_label}",
                                f"text={hit.text}",
                            ]
                        )
                    )
            elif args.cmd == "create-visual-asset":
                asset = create_visual_asset(
                    session,
                    book_id=args.book_id,
                    asset_type=args.asset_type,
                    chapter_number=args.chapter_number or None,
                    style=args.style,
                    dry_run=not args.live,
                )
                print(f"visual_asset_id={asset.id}")
                print(f"status={asset.status}")
                print(f"model={asset.model}")
                print(f"artifact_path={asset.artifact_path}")
            elif args.cmd == "list-visual-assets":
                for asset in list_visual_assets(session, book_id=args.book_id, asset_type=args.asset_type, limit=args.limit):
                    print(
                        "\t".join(
                            [
                                str(asset.id),
                                f"type={asset.asset_type}",
                                f"chapter_id={asset.chapter_id or ''}",
                                f"status={asset.status}",
                                f"model={asset.model}",
                                f"artifact={asset.artifact_path}",
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
            elif args.cmd == "create-feedback-adjustment":
                adjustment = create_feedback_adjustment(
                    session,
                    book_id=args.book_id,
                    target_chapter_number=args.target_chapter_number,
                    feedback_ids=args.feedback_id,
                    adjustment_text=args.adjustment_text,
                )
                print(f"feedback_adjustment_id={adjustment.id}")
                print(f"book_id={adjustment.book_id}")
                print(f"target_chapter_number={adjustment.target_chapter_number}")
                print(f"feedback_ids={adjustment.feedback_ids}")
                print(f"status={adjustment.status}")
                print(f"adjustment_text={adjustment.adjustment_text}")
                _print_revision_decision(adjustment.adjustment_text)
                if args.apply_to_brief:
                    brief = apply_feedback_adjustment_to_brief(session, adjustment_id=adjustment.id)
                    print(f"brief_id={brief.id}")
                    print(f"applied_status=applied")
            elif args.cmd == "list-feedback-adjustments":
                for adjustment in list_feedback_adjustments(
                    session,
                    book_id=args.book_id,
                    status=args.status,
                    limit=args.limit,
                ):
                    print(
                        "\t".join(
                            [
                                str(adjustment.id),
                                f"book={adjustment.book_id}",
                                f"target_chapter={adjustment.target_chapter_number}",
                                f"feedback_ids={adjustment.feedback_ids}",
                                f"status={adjustment.status}",
                                f"text={adjustment.adjustment_text}",
                            ]
                        )
                    )
            elif args.cmd == "apply-feedback-adjustment":
                brief = apply_feedback_adjustment_to_brief(session, adjustment_id=args.adjustment_id)
                print(f"brief_id={brief.id}")
                print(f"chapter_id={brief.chapter_id}")
                print(f"status={brief.status}")
            elif args.cmd == "submit-revision-suggestion":
                feedback, adjustment, brief, version = submit_revision_suggestion(
                    session,
                    book_id=args.book_id,
                    chapter_number=args.chapter_number,
                    suggestion_text=args.suggestion,
                    platform=args.platform,
                    revision_mode=args.revision_mode,
                )
                print(f"feedback_id={feedback.id}")
                print(f"feedback_adjustment_id={adjustment.id}")
                print(f"brief_id={brief.id}")
                print(f"brief_status={brief.status}")
                print(f"latest_version_id={version.id if version else ''}")
                print(f"latest_version_status={version.status if version else ''}")
                _print_revision_decision(adjustment.adjustment_text)
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
