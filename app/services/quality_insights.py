from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport


@dataclass(frozen=True)
class ChapterQualitySnapshot:
    chapter_number: int
    version_id: int
    quality_report_id: int
    score: int
    passed: bool
    weak_dimensions: list[str]
    issue_count: int


@dataclass(frozen=True)
class QualityTrendReport:
    book_id: int
    report_count: int
    passed_count: int
    failed_count: int
    average_score: float
    weak_dimension_counts: dict[str, int]
    snapshots: list[ChapterQualitySnapshot]


@dataclass(frozen=True)
class QualityCalibrationReport:
    book_id: int
    report_count: int
    passed_count: int
    failed_count: int
    failure_rate: float
    average_score: float
    weak_dimension_counts: dict[str, int]
    auto_revision_brief_count: int
    auto_revision_brief_coverage: float
    ready_for_trial: bool
    blockers: list[str]


def build_quality_trends(session: Session, *, book_id: int, limit: int = 20) -> QualityTrendReport:
    stmt = (
        select(QualityReport, ChapterVersion, Chapter)
        .join(ChapterVersion, ChapterVersion.id == QualityReport.chapter_version_id)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .where(Chapter.book_id == book_id)
        .order_by(QualityReport.id.desc())
        .limit(limit)
    )
    rows = session.execute(stmt).all()
    snapshots: list[ChapterQualitySnapshot] = []
    weak_counts: Counter[str] = Counter()
    total_score = 0
    passed_count = 0
    for quality, version, chapter in rows:
        weak_dimensions, issue_count = _parse_quality_report(quality.report)
        weak_counts.update(weak_dimensions)
        total_score += quality.score
        if quality.passed:
            passed_count += 1
        snapshots.append(
            ChapterQualitySnapshot(
                chapter_number=chapter.chapter_number,
                version_id=version.id,
                quality_report_id=quality.id,
                score=quality.score,
                passed=quality.passed,
                weak_dimensions=weak_dimensions,
                issue_count=issue_count,
            )
        )
    report_count = len(snapshots)
    average = round(total_score / report_count, 2) if report_count else 0.0
    return QualityTrendReport(
        book_id=book_id,
        report_count=report_count,
        passed_count=passed_count,
        failed_count=report_count - passed_count,
        average_score=average,
        weak_dimension_counts=dict(sorted(weak_counts.items())),
        snapshots=snapshots,
    )


def build_quality_calibration(
    session: Session,
    *,
    book_id: int,
    limit: int = 20,
    max_failure_rate: float = 0.35,
    min_average_score: float = 70.0,
) -> QualityCalibrationReport:
    trends = build_quality_trends(session, book_id=book_id, limit=limit)
    failed_snapshots = [item for item in trends.snapshots if not item.passed]
    auto_brief_count = _auto_revision_brief_count(session, book_id=book_id, failed_snapshots=failed_snapshots)
    failure_rate = round(trends.failed_count / trends.report_count, 4) if trends.report_count else 0.0
    coverage = round(auto_brief_count / trends.failed_count, 4) if trends.failed_count else 1.0
    blockers: list[str] = []
    if trends.report_count < 1:
        blockers.append("缺少质检样本")
    if trends.average_score < min_average_score:
        blockers.append(f"平均分低于阈值 {min_average_score}")
    if failure_rate > max_failure_rate:
        blockers.append(f"失败率高于阈值 {max_failure_rate}")
    if trends.failed_count and coverage < 1.0:
        blockers.append("失败质检未全部生成 revision brief")
    return QualityCalibrationReport(
        book_id=book_id,
        report_count=trends.report_count,
        passed_count=trends.passed_count,
        failed_count=trends.failed_count,
        failure_rate=failure_rate,
        average_score=trends.average_score,
        weak_dimension_counts=trends.weak_dimension_counts,
        auto_revision_brief_count=auto_brief_count,
        auto_revision_brief_coverage=coverage,
        ready_for_trial=not blockers,
        blockers=blockers,
    )


def _auto_revision_brief_count(session: Session, *, book_id: int, failed_snapshots: list[ChapterQualitySnapshot]) -> int:
    if not failed_snapshots:
        return 0
    count = 0
    for snapshot in failed_snapshots:
        chapter = session.scalar(
            select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == snapshot.chapter_number)
        )
        if not chapter:
            continue
        briefs = session.scalars(
            select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        )
        if any(_brief_covers_failed_quality(brief, quality_report_id=snapshot.quality_report_id) for brief in briefs):
            count += 1
    return count


def _brief_covers_failed_quality(brief: ChapterBrief, *, quality_report_id: int) -> bool:
    text = f"{brief.goal}\n{brief.required_beats}\n{brief.constraints}"
    legacy_marker = f"质检报告 #{quality_report_id}"
    current_markers = (
        "验证失败后的修订循环",
        "补足本章核心承诺",
        "必须按通用章节生产标准重写成完整章节",
        "补足关键场景，使正文字数达到最低要求",
    )
    return legacy_marker in text or any(marker in text for marker in current_markers)


def _parse_quality_report(report: str) -> tuple[list[str], int]:
    try:
        data = json.loads(report)
    except json.JSONDecodeError:
        return [], 0
    if not isinstance(data, dict):
        return [], 0
    dimensions = data.get("dimensions", {})
    issues = data.get("issues", [])
    weak_dimensions = [
        name
        for name, score in dimensions.items()
        if isinstance(name, str) and isinstance(score, int) and score < 70
    ] if isinstance(dimensions, dict) else []
    issue_count = len(issues) if isinstance(issues, list) else 0
    return weak_dimensions, issue_count
