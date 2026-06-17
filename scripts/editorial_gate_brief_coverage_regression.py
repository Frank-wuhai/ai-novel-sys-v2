from __future__ import annotations

from app.services.production_reviewing import _apply_editorial_gate
from app.services.quality import QualityResult


def main() -> int:
    rule_result = QualityResult(
        passed=False,
        score=84,
        report="{}",
        dimensions={"brief_coverage": 45},
        issues=["brief_coverage_underfulfilled: 45"],
    )
    report_data = {
        "score": 84,
        "passed": False,
        "status": "FAIL",
        "issues": ["brief_coverage_underfulfilled: 45"],
        "dimensions": {"brief_coverage": 45},
        "hard_gate": {"passed": True, "issues": []},
        "llm_review": {"status": "completed", "score": 92, "verdict": "pass"},
    }
    _apply_editorial_gate(rule_result, report_data)
    blockers = report_data.get("editorial_gate", {}).get("soft_override_blockers", [])
    if report_data.get("passed"):
        print("editorial gate incorrectly passed low brief coverage")
        print(report_data)
        return 1
    if not any(str(item).startswith("brief_coverage=") for item in blockers):
        print("brief coverage was not recorded as an override blocker")
        print(report_data)
        return 1
    print("editorial-gate-brief-coverage-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
