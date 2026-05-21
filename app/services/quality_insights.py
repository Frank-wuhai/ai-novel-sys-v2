from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterVersion, QualityReport


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
