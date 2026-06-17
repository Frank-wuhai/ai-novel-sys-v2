from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterVersion, GenerationTask, QualityReport


WATCHED_DIMENSIONS = (
    "readability",
    "author_intent",
    "prose_voice",
    "dialogue_fullness",
    "character_voice",
    "scene_atmosphere",
    "paragraph_aesthetic",
    "chapter_unit_flow",
    "writer_craft",
    "brief_coverage",
)


@dataclass(frozen=True)
class RevisionComparisonResult:
    status: str
    source_version_id: int | None
    current_version_id: int
    restored_version_id: int | None
    score_delta: int
    degraded_dimensions: list[str]
    decision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_version_id": self.source_version_id,
            "current_version_id": self.current_version_id,
            "restored_version_id": self.restored_version_id,
            "score_delta": self.score_delta,
            "degraded_dimensions": self.degraded_dimensions,
            "decision": self.decision,
        }


def compare_and_restore_if_regressed(
    session: Session,
    *,
    current_version: ChapterVersion,
    current_quality: QualityReport,
) -> RevisionComparisonResult:
    if not str(current_version.source or "").startswith("revision:"):
        return RevisionComparisonResult("skipped", None, current_version.id, None, 0, [], "不是修订稿，不做版本对比。")
    if str(current_version.source or "").startswith(("revision_compare_restore:", "revision_recovery:", "editorial_rollback:")):
        return RevisionComparisonResult("skipped", None, current_version.id, None, 0, [], "恢复稿不再触发恢复。")
    source_version = _source_version_for_revision(session, version_id=current_version.id)
    if not source_version:
        return RevisionComparisonResult("missing_source", None, current_version.id, None, 0, [], "找不到源版本，无法对比。")
    source_quality = _latest_quality(session, version_id=source_version.id)
    if not source_quality:
        return RevisionComparisonResult("missing_source_quality", source_version.id, current_version.id, None, 0, [], "源版本缺少质检报告。")
    source_data = _loads_json(source_quality.report)
    current_data = _loads_json(current_quality.report)
    score_delta = int(current_quality.score or 0) - int(source_quality.score or 0)
    degraded = _degraded_dimensions(source_data, current_data)
    should_restore = (
        (not current_quality.passed and bool(source_quality.passed))
        or score_delta <= -5
        or len(degraded) >= 3
    )
    result_status = "regressed" if should_restore else "improved_or_stable"
    restored_id = None
    decision = "修订稿未明显变差，保留当前稿继续流程。"
    if should_restore:
        restored = _restore_source_version(
            session,
            source_version=source_version,
            source_quality=source_quality,
            failed_version=current_version,
            failed_quality=current_quality,
            score_delta=score_delta,
            degraded=degraded,
        )
        restored_id = restored.id
        decision = "修订稿低于源稿，已自动恢复到源稿，避免沿更差版本继续修。"
    result = RevisionComparisonResult(
        result_status,
        source_version.id,
        current_version.id,
        restored_id,
        score_delta,
        degraded,
        decision,
    )
    _attach_comparison(current_quality, result)
    session.flush()
    return result


def _source_version_for_revision(session: Session, *, version_id: int) -> ChapterVersion | None:
    for candidate in session.scalars(
        select(GenerationTask)
        .where(GenerationTask.task_type == "revise_chapter", GenerationTask.status == "completed")
        .order_by(GenerationTask.id.desc())
        .limit(80)
    ):
        output = _loads_json(candidate.output_json)
        if int(output.get("version_id") or 0) != version_id:
            continue
        input_data = _loads_json(candidate.input_json)
        source_id = int(input_data.get("source_version_id") or 0)
        return session.get(ChapterVersion, source_id) if source_id else None
    return None


def _latest_quality(session: Session, *, version_id: int) -> QualityReport | None:
    return session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version_id)
        .order_by(QualityReport.id.desc())
    )


def _degraded_dimensions(source_data: dict, current_data: dict) -> list[str]:
    source_dims = source_data.get("dimensions") if isinstance(source_data.get("dimensions"), dict) else {}
    current_dims = current_data.get("dimensions") if isinstance(current_data.get("dimensions"), dict) else {}
    rows: list[str] = []
    for name in WATCHED_DIMENSIONS:
        before = int(source_dims.get(name) or 0)
        after = int(current_dims.get(name) or 0)
        if before and before - after >= 8:
            rows.append(f"{name}:{before}->{after}")
    return rows


def _restore_source_version(
    session: Session,
    *,
    source_version: ChapterVersion,
    source_quality: QualityReport,
    failed_version: ChapterVersion,
    failed_quality: QualityReport,
    score_delta: int,
    degraded: list[str],
) -> ChapterVersion:
    restored_status = "reviewed_pass" if source_quality.passed else "needs_revision"
    restored = ChapterVersion(
        chapter_id=failed_version.chapter_id,
        version_number=_next_version_number(session, failed_version.chapter_id),
        title=source_version.title,
        content=source_version.content,
        status=restored_status,
        source=f"revision_compare_restore:v{source_version.id}",
    )
    session.add(restored)
    session.flush()
    source_report = _loads_json(source_quality.report)
    source_report["revision_comparison_restore"] = {
        "failed_version_id": failed_version.id,
        "failed_quality_id": failed_quality.id,
        "source_version_id": source_version.id,
        "source_quality_id": source_quality.id,
        "score_delta": score_delta,
        "degraded_dimensions": degraded,
        "reason": "修订稿低于源稿，自动恢复源稿作为当前最佳版本。",
    }
    session.add(
        QualityReport(
            chapter_version_id=restored.id,
            score=source_quality.score,
            passed=source_quality.passed,
            report=json.dumps(source_report, ensure_ascii=False),
        )
    )
    return restored


def _attach_comparison(quality: QualityReport, result: RevisionComparisonResult) -> None:
    data = _loads_json(quality.report)
    data["revision_comparison"] = result.to_dict()
    quality.report = json.dumps(data, ensure_ascii=False)


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.version_number.desc()))
    return (latest.version_number if latest else 0) + 1


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
