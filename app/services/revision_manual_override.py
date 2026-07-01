"""Phase 2/7: manual override for early-stop.

When the revision loop is halted by ``accept_early_stop`` the chapter
moves to ``needs_confirmation`` and awaits a human decision. If the
editor is unhappy with the best version so far and wants the pipeline
to keep polishing, they call ``request_manual_revision_continuation``:

  1. flip chapter status back to ``needs_revision`` so the revise
     branch is reachable again.
  2. append a ``revision_manual_override`` PlatformFeedback row that
     acts as a "fresh warm-up point" marker for the early-stop policy.

The early-stop engine keeps its pure-function contract (no DB); the
integration layer (planning.py) is responsible for pruning the version
score history down to versions produced AFTER the most-recent override
before handing it to ``evaluate_early_stop``. That naturally re-arms
``min_versions_before_stop`` (=5 new attempts required before the loop
can auto-stop again) without changing policy semantics.

This module is deliberately small: two entry points, both idempotent,
both write append-only rows. It's the human-in-the-loop escape hatch
between phase2/1b's auto stop and the eventual publish decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterVersion, PlatformFeedback


OVERRIDE_METRIC = "revision_manual_override"
STOP_METRIC = "revision_early_stop"


@dataclass(frozen=True)
class ManualOverrideResult:
    chapter_id: int
    performed: bool
    reason: str
    baseline_version_number: Optional[int]

    def to_dict(self) -> dict:
        return {
            "chapter_id": self.chapter_id,
            "performed": self.performed,
            "reason": self.reason,
            "baseline_version_number": self.baseline_version_number,
        }


def request_manual_revision_continuation(
    session: Session,
    *,
    chapter_id: int,
    note: str = "",
) -> ManualOverrideResult:
    """Reopen the revision loop after an early-stop.

    Idempotent: calling twice in a row on the same chapter records two
    override rows but leaves the chapter status at ``needs_revision``.
    Only performs work if the chapter is currently ``needs_confirmation``
    (i.e. sitting on an accept_early_stop result). Any other status is a
    no-op with a descriptive reason.
    """
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        return ManualOverrideResult(chapter_id, False, "chapter not found", None)

    if chapter.status != "needs_confirmation":
        return ManualOverrideResult(
            chapter_id,
            False,
            f"chapter status={chapter.status!r} is not eligible; only needs_confirmation can be reopened",
            None,
        )

    # find the current highest version number as the override baseline
    baseline = session.scalar(
        select(ChapterVersion.version_number)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.desc())
        .limit(1)
    )

    chapter.status = "needs_revision"
    session.add(chapter)

    session.add(
        PlatformFeedback(
            book_id=chapter.book_id,
            chapter_id=chapter_id,
            platform="production_kernel",
            metric_name=OVERRIDE_METRIC,
            metric_value=str(baseline) if baseline is not None else "0",
            raw_text=(note or "operator manually reopened revision loop")[:1000],
        )
    )
    session.flush()

    return ManualOverrideResult(
        chapter_id,
        True,
        note or "reopened for manual continuation",
        int(baseline) if baseline is not None else None,
    )


def find_active_override_baseline(session: Session, *, chapter_id: int) -> Optional[int]:
    """Return the highest version_number recorded on the *latest* override.

    Callers use this to prune version-score history down to versions
    strictly greater than the baseline before invoking evaluate_early_stop.

    Returns None when no override has been performed for this chapter.
    An override that predates the newest stop marker is ignored — after
    a fresh accept_early_stop the loop is treated as "no override
    active", which is exactly what we want (each override arms one and
    only one continuation window).
    """
    latest_override = session.scalar(
        select(PlatformFeedback)
        .where(
            PlatformFeedback.chapter_id == chapter_id,
            PlatformFeedback.metric_name == OVERRIDE_METRIC,
        )
        .order_by(PlatformFeedback.id.desc())
        .limit(1)
    )
    if latest_override is None:
        return None

    latest_stop = session.scalar(
        select(PlatformFeedback)
        .where(
            PlatformFeedback.chapter_id == chapter_id,
            PlatformFeedback.metric_name == STOP_METRIC,
        )
        .order_by(PlatformFeedback.id.desc())
        .limit(1)
    )
    if latest_stop is not None and latest_stop.id > latest_override.id:
        # a stop happened after the override — the override window is spent.
        return None

    try:
        return int(latest_override.metric_value)
    except (TypeError, ValueError):
        return None
