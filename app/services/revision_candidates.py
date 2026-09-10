from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Chapter, ChapterVersion, QualityReport
from app.services.chapter_revision import revise_chapter
from app.services.production_reviewing import review_chapter


@dataclass(frozen=True)
class RevisionCandidateResult:
    selected_version: ChapterVersion
    candidates: list[dict[str, Any]]
    selected_reason: str

    def audit_payload(self) -> dict[str, Any]:
        return {
            "selected_version_id": self.selected_version.id,
            "selected_reason": self.selected_reason,
            "candidates": self.candidates,
        }


def run_revision_candidates(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    dry_run: bool = True,
    candidate_count: int | None = None,
) -> RevisionCandidateResult:
    count = max(1, min(3, int(candidate_count or settings.llm_revision_candidate_count or 1)))
    if count <= 1:
        version = revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
        return RevisionCandidateResult(
            selected_version=version,
            candidates=[{"round": 1, "generated_version_id": version.id, "mode": "single"}],
            selected_reason="single_candidate_mode",
        )

    candidates: list[dict[str, Any]] = []
    best_version: ChapterVersion | None = None
    best_quality: QualityReport | None = None

    for round_number in range(1, count + 1):
        generated = revise_chapter(session, book_id=book_id, chapter_number=chapter_number, dry_run=dry_run)
        quality = review_chapter(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            llm_review=not dry_run,
            review_dry_run=dry_run,
            auto_revision_brief=False,
        )
        latest = _latest_version(session, book_id=book_id, chapter_number=chapter_number) or generated
        latest_quality = _latest_quality(session, version_id=latest.id)
        record = {
            "round": round_number,
            "generated_version_id": generated.id,
            "generated_status": generated.status,
            "reviewed_version_id": latest.id,
            "reviewed_status": latest.status,
            "quality_id": latest_quality.id if latest_quality else None,
            "score": int(latest_quality.score or 0) if latest_quality else None,
            "passed": bool(latest_quality.passed) if latest_quality else False,
            "source": latest.source,
        }
        candidates.append(record)
        if latest_quality and _candidate_rank(latest_quality, latest) > _candidate_rank(best_quality, best_version):
            best_version = latest
            best_quality = latest_quality
        if latest_quality and latest_quality.passed and latest.status in {"reviewed_pass", "approved"}:
            return RevisionCandidateResult(latest, candidates, "candidate_passed_quality_gate")
        if latest.status != "needs_revision":
            break

    selected = best_version or _latest_version(session, book_id=book_id, chapter_number=chapter_number)
    if selected is None:
        raise ValueError("revision candidate cycle produced no selectable version")
    latest = _latest_version(session, book_id=book_id, chapter_number=chapter_number)
    if latest is not None and selected.id != latest.id:
        selected = _copy_selected_candidate(session, source_version=selected, source_quality=best_quality)
    return RevisionCandidateResult(selected, candidates, "best_ranked_candidate_selected")


def _candidate_rank(quality: QualityReport | None, version: ChapterVersion | None) -> tuple[int, int, int]:
    if quality is None or version is None:
        return (-1, -1, -1)
    passed_rank = 1 if quality.passed else 0
    status_rank = 1 if version.status in {"reviewed_pass", "approved"} else 0
    return (passed_rank, int(quality.score or 0), status_rank)


def _latest_version(session: Session, *, book_id: int, chapter_number: int) -> ChapterVersion | None:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return None
    return session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )


def _latest_quality(session: Session, *, version_id: int) -> QualityReport | None:
    return session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version_id)
        .order_by(QualityReport.id.desc())
    )


def _copy_selected_candidate(
    session: Session,
    *,
    source_version: ChapterVersion,
    source_quality: QualityReport | None,
) -> ChapterVersion:
    selected = ChapterVersion(
        chapter_id=source_version.chapter_id,
        version_number=_next_version_number(session, source_version.chapter_id),
        title=source_version.title,
        content=source_version.content,
        status=source_version.status,
        source=f"revision_candidate_select:v{source_version.id}",
    )
    session.add(selected)
    session.flush()
    if source_quality is not None:
        report = _loads_json(source_quality.report)
        report["revision_candidate_selection"] = {
            "source_version_id": source_version.id,
            "source_quality_id": source_quality.id,
            "reason": "best candidate selected after multi-candidate revision cycle",
        }
        session.add(
            QualityReport(
                chapter_version_id=selected.id,
                score=source_quality.score,
                passed=source_quality.passed,
                report=json.dumps(report, ensure_ascii=False),
            )
        )
    session.flush()
    return selected


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
    )
    return (latest.version_number if latest else 0) + 1


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
