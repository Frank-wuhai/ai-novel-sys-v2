"""Phase 2/6: revision-progress observability.

The revision loop can now iterate up to 30 times, apply local_patch,
and be early-stopped by revision_early_stop. Operators need to see:

  * ``version_count`` — how many revise iterations this chapter has run.
  * ``first_pass_version`` — the earliest version that scored >= 75
    (a.k.a. the "verdict=pass" boundary). None means we're still below.
  * ``best_score`` and ``best_version_number`` — the plateau the loop is
    hovering on; useful to correlate with ``early_stopped``.
  * ``early_stopped`` — True iff revision_early_stop signalled a stop
    on the latest evaluation (kernel published/promoted a version from
    the ``accept_early_stop`` branch).
  * ``verdict`` — 3-tier verdict of the latest scored version:
    hard_fail / soft_pass / pass (per phase2/3).

These are read-only aggregations over ChapterVersion + QualityReport rows.
No LLM, no mutation, no side effects — safe to call from any dashboard
render loop and cheap enough to compute per-chapter on every refresh.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import ChapterVersion, PlatformFeedback, QualityReport
from app.services.quality import PASS_FLOOR


@dataclass(frozen=True)
class RevisionProgress:
    """Compact snapshot of the revision-loop state for one chapter."""

    chapter_id: int
    version_count: int
    first_pass_version: Optional[int]
    latest_version_number: Optional[int]
    latest_score: Optional[int]
    latest_verdict: Optional[str]
    best_version_number: Optional[int]
    best_score: Optional[int]
    early_stopped: bool
    stop_reason: Optional[str] = None
    # Provenance fields let dashboards explain the numbers back to operators.
    pass_threshold: int = PASS_FLOOR
    tiers: dict = field(default_factory=lambda: {"pass": 75, "soft_pass": 65, "hard_floor": 65})

    def to_dict(self) -> dict:
        return asdict(self)


def describe_revision_progress(
    session: Session,
    *,
    chapter_id: int,
) -> RevisionProgress:
    """Assemble a RevisionProgress snapshot for ``chapter_id``.

    Every field is computed from ChapterVersion + QualityReport; if the
    chapter has no versions yet, returns an all-empty snapshot with
    version_count=0 and early_stopped=False.
    """
    version_count = (
        session.scalar(select(func.count(ChapterVersion.id)).where(ChapterVersion.chapter_id == chapter_id)) or 0
    )
    if version_count == 0:
        return RevisionProgress(
            chapter_id=chapter_id,
            version_count=0,
            first_pass_version=None,
            latest_version_number=None,
            latest_score=None,
            latest_verdict=None,
            best_version_number=None,
            best_score=None,
            early_stopped=False,
        )

    # ---- latest version ------------------------------------------------
    latest_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
        .limit(1)
    )
    latest_report = None
    if latest_version is not None:
        latest_report = session.scalar(
            select(QualityReport).where(QualityReport.chapter_version_id == latest_version.id)
        )

    latest_score = None
    latest_verdict = None
    if latest_report is not None:
        latest_score = int(latest_report.score) if latest_report.score is not None else None
        latest_verdict = _extract_verdict(latest_report)

    # ---- best score + earliest passing version ------------------------
    # Join versions + reports; iterate to find the earliest passing one and
    # the maximum score seen.
    rows = session.execute(
        select(ChapterVersion.version_number, QualityReport.score, QualityReport.report)
        .join(QualityReport, QualityReport.chapter_version_id == ChapterVersion.id)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.asc())
    ).all()

    first_pass_version: Optional[int] = None
    best_score: Optional[int] = None
    best_version_number: Optional[int] = None
    for version_number, score, _report in rows:
        if score is None:
            continue
        if best_score is None or int(score) > best_score:
            best_score = int(score)
            best_version_number = int(version_number)
        if first_pass_version is None and int(score) >= PASS_FLOOR:
            first_pass_version = int(version_number)

    # ---- early-stop detection ------------------------------------------
    # planning.run_next_action writes a PlatformFeedback row with
    # metric_name='revision_early_stop' whenever accept_early_stop fires.
    # Read the most-recent one; metric_value = version_id, raw_text = reason.
    early_stopped = False
    stop_reason = None
    marker = session.scalar(
        select(PlatformFeedback)
        .where(
            PlatformFeedback.chapter_id == chapter_id,
            PlatformFeedback.metric_name == "revision_early_stop",
        )
        .order_by(PlatformFeedback.id.desc())
        .limit(1)
    )
    if marker is not None:
        early_stopped = True
        stop_reason = (marker.raw_text or "").strip() or None

    return RevisionProgress(
        chapter_id=chapter_id,
        version_count=int(version_count),
        first_pass_version=first_pass_version,
        latest_version_number=int(latest_version.version_number) if latest_version else None,
        latest_score=latest_score,
        latest_verdict=latest_verdict,
        best_version_number=best_version_number,
        best_score=best_score,
        early_stopped=early_stopped,
        stop_reason=stop_reason,
    )


def _extract_verdict(report: QualityReport) -> Optional[str]:
    """Pull the verdict field from a QualityReport JSON payload if present."""
    raw = report.report if isinstance(report.report, str) else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError):
        return None
    verdict = payload.get("verdict")
    if verdict in {"pass", "soft_pass", "hard_fail"}:
        return verdict
    # Backwards compatibility: pre-phase2/3 reports only had ``passed``.
    passed = payload.get("passed") if "passed" in payload else None
    if passed is None:
        return None
    return "pass" if passed else "hard_fail"


def project_revision_progress_summary(progress: RevisionProgress) -> str:
    """One-line human summary — ideal for the dashboard header row."""
    parts = [f"版本 {progress.version_count}"]
    if progress.first_pass_version is not None:
        parts.append(f"首达标 v{progress.first_pass_version}")
    else:
        parts.append("尚未达标")
    if progress.best_score is not None:
        parts.append(f"best={progress.best_score}(v{progress.best_version_number})")
    if progress.latest_verdict:
        parts.append(f"最新={progress.latest_verdict}")
    if progress.early_stopped:
        reason = progress.stop_reason or "early-stop"
        parts.append(f"⚠ {reason}")
    return " · ".join(parts)
