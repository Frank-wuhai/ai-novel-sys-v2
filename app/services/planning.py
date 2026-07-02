from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, PublishJob, QualityReport, StoryArc
from app.services.chapter_standards import ensure_chapter_production_standard
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.continuity import default_chapter_continuity_summary, record_chapter_continuity
from app.services.context_contamination import audit_context_contamination, context_anchor_lines
from app.services.execution_mode import ExecutionMode, execution_mode_from_flags
from app.services.feedback import format_chapter_sample_adoption_context, submit_revision_suggestion
from app.services.llm_queue import QUEUE_DRAFT, QUEUE_REBUILD_CANDIDATES, QUEUE_REVISE, enqueue_draft_chapter, enqueue_rebuild_candidates, enqueue_revise_chapter
from app.services.legacy_trace_cleanup import cleanup_active_production_traces
from app.services.pre_draft_inputs import evaluate_pre_draft_inputs, latest_sample_task_id
from app.services.production_actions import AUTO_ACTIONS, MANUAL_ACTIONS
from app.services.production_orchestrator import ProductionSituation, decide_production_route
from app.services.production_strategy import assess_production_strategy
from app.services.production import (
    create_chapter_brief,
    create_publish_job,
    create_revision_brief,
    draft_chapter,
    publish_job_dry_run,
    queue_publish_job,
    retry_publish_job,
    revise_chapter,
    review_chapter,
)
from app.services.chapter_samples import adopt_chapter_sample, generate_chapter_samples
from app.services.dashboard_production_actions import repair_chapter_brief
from app.services.production_state import next_version_number
from app.services.production_gate import check_production_gate
from app.services.rebuild_candidates import generate_rebuild_candidates
from app.services.revision_supervisor import apply_revision_budget_recovery, persistent_revision_budget
from app.services.revision_success_boost import apply_revision_success_boost
from app.services.story import arcs_for_chapter, list_story_arcs
from app.services.story_dna import chapter_engine_for_number, story_dna_for_book
from app.workflows.state_machine import move


@dataclass(frozen=True)
class ChapterPlanItem:
    chapter_number: int
    chapter_id: int | None
    brief_id: int | None
    latest_version_id: int | None
    latest_version_status: str
    latest_quality_passed: bool | None
    publish_job_id: int | None
    publish_job_status: str
    next_action: str
    reason: str


@dataclass(frozen=True)
class RunNextActionResult:
    chapter_number: int
    action: str
    status: str
    message: str
    object_id: int | None = None


@dataclass(frozen=True)
class BookCycleResult:
    executed: list[RunNextActionResult]
    blocked: list[ChapterPlanItem]
    done: list[ChapterPlanItem]


@dataclass(frozen=True)
class HumanDecisionItem:
    decision_type: str
    chapter_number: int
    chapter_id: int | None
    version_id: int | None
    publish_job_id: int | None
    reason: str
    command_hint: str


@dataclass(frozen=True)
class HumanDecisionPackage:
    items: list[HumanDecisionItem]
    continuity_count: int
    approval_count: int
    publish_count: int
    inspect_count: int


TeamDecisionItem = HumanDecisionItem
TeamDecisionPackage = HumanDecisionPackage


@dataclass(frozen=True)
class ChapterBriefFields:
    goal: str
    required_beats: str
    constraints: str


def create_chapter_plan(
    session: Session,
    *,
    book_id: int,
    start: int,
    count: int,
    goal_prefix: str,
    required_beats: str = "",
    constraints: str = "",
) -> list[ChapterBrief]:
    if start < 1:
        raise ValueError("start must be >= 1")
    if count < 1:
        raise ValueError("count must be >= 1")
    created: list[ChapterBrief] = []
    for chapter_number in range(start, start + count):
        chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
        if chapter and _latest_brief(session, chapter_id=chapter.id):
            continue
        fields = _chapter_brief_fields(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            goal_prefix=goal_prefix,
            required_beats=required_beats,
            constraints=constraints,
        )
        brief = create_chapter_brief(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            goal=fields.goal,
            required_beats=fields.required_beats,
            constraints=fields.constraints,
        )
        created.append(brief)
    return created


def create_arc_chapter_plan(
    session: Session,
    *,
    book_id: int,
    arc_number: int,
    required_beats: str = "",
    constraints: str = "",
) -> list[ChapterBrief]:
    arc = next((item for item in list_story_arcs(session, book_id=book_id) if item.arc_number == arc_number), None)
    if not arc:
        raise ValueError(f"story arc not found: {arc_number}")
    return create_chapter_plan(
        session,
        book_id=book_id,
        start=arc.start_chapter,
        count=arc.end_chapter - arc.start_chapter + 1,
        goal_prefix=arc.title or f"剧情段{arc.arc_number}",
        required_beats=required_beats,
        constraints=constraints,
    )


def upgrade_chapter_briefs_production_standards(session: Session, *, book_id: int) -> int:
    chapters = list(session.scalars(select(Chapter).where(Chapter.book_id == book_id)))
    updated = 0
    for chapter in chapters:
        brief = _latest_brief(session, chapter_id=chapter.id)
        if not brief:
            continue
        arc = next(iter(arcs_for_chapter(session, book_id=book_id, chapter_number=chapter.chapter_number, limit=1)), None)
        phase = _arc_phase(arc, chapter.chapter_number) if arc else ""
        arc_goal = arc.goal if arc else ""
        updated_constraints = ensure_chapter_production_standard(
            brief.constraints,
            chapter_number=chapter.chapter_number,
            arc_phase=phase,
            arc_goal=arc_goal,
        )
        if updated_constraints != brief.constraints:
            brief.constraints = updated_constraints
            updated += 1
    session.flush()
    return updated


def plan_chapters(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    apply_state_repairs: bool = True,
) -> list[ChapterPlanItem]:
    if start < 1:
        raise ValueError("start must be >= 1")
    if count < 1:
        raise ValueError("count must be >= 1")
    return [
        _plan_one(session, book_id=book_id, chapter_number=number, apply_state_repairs=apply_state_repairs)
        for number in range(start, start + count)
    ]


def _should_apply_reading_assessment(quality: QualityReport) -> bool:
    report = _loads_json(quality.report)
    if isinstance(report.get("reading_assessment"), dict):
        return True
    if quality.passed:
        return True
    score = int(quality.score or report.get("score") or 0)
    hard_gate = report.get("hard_gate") if isinstance(report.get("hard_gate"), dict) else {}
    hard_gate_passed = bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS")
    return score >= 70 and hard_gate_passed


def _execute_accept_early_stop(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    item,
) -> RunNextActionResult:
    """Sprint 2 P0-1: promote the best passing version to reviewed_pass and
    flip chapter.status to needs_confirmation. Extracted from run_next_action
    so regressions can drive it directly without going through the state-repair
    replan pass in ``_plan_one`` (which can flip quality.passed via
    ``maybe_apply_reading_assessment`` and shadow the accept_early_stop
    branch).

    See scripts/accept_early_stop_advance_regression.py — before this fix,
    accept_early_stop only mutated chapter.status. The best version stayed at
    ``needs_revision`` and the next orchestrator tick routed straight back
    into ``_decide_revision_route`` → ``accept_early_stop`` forever.
    """
    action = "accept_early_stop"
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if chapter is None or not item.latest_version_id:
        return RunNextActionResult(
            chapter_number,
            action,
            "executed",
            item.reason or "early-stop triggered without chapter/version",
            item.latest_version_id,
        )
    if chapter.status != "needs_confirmation":
        chapter.status = "needs_confirmation"
        session.add(chapter)
        session.flush()

    # Locate the version early-stop deemed acceptable.
    from app.services.production_state import collect_version_scores
    from app.services.revision_early_stop import evaluate_early_stop

    version_scores = collect_version_scores(session, chapter.id)
    best_version_number: int | None = None
    if version_scores:
        decision = evaluate_early_stop(version_scores)
        if decision.should_stop and decision.best_version_number is not None:
            best_version_number = decision.best_version_number
    promoted_version_id: int | None = None
    if best_version_number is not None:
        best_v = session.scalar(
            select(ChapterVersion).where(
                ChapterVersion.chapter_id == chapter.id,
                ChapterVersion.version_number == best_version_number,
            )
        )
        if best_v is not None and best_v.status == "needs_revision":
            best_v.status = "reviewed_pass"
            session.add(best_v)
            session.flush()
            promoted_version_id = best_v.id

    from app.models.entities import PlatformFeedback  # local import — avoid cycle
    session.add(
        PlatformFeedback(
            book_id=book_id,
            chapter_id=chapter.id,
            platform="production_kernel",
            metric_name="revision_early_stop",
            metric_value=str(promoted_version_id or item.latest_version_id),
            raw_text=(item.reason or "early-stop triggered")[:1000],
        )
    )
    session.flush()
    return RunNextActionResult(
        chapter_number,
        action,
        "executed",
        item.reason or "early-stop 已停止修订循环，等待作者确认最佳版本。",
        item.latest_version_id,
    )


