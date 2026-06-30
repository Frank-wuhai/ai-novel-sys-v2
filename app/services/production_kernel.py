from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.services.planning import ChapterPlanItem, RunNextActionResult, plan_chapters, run_next_action
from app.services.production_decision import ProductionDecision, decide_chapter_production


HEAVY_GENERATION_ACTIONS = {
    "draft_chapter",
    "revise_chapter",
    "enqueue_draft_chapter",
    "enqueue_revise_chapter",
}

MANUAL_CONFIRMATION_ACTIONS = {"approve_chapter", "mark_publish_job"}


@dataclass(frozen=True)
class KernelPlan:
    item: ChapterPlanItem
    decision: ProductionDecision


@dataclass(frozen=True)
class KernelStepResult:
    action: str
    status: str
    message: str
    object_id: int | None = None

    def to_author_event(self) -> dict:
        return {
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "object_id": self.object_id,
        }


@dataclass(frozen=True)
class KernelRunResult:
    executed: list[dict]
    terminal_status: str
    terminal_message: str

    @property
    def latest_result(self) -> dict:
        return self.executed[-1] if self.executed else {}


class ProductionKernel:
    """Single production entry point for dashboard/author mode.

    Legacy services still perform concrete work, but production flow decisions
    are centralized here so dashboard, author mode, and queue recovery stop at
    the same terminal points.
    """

    def __init__(self, session: Session, *, book_id: int, chapter_number: int, platform: str = "manual") -> None:
        if book_id < 1 or chapter_number < 1:
            raise ValueError("book_id and chapter_number are required")
        self.session = session
        self.book_id = book_id
        self.chapter_number = chapter_number
        self.platform = platform

    def plan(self, *, apply_state_repairs: bool = True) -> KernelPlan:
        item = plan_chapters(
            self.session,
            book_id=self.book_id,
            start=self.chapter_number,
            count=1,
            apply_state_repairs=apply_state_repairs,
        )[0]
        return KernelPlan(item=item, decision=decide_chapter_production(item))

    def step(self, *, dry_run: bool = False, preview_only: bool = False) -> KernelStepResult:
        plan = self.plan(apply_state_repairs=True)
        item = plan.item
        decision = plan.decision
        action = item.next_action
        if action == "done":
            return KernelStepResult(
                action=action,
                status="completed",
                message=decision.next_step or item.reason or "章节已完成。",
                object_id=item.latest_version_id or item.publish_job_id,
            )
        if decision.needs_author or action in MANUAL_CONFIRMATION_ACTIONS:
            return KernelStepResult(
                action=action,
                status="blocked",
                message=decision.next_step or item.reason,
                object_id=item.latest_version_id or item.publish_job_id,
            )
        if not decision.can_continue:
            return KernelStepResult(
                action=action,
                status="blocked",
                message=decision.next_step or item.reason,
                object_id=item.latest_version_id or item.brief_id,
            )
        effective_preview_only = preview_only or dry_run
        result = run_next_action(
            self.session,
            book_id=self.book_id,
            chapter_number=self.chapter_number,
            dry_run=dry_run,
            queue_generation=(not dry_run and action in HEAVY_GENERATION_ACTIONS),
            platform=self.platform,
            preview_only=effective_preview_only,
        )
        if result.action in {"enqueue_draft_chapter", "enqueue_revise_chapter"} and result.status == "executed":
            return KernelStepResult(
                action=result.action,
                status="queued",
                message=result.message,
                object_id=result.object_id,
            )
        return KernelStepResult(
            action=result.action,
            status=result.status,
            message=result.message,
            object_id=result.object_id,
        )

    def run_until_terminal(
        self,
        *,
        dry_run: bool = False,
        max_steps: int = 30,
        on_progress=None,
    ) -> KernelRunResult:
        executed: list[dict] = []
        for _ in range(max(1, min(80, int(max_steps or 30)))):
            result = self.step(dry_run=dry_run)
            event = result.to_author_event()
            executed.append(event)
            if on_progress:
                on_progress(list(executed))
            if _is_terminal_event(event):
                break
        else:
            executed.append(
                {
                    "action": "kernel_step_limit",
                    "status": "blocked",
                    "message": "生产内核达到本轮安全步数上限，已暂停以避免流程失控。",
                    "object_id": None,
                }
            )
            if on_progress:
                on_progress(list(executed))
        terminal = kernel_terminal_status(executed)
        return KernelRunResult(
            executed=executed,
            terminal_status=terminal["status"],
            terminal_message=terminal["message"],
        )


def _is_terminal_event(event: dict) -> bool:
    action = str(event.get("action") or "")
    status = str(event.get("status") or "")
    if status in {"queued", "blocked", "failed", "preview", "completed"}:
        return True
    if action in {"enqueue_draft_chapter", "enqueue_revise_chapter"}:
        return True
    if action in MANUAL_CONFIRMATION_ACTIONS:
        return True
    return False


def kernel_terminal_status(executed: list[dict]) -> dict:
    if not executed:
        return {"status": "system_failed", "message": "生产内核没有执行任何步骤。"}
    last = executed[-1]
    action = str(last.get("action") or "")
    status = str(last.get("status") or "")
    message = str(last.get("message") or "")
    if action == "approve_chapter":
        return {"status": "ready_for_adoption", "message": "主编准定稿已完成，等待确认采用或交回主笔修订。"}
    if action == "done" or status == "completed":
        return {"status": "completed", "message": message or "当前章节已完成，可以切换到下一章。"}
    if status == "failed":
        return {"status": "system_failed", "message": message or "模型或系统执行失败。"}
    if status == "queued":
        return {"status": "queued", "message": message or "模型任务已进入后台队列。"}
    if status == "preview":
        return {"status": "preview", "message": message or "已预览下一步动作，未写入生产状态。"}
    if status == "blocked":
        return {"status": "auto_paused", "message": message or "自动生产已暂停，等待下一步确认。"}
    return {"status": "auto_paused", "message": message or "生产内核已暂停，系统已保留当前最佳状态。"}
