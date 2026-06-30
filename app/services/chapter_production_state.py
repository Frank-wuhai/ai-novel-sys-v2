from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, PublishJob, QualityReport
from app.services.llm_queue import VISIBLE_QUEUE_TYPES


@dataclass(frozen=True)
class ChapterProductionState:
    book_id: int
    chapter_number: int
    status: str
    chapter_id: int | None
    latest_version_id: int | None
    latest_version_status: str
    latest_quality_id: int | None
    latest_quality_passed: bool | None
    active_revision_brief_id: int | None
    active_task_id: int | None
    active_task_status: str
    publish_job_id: int | None
    publish_job_status: str
    blockers: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "book_id": self.book_id,
            "chapter_number": self.chapter_number,
            "status": self.status,
            "chapter_id": self.chapter_id,
            "latest_version_id": self.latest_version_id,
            "latest_version_status": self.latest_version_status,
            "latest_quality_id": self.latest_quality_id,
            "latest_quality_passed": self.latest_quality_passed,
            "active_revision_brief_id": self.active_revision_brief_id,
            "active_task_id": self.active_task_id,
            "active_task_status": self.active_task_status,
            "publish_job_id": self.publish_job_id,
            "publish_job_status": self.publish_job_status,
            "blockers": list(self.blockers),
        }


def get_chapter_production_state(session: Session, *, book_id: int, chapter_number: int) -> ChapterProductionState:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return ChapterProductionState(
            book_id=book_id,
            chapter_number=chapter_number,
            status="not_started",
            chapter_id=None,
            latest_version_id=None,
            latest_version_status="missing",
            latest_quality_id=None,
            latest_quality_passed=None,
            active_revision_brief_id=None,
            active_task_id=None,
            active_task_status="",
            publish_job_id=None,
            publish_job_status="",
            blockers=(),
        )
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    quality = (
        session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
        if version
        else None
    )
    revision_brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    task = _active_generation_task(session, book_id=book_id, chapter_number=chapter_number)
    job = session.scalar(
        select(PublishJob)
        .where(PublishJob.chapter_version_id == version.id)
        .order_by(PublishJob.id.desc())
    ) if version else None
    status, blockers = _normalized_status(
        chapter=chapter,
        version=version,
        quality=quality,
        revision_brief=revision_brief,
        task=task,
        job=job,
    )
    return ChapterProductionState(
        book_id=book_id,
        chapter_number=chapter_number,
        status=status,
        chapter_id=chapter.id,
        latest_version_id=version.id if version else None,
        latest_version_status=version.status if version else "missing",
        latest_quality_id=quality.id if quality else None,
        latest_quality_passed=quality.passed if quality else None,
        active_revision_brief_id=revision_brief.id if revision_brief else None,
        active_task_id=task.id if task else None,
        active_task_status=task.status if task else "",
        publish_job_id=job.id if job else None,
        publish_job_status=job.status if job else "",
        blockers=tuple(blockers),
    )


def _normalized_status(
    *,
    chapter: Chapter,
    version: ChapterVersion | None,
    quality: QualityReport | None,
    revision_brief: ChapterBrief | None,
    task: GenerationTask | None,
    job: PublishJob | None,
) -> tuple[str, list[str]]:
    blockers: list[str] = []
    if task and task.status == "running":
        return "drafting" if task.task_type.endswith("draft_chapter") else "revising", blockers
    if task and task.status == "pending":
        return "queued", blockers
    if not version:
        return "blueprint_ready" if chapter.status in {"briefing", "planned"} else "not_started", blockers
    if version.status == "draft":
        if quality:
            blockers.append("draft_has_quality_but_status_not_reconciled")
        return "draft_ready", blockers
    if version.status == "needs_revision":
        if not revision_brief:
            blockers.append("needs_revision_without_active_contract")
            return "needs_revision", blockers
        return "needs_revision", blockers
    if version.status == "reviewed_pass":
        if not quality or not quality.passed:
            blockers.append("reviewed_pass_without_passing_quality")
            return "needs_revision", blockers
        if chapter.status == "continuity_recorded":
            return "ready_for_adoption", blockers
        return "reviewed_pass", blockers
    if version.status == "approved":
        if job:
            return ("publish_ready" if job.status in {"draft", "ready", "queued"} else str(job.status or "publish_ready")), blockers
        return "approved", blockers
    return version.status or "unknown", blockers


def _active_generation_task(session: Session, *, book_id: int, chapter_number: int) -> GenerationTask | None:
    for task in session.scalars(
        select(GenerationTask)
        .where(
            GenerationTask.book_id == book_id,
            GenerationTask.task_type.in_(VISIBLE_QUEUE_TYPES),
            GenerationTask.status.in_(("pending", "running")),
        )
        .order_by(GenerationTask.id.desc())
    ):
        if _loads(task.input_json).get("chapter_number") == chapter_number:
            return task
    return None


def _loads(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