def run_next_action(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal_prefix: str = "自动规划",
    required_beats: str = "",
    constraints: str = "",
    platform: str = "manual",
    dry_run: bool = True,
    queue_generation: bool = False,
    preview_only: bool = False,
    mode: ExecutionMode | str | None = None,
) -> RunNextActionResult:
    execution_mode = execution_mode_from_flags(dry_run=dry_run, preview_only=preview_only, mode=mode)
    dry_llm = execution_mode.uses_dry_llm
    queue_heavy_generation = queue_generation or execution_mode.queues_heavy_generation
    item = _plan_one(session, book_id=book_id, chapter_number=chapter_number)
    action = item.next_action
    gate_message = _production_gate_blocker(session, book_id=book_id, action=action)
    if gate_message:
        return RunNextActionResult(chapter_number, action, "blocked", gate_message, None)
    if execution_mode.is_preview:
        return RunNextActionResult(chapter_number, action, "preview", item.reason, item.latest_version_id or item.brief_id or item.publish_job_id)
    if action == "create_chapter_brief":
        fields = _chapter_brief_fields(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            goal_prefix=goal_prefix,
            required_beats=required_beats,
            constraints=constraints,
        )
        brief = create_chapter_brief(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            goal=fields.goal,
            required_beats=fields.required_beats,
            constraints=fields.constraints,
        )
        return RunNextActionResult(chapter_number, action, "executed", "created chapter brief", brief.id)
    if action == "draft_chapter":
        if queue_heavy_generation:
            task = enqueue_draft_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_llm)
            return RunNextActionResult(chapter_number, "enqueue_draft_chapter", "executed", "queued draft generation task", task.id)
        version = draft_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_llm)
        return RunNextActionResult(chapter_number, action, "executed", "created draft version", version.id)
    if action == "generate_chapter_samples":
        task = generate_chapter_samples(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            sample_count=3,
            dry_run=dry_llm,
        )
        return RunNextActionResult(chapter_number, action, "executed", "generated chapter samples for pre-draft direction", task.id)
    if action == "adopt_recommended_chapter_sample":
        chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
        brief = _latest_brief(session, chapter_id=chapter.id) if chapter else None
        permit = evaluate_pre_draft_inputs(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
        task_id = permit.sample_task_id or latest_sample_task_id(session, book_id=book_id, chapter_number=chapter_number)
        if not task_id or not permit.recommended_sample_index:
            return RunNextActionResult(chapter_number, action, "blocked", "no recommended chapter sample is available", None)
        adopted = adopt_chapter_sample(
            session,
            task_id=task_id,
            sample_index=permit.recommended_sample_index,
        )
        return RunNextActionResult(
            chapter_number,
            action,
            "executed",
            f"adopted recommended chapter sample #{permit.recommended_sample_index}",
            adopted.brief_id,
        )
    if action == "repair_chapter_brief":
        brief = repair_chapter_brief(session, book_id=book_id, chapter_number=chapter_number)
        return RunNextActionResult(chapter_number, action, "executed", "repaired current chapter brief before drafting", brief.id)
    if action == "wait_previous_chapter_readable":
        return RunNextActionResult(chapter_number, action, "blocked", item.reason, item.latest_version_id)
    if action == "resolve_deferred_backlog":
        return RunNextActionResult(chapter_number, action, "blocked", item.reason, item.latest_version_id)
    if action == "review_chapter":
        report = review_chapter(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            llm_review=True,
            review_dry_run=dry_run,
        )
        return RunNextActionResult(chapter_number, action, "executed", f"quality passed={report.passed} score={report.score}", report.id)
    if action == "create_revision_brief":
        brief = create_revision_brief(session, book_id=book_id, chapter_number=chapter_number)
        return RunNextActionResult(chapter_number, action, "executed", "created revision brief", brief.id)
    if action == "revise_chapter":
        if not dry_llm:
            chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
            latest_version = _latest_version(session, chapter_id=chapter.id) if chapter else None
            revision_brief = _latest_revision_brief(session, chapter_id=chapter.id) if chapter else None
            if not _revision_budget_guard_should_defer(latest_version, revision_brief):
                budget = persistent_revision_budget(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                    max_full_revisions=settings.revision_persistent_max_full_revisions,
                )
                if budget.exceeded:
                    recovery = apply_revision_budget_recovery(
                        session,
                        book_id=book_id,
                        chapter_number=chapter_number,
                        force_rebuild_reason=budget.reason,
                    )
                    status = "executed" if recovery.status in {"recovered", "restored_readable", "restored_readable_needs_revision"} else "blocked"
                    return RunNextActionResult(
                        chapter_number,
                        "revision_budget_recovery",
                        status,
                        f"{recovery.message} ({budget.reason})",
                        recovery.recovery_brief_id or recovery.recovery_version_id,
                    )
        guard_brief = _maybe_apply_revision_loop_guard(session, book_id=book_id, chapter_number=chapter_number)
        boost = apply_revision_success_boost(session, book_id=book_id, chapter_number=chapter_number)
        if queue_heavy_generation:
            task = enqueue_revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_llm)
            message = "queued revision generation task"
            if guard_brief:
                message = f"revision safety guard applied; queued light revision task with brief {guard_brief.id}"
            elif boost.applied:
                message = f"revision focus boost applied; queued revision task with brief {boost.brief_id}"
            return RunNextActionResult(chapter_number, "enqueue_revise_chapter", "executed", message, task.id)
        version, loop_message = _run_bounded_revision_quality_loop(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            dry_run=dry_llm,
        )
        message = "created revised draft version"
        if guard_brief:
            message = f"revision safety guard applied; created light revised draft version with brief {guard_brief.id}"
        elif boost.applied:
            message = f"revision focus boost applied; created revised draft version with brief {boost.brief_id}"
        if loop_message:
            message = f"{message}; {loop_message}"
        return RunNextActionResult(chapter_number, action, "executed", message, version.id)
    if action == "create_publish_job":
        if not item.latest_version_id:
            raise ValueError("latest version is required to create publish job")
        job = create_publish_job(session, version_id=item.latest_version_id, platform=platform)
        return RunNextActionResult(chapter_number, action, "executed", "created publish job", job.id)
    if action == "publish_job_dry_run":
        if not item.publish_job_id:
            raise ValueError("publish job is required for dry-run")
        job = publish_job_dry_run(session, job_id=item.publish_job_id)
        return RunNextActionResult(chapter_number, action, "executed", "publish dry-run completed", job.id)
    if action == "queue_publish_job":
        if not item.publish_job_id:
            raise ValueError("publish job is required to queue")
        job = queue_publish_job(session, job_id=item.publish_job_id)
        return RunNextActionResult(chapter_number, action, "executed", "publish job queued", job.id)
    if action == "retry_publish_job":
        if not item.publish_job_id:
            raise ValueError("publish job is required to retry")
        job = retry_publish_job(session, job_id=item.publish_job_id)
        return RunNextActionResult(chapter_number, action, "executed", "publish job retried", job.id)
    if action == "record_chapter_continuity":
        summary = default_chapter_continuity_summary(session, book_id=book_id, chapter_number=chapter_number)
        result = record_chapter_continuity(session, book_id=book_id, chapter_number=chapter_number, summary=summary)
        return RunNextActionResult(chapter_number, action, "executed", "recorded chapter continuity", result.chapter_id)
    if action in MANUAL_ACTIONS:
        return RunNextActionResult(chapter_number, action, "blocked", "confirmation required", None)
    if action == "revision_budget_recovery":
        recovery = apply_revision_budget_recovery(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            force_rebuild_reason=item.reason,
        )
        status = "executed" if recovery.status in {"recovered", "restored_readable", "restored_readable_needs_revision"} else "blocked"
        return RunNextActionResult(
            chapter_number,
            "revision_budget_recovery",
            status,
            recovery.message,
            recovery.recovery_brief_id or recovery.recovery_version_id,
        )
    if action == "revision_trend_recovery":
        brief = _apply_revision_trend_recovery(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            reason=item.reason,
        )
        if not brief:
            return RunNextActionResult(chapter_number, action, "blocked", item.reason, item.latest_version_id)
        return RunNextActionResult(
            chapter_number,
            "revision_trend_recovery",
            "executed",
            "修订趋势劣化，已自动回退到近期最佳稿并生成换策略修订单。",
            brief.id,
        )
    if action == "generate_rebuild_candidates":
        if queue_heavy_generation:
            task = enqueue_rebuild_candidates(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_llm, candidate_count=3)
            return RunNextActionResult(chapter_number, action, "queued", "queued rebuild candidate generation task", task.id)
        result = generate_rebuild_candidates(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            candidate_count=3,
            dry_run=dry_llm,
        )
        return RunNextActionResult(
            chapter_number,
            action,
            "executed",
            f"已生成 {result.candidate_count} 个重建候选并选择最高分版本 score={result.selected_score}",
            result.selected_version_id,
        )
    if action == "defer_chapter_for_later":
        return RunNextActionResult(
            chapter_number,
            action,
            "blocked",
            "当前策略已禁止未通过章节暂存后切下一章；请继续回炉修订或多候选重建，直到正式通过。",
            item.latest_version_id,
        )
    if action == "accept_early_stop":
        return _execute_accept_early_stop(
            session, book_id=book_id, chapter_number=chapter_number, item=item
        )
    if action == "done":
        return RunNextActionResult(chapter_number, action, "completed", "chapter is complete", None)
    return RunNextActionResult(chapter_number, action, "blocked", item.reason, None)


