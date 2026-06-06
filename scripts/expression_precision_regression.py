from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.services.expression_precision import evaluate_expression_precision


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Calibrate expression precision heuristics.")
    parser.add_argument("--cases", default=str(ROOT / "evals" / "expression_precision_cases.json"))
    args = parser.parse_args()

    payload = json.loads(Path(args.cases).read_text(encoding="utf-8"))
    rows = []
    failures = []
    for case in payload.get("cases", []):
        report = evaluate_expression_precision(str(case.get("text") or "")).to_dict()
        expect = case.get("expect") or {}
        status, notes = _case_status(report, expect)
        row = {
            "name": case.get("name", ""),
            "status": status,
            "score": report.get("score"),
            "checks": report.get("checks", {}),
            "examples": report.get("examples", []),
            "notes": notes,
        }
        rows.append(row)
        if status != "pass":
            failures.append(row)

    result = {
        "status": "pass" if not failures else "fail",
        "case_count": len(rows),
        "failure_count": len(failures),
        "cases": rows,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _case_status(report: dict, expect: dict) -> tuple[str, list[str]]:
    notes: list[str] = []
    score = int(report.get("score") or 0)
    min_score = int(expect.get("min_score") or 0)
    max_score = int(expect.get("max_score") or 0)
    examples_text = "\n".join(str(item) for item in report.get("examples", []))
    if min_score and score < min_score:
        notes.append(f"score_low:{score}<{min_score}")
    if max_score and score > max_score:
        notes.append(f"score_high:{score}>{max_score}")
    for marker in expect.get("must_examples_contain", []):
        if str(marker) not in examples_text:
            notes.append(f"missing_example:{marker}")
    for marker in expect.get("forbid_examples_contain", []):
        if str(marker) in examples_text:
            notes.append(f"unexpected_example:{marker}")
    return ("pass" if not notes else "fail"), notes


if __name__ == "__main__":
    raise SystemExit(main())
