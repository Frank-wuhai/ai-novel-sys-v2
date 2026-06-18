from __future__ import annotations

from dataclasses import dataclass
from typing import Any


AUTO_ACTIONS = {
    "create_chapter_brief",
    "draft_chapter",
    "enqueue_draft_chapter",
    "review_chapter",
    "create_revision_brief",
    "revision_trend_recovery",
    "revise_chapter",
    "enqueue_revise_chapter",
    "create_publish_job",
    "publish_job_dry_run",
    "queue_publish_job",
    "retry_publish_job",
}


@dataclass(frozen=True)
class ProductionDecision:
    status: str
    stage: str
    label: str
    headline: str
    next_step: str
    primary_label: str
    primary_intent: str
    can_continue: bool
    needs_author: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "label": self.label,
            "headline": self.headline,
            "next_step": self.next_step,
            "primary_label": self.primary_label,
            "primary_intent": self.primary_intent,
            "can_continue": self.can_continue,
            "needs_author": self.needs_author,
            "reason": self.reason,
        }


def decide_chapter_production(item: Any | None) -> ProductionDecision:
    if item is None:
        return ProductionDecision(
            status="idle",
            stage="select",
            label="未选择章节",
            headline="请选择有效章节",
            next_step="当前章不在加载范围内，刷新或调整章节范围。",
            primary_label="刷新状态",
            primary_intent="refresh",
            can_continue=False,
            needs_author=False,
            reason="chapter not loaded",
        )

    action = str(getattr(item, "next_action", "") or "")
    version_status = str(getattr(item, "latest_version_status", "") or "")
    quality_passed = getattr(item, "latest_quality_passed", None) is True
    reason = str(getattr(item, "reason", "") or "")

    if action == "wait_generation_task":
        return ProductionDecision(
            status="background_working",
            stage="generate",
            label="后台处理中",
            headline="后台正在生成",
            next_step="等待后台生成完成，不要重复启动。",
            primary_label="等待自动刷新",
            primary_intent="wait",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )

    if version_status == "needs_revision":
        if action in {"revise_chapter", "enqueue_revise_chapter"}:
            return ProductionDecision(
                status="needs_revision",
                stage="revise",
                label="待修订",
                headline="当前章需要按修订合同继续处理",
                next_step="点击继续生产，让系统按当前有效修订合同生成下一版。",
                primary_label="继续修订当前章",
                primary_intent="continue",
                can_continue=True,
                needs_author=False,
                reason=reason,
            )
        if action == "revision_trend_recovery":
            return ProductionDecision(
                status="revision_recovery",
                stage="revise",
                label="需自动回退",
                headline="修订趋势劣化，先自动回退并换策略",
                next_step="点击继续生产，让系统回退到近期最佳稿并生成换策略修订单。",
                primary_label="自动回退并换策略",
                primary_intent="continue",
                can_continue=True,
                needs_author=False,
                reason=reason,
            )
        if quality_passed:
            return ProductionDecision(
                status="needs_revision",
                stage="revise",
                label="已过质检但仍需修订",
                headline="当前稿不能审批，仍有有效修订合同",
                next_step="不要确认当前章；继续按修订合同处理，或阅读后写更具体修改意见。",
                primary_label="继续修订当前章",
                primary_intent="continue",
                can_continue=action in AUTO_ACTIONS,
                needs_author=False,
                reason=reason,
            )

    if action in {"create_chapter_brief", "draft_chapter", "enqueue_draft_chapter", "review_chapter", "create_revision_brief"}:
        return ProductionDecision(
            status="can_continue",
            stage="produce",
            label="可自动推进",
            headline="可以继续生产当前章",
            next_step="点击继续生产，让系统推进到可读稿或新的判断点。",
            primary_label="生产到可读稿",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )

    if action == "record_chapter_continuity":
        return ProductionDecision(
            status="quality_passed",
            stage="continuity",
            label="质检通过，待回写",
            headline="当前章已过质检，等待记录连续性",
            next_step="先记录章节连续性，完成后再进入阅读确认。",
            primary_label="记录连续性",
            primary_intent="continue",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )

    if action == "approve_chapter":
        return ProductionDecision(
            status="needs_author",
            stage="approve",
            label="待你审批",
            headline="可读稿等待你的判断",
            next_step="阅读当前章；满意就通过，不满意就写修改意见。",
            primary_label="阅读并审批当前章",
            primary_intent="approve",
            can_continue=False,
            needs_author=True,
            reason=reason,
        )

    if action == "mark_publish_job":
        return ProductionDecision(
            status="ready_to_publish",
            stage="publish",
            label="待发布确认",
            headline="章节已到发布准备",
            next_step="检查发布预览后确认发布。",
            primary_label="查看发布任务",
            primary_intent="open_publish",
            can_continue=False,
            needs_author=True,
            reason=reason,
        )

    if action in {"create_publish_job", "publish_job_dry_run", "queue_publish_job", "retry_publish_job"}:
        return ProductionDecision(
            status="publish_prepare",
            stage="publish",
            label="待发布准备",
            headline="可以推进发布准备",
            next_step="点击继续生产，系统会创建或推进发布准备。",
            primary_label="继续发布准备",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )

    if action == "done":
        return ProductionDecision(
            status="done",
            stage="complete",
            label="已完成",
            headline="当前章已完成",
            next_step="可以切换到下一章。",
            primary_label="切换下一章",
            primary_intent="next_chapter",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )

    if version_status in {"missing", "no_version", ""}:
        return ProductionDecision(
            status="not_started",
            stage="produce",
            label="未开始",
            headline="当前章尚未开始",
            next_step="点击继续生产创建本章内容。",
            primary_label="生产到可读稿",
            primary_intent="continue",
            can_continue=action in AUTO_ACTIONS,
            needs_author=False,
            reason=reason,
        )

    if action.startswith("inspect"):
        return ProductionDecision(
            status="needs_inspection",
            stage="diagnose",
            label="需要检查",
            headline="当前章需要排查",
            next_step="查看后台状态和章节内容后再决定下一步。",
            primary_label="打开排错",
            primary_intent="open_debug",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )

    return ProductionDecision(
        status="in_progress",
        stage="produce",
        label="处理中",
        headline="当前章有未归类状态",
        next_step="按当前下一步动作继续，若状态矛盾则刷新或排错。",
        primary_label="刷新状态",
        primary_intent="refresh",
        can_continue=action in AUTO_ACTIONS,
        needs_author=False,
        reason=reason,
    )
