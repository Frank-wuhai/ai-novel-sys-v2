from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
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


CURRENT_PROCESS: subprocess.Popen | None = None
RECORDER: "RegressionRunRecorder | None" = None


class RegressionRunRecorder:
    def __init__(self, *, args: argparse.Namespace, checks: list[tuple[str, list[str]]]) -> None:
        self.started_at = datetime.now(UTC)
        self.run_id = self.started_at.strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
        self.path = ROOT / "data" / "regression_runs" / f"{self.run_id}.json"
        self.args = vars(args)
        self.checks = [{"name": name, "command": [sys.executable, *command]} for name, command in checks]
        self.results: list[CheckResult] = []
        self.current_check: dict | None = None
        self.final_status = "RUNNING"
        self.interrupted_signal: str | None = None

    def mark_current(self, *, name: str, command: list[str]) -> None:
        self.current_check = {"name": name, "command": [sys.executable, *command], "started_at": _utc_now()}
        self.write(status="RUNNING")

    def add_result(self, result: CheckResult) -> None:
        self.results.append(result)
        self.current_check = None
        self.write(status="RUNNING")

    def interrupt(self, *, signum: int) -> None:
        self.interrupted_signal = signal.Signals(signum).name
        self.write(status="INTERRUPTED")

    def write(self, *, status: str) -> None:
        self.final_status = status
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at.isoformat(),
            "finished_at": None if status == "RUNNING" else _utc_now(),
            "duration_seconds": round(time.perf_counter() - START_MONOTONIC, 3),
            "args": self.args,
            "interrupted_signal": self.interrupted_signal,
            "current_check": self.current_check,
            "checks": self.checks,
            "results": [_result_payload(result) for result in self.results],
            "summary": _summary_payload(self.results, status=status),
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


START_MONOTONIC = time.perf_counter()


def main() -> int:
    global RECORDER
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
            ("author_command_center_failed_sample", ["scripts/author_command_center_failed_sample_regression.py"]),
            ("author_runner_cycle", ["scripts/author_runner_cycle_regression.py"]),
            ("skeleton_repair_dashboard", ["scripts/skeleton_repair_dashboard_regression.py"]),
            ("production_scaffold", ["scripts/production_scaffold_regression.py"]),
            ("production_gate", ["scripts/production_gate_regression.py"]),
            ("production_control_summary_wording", ["scripts/production_control_summary_wording_regression.py"]),
            ("revision_early_stop", ["scripts/revision_early_stop_regression.py"]),
            ("early_stop_orchestrator", ["scripts/early_stop_orchestrator_regression.py"]),
            ("production_hardening", ["scripts/production_hardening_regression.py"]),
            ("production_invariants", ["scripts/production_invariants_regression.py"]),
            ("worker_stability", ["scripts/worker_stability_regression_test.py"]),
            ("generation_queue_recovery", ["scripts/generation_queue_recovery_regression.py"]),
            ("generation_queue_daemon", ["scripts/generation_queue_daemon_regression.py"]),
            ("generation_queue_multiprocess_claim", ["scripts/generation_queue_multiprocess_claim_regression.py"]),
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
            ("pre_draft_inputs", ["scripts/pre_draft_inputs_regression.py"]),
            ("production_router", ["scripts/production_router_regression.py"]),
            ("production_orchestrator", ["scripts/production_orchestrator_regression.py"]),
            ("production_kernel", ["scripts/production_kernel_regression.py"]),
            ("production_sandbox", ["scripts/production_sandbox_regression.py"]),
            ("chapter_production_state", ["scripts/chapter_production_state_regression.py"]),
            ("book2_production_kernel", ["scripts/book2_production_kernel_regression.py"]),
            ("production_optimization", ["scripts/production_optimization_regression.py"]),
            ("production_blueprint", ["scripts/production_blueprint_regression.py"]),
            ("revision_contract_manager", ["scripts/revision_contract_manager_regression.py"]),
            ("production_strategy", ["scripts/production_strategy_regression.py"]),
            ("production_strategy_pipeline", ["scripts/production_strategy_pipeline_regression.py"]),
            ("production_strategy_rule_coverage", ["scripts/production_strategy_rule_coverage_regression.py"]),
            ("production_state_matrix", ["scripts/production_state_matrix_regression.py"]),
            ("production_transition_matrix", ["scripts/production_transition_matrix_regression.py"]),
            ("self_repair", ["scripts/self_repair_regression.py"]),
            ("production_decision", ["scripts/production_decision_regression.py"]),
            ("production_action_consistency", ["scripts/production_action_consistency_regression.py"]),
            ("agent_plan_utilization", ["scripts/agent_plan_utilization_regression.py"]),
            ("rebuild_candidates", ["scripts/rebuild_candidates_regression.py"]),
            ("reading_assessment_rebind", ["scripts/reading_assessment_rebind_regression.py"]),
            ("reading_assessment_state", ["scripts/reading_assessment_state_regression.py"]),
            ("dashboard_current_chapter_guard", ["scripts/dashboard_current_chapter_guard_regression.py"]),
            ("dashboard_generation_status", ["scripts/dashboard_generation_status_regression.py"]),
            ("dashboard_explain", ["scripts/dashboard_explain_regression.py"]),
            ("dashboard_real_click_path", ["scripts/dashboard_real_click_path_regression.py"]),
            ("dashboard_quality_verdict", ["scripts/dashboard_quality_verdict_regression.py"]),
            ("context_contamination", ["scripts/context_contamination_regression.py"]),
            ("context_generic_power", ["scripts/context_generic_power_regression.py"]),
            ("skeleton_context_reset", ["scripts/skeleton_context_reset_regression.py"]),
            ("skeleton_global_sync", ["scripts/skeleton_global_sync_regression.py"]),
            ("legacy_trace_cleanup", ["scripts/legacy_trace_cleanup_regression.py"]),
            ("revision_success_boost", ["scripts/revision_success_boost_regression.py"]),
            ("production_packet_brief_self_heal", ["scripts/production_packet_brief_self_heal_regression.py"]),
            ("brief_write_sanitizer", ["scripts/brief_write_sanitizer_regression.py"]),
            ("system_trash", ["scripts/system_trash_regression.py"]),
            ("revision_intent", ["scripts/revision_intent_regression.py"]),
            ("revision_clean_rebuild", ["scripts/revision_clean_rebuild_regression.py"]),
            ("revision_comparison", ["scripts/revision_comparison_regression.py"]),
            ("editorial_gate_brief_coverage", ["scripts/editorial_gate_brief_coverage_regression.py"]),
            ("editorial_gate_budget", ["scripts/editorial_gate_budget_regression.py"]),
            ("editorial_stratification", ["scripts/editorial_stratification_regression.py"]),
            ("naming_governance", ["scripts/naming_governance_regression.py"]),
            ("narrative_logic", ["scripts/narrative_logic_regression.py"]),
            ("scene_expansion", ["scripts/scene_expansion_regression.py"]),
            ("production_llm_json_repair", ["scripts/production_llm_json_repair_regression.py"]),
            ("chapter_sample_json_repair", ["scripts/chapter_sample_json_repair_regression.py"]),
            ("sample_adoption_continuity", ["scripts/sample_adoption_continuity_regression.py"]),
            ("chapter_unit", ["scripts/chapter_unit_regression.py"]),
            ("chapter_unit_plan", ["scripts/chapter_unit_plan_regression.py"]),
            ("production_run_review", ["scripts/production_run_review_regression.py"]),
            ("sample_diversity", ["scripts/chapter_sample_diversity_regression.py"]),
            ("quality_revision_brief", ["scripts/quality_revision_brief_regression.py"]),
            ("system_baseline", ["scripts/system_baseline_check.py"]),
            ("quality", ["scripts/quality_regression.py", "--json"]),
        ]
    )

    RECORDER = RegressionRunRecorder(args=args, checks=checks)
    _install_signal_handlers()
    RECORDER.write(status="RUNNING")
    print(f"regression_run_id={RECORDER.run_id}")
    print(f"regression_artifact={RECORDER.path.relative_to(ROOT)}")

    results: list[CheckResult] = []
    failed = False
    for name, command in checks:
        result = _run_check(name, command)
        if name == "quality":
            result = _classify_json_status_result(result, strict=args.strict_quality, label="quality")
        if name == "sample_diversity":
            result = _classify_json_status_result(result, strict=False, label="sample_diversity")
        results.append(result)
        RECORDER.add_result(result)
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
    final_status = "FAIL" if failed else "PASS"
    print("regression_status=" + final_status)
    RECORDER.write(status=final_status)
    return 1 if failed else 0