_PRODUCTION_GATED_ACTIONS = {
    "draft_chapter",
    "generate_chapter_samples",
    "adopt_recommended_chapter_sample",
    "repair_chapter_brief",
    "enqueue_draft_chapter",
    "review_chapter",
    "revise_chapter",
    "enqueue_revise_chapter",
    "create_publish_job",
    "publish_job_dry_run",
    "queue_publish_job",
    "retry_publish_job",
}


def _production_gate_blocker(session: Session, *, book_id: int, action: str) -> str:
    result = check_production_gate(session, book_id=book_id, action=action)
    return "" if result.passed else result.message


def _run_bounded_revision_quality_loop(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool,
) -> tuple[ChapterVersion, str]:
    max_rounds = max(1, min(3, settings.production_auto_revision_loop_max_rounds))
    version = revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
    if dry_run or max_rounds <= 1:
        return version, ""
    stages: list[str] = [f"round1:revision_v{version.id}"]
    for round_number in range(1, max_rounds + 1):
        quality = review_chapter(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            llm_review=True,
            review_dry_run=dry_run,
        )
        stages.append(f"round{round_number}:review_{quality.score}_{'pass' if quality.passed else 'fail'}")
        if quality.passed or round_number >= max_rounds:
            break
        apply_revision_success_boost(session, book_id=book_id, chapter_number=chapter_number)
        version = revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
        stages.append(f"round{round_number + 1}:revision_v{version.id}")
    return version, "auto loop " + " -> ".join(stages)


def run_book_cycle(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    max_steps: int = 10,
    goal_prefix: str = "自动规划",
    required_beats: str = "",
    constraints: str = "",
    platform: str = "manual",
    dry_run: bool = True,
    queue_generation: bool = False,
) -> BookCycleResult:
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    execution_mode = execution_mode_from_flags(dry_run=dry_run, preview_only=False, mode=None)
    executed: list[RunNextActionResult] = []
    for _ in range(max_steps):
        items = plan_chapters(session, book_id=book_id, start=start, count=count)
        runnable = next((item for item in items if item.next_action in AUTO_ACTIONS), None)
        if not runnable:
            break
        result = run_next_action(
            session,
            book_id=book_id,
            chapter_number=runnable.chapter_number,
            goal_prefix=goal_prefix,
            required_beats=required_beats,
            constraints=constraints,
            platform=platform,
            mode=execution_mode,
            queue_generation=queue_generation,
        )
        if result.status != "executed":
            break
        executed.append(result)

    final_items = plan_chapters(session, book_id=book_id, start=start, count=count)
    blocked = [
        item
        for item in final_items
        if item.next_action in MANUAL_ACTIONS or item.next_action.startswith("inspect") or item.next_action == "wait_generation_task"
    ]
    done = [item for item in final_items if item.next_action == "done"]
    return BookCycleResult(executed=executed, blocked=blocked, done=done)


def build_human_decision_package(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    apply_state_repairs: bool = True,
) -> HumanDecisionPackage:
    items: list[HumanDecisionItem] = []
    for item in plan_chapters(session, book_id=book_id, start=start, count=count, apply_state_repairs=apply_state_repairs):
        decision = _decision_item(item, book_id=book_id)
        if decision:
            items.append(decision)
    return HumanDecisionPackage(
        items=items,
        continuity_count=sum(1 for item in items if item.decision_type == "continuity_writeback"),
        approval_count=sum(1 for item in items if item.decision_type == "human_approval"),
        publish_count=sum(1 for item in items if item.decision_type == "final_publish_confirmation"),
        inspect_count=sum(1 for item in items if item.decision_type == "manual_inspection"),
    )


def build_team_decision_package(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 10,
    apply_state_repairs: bool = True,
) -> TeamDecisionPackage:
    return build_human_decision_package(
        session,
        book_id=book_id,
        start=start,
        count=count,
        apply_state_repairs=apply_state_repairs,
    )


def _decision_item(item: ChapterPlanItem, *, book_id: int) -> HumanDecisionItem | None:
    if item.next_action == "approve_chapter":
        version_id = item.latest_version_id or 0
        return HumanDecisionItem(
            decision_type="human_approval",
            chapter_number=item.chapter_number,
            chapter_id=item.chapter_id,
            version_id=item.latest_version_id,
            publish_job_id=None,
            reason=item.reason,
            command_hint=f"python -m app.cli approve-chapter --version-id {version_id} --reviewer human",
        )
    if item.next_action == "mark_publish_job":
        job_id = item.publish_job_id or 0
        return HumanDecisionItem(
            decision_type="final_publish_confirmation",
            chapter_number=item.chapter_number,
            chapter_id=item.chapter_id,
            version_id=item.latest_version_id,
            publish_job_id=item.publish_job_id,
            reason=item.reason,
            command_hint=f"python -m app.cli mark-publish-job --job-id {job_id} --status published --report \"...\"",
        )
    if item.next_action.startswith("inspect"):
        return HumanDecisionItem(
            decision_type="manual_inspection",
            chapter_number=item.chapter_number,
            chapter_id=item.chapter_id,
            version_id=item.latest_version_id,
            publish_job_id=item.publish_job_id,
            reason=item.reason,
            command_hint=f"python -m app.cli show-chapter --book-id {book_id} --chapter-number {item.chapter_number}",
        )
    return None


def _maybe_apply_revision_loop_guard(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
) -> ChapterBrief | None:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if not chapter:
        return None
    version = _latest_version(session, chapter_id=chapter.id)
    if not version or version.status != "needs_revision":
        return None
    brief = _latest_revision_brief(session, chapter_id=chapter.id)
    quality = _latest_quality(session, version_id=version.id)
    if not brief or not quality or quality.passed:
        return None
    brief_text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if "system_revision_loop_guard" in brief_text:
        return None
    if not _revision_brief_is_heavy(brief_text):
        return None
    if _quality_needs_broader_revision(quality):
        return None
    failed_count = _recent_failed_revision_count(session, chapter_id=chapter.id, limit=4)
    slow_count = _recent_slow_revision_task_count(session, book_id=book_id, chapter_number=chapter_number, limit=4)
    if failed_count < 2 and slow_count < 2:
        return None
    suggestion = _revision_loop_guard_suggestion(
        chapter_number=chapter_number,
        quality=quality,
        failed_count=failed_count,
        slow_count=slow_count,
    )
    _feedback, _adjustment, guard_brief, _version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="system_revision_loop_guard",
        suggestion_text=suggestion,
        revision_mode="local_patch",
    )
    if "修订模式:local_patch" not in (guard_brief.required_beats or ""):
        guard_brief.required_beats = "\n".join([guard_brief.required_beats or "", "修订模式:local_patch"]).strip()
        session.flush()
    return guard_brief


def _quality_needs_broader_revision(quality: QualityReport) -> bool:
    report = _loads_json(quality.report)
    dimensions = report.get("dimensions") if isinstance(report.get("dimensions"), dict) else {}
    broader_dimension_thresholds = {
        "chapter_necessity": 55,
        "scene_atmosphere": 45,
        "payoff_grounding": 55,
        "character_action": 65,
        "chapter_unit_flow": 63,
    }
    if any(int(dimensions.get(name) or 100) < threshold for name, threshold in broader_dimension_thresholds.items()):
        return True
    review = report.get("llm_review") if isinstance(report.get("llm_review"), dict) else {}
    review_text = "；".join(str(item) for item in [*(review.get("issues") or []), *(review.get("revision_suggestions") or [])])
    broader_markers = (
        "文笔生硬",
        "感情基调",
        "心理活动",
        "爽点",
        "奖励感",
        "章末钩子",
        "主角反应不自然",
        "单元目标",
        "节奏",
    )
    return any(marker in review_text for marker in broader_markers)


