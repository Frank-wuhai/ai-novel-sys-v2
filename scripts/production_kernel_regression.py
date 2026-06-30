from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.services.production_kernel as kernel
from app.services.planning import ChapterPlanItem, RunNextActionResult
from regression_db import isolated_database


def _item(action: str, *, version_status: str = "needs_revision", quality_passed: bool | None = False) -> ChapterPlanItem:
    return ChapterPlanItem(
        chapter_number=1,
        chapter_id=1,
        brief_id=1,
        latest_version_id=10,
        latest_version_status=version_status,
        latest_quality_passed=quality_passed,
        publish_job_id=None,
        publish_job_status="",
        next_action=action,
        reason="regression",
    )


def main() -> int:
    isolated_database("production-kernel-regression")
    failures: list[str] = []
    original_plan = kernel.plan_chapters
    original_run = kernel.run_next_action
    try:
        def approve_plan(*args, **kwargs):
            return [_item("approve_chapter", version_status="reviewed_pass", quality_passed=True)]

        kernel.plan_chapters = approve_plan
        result = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).step()
        if result.status != "blocked" or result.action != "approve_chapter":
            failures.append(f"manual_approval_was_not_blocked:{result}")

        def done_plan(*args, **kwargs):
            return [_item("done", version_status="published", quality_passed=True)]

        kernel.plan_chapters = done_plan
        result = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).step()
        if result.status != "completed" or result.action != "done":
            failures.append(f"done_should_be_completed_not_blocked:{result}")
        terminal = kernel.kernel_terminal_status([result.to_author_event()])
        if terminal.get("status") != "completed":
            failures.append(f"done_terminal_status_not_completed:{terminal}")

        calls: list[dict] = []

        def revise_plan(*args, **kwargs):
            return [_item("revise_chapter")]

        def fake_run_next_action(*args, **kwargs):
            calls.append(dict(kwargs))
            action = kernel.plan_chapters(*args, **kwargs)[0].next_action
            if kwargs.get("mode") == "preview":
                return RunNextActionResult(1, action, "preview", "preview only", 10)
            if action == "revise_chapter":
                return RunNextActionResult(1, "enqueue_revise_chapter", "executed", "queued revision generation task", 77)
            return RunNextActionResult(1, action, "executed", f"executed {action}", 80 + len(calls))

        kernel.plan_chapters = revise_plan
        kernel.run_next_action = fake_run_next_action
        result = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).step(dry_run=False)
        if result.status != "queued" or result.action != "enqueue_revise_chapter":
            failures.append(f"revision_not_reported_as_queued:{result}")
        if not calls or calls[-1].get("queue_generation") is not True:
            failures.append(f"revision_not_forced_to_queue:{calls}")

        sequence = ["review_chapter", "create_revision_brief", "revise_chapter"]

        def sequence_plan(*args, **kwargs):
            action = sequence[min(len(calls), len(sequence) - 1)]
            status = "draft" if action == "review_chapter" else "needs_revision"
            return [_item(action, version_status=status)]

        def fake_sequence_run(*args, **kwargs):
            action = sequence[min(len(calls), len(sequence) - 1)]
            calls.append(dict(kwargs))
            if action == "revise_chapter":
                return RunNextActionResult(1, "enqueue_revise_chapter", "executed", "queued revision generation task", 88)
            return RunNextActionResult(1, action, "executed", f"executed {action}", 80 + len(calls))

        calls.clear()
        kernel.plan_chapters = sequence_plan
        kernel.run_next_action = fake_sequence_run
        run = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).run_until_terminal(dry_run=False)
        actions = [item.get("action") for item in run.executed]
        if actions != ["review_chapter", "create_revision_brief", "enqueue_revise_chapter"]:
            failures.append(f"kernel_run_until_terminal_wrong_actions:{actions}")
        if run.terminal_status != "queued":
            failures.append(f"kernel_run_until_terminal_wrong_status:{run.terminal_status}")

        calls.clear()
        kernel.plan_chapters = revise_plan
        kernel.run_next_action = fake_run_next_action
        result = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).step(dry_run=True)
        if result.status != "preview" or result.action != "revise_chapter":
            failures.append(f"dry_run_step_should_preview:{result}")
        if (
            not calls
            or calls[-1].get("mode") != "preview"
            or calls[-1].get("queue_generation") is not False
            or "dry_run" in calls[-1]
            or "preview_only" in calls[-1]
        ):
            failures.append(f"dry_run_step_not_preview_only:{calls}")

        calls.clear()
        kernel.plan_chapters = revise_plan
        kernel.run_next_action = fake_run_next_action
        dry_run = kernel.ProductionKernel(object(), book_id=1, chapter_number=1).run_until_terminal(dry_run=True)
        if [item.get("status") for item in dry_run.executed] != ["preview"]:
            failures.append(f"dry_run_run_until_terminal_should_stop_after_preview:{dry_run.executed}")
        if dry_run.terminal_status != "preview":
            failures.append(f"dry_run_terminal_status_not_preview:{dry_run.terminal_status}")
    finally:
        kernel.plan_chapters = original_plan
        kernel.run_next_action = original_run

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-kernel-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
