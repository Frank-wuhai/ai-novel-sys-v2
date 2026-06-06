from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    elapsed_seconds: float
    output: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Run AI Novel System v2 local regression checks.")
    parser.add_argument("--skip-smoke", action="store_true", help="skip the broad smoke test")
    parser.add_argument("--strict-quality", action="store_true", help="fail when quality regression returns attention")
    args = parser.parse_args()

    checks: list[tuple[str, list[str]]] = []
    if not args.skip_smoke:
        checks.append(("smoke", ["scripts/smoke_test.py"]))
    checks.extend(
        [
            ("migration", ["scripts/migration_regression_test.py"]),
            ("readiness", ["scripts/readiness_regression_test.py"]),
            ("author_command_center", ["scripts/author_command_center_regression.py"]),
            ("skeleton_repair_dashboard", ["scripts/skeleton_repair_dashboard_regression.py"]),
            ("production_scaffold", ["scripts/production_scaffold_regression.py"]),
            ("worker_stability", ["scripts/worker_stability_regression_test.py"]),
            ("database_restore", ["scripts/database_restore_regression_test.py"]),
            ("architecture_boundary", ["scripts/architecture_boundary_regression.py"]),
            ("writing_intelligence", ["scripts/writing_intelligence_regression.py"]),
            ("expression_precision", ["scripts/expression_precision_regression.py"]),
            ("chapter_unit", ["scripts/chapter_unit_regression.py"]),
            ("chapter_unit_plan", ["scripts/chapter_unit_plan_regression.py"]),
            ("production_run_review", ["scripts/production_run_review_regression.py"]),
            ("sample_diversity", ["scripts/chapter_sample_diversity_regression.py"]),
            ("quality", ["scripts/quality_regression.py", "--json"]),
        ]
    )

    results: list[CheckResult] = []
    failed = False
    for name, command in checks:
        result = _run_check(name, command)
        if name == "quality":
            result = _classify_json_status_result(result, strict=args.strict_quality, label="quality")
        if name == "sample_diversity":
            result = _classify_json_status_result(result, strict=False, label="sample_diversity")
        results.append(result)
        print(f"{result.status}\t{name}\t{result.elapsed_seconds:.1f}s")
        if result.status == "FAIL":
            failed = True
            print(result.output)

    attention = [item for item in results if item.status == "ATTENTION"]
    if attention:
        print("attention=" + ",".join(item.name for item in attention))
    print("regression_status=" + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


def _run_check(name: str, command: list[str]) -> CheckResult:
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, *command],
        cwd=str(ROOT),
        text=True,
        capture_output=True,
    )
    elapsed = time.perf_counter() - started
    output = (result.stdout + result.stderr).strip()
    status = "PASS" if result.returncode == 0 else "FAIL"
    return CheckResult(name=name, status=status, elapsed_seconds=elapsed, output=output)


def _classify_json_status_result(result: CheckResult, *, strict: bool, label: str) -> CheckResult:
    if result.status == "FAIL":
        return result
    try:
        payload = json.loads(result.output or "{}")
    except json.JSONDecodeError:
        return CheckResult(
            name=result.name,
            status="FAIL",
            elapsed_seconds=result.elapsed_seconds,
            output=f"{label} regression did not return valid JSON\n" + result.output,
        )
    if "results" in payload:
        statuses = [str(item.get("status") or "") for item in payload.get("results", []) if isinstance(item, dict)]
    else:
        statuses = [str(payload.get("status") or "")]
    if any(status in {"fail", "missing_book"} for status in statuses):
        return CheckResult(result.name, "FAIL", result.elapsed_seconds, result.output)
    if any(status == "attention" for status in statuses):
        return CheckResult(result.name, "FAIL" if strict else "ATTENTION", result.elapsed_seconds, result.output)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
