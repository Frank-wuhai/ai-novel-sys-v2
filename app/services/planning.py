from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, PublishJob, QualityReport, StoryArc
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
from app.services.story import arcs_for_chapter, list_story_arcs


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


@dataclass(frozen=True)
class ChapterBriefFields:
    goal: str
    required_beats: str
    constraints: str


AUTO_ACTIONS = {
    "create_chapter_brief",
    "draft_chapter",
    "review_chapter",
    "create_revision_brief",
    "revise_chapter",
    "create_publish_job",
    "publish_job_dry_run",
    "queue_publish_job",
    "retry_publish_job",
}

MANUAL_ACTIONS = {"record_chapter_continuity", "approve_chapter", "mark_publish_job"}


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


def plan_chapters(session: Session, *, book_id: int, start: int = 1, count: int = 10) -> list[ChapterPlanItem]:
    if start < 1:
        raise ValueError("start must be >= 1")
    if count < 1:
        raise ValueError("count must be >= 1")
    return [_plan_one(session, book_id=book_id, chapter_number=number) for number in range(start, start + count)]


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
) -> RunNextActionResult:
    item = _plan_one(session, book_id=book_id, chapter_number=chapter_number)
    action = item.next_action
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
        version = draft_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
        return RunNextActionResult(chapter_number, action, "executed", "created draft version", version.id)
    if action == "review_chapter":
        report = review_chapter(session, book_id=book_id, chapter_number=chapter_number)
        return RunNextActionResult(chapter_number, action, "executed", f"quality passed={report.passed} score={report.score}", report.id)
    if action == "create_revision_brief":
        brief = create_revision_brief(session, book_id=book_id, chapter_number=chapter_number)
        return RunNextActionResult(chapter_number, action, "executed", "created revision brief", brief.id)
    if action == "revise_chapter":
        version = revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
        return RunNextActionResult(chapter_number, action, "executed", "created revised draft version", version.id)
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
    if action in {"record_chapter_continuity", "approve_chapter", "mark_publish_job"}:
        return RunNextActionResult(chapter_number, action, "blocked", "manual decision required", None)
    if action == "done":
        return RunNextActionResult(chapter_number, action, "noop", "chapter is complete", None)
    return RunNextActionResult(chapter_number, action, "blocked", item.reason, None)


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
) -> BookCycleResult:
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
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
            dry_run=dry_run,
        )
        if result.status != "executed":
            break
        executed.append(result)

    final_items = plan_chapters(session, book_id=book_id, start=start, count=count)
    blocked = [item for item in final_items if item.next_action in MANUAL_ACTIONS or item.next_action.startswith("inspect")]
    done = [item for item in final_items if item.next_action == "done"]
    return BookCycleResult(executed=executed, blocked=blocked, done=done)


def build_human_decision_package(session: Session, *, book_id: int, start: int = 1, count: int = 10) -> HumanDecisionPackage:
    items: list[HumanDecisionItem] = []
    for item in plan_chapters(session, book_id=book_id, start=start, count=count):
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


def _decision_item(item: ChapterPlanItem, *, book_id: int) -> HumanDecisionItem | None:
    if item.next_action == "record_chapter_continuity":
        return HumanDecisionItem(
            decision_type="continuity_writeback",
            chapter_number=item.chapter_number,
            chapter_id=item.chapter_id,
            version_id=item.latest_version_id,
            publish_job_id=None,
            reason=item.reason,
            command_hint=(
                f"python -m app.cli record-chapter-continuity --book-id {book_id} --chapter-number {item.chapter_number} "
                "--summary \"...\" --character-state \"CHARACTER_ID:...\" --new-foreshadow \"...\" --payoff \"FORESHADOW_ID:...\""
            ),
        )
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


def _plan_one(session: Session, *, book_id: int, chapter_number: int) -> ChapterPlanItem:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    if not chapter:
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

    brief = _latest_brief(session, chapter_id=chapter.id)
    version = _latest_version(session, chapter_id=chapter.id)
    quality = _latest_quality(session, version_id=version.id) if version else None
    job = _latest_publish_job(session, version_id=version.id) if version else None

    if not brief:
        action, reason = "create_chapter_brief", "chapter exists but brief is missing"
    elif not version:
        action, reason = "draft_chapter", "brief is ready and no chapter version exists"
    elif version.status == "draft":
        action, reason = "review_chapter", "latest version is draft"
    elif version.status == "needs_revision":
        revision_brief = _latest_revision_brief(session, chapter_id=chapter.id)
        if revision_brief:
            action, reason = "revise_chapter", "latest version failed quality and revision brief exists"
        else:
            action, reason = "create_revision_brief", "latest version failed quality"
    elif version.status == "reviewed_pass":
        if chapter.status != "continuity_recorded":
            action, reason = "record_chapter_continuity", "quality passed but continuity has not been recorded"
        else:
            action, reason = "approve_chapter", "quality and continuity are complete"
    elif version.status == "approved":
        action, reason = _publish_action(job)
    else:
        action, reason = "inspect_manually", f"unhandled latest version status: {version.status}"

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


def _chapter_brief_fields(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal_prefix: str,
    required_beats: str = "",
    constraints: str = "",
) -> ChapterBriefFields:
    arcs = arcs_for_chapter(session, book_id=book_id, chapter_number=chapter_number, limit=1)
    if not arcs:
        return ChapterBriefFields(
            goal=f"{goal_prefix} 第{chapter_number}章",
            required_beats=required_beats,
            constraints=constraints,
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
    ]
    constraint_parts.extend(_split_csv(constraints))
    return ChapterBriefFields(
        goal="；".join(goal_parts),
        required_beats="，".join(_dedupe(beat_parts)),
        constraints="，".join(_dedupe(constraint_parts)),
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
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _chapter(session: Session, *, book_id: int, chapter_number: int) -> Chapter | None:
    return session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))


def _latest_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
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


def _latest_publish_job(session: Session, *, version_id: int) -> PublishJob | None:
    return session.scalar(select(PublishJob).where(PublishJob.chapter_version_id == version_id).order_by(PublishJob.id.desc()))
