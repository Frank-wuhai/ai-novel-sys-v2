"""Chapter closed-state helper.

Sprint 2 P1-3 stage-9 (extended P2-Ch27): after ``accept_early_stop`` moves a
chapter into a terminal state (``needs_confirmation`` /
``continuity_recorded`` / ``approved`` / ``published``), later planner /
reading_assessment / editorial passes must NOT demote the promoted version
back to ``needs_revision``. Otherwise stale revision briefs (or freshly
generated ones triggered by a plan pass on the very same tick) would flip
the just-promoted version, causing continuity gates on Ch+1 to block
forever and producing endless ``revision_early_stop`` feedback rows.

There are 5 distinct demote paths in the service layer:

  1. ``app/services/feedback.py:submit_revision_suggestion``     (feedback_reopen)
  2. ``app/services/planning.py:_plan_one``                      (apply_state_repairs)
  3. ``app/services/reading_assessment.py:_ensure_assessment``   (external_revision_contract)
  4. ``app/services/reading_assessment.py:_ensure_assessment``   (REVISION_ACTIONS)
  5. ``app/services/reading_assessment.py:_apply_unified_verdict`` (requires_revision)
  6. ``app/services/editorial_stratification.py:analyse_editorial``     (hard set)

Every path must call this guard before demoting.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Chapter

# States that indicate the chapter's editorial verdict is already sealed.
# Demoting a version once the chapter has entered any of these states is a
# regression — the chapter has already been accepted by the pipeline and any
# further "needs_revision" verdict must go through an explicit revision task,
# not a silent version-level status flip.
CLOSED_STATES = frozenset({
    "needs_confirmation",
    "approved",
    "continuity_recorded",
    "published",
})


def chapter_is_in_closed_state(session: Session, chapter_id: int) -> bool:
    """Return True when the chapter has entered a post-accept terminal state.

    Callers should skip any version demote / status flip when this returns
    True. The row is fetched via ``Session.get`` which uses the identity map
    when the chapter is already loaded, so repeated calls are cheap.
    """
    chapter = session.get(Chapter, chapter_id)
    return chapter is not None and chapter.status in CLOSED_STATES