def _plan_one(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    apply_state_repairs: bool = True,
) -> ChapterPlanItem:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if not chapter:
        blocker_action, blocker_reason = _pre_chapter_creation_blocker(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
        )
        if blocker_action:
            return ChapterPlanItem(
                chapter_number=chapter_number,
                chapter_id=None,
                brief_id=None,
                latest_version_id=None,
                latest_version_status="missing",
                latest_quality_passed=None,
                publish_job_id=None,
                publish_job_status="",
                next_action=blocker_action,
                reason=blocker_reason,
            )
        return ChapterPlanItem(
            chapter_number=chapter_number,
            chapter_id=None,
            brief_id=None,
            latest_version_id=None,
            latest_version_status="missing",
            latest_quality_passed=None,
            publish_job_id=None,
            publish_job_status="",
            next_action="create_chapter_brief",
            reason="chapter and brief are missing",
        )

    if apply_state_repairs:
        cleanup_active_production_traces(session, book_id=book_id)
        _restore_reconciled_readable_version(session, chapter_id=chapter.id)
    brief = _latest_brief(session, chapter_id=chapter.id)
    version = _latest_version(session, chapter_id=chapter.id)
    quality = _latest_quality(session, version_id=version.id) if version else None
    job = _latest_publish_job(session, version_id=version.id) if version else None
    active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if apply_state_repairs and _accept_passed_budget_recovery_version(
        session,
        version=version,
        quality=quality,
        active_revision_brief=active_revision_brief,
    ):
        version = _latest_version(session, chapter_id=chapter.id)
        quality = _latest_quality(session, version_id=version.id) if version else None
        active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if apply_state_repairs and _close_stale_passed_revision_contract(
        session,
        version=version,
        quality=quality,
        active_revision_brief=active_revision_brief,
    ):
        version = _latest_version(session, chapter_id=chapter.id)
        quality = _latest_quality(session, version_id=version.id) if version else None
        active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if (
        apply_state_repairs
        and version
        and quality
        and version.status in {"reviewed_pass", "approved"}
        and _quality_report_has_unresolved_gate_blocker(quality)
    ):
        from app.services.reading_assessment import maybe_apply_reading_assessment

        maybe_apply_reading_assessment(session, book_id=book_id, chapter_number=chapter_number, quality=quality)
        version = _latest_version(session, chapter_id=chapter.id)
        quality = _latest_quality(session, version_id=version.id) if version else None
        active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if (
        apply_state_repairs
        and version
        and quality
        and version.status in {"reviewed_pass", "approved"}
        and _should_apply_reading_assessment(quality)
        and not _quality_has_formal_reading_approval(quality)
    ):
        from app.services.reading_assessment import maybe_apply_reading_assessment

        maybe_apply_reading_assessment(session, book_id=book_id, chapter_number=chapter_number, quality=quality)
        version = _latest_version(session, chapter_id=chapter.id)
        quality = _latest_quality(session, version_id=version.id) if version else None
        active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if (
        apply_state_repairs
        and
        version
        and version.status == "needs_revision"
        and quality
        and _should_apply_reading_assessment(quality)
        and not _revision_brief_blocks_reading_assessment_takeover(active_revision_brief)
    ):
        from app.services.reading_assessment import maybe_apply_reading_assessment, reading_assessment_requires_revision

        assessment = maybe_apply_reading_assessment(session, book_id=book_id, chapter_number=chapter_number, quality=quality)
        if assessment.action == "approve_ready":
            version = _latest_version(session, chapter_id=chapter.id)
            quality = _latest_quality(session, version_id=version.id) if version else None
            active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
        elif reading_assessment_requires_revision(_loads_json(quality.report)):
            active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if (
        apply_state_repairs
        and
        version
        and quality
        and version.status == "needs_revision"
        and not _revision_brief_blocks_quality_reconcile(active_revision_brief)
    ):
        from app.services.production_reviewing import reconcile_existing_quality_report

        if reconcile_existing_quality_report(session, version=version, quality=quality):
            version = _latest_version(session, chapter_id=chapter.id)
            quality = _latest_quality(session, version_id=version.id) if version else None
            active_revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    if (
        apply_state_repairs
        and version
        and version.status in {"reviewed_pass", "approved"}
        and _revision_brief_blocks_quality_reconcile(active_revision_brief)
    ):
        version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
        quality = _latest_quality(session, version_id=version.id)
        session.flush()
    elif apply_state_repairs and version and version.status in {"reviewed_pass", "approved"}:
        _supersede_active_revision_briefs(session, chapter_id=chapter.id)
        from app.services.chapter_archive import archive_chapter_history_after_readable

        archive_chapter_history_after_readable(session, chapter_id=chapter.id, readable_version_id=version.id)
    if apply_state_repairs:
        _inherit_protected_inputs_into_active_revision_brief(session, book_id=book_id, chapter_number=chapter_number, chapter_id=chapter.id)

    draft_queue_task = None
    revision_queue_task = None
    revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    budget_blocker = ""
    trend_blocker = ""
    should_generate_rebuild_candidates = False
    reading_assessment_requires_revision = False
    protected_review_contract_passed = False
    feedback_marker_without_quality = False
    revision_matches_quality_or_feedback = False
    story_clean_revision_brief = False
    production_strategy = None
    has_sample_adoption = bool(format_chapter_sample_adoption_context(session, book_id=book_id, chapter_number=chapter_number))
    has_continuity_context = _has_previous_chapter_context(session, book_id=book_id, chapter_number=chapter_number)
    if not version:
        draft_queue_task = _active_generation_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_DRAFT)
    elif version.status == "needs_revision":
        revision_queue_task = _active_generation_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_REVISE)
        rebuild_queue_task = _active_generation_queue_task(session, book_id=book_id, chapter_number=chapter_number, queue_type=QUEUE_REBUILD_CANDIDATES)
        if rebuild_queue_task and not revision_queue_task:
            revision_queue_task = rebuild_queue_task
        trend_blocker = (
            ""
            if _revision_brief_defers_trend_recovery(version, revision_brief, quality)
            else _revision_quality_trend_blocker(session, chapter_id=chapter.id)
        )
        if revision_brief:
            budget_blocker = _revision_budget_blocker(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                version=version,
                brief=revision_brief,
            )
            should_generate_rebuild_candidates = _should_generate_rebuild_candidates(
                session,
                chapter_id=chapter.id,
                revision_brief=revision_brief,
                latest_version=version,
            )
            reading_assessment_requires_revision = bool(quality and _reading_assessment_requires_revision(quality))
            protected_review_contract_passed = bool(quality and quality.passed and _revision_brief_has_protected_review_marker(revision_brief))
            feedback_marker_without_quality = bool(_revision_brief_has_feedback_marker(revision_brief) and quality is None)
            revision_matches_quality_or_feedback = bool(
                quality
                and (_revision_brief_matches_quality(revision_brief, quality) or _revision_brief_matches_feedback_reopen(revision_brief, quality))
            )
            story_clean_revision_brief = _revision_brief_is_story_clean(revision_brief)
        production_strategy = assess_production_strategy(
            session,
            chapter_id=chapter.id,
            latest_version=version,
            latest_quality=quality,
            revision_brief=revision_brief,
            has_sample_adoption=has_sample_adoption,
            has_continuity_context=has_continuity_context,
        )
    publish_action, publish_reason = _publish_action(job) if version and version.status == "approved" else ("", "")
    # ---- early-stop signal (phase 2/1b) -----------------------------------
    # Only meaningful once a revision loop is actually in progress: a chapter
    # with a revision_brief that is still iterating. For chapters that never
    # reached revision (draft-only, brief-only), early-stop must not fire.
    early_stop_should_stop = False
    early_stop_reason = ""
    early_stop_best_version = None
    early_stop_best_score = None
    early_stop_triggered_rules: tuple[str, ...] = ()
    if revision_brief is not None:
        from app.services.production_state import collect_version_scores
        from app.services.revision_early_stop import evaluate_early_stop
        from app.services.revision_manual_override import find_active_override_baseline

        version_scores = collect_version_scores(session, chapter.id)
        # Phase 2/7: if the operator manually reopened the loop, prune the
        # history so only versions AFTER the override baseline are visible
        # to the early-stop engine. That naturally re-arms the
        # min_versions_before_stop warm-up (5 fresh versions required
        # before another accept_early_stop can fire).
        override_baseline = find_active_override_baseline(session, chapter_id=chapter.id)
        if override_baseline is not None:
            version_scores = [vs for vs in version_scores if vs.version_number > override_baseline]
        if version_scores:
            decision = evaluate_early_stop(version_scores)
            early_stop_should_stop = decision.should_stop
            early_stop_reason = decision.stop_reason
            early_stop_best_version = decision.best_version_number
            early_stop_best_score = decision.best_score
            early_stop_triggered_rules = decision.triggered_rules
    route = decide_production_route(
        ProductionSituation(
            chapter_number=chapter_number,
            chapter_status=chapter.status,
            has_brief=bool(brief),
            latest_version_status=version.status if version else "missing",
            latest_quality_passed=quality.passed if quality else None,
            publish_action=publish_action,
            publish_reason=publish_reason,
            draft_queue_status=draft_queue_task.status if draft_queue_task else "",
            draft_queue_id=draft_queue_task.id if draft_queue_task else None,
            revision_queue_status=revision_queue_task.status if revision_queue_task else "",
            revision_queue_id=revision_queue_task.id if revision_queue_task else None,
            has_revision_brief=bool(revision_brief),
            has_sample_adoption=has_sample_adoption,
            has_continuity_context=has_continuity_context,
            budget_blocker=budget_blocker,
            trend_blocker=trend_blocker,
            should_generate_rebuild_candidates=should_generate_rebuild_candidates,
            reading_assessment_requires_revision=reading_assessment_requires_revision,
            protected_review_contract_passed=protected_review_contract_passed,
            feedback_marker_without_quality=feedback_marker_without_quality,
            revision_matches_quality_or_feedback=revision_matches_quality_or_feedback,
            story_clean_revision_brief=story_clean_revision_brief,
            strategy_action=production_strategy.action if production_strategy else "",
            strategy_intent=production_strategy.intent if production_strategy else "",
            strategy_reason=production_strategy.reason if production_strategy else "",
            strategy_category=production_strategy.category if production_strategy else "",
            strategy_confidence=production_strategy.confidence if production_strategy else 0,
            strategy_evidence=production_strategy.evidence if production_strategy else (),
            early_stop_should_stop=early_stop_should_stop,
            early_stop_reason=early_stop_reason,
            early_stop_best_version=early_stop_best_version,
            early_stop_best_score=early_stop_best_score,
            early_stop_triggered_rules=early_stop_triggered_rules,
        )
    )
    action, reason = route.action, route.reason
    if apply_state_repairs and action == "draft_chapter":
        permit = evaluate_pre_draft_inputs(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
        if not permit.passed:
            action, reason = permit.action, permit.reason
    if apply_state_repairs and action == "revise_chapter":
        apply_revision_success_boost(session, book_id=book_id, chapter_number=chapter_number)

    return ChapterPlanItem(
        chapter_number=chapter_number,
        chapter_id=chapter.id,
        brief_id=brief.id if brief else None,
        latest_version_id=version.id if version else None,
        latest_version_status=version.status if version else "missing",
        latest_quality_passed=quality.passed if quality else None,
        publish_job_id=job.id if job else None,
        publish_job_status=job.status if job else "",
        next_action=action,
        reason=reason,
    )


def _pre_chapter_creation_blocker(session: Session, *, book_id: int, chapter_number: int) -> tuple[str, str]:
    if chapter_number <= 1:
        return "", ""
    segment_size = 5
    previous_closed_segment_end = ((chapter_number - 1) // segment_size) * segment_size
    if previous_closed_segment_end > 0:
        for chapter in session.scalars(
            select(Chapter)
            .where(
                Chapter.book_id == book_id,
                Chapter.chapter_number <= previous_closed_segment_end,
                Chapter.status == "continuity_deferred",
            )
            .order_by(Chapter.chapter_number)
        ):
            latest = _latest_version(session, chapter_id=chapter.id)
            if latest and latest.status == "needs_revision":
                return (
                    "resolve_deferred_backlog",
                    f"第{chapter_number}章暂停生产：第{chapter.chapter_number}章仍未通过，不能创建下一生产段的新章节。",
                )

    previous_number = chapter_number - 1
    previous = _chapter(session, book_id=book_id, chapter_number=previous_number)
    if not previous:
        return (
            "wait_previous_chapter_readable",
            f"第{chapter_number}章不能先生产：第{previous_number}章还不存在，无法建立读者连续性。",
        )
    latest = _latest_version(session, chapter_id=previous.id)
    if not latest:
        return (
            "wait_previous_chapter_readable",
            f"第{chapter_number}章不能先生产：第{previous_number}章还没有可承接正文。",
        )
    quality = _latest_quality(session, version_id=latest.id)
    if latest.status in {"reviewed_pass", "approved"} and not (quality and quality.passed is False):
        return "", ""
    score = f"，评分 {quality.score}" if quality and quality.score is not None else ""
    verdict = "未通过" if quality and quality.passed is False else "未定稿"
    return (
        "wait_previous_chapter_readable",
        f"第{chapter_number}章暂停生产：第{previous_number}章最新版本 v{latest.version_number} 状态为 {latest.status}{score}，{verdict}。先让上一章进入 reviewed_pass/approved。",
    )


def _revision_brief_blocks_quality_reconcile(brief: ChapterBrief | None) -> bool:
    if not brief or brief.status != "revision_ready":
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        _revision_brief_has_feedback_marker(brief)
        or _revision_brief_has_protected_review_marker(brief)
        or "system_revision_trend_recovery" in text
        or _revision_brief_is_story_clean(brief)
    )


def _quality_report_has_unresolved_gate_blocker(quality: QualityReport | None) -> bool:
    if not quality:
        return False
    data = _loads_json(quality.report)
    issues = [str(item) for item in data.get("issues") or []]
    if any(item.startswith("chapter_type_gate_failed") for item in issues):
        return True
    chapter_type_gate = data.get("chapter_type_gate") if isinstance(data.get("chapter_type_gate"), dict) else {}
    if chapter_type_gate and not bool(chapter_type_gate.get("passed")):
        return True
    hard_gate = data.get("hard_gate") if isinstance(data.get("hard_gate"), dict) else {}
    if hard_gate and not bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS"):
        return True
    return False


def _quality_has_formal_reading_approval(quality: QualityReport | None) -> bool:
    if not quality:
        return False
    from app.services.reading_assessment import reading_assessment_approval_ready

    return reading_assessment_approval_ready(_loads_json(quality.report))


def _defer_chapter_for_later(session: Session, *, book_id: int, chapter_number: int, reason: str) -> int:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if not chapter:
        raise ValueError("chapter not found")
    version = _latest_version(session, chapter_id=chapter.id)
    if not version or version.status != "needs_revision":
        raise ValueError("only a needs_revision chapter can be deferred")
    quality = _latest_quality(session, version_id=version.id)
    if not quality or int(quality.score or 0) < 70:
        raise ValueError("当前章还没有达到可读承接线，不能暂存推进后续章节。")
    compact = " ".join((version.content or "").split())
    ending = compact[-360:] if len(compact) > 360 else compact
    chapter.summary = (
        f"PROVISIONAL_CONTINUITY: 第{chapter_number}章暂存回炉，当前稿 score={quality.score}，"
        f"未达发布标准但可供后续章节临时承接。暂存原因：{reason}。"
        f"章末后果/下一章临时承接：{ending}"
    )
    chapter.status = "continuity_deferred"
    for brief in session.scalars(
        select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
    ):
        text = _revision_brief_text(brief)
        if "deferred_revision_backlog" not in text:
            brief.constraints = "\n".join(
                item
                for item in [
                    brief.constraints or "",
                    "deferred_revision_backlog: 当前章已暂存供后续承接；发布前必须回炉修到正式通过，不得直接发布。",
                ]
                if item
            )
    session.flush()
    return version.id


def _accept_passed_budget_recovery_version(
    session: Session,
    *,
    version: ChapterVersion | None,
    quality: QualityReport | None,
    active_revision_brief: ChapterBrief | None,
) -> bool:
    if not version or not quality or not active_revision_brief:
        return False
    if version.status != "needs_revision" or not quality.passed:
        return False
    if not _active_revision_budget_recovery(version, active_revision_brief):
        return False
    if not _ensure_formal_quality_approval(session, version=version, quality=quality):
        return False
    version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    _supersede_active_revision_briefs(session, chapter_id=version.chapter_id)
    session.flush()
    return True


def _close_stale_passed_revision_contract(
    session: Session,
    *,
    version: ChapterVersion | None,
    quality: QualityReport | None,
    active_revision_brief: ChapterBrief | None,
) -> bool:
    if not version or not quality or not active_revision_brief:
        return False
    if version.status != "needs_revision" or not quality.passed:
        return False
    if not _ensure_formal_quality_approval(session, version=version, quality=quality):
        return False
    if not _revision_brief_blocks_quality_reconcile(active_revision_brief):
        return False
    if not _revision_brief_mentions_any_version(active_revision_brief):
        return False
    if _revision_brief_targets_version(active_revision_brief, version):
        return False
    active_revision_brief.status = "superseded"
    version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    session.flush()
    return True


def _ensure_formal_quality_approval(
    session: Session,
    *,
    version: ChapterVersion,
    quality: QualityReport,
) -> bool:
    chapter = session.get(Chapter, version.chapter_id)
    if not chapter:
        return False
    from app.services.reading_assessment import maybe_apply_reading_assessment, reading_assessment_approval_ready

    assessment = maybe_apply_reading_assessment(
        session,
        book_id=chapter.book_id,
        chapter_number=chapter.chapter_number,
        quality=quality,
    )
    if assessment.action != "approve_ready":
        return False
    return reading_assessment_approval_ready(_loads_json(quality.report)) and bool(quality.passed)


def _revision_brief_blocks_reading_assessment_takeover(brief: ChapterBrief | None) -> bool:
    if not brief or brief.status != "revision_ready":
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        _revision_brief_has_feedback_marker(brief)
        or "system_revision_trend_recovery" in text
        or _revision_brief_is_budget_recovery(brief)
        or "clean_rebuild_contract" in text
    )


