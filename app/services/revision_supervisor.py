from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterVersion, QualityReport


@dataclass(frozen=True)
class RevisionSupervisionReport:
    status: str
    latest_score: int | None
    previous_score: int | None
    failed_revision_count: int
    blockers: list[str]
    next_action: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "latest_score": self.latest_score,
            "previous_score": self.previous_score,
            "failed_revision_count": self.failed_revision_count,
            "blockers": self.blockers,
            "next_action": self.next_action,
        }


def supervise_revision_trend(session: Session, *, book_id: int, chapter_number: int) -> RevisionSupervisionReport:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return RevisionSupervisionReport("missing_chapter", None, None, 0, ["chapter not found"], "先生成章节说明。")
    rows = _failed_quality_rows(session, chapter_id=chapter.id, limit=5)
    if not rows:
        return RevisionSupervisionReport("no_failed_revision", None, None, 0, [], "按正常生产流程推进。")
    latest_version, latest_quality, latest_report = rows[0]
    previous_score = int(rows[1][1].score or 0) if len(rows) > 1 else None
    latest_score = int(latest_quality.score or 0)
    blockers: list[str] = []
    if previous_score is not None and latest_score <= previous_score:
        blockers.append(f"latest_not_improved:v{latest_version.id}:{latest_score}<={previous_score}")
    if len(rows) >= 2:
        latest_dims = latest_report.get("dimensions") if isinstance(latest_report.get("dimensions"), dict) else {}
        previous_dims = rows[1][2].get("dimensions") if isinstance(rows[1][2].get("dimensions"), dict) else {}
        watched = ("brief_coverage", "reader_momentum", "hook_strength", "chapter_unit_flow", "scene_atmosphere", "writer_craft")
        degraded = [
            f"{name}:{int(previous_dims.get(name) or 0)}->{int(latest_dims.get(name) or 0)}"
            for name in watched
            if int(previous_dims.get(name) or 0) - int(latest_dims.get(name) or 0) >= 8
        ]
        if len(degraded) >= 2:
            blockers.append("dimension_degraded:" + ",".join(degraded[:4]))
    status = "degrading" if blockers else "stable"
    next_action = "自动回退到近期最佳稿并换策略修订。" if blockers else "继续当前修订策略，但不得扩大修订范围。"
    return RevisionSupervisionReport(status, latest_score, previous_score, len(rows), blockers, next_action)


def _failed_quality_rows(session: Session, *, chapter_id: int, limit: int) -> list[tuple[ChapterVersion, QualityReport, dict]]:
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
        quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
        if quality and not quality.passed:
            rows.append((version, quality, _loads_json(quality.report)))
    return rows


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