def _run_check(name: str, command: list[str]) -> CheckResult:
    global CURRENT_PROCESS
    if RECORDER:
        RECORDER.mark_current(name=name, command=command)
    started = time.perf_counter()
    process = subprocess.Popen(
        [sys.executable, *command],
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    CURRENT_PROCESS = process
    stdout, stderr = process.communicate()
    CURRENT_PROCESS = None
    elapsed = time.perf_counter() - started
    output = ((stdout or "") + (stderr or "")).strip()
    status = "PASS" if process.returncode == 0 else "FAIL"
    return CheckResult(name=name, status=status, elapsed_seconds=elapsed, output=output)


def _install_signal_handlers() -> None:
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _handle_interrupt)


def _handle_interrupt(signum, _frame) -> None:
    if RECORDER:
        RECORDER.interrupt(signum=signum)
        print(f"regression_status=INTERRUPTED\tsignal={signal.Signals(signum).name}")
        print(f"regression_artifact={RECORDER.path.relative_to(ROOT)}")
    if CURRENT_PROCESS and CURRENT_PROCESS.poll() is None:
        CURRENT_PROCESS.terminate()
        try:
            CURRENT_PROCESS.wait(timeout=5)
        except subprocess.TimeoutExpired:
            CURRENT_PROCESS.kill()
    raise SystemExit(128 + signum)


def _result_payload(result: CheckResult) -> dict:
    return {
        "name": result.name,
        "status": result.status,
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "output": result.output,
    }


def _summary_payload(results: list[CheckResult], *, status: str) -> dict:
    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    failed = [result.name for result in results if result.status == "FAIL"]
    attention = [result.name for result in results if result.status == "ATTENTION"]
    return {
        "status": status,
        "total_completed": len(results),
        "counts": dict(sorted(counts.items())),
        "failed": failed,
        "attention": attention,
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