def _supersede_active_revision_briefs(session: Session, *, chapter_id: int) -> int:
    changed = 0
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"
        changed += 1
    if changed:
        session.flush()
    return changed


def _restore_reconciled_readable_version(session: Session, *, chapter_id: int) -> ChapterVersion | None:
    latest = _latest_version(session, chapter_id=chapter_id)
    if not latest or latest.status in {"reviewed_pass", "approved", "draft"}:
        return None
    active_revision_brief = _latest_revision_brief(session, chapter_id=chapter_id)
    if _revision_brief_blocks_quality_reconcile(active_revision_brief):
        return None
    from app.services.production_reviewing import reconcile_existing_quality_report

    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(8)
        )
    )
    reconciled: list[tuple[ChapterVersion, QualityReport]] = []
    for version in versions:
        quality = _latest_quality(session, version_id=version.id)
        if not quality:
            continue
        if reconcile_existing_quality_report(session, version=version, quality=quality):
            reconciled.append((version, quality))
    if not reconciled:
        return None
    source_version, source_quality = max(reconciled, key=lambda row: (int(row[1].score or 0), int(row[0].id or 0)))
    latest = _latest_version(session, chapter_id=chapter_id)
    if latest and latest.id == source_version.id:
        return source_version
    restored = ChapterVersion(
        chapter_id=chapter_id,
        version_number=next_version_number(session, chapter_id),
        title=source_version.title,
        content=source_version.content,
        status="reviewed_pass",
        source=f"quality_reconcile:v{source_version.id}",
    )
    session.add(restored)
    session.flush()
    report = _loads_json(source_quality.report)
    report["reconciled_from_version_id"] = source_version.id
    report["reconcile_reason"] = "历史稿硬门禁通过且主编审稿通过，按当前审稿规则恢复为可读稿。"
    session.add(
        QualityReport(
            chapter_version_id=restored.id,
            score=int(source_quality.score or report.get("score") or 75),
            passed=True,
            report=json.dumps(report, ensure_ascii=False),
        )
    )
    _supersede_active_revision_briefs(session, chapter_id=chapter_id)
    session.flush()
    return restored


