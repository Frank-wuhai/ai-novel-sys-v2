"""Regression: quality gate three-tier verdict (phase2/3).

The quality report now exposes a ``verdict`` field with one of:
  - hard_fail : must continue revising (score<65, blocking issue, or hard
                dimension floor breached).
  - soft_pass : 65 <= score < 75, publishable if human accepts.
  - pass      : score >= 75, recommended stop.

``passed`` remains a bool = verdict in {soft_pass, pass}.

We test the pure function ``classify_quality_verdict`` directly (that's the
canonical decision surface -- run_quality is a thin wrapper that fills
score/hard_dimension_ok/issues from real dimensions).
"""

from __future__ import annotations

from app.services.quality import HARD_FLOOR, PASS_FLOOR, classify_quality_verdict


def main() -> int:
    failures: list[str] = []

    # --- pass tier (score >= 75) ------------------------------------------
    if classify_quality_verdict(score=80, hard_dimension_ok=True, has_blocking_issues=False) != "pass":
        failures.append("case1 pass tier: score=80 must yield pass")

    # --- pass tier exact boundary ----------------------------------------
    if classify_quality_verdict(score=PASS_FLOOR, hard_dimension_ok=True, has_blocking_issues=False) != "pass":
        failures.append(f"case2 boundary {PASS_FLOOR}: must yield pass")

    # --- soft_pass tier (65 <= score < 75) -------------------------------
    if classify_quality_verdict(score=70, hard_dimension_ok=True, has_blocking_issues=False) != "soft_pass":
        failures.append("case3 soft_pass 70: must yield soft_pass")

    if classify_quality_verdict(score=HARD_FLOOR, hard_dimension_ok=True, has_blocking_issues=False) != "soft_pass":
        failures.append(f"case4 boundary {HARD_FLOOR}: must yield soft_pass")

    # score=74 still soft_pass (just under pass floor)
    if classify_quality_verdict(score=74, hard_dimension_ok=True, has_blocking_issues=False) != "soft_pass":
        failures.append("case4b boundary 74: must yield soft_pass")

    # --- hard_fail tier (score < 65) -------------------------------------
    if classify_quality_verdict(score=64, hard_dimension_ok=True, has_blocking_issues=False) != "hard_fail":
        failures.append("case5 hard_fail 64: must yield hard_fail")

    if classify_quality_verdict(score=40, hard_dimension_ok=True, has_blocking_issues=False) != "hard_fail":
        failures.append("case6 hard_fail 40: must yield hard_fail")

    # score=0 hard_fail (floor)
    if classify_quality_verdict(score=0, hard_dimension_ok=True, has_blocking_issues=False) != "hard_fail":
        failures.append("case6b hard_fail 0: must yield hard_fail")

    # --- hard dimension breach forces hard_fail even at high score --------
    if classify_quality_verdict(score=85, hard_dimension_ok=False, has_blocking_issues=False) != "hard_fail":
        failures.append("case7 hard_dim breach score=85: must yield hard_fail")

    # --- blocking issue forces hard_fail even at 80 ----------------------
    if classify_quality_verdict(score=80, hard_dimension_ok=True, has_blocking_issues=True) != "hard_fail":
        failures.append("case8 blocking issue score=80: must yield hard_fail")

    # --- combined: blocking + low score still hard_fail ------------------
    if classify_quality_verdict(score=50, hard_dimension_ok=False, has_blocking_issues=True) != "hard_fail":
        failures.append("case9 combined failure: must yield hard_fail")

    # --- thresholds constants ---------------------------------------------
    if HARD_FLOOR != 65:
        failures.append(f"HARD_FLOOR must be 65, got {HARD_FLOOR}")
    if PASS_FLOOR != 75:
        failures.append(f"PASS_FLOOR must be 75, got {PASS_FLOOR}")

    # --- report JSON payload includes verdict + thresholds ---------------
    # Sanity check the run_quality report shape by constructing a minimal
    # QualityResult wrapper -- since we can't run the full pipeline here we
    # just assert the pure function stays in agreement with what quality.py
    # will emit.
    for score, expected in [(80, "pass"), (70, "soft_pass"), (60, "hard_fail")]:
        got = classify_quality_verdict(score=score, hard_dimension_ok=True, has_blocking_issues=False)
        if got != expected:
            failures.append(f"case10 score={score}: verdict {got!r} != {expected!r}")

    if failures:
        print("quality_gate_tiers_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("quality_gate_tiers_regression=PASS")
    print("cases_evaluated=13")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
