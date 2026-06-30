from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app.services.author_runner as runner
from regression_db import isolated_database


class FakeStep:
    def __init__(self, action: str, status: str, message: str, object_id: int | None = None) -> None:
        self.action = action
        self.status = status
        self.message = message
        self.object_id = object_id

    def to_author_event(self) -> dict:
        return {
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "object_id": self.object_id,
        }


class FakeKernel:
    calls = 0

    def __init__(self, *args, **kwargs) -> None:
        pass

    def run_until_terminal(self, *, dry_run: bool = False, max_steps: int = 30, on_progress=None):
        FakeKernel.calls += 1
        executed = [
            {"action": "review_chapter", "status": "executed", "message": "reviewed current draft", "object_id": 101},
            {"action": "create_revision_brief", "status": "executed", "message": "created revision contract", "object_id": 102},
            {"action": "enqueue_revise_chapter", "status": "queued", "message": "queued revision generation task", "object_id": 123},
        ]
        if on_progress:
            on_progress(executed)
        return runner.AuthorModeRun(executed=executed, terminal_status="queued", terminal_message="queued revision generation task")


def main() -> int:
    isolated_database("author-runner-cycle-regression")
    failures: list[str] = []
    original_kernel = runner.ProductionKernel
    progress: list[list[dict]] = []
    try:
        runner.ProductionKernel = FakeKernel
        result = runner.run_author_mode(
            book_id=1,
            chapter_number=1,
            max_revision_cycles=8,
            on_progress=lambda items: progress.append(items),
        )
        if FakeKernel.calls != 1:
            failures.append(f"author_runner_did_not_auto_continue_to_queue:{FakeKernel.calls}")
        actions = [item.get("action") for item in result.executed]
        if actions != ["review_chapter", "create_revision_brief", "enqueue_revise_chapter"]:
            failures.append(f"unexpected_actions:{actions}")
        if result.terminal_status != "queued":
            failures.append(f"queued_step_not_terminal:{result.terminal_status}:{result.terminal_message}")
        if not progress or progress[-1][-1].get("status") != "queued":
            failures.append(f"progress_missing_queued_step:{progress}")
    finally:
        runner.ProductionKernel = original_kernel

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("author-runner-cycle-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