def _active_revision_trend_recovery(version: ChapterVersion, brief: ChapterBrief | None) -> bool:
    if not str(version.source or "").startswith("revision_recovery:") or not brief:
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return "system_revision_trend_recovery" in text


def _revision_brief_defers_trend_recovery(
    version: ChapterVersion | None,
    brief: ChapterBrief | None,
    quality: QualityReport | None,
) -> bool:
    if not version or not brief or brief.status != "revision_ready":
        return False
    if _active_revision_trend_recovery(version, brief) or _revision_brief_is_rebuild_recovery(brief):
        return True
    if _revision_brief_has_protected_review_marker(brief):
        return True
    if _active_revision_budget_recovery(version, brief):
        return True
    return bool(quality and _reading_assessment_requires_revision(quality) and _revision_brief_has_protected_review_marker(brief))


def _revision_brief_is_rebuild_recovery(brief: ChapterBrief | None) -> bool:
    if not brief:
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return "system_revision_budget_recovery" in text and ("修订模式:rewrite" in text or "coverage_rebuild:" in text)


def _revision_brief_is_budget_recovery(brief: ChapterBrief | None) -> bool:
    if not brief:
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        "system_revision_budget_recovery" in text
        or "persistent_revision_budget:" in text
        or "自动修订预算触顶" in text
    )


def _active_revision_budget_recovery(version: ChapterVersion | None, brief: ChapterBrief | None) -> bool:
    if not version:
        return False
    source = str(version.source or "")
    if not source.startswith(("revision_budget_recovery:", "revision_budget_readable_restore:")):
        return False
    if not brief:
        return True
    return True


def _revision_budget_guard_should_defer(version: ChapterVersion | None, brief: ChapterBrief | None) -> bool:
    if _active_revision_budget_recovery(version, brief):
        return True
    if _revision_brief_targets_version(brief, version):
        return True
    if version and brief and str(version.source or "").startswith("revision_budget_readable_restore:"):
        return True
    if version and str(version.source or "").startswith("rebuild_candidate_selected:") and _revision_brief_has_protected_review_marker(brief):
        return True
    return False


def _revision_brief_targets_version(brief: ChapterBrief | None, version: ChapterVersion | None) -> bool:
    if not brief or not version:
        return False
    text = _revision_brief_text(brief)
    version_label = rf"v{int(version.id)}(?!\d)"
    current_markers = (
        rf"合同当前底稿\s*[：:]\s*{version_label}",
        rf"源版本锁定\s*[：:]\s*{version_label}",
        rf"当前待修底稿\s*[：:]\s*{version_label}",
        rf"以\s*{version_label}\s*为底稿",
        rf"source_version_id\s*[=:]\s*{int(version.id)}(?!\d)",
    )
    return any(re.search(pattern, text) for pattern in current_markers)


def _revision_brief_mentions_any_version(brief: ChapterBrief | None) -> bool:
    if not brief:
        return False
    return bool(re.search(r"v\d+(?!\d)|source_version_id\s*[=:]\s*\d+", _revision_brief_text(brief)))


def _revision_brief_text(brief: ChapterBrief) -> str:
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])


def _revision_budget_blocker(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    version: ChapterVersion | None,
    brief: ChapterBrief | None,
) -> str:
    if _revision_budget_guard_should_defer(version, brief):
        return ""
    budget = persistent_revision_budget(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        max_full_revisions=settings.revision_persistent_max_full_revisions,
    )
    return budget.reason if budget.exceeded else ""


def _should_generate_rebuild_candidates(
    session: Session,
    *,
    chapter_id: int,
    revision_brief: ChapterBrief | None,
    latest_version: ChapterVersion | None = None,
) -> bool:
    if not revision_brief or revision_brief.status != "revision_ready":
        return False
    if latest_version and str(latest_version.source or "").startswith("rebuild_candidate_selected:"):
        return False
    text = "\n".join([revision_brief.goal or "", revision_brief.required_beats or "", revision_brief.constraints or ""])
    if "system_revision_budget_recovery" in text and _recent_budget_recovery_failed_revision_count(session, chapter_id=chapter_id, limit=8) >= 2:
        return True
    if "reading_assessment_auto_quality#" not in text and "clean_rebuild_contract@" not in text:
        return False
    if not any(marker in text for marker in ("revision_mode:fresh", "revision_mode:rewrite", "需重建", "失败结构不得沿用")):
        return False
    rows = _recent_failed_quality_rows(session, chapter_id=chapter_id, limit=5)
    rebuild_failures = 0
    for _version, quality, report in rows:
        assessment = report.get("reading_assessment") if isinstance(report.get("reading_assessment"), dict) else {}
        if assessment.get("action") == "auto_rebuild" or int(quality.score or 0) < 70:
            rebuild_failures += 1
    return rebuild_failures >= 3


def _recent_budget_recovery_failed_revision_count(session: Session, *, chapter_id: int, limit: int) -> int:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    failed = 0
    seen_recovery = False
    for version in versions:
        source = str(version.source or "")
        if source.startswith("revision_budget_recovery:"):
            seen_recovery = True
            continue
        if not seen_recovery or not source.startswith("revision:"):
            continue
        quality = _latest_quality(session, version_id=version.id)
        if quality and not quality.passed and int(quality.score or 0) < 60:
            failed += 1
    return failed


def _revision_brief_is_heavy(text: str) -> bool:
    normalized = (text or "").replace("：", ":")
    heavy_markers = (
        "修订模式:fresh",
        "修订模式:rewrite",
        "整章重写",
        "结构性重写",
        "按最新生产骨架重启",
        "旧稿已废弃",
        "采用章节小样",
        "小样气质",
        "叙事发动机合同",
    )
    return len(normalized) >= 2600 or any(marker in normalized for marker in heavy_markers)


def _recent_failed_revision_count(session: Session, *, chapter_id: int, limit: int) -> int:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.source.like("revision:%"))
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    failed = 0
    for version in versions:
        quality = _latest_quality(session, version_id=version.id)
        if quality and not quality.passed:
            failed += 1
    return failed


