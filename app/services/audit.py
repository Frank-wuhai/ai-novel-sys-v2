from __future__ import annotations

import difflib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterVersion, GenerationTask, QualityReport


@dataclass(frozen=True)
class VersionAudit:
    version: ChapterVersion
    chapter: Chapter
    latest_quality: QualityReport | None
    generation_tasks: list[GenerationTask]


def list_chapter_versions(session: Session, *, book_id: int, chapter_number: int) -> list[ChapterVersion]:
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number)
    return list(session.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.version_number)))


def get_version_audit(session: Session, *, version_id: int) -> VersionAudit:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    chapter = session.get(Chapter, version.chapter_id)
    if not chapter:
        raise ValueError("chapter version points to missing chapter")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc())
    )
    tasks = list(_generation_tasks_for_version(session, version_id=version.id))
    return VersionAudit(version=version, chapter=chapter, latest_quality=quality, generation_tasks=tasks)


def list_generation_tasks(
    session: Session,
    *,
    book_id: int | None = None,
    task_type: str = "",
    status: str = "",
    limit: int = 20,
) -> list[GenerationTask]:
    stmt = select(GenerationTask).order_by(GenerationTask.id.desc()).limit(limit)
    if book_id is not None:
        stmt = stmt.where(GenerationTask.book_id == book_id)
    if task_type:
        stmt = stmt.where(GenerationTask.task_type == task_type)
    if status:
        stmt = stmt.where(GenerationTask.status == status)
    return list(session.scalars(stmt))


def get_generation_task(session: Session, *, task_id: int) -> GenerationTask:
    task = session.get(GenerationTask, task_id)
    if not task:
        raise ValueError(f"generation task not found: {task_id}")
    return task


def compare_versions(session: Session, *, left_version_id: int, right_version_id: int) -> str:
    left = session.get(ChapterVersion, left_version_id)
    right = session.get(ChapterVersion, right_version_id)
    if not left:
        raise ValueError(f"chapter version not found: {left_version_id}")
    if not right:
        raise ValueError(f"chapter version not found: {right_version_id}")
    diff = difflib.unified_diff(
        left.content.splitlines(),
        right.content.splitlines(),
        fromfile=f"version#{left.id}",
        tofile=f"version#{right.id}",
        lineterm="",
    )
    return "\n".join(diff)


def task_output_version_id(task: GenerationTask) -> int | None:
    data = _loads_json(task.output_json)
    value = data.get("version_id")
    return value if isinstance(value, int) else None


def task_summary(task: GenerationTask) -> str:
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    version_id = output_data.get("version_id") or output_data.get("child_version_id")
    return "\t".join(
        [
            f"{task.id}",
            f"book={task.book_id}",
            f"type={task.task_type}",
            f"status={task.status}",
            f"chapter={input_data.get('chapter_number', '')}",
            f"version={version_id or ''}",
            f"child_task={output_data.get('child_generation_task_id', '')}",
            f"provider={output_data.get('provider', '')}",
            f"model={output_data.get('model', '')}",
            f"estimated_tokens={output_data.get('estimated_total_tokens', '')}",
            f"elapsed_ms={output_data.get('elapsed_ms', '')}",
        ]
    )


def pretty_json(value: str) -> str:
    data = _loads_json(value)
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _generation_tasks_for_version(session: Session, *, version_id: int) -> list[GenerationTask]:
    tasks = list(session.scalars(select(GenerationTask).order_by(GenerationTask.id)))
    return [task for task in tasks if task_output_version_id(task) == version_id]


def _chapter(session: Session, *, book_id: int, chapter_number: int) -> Chapter:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    return chapter


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}
