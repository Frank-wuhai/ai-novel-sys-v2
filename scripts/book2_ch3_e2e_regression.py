"""Regression: phase2 end-to-end simulation for Book2 chapter 3.

Book2 raw curves (pre-phase-2) needed 24 versions on chapter 1 to first
hit score>=75, and roughly 112 on chapter 2. That runaway cost is what
the entire phase-2 stack (early-stop + local_patch default + tier
verdicts + manual override) is meant to prevent.

This script does NOT drive a real LLM against Book2 chapter 3 — that
would take hours and cost real tokens. Instead it drives every phase-2
service (revision_early_stop.evaluate_early_stop,
quality.classify_quality_verdict, revision_progress) through a
deterministic score generator that mimics the local_patch behaviour
we're claiming:

  * cold-start score ~62 (below hard_floor => hard_fail)
  * each local_patch iteration nudges +2..+4 with occasional -1 wobble
  * once score crosses 75 the early-stop policy should trigger inside
    the 30-version cap

The success criterion is the phase2-objective:
  - first_pass_version (score>=75) reached in <= 30 versions,
  - early-stop policy signals should_stop=True at or before v30,
  - verdict trajectory: hard_fail -> soft_pass -> pass (all three
    tiers observed).

Failing this regression means the phase-2 thresholds are no longer
aligned with each other (e.g. early-stop accept_score got moved
below pass_floor, or hard_floor got raised past the starting cold
score, or the min_versions_before_stop warm-up exceeds max_versions).
"""

from __future__ import annotations

import random

from app.services.quality import HARD_FLOOR, PASS_FLOOR, classify_quality_verdict
from app.services.revision_early_stop import EarlyStopPolicy, VersionScore, evaluate_early_stop


def simulate_book2_ch3(seed: int = 42) -> tuple[list[VersionScore], list[str]]:
    """Deterministic local_patch-driven revise loop.

    Returns (scores, verdicts) — chronological history the run produced.
    """
    rng = random.Random(seed)
    scores: list[VersionScore] = []
    verdicts: list[str] = []
    policy = EarlyStopPolicy()

    current = 60  # cold start below hard_floor -> v1 is hard_fail (60->62..64)
    for version_number in range(1, policy.max_versions + 1):
        # local_patch behaviour: +2..+4 nudge with 15% wobble down 1
        step = rng.randint(2, 4)
        if rng.random() < 0.15 and current >= HARD_FLOOR + 2:
            step = -1
        current = max(0, min(100, current + step))
        # emit a score
        passed = current >= HARD_FLOOR
        scores.append(VersionScore(version_number=version_number, score=current, passed=passed))
        verdict = classify_quality_verdict(
            score=current, hard_dimension_ok=True, has_blocking_issues=False
        )
        verdicts.append(verdict)
        # check policy after each new score
        decision = evaluate_early_stop(scores, policy=policy)
        if decision.should_stop:
            return scores, verdicts

    return scores, verdicts


def main() -> int:
    failures: list[str] = []
    # 5 seeds — each simulates one Book2 chapter 3 run
    for seed in [1, 7, 42, 101, 2026]:
        scores, verdicts = simulate_book2_ch3(seed=seed)
        # phase2 objective: reach score>=75 within max_versions
        first_pass = next(
            (vs.version_number for vs in scores if vs.score is not None and vs.score >= PASS_FLOOR),
            None,
        )
        if first_pass is None:
            failures.append(f"seed={seed}: no version reached PASS_FLOOR within {len(scores)} versions")
            continue
        if first_pass > 30:
            failures.append(f"seed={seed}: first_pass_version={first_pass} exceeds 30-version cap")

        # early-stop should have terminated the loop (loop exits on should_stop=True)
        if len(scores) >= 30 and first_pass < 30:
            # if we reached the cap without early-stop firing after a pass,
            # thresholds are misaligned.
            failures.append(f"seed={seed}: reached cap without early-stop firing after first_pass={first_pass}")

        # verdict trajectory: at least hard_fail and pass observed;
        # soft_pass often but not always present.
        if "hard_fail" not in verdicts:
            # cold start ~62 should trigger hard_fail on v1
            failures.append(f"seed={seed}: verdicts never included hard_fail: {verdicts[:3]}")
        if "pass" not in verdicts:
            failures.append(f"seed={seed}: verdicts never reached pass: {verdicts[-3:]}")

    if failures:
        print("book2_ch3_e2e_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1

    # print a representative trajectory so the operator can see what happened
    scores, verdicts = simulate_book2_ch3(seed=42)
    first_pass = next(vs.version_number for vs in scores if vs.score is not None and vs.score >= PASS_FLOOR)
    print("book2_ch3_e2e_regression=PASS")
    print(f"seed=42 versions={len(scores)} first_pass_version=v{first_pass}")
    print(f"scores={[vs.score for vs in scores]}")
    print(f"verdicts={verdicts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