def _revision_quality_trend_blocker(session: Session, *, chapter_id: int) -> str:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(5)
        )
    )
    failed_rows: list[tuple[ChapterVersion, QualityReport, dict]] = []
    for version in versions:
        quality = _latest_quality(session, version_id=version.id)
        if not quality or quality.passed:
            continue
        failed_rows.append((version, quality, _loads_json(quality.report)))
    if len(failed_rows) < 2:
        return ""
    latest_version, latest_quality, latest_report = failed_rows[0]
    previous_version, previous_quality, previous_report = failed_rows[1]
    latest_score = int(latest_quality.score or 0)
    previous_score = int(previous_quality.score or 0)
    if latest_score <= previous_score:
        return (
            "修订趋势劣化：最新未通过稿 "
            f"v{latest_version.id} score={latest_score} 没有高于上一版 "
            f"v{previous_version.id} score={previous_score}。停止继续自动修订，避免把坏方向修深。"
        )
    best_recent_score = max(int(row[1].score or 0) for row in failed_rows[1:])
    if best_recent_score - latest_score >= 8:
        return (
            "修订趋势劣化：最新未通过稿明显低于近期最佳未通过稿，"
            f"latest={latest_score}, recent_best={best_recent_score}。需要自动回退并换策略修订。"
        )
    latest_dims = latest_report.get("dimensions") if isinstance(latest_report.get("dimensions"), dict) else {}
    previous_dims = previous_report.get("dimensions") if isinstance(previous_report.get("dimensions"), dict) else {}
    watched = ("brief_coverage", "reader_momentum", "hook_strength", "chapter_unit_flow", "scene_atmosphere", "writer_craft")
    degraded = [
        f"{name}:{int(previous_dims.get(name) or 0)}->{int(latest_dims.get(name) or 0)}"
        for name in watched
        if int(previous_dims.get(name) or 0) - int(latest_dims.get(name) or 0) >= 8
    ]
    if len(degraded) >= 2:
        return "修订趋势劣化：关键维度连续下降（" + "；".join(degraded[:4]) + "）。需要自动回退并换策略修订。"
    return ""


def _apply_revision_trend_recovery(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    reason: str,
) -> ChapterBrief | None:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if not chapter:
        return None
    active_brief = _latest_revision_brief(session, chapter_id=chapter.id)
    active_text = "\n".join([active_brief.goal or "", active_brief.required_beats or "", active_brief.constraints or ""]) if active_brief else ""
    latest = _latest_version(session, chapter_id=chapter.id)
    if (
        active_brief
        and "system_revision_trend_recovery" in active_text
        and latest
        and str(latest.source or "").startswith("revision_recovery:")
    ):
        return active_brief
    if active_brief and "system_revision_trend_recovery" in active_text and latest and str(latest.source or "").startswith("revision:"):
        from app.services.revision_supervisor import apply_revision_budget_recovery

        recovery = apply_revision_budget_recovery(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            force_rebuild_reason="trend_recovery_failed",
        )
        return session.get(ChapterBrief, recovery.recovery_brief_id) if recovery.recovery_brief_id else None
    rows = _recent_failed_quality_rows(session, chapter_id=chapter.id, limit=5)
    if len(rows) < 2:
        return None
    latest_version = rows[0][0]
    clean_rows = [
        row
        for row in rows
        if not _version_content_has_stale_context(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            content=row[0].content,
        )
    ]
    if len(clean_rows) < 2:
        return None
    best_version, best_quality, best_report = max(clean_rows, key=lambda row: (int(row[1].score or 0), -int(row[0].id or 0)))
    if best_version.id == latest_version.id:
        return None
    recovery_version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=best_version.title,
        content=best_version.content,
        status="needs_revision",
        source=f"revision_recovery:v{best_version.id}",
    )
    session.add(recovery_version)
    session.flush()
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"
    suggestion = _revision_trend_recovery_suggestion(
        chapter_number=chapter_number,
        reason=reason,
        best_version=best_version,
        best_quality=best_quality,
        latest_version=latest_version,
        best_report=best_report,
    )
    _feedback, _adjustment, recovery_brief, _version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="system_revision_trend_recovery",
        suggestion_text=suggestion,
        revision_mode="targeted",
    )
    recovery_brief.goal = f"换策略修订第{chapter_number}章：以近期最佳稿 v{best_version.id} 为底稿，修复未通过项，不沿坏稿继续。"
    recovery_brief.required_beats = "\n".join(
        [
            "system_revision_trend_recovery: detected",
            f"恢复底稿：v{best_version.id} score={int(best_quality.score or 0)}；废弃劣化稿：v{latest_version.id}。",
            "修订模式:targeted；不得 fresh，不得整章重写，不得继续沿最新劣化稿修。",
            "本轮目标：保留近期最佳稿的主事件、场景顺序、人物行动链和章末方向，只修导致未通过的具体单元。",
            "换策略要求：若上一轮靠扩大冲突失败，本轮改为增强场景可视化、人物反应递进、动作后果、对白声线和钩子落地。",
        ]
    )
    recovery_brief.constraints = "\n".join(
        [
            recovery_brief.constraints or "",
            "system_revision_trend_recovery: 自动趋势恢复，不向作者索要方向，系统先自行换策略处理。",
            "禁止：追杀模板、现实机构关注、门派通缉、系统面板直接解题、冷硬装酷式精炼。",
            "验收：self_check 必须说明从哪一版恢复、废弃了哪一版、具体换了什么修订策略。",
        ]
    )
    sanitize_existing_chapter_brief(session, book_id=book_id, brief=recovery_brief)
    session.flush()
    return recovery_brief


def _version_content_has_stale_context(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    content: str,
) -> bool:
    anchors = "\n".join(context_anchor_lines(session, book_id=book_id))
    report = audit_context_contamination(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        brief_text=anchors,
        canon_context=anchors,
        previous_content=content or "",
    )
    return any("previous_content 含旧设定锚点" in blocker for blocker in report.blockers)


def _recent_failed_quality_rows(
    session: Session,
    *,
    chapter_id: int,
    limit: int,
) -> list[tuple[ChapterVersion, QualityReport, dict]]:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    rows: list[tuple[ChapterVersion, QualityReport, dict]] = []
    for version in versions:
        quality = _latest_quality(session, version_id=version.id)
        if quality and not quality.passed:
            rows.append((version, quality, _loads_json(quality.report)))
    return rows


def _revision_trend_recovery_suggestion(
    *,
    chapter_number: int,
    reason: str,
    best_version: ChapterVersion,
    best_quality: QualityReport,
    latest_version: ChapterVersion,
    best_report: dict,
) -> str:
    issues = [str(item) for item in (best_report.get("issues") or [])[:6]]
    warnings = [str(item) for item in (best_report.get("warnings") or [])[:6]]
    return "\n".join(
        [
            "system_revision_trend_recovery: detected",
            f"第{chapter_number}章自动修订趋势劣化：{reason}",
            f"恢复到近期最佳未通过稿 v{best_version.id} score={int(best_quality.score or 0)}，废弃最新劣化稿 v{latest_version.id}。",
            "不要向作者索要方向；系统先换策略修。",
            "不要继续沿最新劣化稿修；不要 fresh；不要整章重写；不要扩大俗套冲突。",
            "保留恢复稿的主事件、场景顺序、人物行动链和章末方向。",
            "只修明确未通过项：画面可视化、行动后果、对白丰满度、人物反应递进、章末钩子落地。",
            "当前最佳稿问题：" + "；".join(issues) if issues else "当前最佳稿问题：按质量报告修复弱项。",
            "当前最佳稿提醒：" + "；".join(warnings) if warnings else "",
        ]
    )


def _recent_slow_revision_task_count(session: Session, *, book_id: int, chapter_number: int, limit: int) -> int:
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type == "revise_chapter")
            .order_by(GenerationTask.id.desc())
            .limit(limit * 3)
        )
    )
    count = 0
    seen = 0
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        seen += 1
        output_data = _loads_json(task.output_json)
        elapsed = int(output_data.get("elapsed_ms") or 0)
        total_tokens = int(output_data.get("actual_total_tokens") or 0)
        prompt_chars = int(output_data.get("prompt_chars") or 0)
        if elapsed >= 120_000 or total_tokens >= 15_000 or prompt_chars >= 18_000:
            count += 1
        if seen >= limit:
            break
    return count


def _revision_loop_guard_suggestion(
    *,
    chapter_number: int,
    quality: QualityReport,
    failed_count: int,
    slow_count: int,
) -> str:
    report = _loads_json(quality.report)
    issues = [str(item) for item in (report.get("issues") or [])[:6]]
    warnings = [str(item) for item in (report.get("warnings") or [])[:8]]
    repair_contract = []
    unit_report = report.get("chapter_unit_report") if isinstance(report.get("chapter_unit_report"), dict) else {}
    if isinstance(unit_report, dict):
        repair_contract = [str(item) for item in (unit_report.get("repair_contract") or [])[:5]]
    rows = [
        "system_revision_loop_guard: detected",
        f"第{chapter_number}章已触发修订熔断：近期修订失败 {failed_count} 次，高耗时修订 {slow_count} 次。",
        "不要继续 fresh/整章重写，也不要继续扩大旧小样合同。",
        "保留当前稿中已经可用的主线、场景顺序、人物行动和章末方向，只做轻量修补。",
        "优先处理当前质检阻断项：画面可视化、对白丰满度、本章目标覆盖、单元承接、动作后果。",
        "只改失败单元、弱段落和明确问题句；不得重排整章结构，不得重新引入旧小样长文本。",
    ]
    if issues:
        rows.append("当前阻断问题：" + "；".join(issues))
    if warnings:
        rows.append("当前优化提醒：" + "；".join(warnings))
    if repair_contract:
        rows.append("局部修复合同：" + "；".join(repair_contract))
    return "\n".join(rows)


