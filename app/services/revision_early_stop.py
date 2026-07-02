"""Early-stop decision engine for the revision loop.

Motivation
==========

Book2 quality-report curves showed that once a chapter has iterated past the
"pass threshold" (score >= 75) the marginal gain of continuing to revise is
essentially zero.  Chapter 1 first passed at v24, plateaued at v51 (peak 78);
chapter 2 first passed after ~112 versions with a peak of only 84.  Continuing
to iterate burns tokens for effectively no readable gain.

The early-stop policy prevents that runaway cost by declaring a chapter
"good enough — accept the best version so far" as soon as one of four
thresholds fires.

Thresholds (locked from Book2 curve analysis)
=============================================

* ``accept_score_threshold`` (default **75**) — score at or above this counts
  as a pass. Any version reaching this bar stops the loop immediately after
  the ``min_versions_before_stop`` warm-up.
* ``max_versions``          (default **30**) — hard cap on total attempts.
* ``min_versions_before_stop`` (default **5**) — never early-stop before this,
  even on a lucky pass (avoids over-eager acceptance of the very first draft).
* ``no_improvement_window`` (default **10**) — if the best passing score
  hasn't improved for this many *passing* versions, stop.

The policy is a **pure decision function**: given a chronological list of
``(version_number, score, passed)`` tuples, return an ``EarlyStopDecision``.
No DB access, no side effects — this makes it trivial to unit-test every
threshold branch in isolation. Wiring into the orchestrator/kernel happens in
a separate change so the decision layer stays independently verifiable.

Downstream consumers
====================

The orchestrator (``production_orchestrator._revision_route``) checks
``EarlyStopDecision.should_stop`` before choosing any ``revise_chapter``
branch. Dashboards surface ``stop_reason`` and ``best_version_number`` so
operators can see *why* the loop halted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class EarlyStopPolicy:
    """Configurable thresholds. Defaults are locked to Book2 analysis."""

    accept_score_threshold: int = 75
    max_versions: int = 30
    min_versions_before_stop: int = 5
    no_improvement_window: int = 10
    # Plateau detection (added 2026-07-02): stop when the last N versions
    # (including failing ones) drift by <= `plateau_delta` total-score points.
    # This catches the failure mode where revise is producing content but the
    # rule scorer refuses to move — burning tokens with no signal.
    plateau_window: int = 4
    plateau_delta: int = 2

    def __post_init__(self) -> None:  # type: ignore[override]
        if self.accept_score_threshold < 0 or self.accept_score_threshold > 100:
            raise ValueError(f"accept_score_threshold out of range: {self.accept_score_threshold}")
        if self.max_versions < 1:
            raise ValueError(f"max_versions must be >=1, got {self.max_versions}")
        if self.min_versions_before_stop < 0:
            raise ValueError(f"min_versions_before_stop must be >=0, got {self.min_versions_before_stop}")
        if self.no_improvement_window < 1:
            raise ValueError(f"no_improvement_window must be >=1, got {self.no_improvement_window}")
        if self.plateau_window < 2:
            raise ValueError(f"plateau_window must be >=2, got {self.plateau_window}")
        if self.plateau_delta < 0:
            raise ValueError(f"plateau_delta must be >=0, got {self.plateau_delta}")


DEFAULT_POLICY = EarlyStopPolicy()


# ---------------------------------------------------------------------------
# Inputs & outputs
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class VersionScore:
    """Snapshot of a single revision attempt used by the decision engine.

    ``version_number`` is the persistent version id from ``ChapterVersion``.
    ``score`` may be ``None`` when quality has not been evaluated yet — in
    that case the version is treated as *not passing* and *not scoring* for
    the improvement-window logic.
    """

    version_number: int
    score: int | None
    passed: bool


@dataclass(frozen=True)
class EarlyStopDecision:
    """The verdict for a chapter's revision loop.

    ``should_stop=True`` means the orchestrator should skip further
    ``revise_chapter`` routing and either accept ``best_version_number`` (when
    ``best_score >= accept_score_threshold``) or force a rebuild candidate
    generation (when the ceiling was hit without a pass).
    """

    should_stop: bool
    stop_reason: str = ""
    best_version_number: int | None = None
    best_score: int | None = None
    versions_evaluated: int = 0
    passing_versions: int = 0
    triggered_rules: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Core decision function
# ---------------------------------------------------------------------------

def evaluate_early_stop(
    versions: Sequence[VersionScore] | Iterable[VersionScore],
    policy: EarlyStopPolicy = DEFAULT_POLICY,
) -> EarlyStopDecision:
    """Decide whether the revision loop should stop.

    ``versions`` must be in **chronological order** (oldest → newest).

    Precedence:
      1. ``max_versions`` — hard cap wins regardless of score.
      2. ``accept_score_threshold`` (with ``min_versions_before_stop`` warm-up).
      3. ``no_improvement_window`` — plateau escape.
      4. Continue (should_stop=False).
    """

    version_list = list(versions)
    versions_evaluated = len(version_list)

    # Compute best score / best version across all evaluated versions, not
    # only the passing ones — early-stop must be able to surface the best
    # candidate for a "manual continue" fallback (see step 7 of the plan).
    scored = [v for v in version_list if v.score is not None]
    passing = [v for v in scored if v.passed]

    if scored:
        best = max(scored, key=lambda v: (v.score or -1, v.version_number))
        best_version = best.version_number
        best_score = best.score
    else:
        best_version = None
        best_score = None

    # ------------------------------------------------------------------ rule 1
    # Hard cap: even if nothing passes, stop hemorrhaging tokens.
    if versions_evaluated >= policy.max_versions:
        reason = (
            f"max_versions reached ({versions_evaluated}/{policy.max_versions});"
            f" best_score={best_score if best_score is not None else 'n/a'}"
        )
        return EarlyStopDecision(
            should_stop=True,
            stop_reason=reason,
            best_version_number=best_version,
            best_score=best_score,
            versions_evaluated=versions_evaluated,
            passing_versions=len(passing),
            triggered_rules=("max_versions",),
        )

    # ------------------------------------------------------------------ rule 2
    # Passing bar reached — but respect min_versions_before_stop warm-up so
    # a lucky early draft isn't accepted without any comparison surface.
    passing_above_threshold = [
        v for v in passing if v.score is not None and v.score >= policy.accept_score_threshold
    ]
    if passing_above_threshold and versions_evaluated >= policy.min_versions_before_stop:
        best_pass = max(passing_above_threshold, key=lambda v: (v.score or -1, v.version_number))
        reason = (
            f"accept_score_threshold met: v{best_pass.version_number} score={best_pass.score} "
            f">= {policy.accept_score_threshold}; total_versions={versions_evaluated}"
        )
        return EarlyStopDecision(
            should_stop=True,
            stop_reason=reason,
            best_version_number=best_pass.version_number,
            best_score=best_pass.score,
            versions_evaluated=versions_evaluated,
            passing_versions=len(passing),
            triggered_rules=("accept_score_threshold",),
        )

    # ------------------------------------------------------------------ rule 2.5
    # Plateau guard (added 2026-07-02):
    # If the last `plateau_window` versions have rule-score drift <=
    # `plateau_delta`, revise is producing content but not moving the needle.
    # Stop before we burn more tokens; surface the best-so-far so an operator
    # can decide (accept-as-best or manual rebuild).
    #
    # Uses ALL evaluated versions (not just passing), because a rule-flat run
    # with zero passing versions is exactly the failure mode we saw on the
    # 2026-07-02 baseline (three revises all at 45).
    #
    # Respect the min_versions_before_stop warm-up so a genuinely fast draft
    # doesn't get cut short.
    #
    # Also require that the run has NOT been mostly passing — if >=50% of
    # scored versions passed, we defer to the existing no_improvement_window
    # rule (which surfaces the passing-version ceiling more precisely).
    plateau_eligible = len(passing) * 2 < len(scored)  # passing < 50%
    if (
        plateau_eligible
        and versions_evaluated >= max(policy.plateau_window, policy.min_versions_before_stop)
        and len(scored) >= policy.plateau_window
    ):
        window = scored[-policy.plateau_window:]
        window_scores = [int(v.score) for v in window if v.score is not None]
        if len(window_scores) == policy.plateau_window:
            drift = max(window_scores) - min(window_scores)
            if drift <= policy.plateau_delta:
                reason = (
                    f"plateau_stop: last {policy.plateau_window} rule scores drift {drift}"
                    f" <= {policy.plateau_delta} (scores={window_scores}); best_score={best_score}"
                )
                return EarlyStopDecision(
                    should_stop=True,
                    stop_reason=reason,
                    best_version_number=best_version,
                    best_score=best_score,
                    versions_evaluated=versions_evaluated,
                    passing_versions=len(passing),
                    triggered_rules=("plateau_stop",),
                )

    # ------------------------------------------------------------------ rule 3
    # No-improvement plateau: among the *passing* versions, if the best hasn't
    # moved for ``no_improvement_window`` passing versions, stop (accept best).
    # Rationale: below-threshold passes are still worth showing operators, and
    # a flat run of passes indicates the ceiling was hit for this chapter.
    if len(passing) >= policy.no_improvement_window and best_score is not None:
        window = passing[-policy.no_improvement_window:]
        window_best = max((v.score or -1) for v in window)
        # ``best_score`` is over all passing (equivalently since window ⊂ passing).
        earlier_best = max((v.score or -1) for v in passing[: -policy.no_improvement_window]) if len(passing) > policy.no_improvement_window else -1
        if window_best <= earlier_best:
            reason = (
                f"no_improvement_window={policy.no_improvement_window} exceeded: "
                f"window_best={window_best} <= earlier_best={earlier_best}; "
                f"best_score={best_score}"
            )
            return EarlyStopDecision(
                should_stop=True,
                stop_reason=reason,
                best_version_number=best_version,
                best_score=best_score,
                versions_evaluated=versions_evaluated,
                passing_versions=len(passing),
                triggered_rules=("no_improvement_window",),
            )

    # ------------------------------------------------------------------ default
    return EarlyStopDecision(
        should_stop=False,
        stop_reason="",
        best_version_number=best_version,
        best_score=best_score,
        versions_evaluated=versions_evaluated,
        passing_versions=len(passing),
        triggered_rules=(),
    )
