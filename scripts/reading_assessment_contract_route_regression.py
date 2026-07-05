"""Regression: reading_assessment_contract revision brief must route to
revise_chapter even when quality.passed=False and
reading_assessment_requires_revision(quality)=False.

Bug (Book2 Ch4 death loop, 645 briefs, Phase E.3): the auto-generated
`reading_assessment_contract` revision brief had:
  - has_protected_review_marker=True (contains reading_assessment_contract)
  - quality.passed=False → protected_review_contract_passed branch skipped
  - reading_assessment_requires_revision(quality)=False → RA branch skipped
  - No feedback marker, no story_clean, no matching-quality marker
So orchestrator fell through to fallback create_revision_brief every round,
creating ever-more briefs (superseded), never actually calling revise_chapter.

Fix: any revision brief with reading_assessment_contract markers must route
to revise_chapter regardless of quality.passed state. This is the same class
of decoupling as chapter_type_gate soft_pass (P2-Ch44): the automated
contract must not be gated by quality flag it was designed to remediate.
"""

from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.services.production_orchestrator import ProductionSituation, decide_production_route


def _base(**over):
    d = dict(
        chapter_number=4,
        chapter_status="drafted",
        has_brief=True,
        latest_version_status="needs_revision",
        latest_quality_passed=False,
        has_revision_brief=True,
        has_reading_assessment_contract_brief=True,  # NEW signal
    )
    d.update(over)
    return ProductionSituation(**d)


def test_ra_contract_brief_quality_failed_routes_to_revise():
    """The exact Book2 Ch4 v1073 death-loop shape."""
    sit = _base(
        latest_quality_passed=False,
        reading_assessment_requires_revision=False,
        protected_review_contract_passed=False,
        feedback_marker_without_quality=False,
        revision_matches_quality_or_feedback=False,
        story_clean_revision_brief=False,
    )
    d = decide_production_route(sit)
    assert d.action == "revise_chapter", f"expected revise_chapter got {d.action}: {d.reason}"
    assert "reading_assessment" in d.intent.lower() or "reading_assessment" in d.reason


def test_ra_contract_brief_quality_passed_routes_to_revise():
    """Still routes to revise even when quality.passed=True; matches protected_review branch."""
    sit = _base(latest_quality_passed=True, protected_review_contract_passed=True)
    d = decide_production_route(sit)
    assert d.action == "revise_chapter"


def test_no_ra_contract_and_no_other_marker_falls_through_to_create():
    """Fallback path preserved for non-RA-contract briefs."""
    sit = _base(
        has_reading_assessment_contract_brief=False,
        reading_assessment_requires_revision=False,
        protected_review_contract_passed=False,
        feedback_marker_without_quality=False,
        revision_matches_quality_or_feedback=False,
        story_clean_revision_brief=False,
    )
    d = decide_production_route(sit)
    assert d.action == "create_revision_brief", f"got {d.action}"


def test_ra_contract_but_early_stop_still_wins():
    """Early-stop preempts revise like other revision branches."""
    sit = _base(
        should_generate_rebuild_candidates=False,
        early_stop_should_stop=True,
        early_stop_best_version=3,
        early_stop_best_score=78,  # >=75 triggers accept_early_stop preemption
    )
    d = decide_production_route(sit)
    assert d.action == "accept_early_stop", f"got {d.action}"


def test_ra_contract_generate_rebuild_candidates_still_wins():
    """rebuild_candidates preempts RA-contract routing (rebuild is higher priority)."""
    sit = _base(should_generate_rebuild_candidates=True)
    d = decide_production_route(sit)
    assert d.action == "generate_rebuild_candidates", f"got {d.action}"


if __name__ == "__main__":
    tests = [
        test_ra_contract_brief_quality_failed_routes_to_revise,
        test_ra_contract_brief_quality_passed_routes_to_revise,
        test_no_ra_contract_and_no_other_marker_falls_through_to_create,
        test_ra_contract_but_early_stop_still_wins,
        test_ra_contract_generate_rebuild_candidates_still_wins,
    ]
    fail = 0
    for t in tests:
        try:
            t()
            print(f"PASS: {t.__name__}")
        except AssertionError as e:
            print(f"FAIL: {t.__name__}: {e}")
            fail += 1
        except Exception as e:
            print(f"ERROR: {t.__name__}: {e}")
            fail += 1
    print(f"=== {len(tests)-fail}/{len(tests)} PASS ===")
    raise SystemExit(fail)