def _publish_action(job: PublishJob | None) -> tuple[str, str]:
    if not job:
        return "create_publish_job", "approved version has no publish job"
    if job.status == "pending":
        return "publish_job_dry_run", "publish job is pending dry-run"
    if job.status == "dry_run_ready":
        return "queue_publish_job", "publish dry-run is ready"
    if job.status == "queued":
        return "mark_publish_job", "publish job is queued for platform automation"
    if job.status == "failed":
        return "retry_publish_job", "publish job failed"
    if job.status == "published":
        return "done", "chapter has been published"
    return "inspect_publish_job", f"unhandled publish job status: {job.status}"


def _has_previous_chapter_context(session: Session, *, book_id: int, chapter_number: int) -> bool:
    if chapter_number <= 1:
        return False
    previous = session.scalar(
        select(Chapter)
        .where(Chapter.book_id == book_id, Chapter.chapter_number < chapter_number)
        .order_by(Chapter.chapter_number.desc())
    )
    if not previous:
        return False
    if (previous.summary or "").strip():
        return True
    return bool(
        session.scalar(
            select(ChapterVersion)
            .where(
                ChapterVersion.chapter_id == previous.id,
                ChapterVersion.status.in_(["approved", "reviewed_pass"]),
            )
            .order_by(ChapterVersion.id.desc())
        )
    )


def _inherit_protected_inputs_into_active_revision_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    chapter_id: int,
) -> bool:
    brief = _latest_revision_brief(session, chapter_id=chapter_id)
    if not brief or brief.status != "revision_ready":
        return False
    sample_context = format_chapter_sample_adoption_context(session, book_id=book_id, chapter_number=chapter_number)
    if not sample_context:
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if _revision_brief_already_has_sample_context(text):
        return False
    new_constraints = "\n".join(item for item in [brief.constraints or "", sample_context] if item)
    if new_constraints == (brief.constraints or ""):
        return False
    brief.constraints = new_constraints
    session.flush()
    return True


def _revision_brief_already_has_sample_context(text: str) -> bool:
    markers = (
        "本章已采用小样方向",
        "小样气质参考",
        "chapter_sample_lab",
        "已采用小样",
        "采用小样",
    )
    return any(marker in text for marker in markers)


def _chapter_brief_fields(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal_prefix: str,
    required_beats: str = "",
    constraints: str = "",
) -> ChapterBriefFields:
    dna = story_dna_for_book(session, book_id=book_id)
    chapter_engine = chapter_engine_for_number(dna, chapter_number) if dna else ""
    arcs = arcs_for_chapter(session, book_id=book_id, chapter_number=chapter_number, limit=1)
    if not arcs:
        beat_parts = [required_beats] if required_beats else []
        if chapter_engine:
            beat_parts.append(f"本章章节发动机:{chapter_engine}")
        return ChapterBriefFields(
            goal=f"{goal_prefix} 第{chapter_number}章",
            required_beats="，".join(_dedupe(_split_csv("，".join(beat_parts)))) if beat_parts else required_beats,
            constraints=ensure_chapter_production_standard(
                "，".join(_dedupe(_split_csv(constraints) + ([f"执行作品DNA章节发动机:{chapter_engine}"] if chapter_engine else []))),
                chapter_number=chapter_number,
            ),
        )
    arc = arcs[0]
    phase = _arc_phase(arc, chapter_number)
    goal_parts = [
        f"{goal_prefix} 第{chapter_number}章",
        f"剧情段：{arc.title}",
        f"阶段：{phase}",
    ]
    if arc.goal:
        goal_parts.append(f"服务目标：{arc.goal}")
    if phase == "climax" and arc.climax:
        goal_parts.append(f"逼近/兑现高潮：{arc.climax}")
    if phase == "resolution" and arc.turn:
        goal_parts.append(f"落到转折：{arc.turn}")

    beat_parts = [
        f"剧情段阶段:{phase}",
        f"本章章节发动机:{chapter_engine}" if chapter_engine else "",
        "压力",
        "选择",
        "代价",
        "信息增量",
        "章末钩子",
    ]
    if arc.goal:
        beat_parts.append(f"推进剧情段目标:{arc.goal}")
    if arc.climax:
        beat_parts.append(f"预埋或推进高潮:{arc.climax}")
    if arc.turn:
        beat_parts.append(f"保持转折方向:{arc.turn}")
    beat_parts.extend(_split_csv(required_beats))

    constraint_parts = [
        f"保持在第{arc.start_chapter}-{arc.end_chapter}章剧情段边界内",
        "不得偏离 Story Bible 和已登记 Canon",
        f"执行作品DNA章节发动机:{chapter_engine}" if chapter_engine else "",
    ]
    constraint_parts.extend(_split_csv(constraints))
    return ChapterBriefFields(
        goal="；".join(goal_parts),
        required_beats="，".join(_dedupe(beat_parts)),
        constraints=ensure_chapter_production_standard(
            "，".join(_dedupe(constraint_parts)),
            chapter_number=chapter_number,
            arc_phase=phase,
            arc_goal=arc.goal,
        ),
    )


def _arc_phase(arc: StoryArc, chapter_number: int) -> str:
    total = max(1, arc.end_chapter - arc.start_chapter + 1)
    index = chapter_number - arc.start_chapter + 1
    ratio = index / total
    if index == 1:
        return "setup"
    if ratio < 0.45:
        return "development"
    if ratio < 0.75:
        return "midpoint"
    if chapter_number < arc.end_chapter:
        return "climax"
    return "resolution"


def _split_csv(value: str) -> list[str]:
    normalized = value.replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item:
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _chapter(session: Session, *, book_id: int, chapter_number: int) -> Chapter | None:
    return session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))


def _latest_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    active = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status != "superseded")
        .order_by(ChapterBrief.id.desc())
    )
    if active:
        return active
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))


def _latest_revision_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    return session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )


def _latest_version(session: Session, *, chapter_id: int) -> ChapterVersion | None:
    return session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.id.desc()))


def _latest_quality(session: Session, *, version_id: int) -> QualityReport | None:
    return session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version_id).order_by(QualityReport.id.desc()))


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _revision_brief_matches_quality(brief: ChapterBrief, quality: QualityReport) -> bool:
    marker = f"质检报告 #{quality.id}"
    return marker in brief.goal or marker in brief.required_beats or marker in brief.constraints


def _revision_brief_matches_feedback_reopen(brief: ChapterBrief, quality: QualityReport) -> bool:
    if not quality.passed:
        return False
    return _revision_brief_has_feedback_marker(brief)


def _revision_brief_has_feedback_marker(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return "反馈调整#" in text or "修订方向#" in text or "机器修订建议#" in text


def _revision_brief_has_protected_review_marker(brief: ChapterBrief | None) -> bool:
    if not brief:
        return False
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return any(
        marker in text
        for marker in (
            "reading_assessment_contract",
            "reading_assessment_auto_quality#",
            "阅读评估结论",
            "当前稿不是正式批准稿",
            "clean_rebuild_contract@v1",
        )
    )


def _reading_assessment_requires_revision(quality: QualityReport) -> bool:
    from app.services.reading_assessment import reading_assessment_requires_revision

    return reading_assessment_requires_revision(_loads_json(quality.report))


def _revision_brief_is_story_clean(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    stale_markers = (
        "依据质检报告",
        "上次质检分数",
        "质量门禁",
        "修订合同:",
        "原始机器修订建议",
        "原始机器修订建议",
        "验收清单:",
    )
    return brief.status == "revision_ready" and not any(marker in text for marker in stale_markers)


def _latest_publish_job(session: Session, *, version_id: int) -> PublishJob | None:
    return session.scalar(select(PublishJob).where(PublishJob.chapter_version_id == version_id).order_by(PublishJob.id.desc()))


def _active_generation_queue_task(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    queue_type: str,
) -> GenerationTask | None:
    tasks = session.scalars(
        select(GenerationTask)
        .where(
            GenerationTask.book_id == book_id,
            GenerationTask.task_type == queue_type,
            GenerationTask.status.in_(["pending", "running"]),
        )
        .order_by(GenerationTask.id)
    )
    for task in tasks:
        try:
            input_data = json.loads(task.input_json or "{}")
        except json.JSONDecodeError:
            input_data = {}
        if input_data.get("chapter_number") == chapter_number:
            return task
    return None
