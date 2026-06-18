from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.system_trash import apply_trash_plan, build_trash_plan


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
    parser.add_argument("--trash-after-pass", action="store_true", help="quarantine safe generated artifacts after all blocking checks pass")
    parser.add_argument("--trash-label", default="after-regressions", help="label for data/trash folder when --trash-after-pass is used")
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
            ("production_gate", ["scripts/production_gate_regression.py"]),
            ("worker_stability", ["scripts/worker_stability_regression_test.py"]),
            ("database_restore", ["scripts/database_restore_regression_test.py"]),
            ("one_button_production", ["scripts/one_button_production_regression.py"]),
            ("architecture_boundary", ["scripts/architecture_boundary_regression.py"]),
            ("writing_intelligence", ["scripts/writing_intelligence_regression.py"]),
            ("expression_precision", ["scripts/expression_precision_regression.py"]),
            ("aesthetic_profile", ["scripts/aesthetic_profile_regression.py"]),
            ("book_aesthetic_standard", ["scripts/book_aesthetic_standard_regression.py"]),
            ("book2_style_flow", ["scripts/book2_style_flow_regression.py"]),
            ("story_dna_workflow", ["scripts/story_dna_workflow_regression.py"]),
            ("story_dna_isolation", ["scripts/story_dna_isolation_regression.py"]),
            ("preflight_brief_repair", ["scripts/preflight_brief_repair_regression.py"]),
            ("production_router", ["scripts/production_router_regression.py"]),
            ("production_decision", ["scripts/production_decision_regression.py"]),
            ("reading_assessment_state", ["scripts/reading_assessment_state_regression.py"]),
            ("context_contamination", ["scripts/context_contamination_regression.py"]),
            ("context_generic_power", ["scripts/context_generic_power_regression.py"]),
            ("skeleton_context_reset", ["scripts/skeleton_context_reset_regression.py"]),
            ("skeleton_global_sync", ["scripts/skeleton_global_sync_regression.py"]),
            ("production_packet_brief_self_heal", ["scripts/production_packet_brief_self_heal_regression.py"]),
            ("brief_write_sanitizer", ["scripts/brief_write_sanitizer_regression.py"]),
            ("system_trash", ["scripts/system_trash_regression.py"]),
            ("revision_intent", ["scripts/revision_intent_regression.py"]),
            ("revision_comparison", ["scripts/revision_comparison_regression.py"]),
            ("editorial_gate_brief_coverage", ["scripts/editorial_gate_brief_coverage_regression.py"]),
            ("editorial_gate_budget", ["scripts/editorial_gate_budget_regression.py"]),
            ("editorial_stratification", ["scripts/editorial_stratification_regression.py"]),
            ("naming_governance", ["scripts/naming_governance_regression.py"]),
            ("narrative_logic", ["scripts/narrative_logic_regression.py"]),
            ("scene_expansion", ["scripts/scene_expansion_regression.py"]),
            ("production_llm_json_repair", ["scripts/production_llm_json_repair_regression.py"]),
            ("chapter_unit", ["scripts/chapter_unit_regression.py"]),
            ("chapter_unit_plan", ["scripts/chapter_unit_plan_regression.py"]),
            ("production_run_review", ["scripts/production_run_review_regression.py"]),
            ("sample_diversity", ["scripts/chapter_sample_diversity_regression.py"]),
            ("quality_revision_brief", ["scripts/quality_revision_brief_regression.py"]),
            ("system_baseline", ["scripts/system_baseline_check.py"]),
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
        for item in attention:
            print(_attention_summary(item))
    if args.trash_after_pass and not failed:
        cleanup = _run_safe_trash_cleanup(label=args.trash_label)
        print(f"trash_status=applied\tmoved_count={cleanup['moved_count']}\ttrash_dir={cleanup['trash_dir']}")
    elif args.trash_after_pass and failed:
        print("trash_status=skipped_due_to_regression_failure")
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


def _attention_summary(result: CheckResult) -> str:
    try:
        payload = json.loads(result.output or "{}")
    except json.JSONDecodeError:
        return f"attention_detail\t{result.name}\tinvalid_json"
    explanations: list[str] = []
    impacts: list[str] = []
    if "results" in payload:
        for item in payload.get("results", []):
            if not isinstance(item, dict):
                continue
            explanations.extend(str(reason) for reason in item.get("attention_explanation", []) if reason)
            if item.get("trial_impact"):
                impacts.append(str(item.get("trial_impact")))
    else:
        explanations.extend(str(reason) for reason in payload.get("attention_explanation", []) if reason)
        if payload.get("trial_impact"):
            impacts.append(str(payload.get("trial_impact")))
    explanation = ";".join(explanations[:6]) or "no_detail"
    impact = ",".join(sorted(set(impacts))) or "unknown"
    return f"attention_detail\t{result.name}\timpact={impact}\treason={explanation}"


def _run_safe_trash_cleanup(*, label: str) -> dict:
    plan = build_trash_plan(root=ROOT)
    return apply_trash_plan(root=ROOT, plan=plan, label=label)


if __name__ == "__main__":
    raise SystemExit(main())
