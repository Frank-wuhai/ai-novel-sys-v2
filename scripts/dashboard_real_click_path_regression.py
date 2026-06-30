from __future__ import annotations

import time
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_HTML = ROOT / "app" / "dashboard.html"
sys.path.insert(0, str(ROOT))

import scripts.run_local_dashboard as dashboard  # noqa: E402
from app.services.author_runner import AuthorModeRun  # noqa: E402


def main() -> int:
    failures: list[str] = []
    html = DASHBOARD_HTML.read_text(encoding="utf-8")
    continue_block = _function_block(html, "async function continueProduction")
    if "const dryRun = $('wizardDryRun').value === 'true';" not in continue_block:
        failures.append("continue_button_does_not_read_dry_run_mode")
    live_branch = _between(continue_block, "if (!dryRun) {", "$('state').textContent = dryRun")
    if "postAction('start_author_background'" not in live_branch:
        failures.append("live_continue_does_not_start_author_background")
    if "postAction('run_current_until_blocked'" in live_branch:
        failures.append("live_continue_still_uses_preview_loop")
    if "max_revision_cycles: Number($('wizardMaxSteps').value || 1)" not in live_branch:
        failures.append("live_continue_does_not_pass_revision_cycle_limit")
    if "value.includes('服务代码已更新')" not in html:
        failures.append("dashboard_missing_stale_server_error_copy")
    auto_resolve_block = _function_block(html, "async function autoResolveAuthorBlocker")
    if "isPublishAction(chapter?.next_action)" not in auto_resolve_block:
        failures.append("auto_resolve_does_not_stop_on_publish_prepare")
    if "当前章已采用，下一步是发布准备，不需要返回修订。" not in auto_resolve_block:
        failures.append("auto_resolve_missing_publish_prepare_copy")
    if "function approvalSuggestedAction" not in html or "进入发布准备" not in html:
        failures.append("approval_panel_missing_publish_prepare_action")
    queue_worker_block = _function_block(
        (ROOT / "scripts" / "run_local_dashboard.py").read_text(encoding="utf-8"),
        "def _start_background_queue_run",
    )
    if "run_author_mode(" not in queue_worker_block or "post_generation_executed" not in queue_worker_block:
        failures.append("queue_completion_does_not_resume_author_review")

    calls: list[dict] = []
    original_preflight = dashboard._production_preflight_payload
    original_repair = dashboard._auto_repair_preflight_if_needed
    original_run_author = dashboard.run_author_mode
    original_fingerprint = dashboard._SERVER_CODE_FINGERPRINT
    with dashboard._BACKGROUND_LOCK:
        dashboard._BACKGROUND_RUNS.clear()
    try:
        dashboard._SERVER_CODE_FINGERPRINT = (("stale", -1, -1),)
        try:
            dashboard._assert_server_code_current("start_author_background")
            failures.append("stale_server_did_not_block_write_action")
        except RuntimeError as exc:
            if "服务代码已更新" not in str(exc):
                failures.append(f"stale_server_wrong_error:{exc}")
        try:
            dashboard._assert_server_code_current("queue_health")
        except RuntimeError as exc:
            failures.append(f"stale_server_blocked_read_action:{exc}")
        dashboard._SERVER_CODE_FINGERPRINT = dashboard._code_fingerprint()

        dashboard._production_preflight_payload = lambda *args, **kwargs: {"passed": True, "blockers": []}
        dashboard._auto_repair_preflight_if_needed = lambda _session, **kwargs: kwargs["preflight"]

        def fake_run_author_mode(**kwargs):
            calls.append(kwargs)
            on_progress = kwargs.get("on_progress")
            if on_progress:
                on_progress([{"action": "revise_chapter", "status": "executed", "message": "fake revised", "object_id": 999}])
            return AuthorModeRun(
                executed=[{"action": "revise_chapter", "status": "executed", "message": "fake revised", "object_id": 999}],
                terminal_status="auto_paused",
                terminal_message="fake done",
            )

        dashboard.run_author_mode = fake_run_author_mode
        result = dashboard._perform_action(
            None,
            {
                "action": "start_author_background",
                "book_id": 2,
                "chapter_number": 1,
                "platform": "manual",
                "max_revision_cycles": 1,
            },
        )
        if result.get("status") != "running" or not result.get("run_id"):
            failures.append(f"start_author_background_did_not_start:{result}")
        deadline = time.time() + 5
        run_payload = {}
        while time.time() < deadline:
            with dashboard._BACKGROUND_LOCK:
                run_payload = dashboard._BACKGROUND_RUNS.get(result.get("run_id"), {}).copy()
            if run_payload.get("status") != "running":
                break
            time.sleep(0.02)
        if run_payload.get("status") != "completed":
            failures.append(f"author_background_did_not_complete:{run_payload}")
        if not calls:
            failures.append("author_runner_was_not_called")
        else:
            call = calls[0]
            if call.get("book_id") != 2 or call.get("chapter_number") != 1:
                failures.append(f"author_runner_wrong_target:{call}")
            if call.get("max_revision_cycles") != 1:
                failures.append(f"author_runner_wrong_revision_limit:{call}")
            if not callable(call.get("on_progress")):
                failures.append("author_runner_missing_progress_callback")
    finally:
        dashboard._production_preflight_payload = original_preflight
        dashboard._auto_repair_preflight_if_needed = original_repair
        dashboard.run_author_mode = original_run_author
        dashboard._SERVER_CODE_FINGERPRINT = original_fingerprint
        with dashboard._BACKGROUND_LOCK:
            dashboard._BACKGROUND_RUNS.clear()

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("dashboard-real-click-path-regression: PASS")
    return 0


def _function_block(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start : start + 2600]


def _between(text: str, start_marker: str, end_marker: str) -> str:
    start = text.find(start_marker)
    if start < 0:
        return ""
    end = text.find(end_marker, start)
    return text[start:] if end < 0 else text[start:end]


if __name__ == "__main__":
    raise SystemExit(main())
