from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.services.production_actions import AUTO_ACTIONS, LEGACY_AUTO_ACTIONS


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
        if action in LEGACY_AUTO_ACTIONS:
            return ProductionDecision(
                status="needs_revision",
                stage="revise",
                label="待自动修订",
                headline="阅读评估未关闭，交回主笔修订",
                next_step="点击继续生产，让系统按主编准定稿标准继续定点修订。",
                primary_label="继续修订当前章",
                primary_intent="continue",
                can_continue=True,
                needs_author=False,
                reason=reason,
            )
        if action in {"revise_chapter", "enqueue_revise_chapter"}:
            has_sample = "已采用小样方向" in reason or "小样" in reason
            has_continuity = "承接上一章" in reason or "上一章后果" in reason
            headline = "当前章需要按修订合同继续处理"
            next_step = "点击继续生产，让系统按当前有效修订合同生成下一版。"
            if has_sample and has_continuity:
                headline = "按已采用小样和上一章后果继续修订"
                next_step = "点击继续生产，系统会继承已采用小样方向，并承接上一章后果生成下一版。"
            elif has_sample:
                headline = "按已采用小样方向继续修订"
                next_step = "点击继续生产，系统会把已采用小样作为高优先级方向生成下一版。"
            elif has_continuity:
                headline = "按上一章后果继续修订"
                next_step = "点击继续生产，系统会优先承接上一章后果生成下一版。"
            return ProductionDecision(
                status="needs_revision",
                stage="revise",
                label="待修订",
                headline=headline,
                next_step=next_step,
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
        if action == "revision_budget_recovery":
            return ProductionDecision(
                status="revision_recovery",
                stage="revise",
                label="需换策略恢复",
                headline="修订合同冲突或预算触顶，先自动换策略",
                next_step="点击继续生产，让系统重写为单一有效修订合同，再继续生成。",
                primary_label="自动换策略恢复",
                primary_intent="continue",
                can_continue=True,
                needs_author=False,
                reason=reason,
            )
        if action == "generate_rebuild_candidates":
            return ProductionDecision(
                status="candidate_rebuild",
                stage="revise",
                label="多候选择优",
                headline="单稿重建连续失败，改用多候选自动择优",
                next_step="点击继续生产，让系统生成多个完整候选稿并自动选择最高分版本。",
                primary_label="生成候选并择优",
                primary_intent="continue",
                can_continue=True,
                needs_author=False,
                reason=reason,
            )
        if action == "defer_chapter_for_later":
            return ProductionDecision(
                status="revision_deadlock",
                stage="revise",
                label="未通过，禁止切章",
                headline="当前章未通过，不能切换到下一章",
                next_step="系统已禁用未通过暂存推进；请先回炉修订或候选择优，直到当前章正式通过。",
                primary_label="继续回炉当前章",
                primary_intent="refresh",
                can_continue=False,
                needs_author=False,
                reason=reason,
            )
        if quality_passed:
            return ProductionDecision(
                status="needs_revision",
                stage="revise",
                label="已过质检但仍需修订",
                headline="当前稿不能采用，仍有有效修订合同",
                next_step="继续按修订合同处理，直到主编准定稿标准关闭合同。",
                primary_label="继续修订当前章",
                primary_intent="continue",
                can_continue=action in AUTO_ACTIONS,
                needs_author=False,
                reason=reason,
            )

    if action == "generate_chapter_samples":
        return ProductionDecision(
            status="pre_draft_samples",
            stage="produce",
            label="生成章节小样",
            headline="正文前先生成小样并择优",
            next_step="点击继续生产，系统会先生成当前章小样，确定方向后再进入正文。",
            primary_label="生成小样并择优",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )
    if action == "adopt_recommended_chapter_sample":
        return ProductionDecision(
            status="pre_draft_samples",
            stage="produce",
            label="采用推荐小样",
            headline="已有可用小样，先自动采用推荐方向",
            next_step="点击继续生产，系统会把推荐小样写入当前章生产说明。",
            primary_label="采用推荐小样",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )
    if action == "repair_chapter_brief":
        return ProductionDecision(
            status="pre_draft_repair",
            stage="produce",
            label="清理生产说明",
            headline="当前章生产说明需要先清理",
            next_step="点击继续生产，系统会清理旧合同/重复内容，再重新判断能否写正文。",
            primary_label="清理生产说明",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )
    if action == "wait_previous_chapter_readable":
        return ProductionDecision(
            status="blocked_by_previous",
            stage="continuity",
            label="等待上一章定稿",
            headline="当前章暂停，先稳定上一章",
            next_step="切回上一章继续修订或确认采用；上一章进入可读定稿后，本章会重新开放小样和正文生产。",
            primary_label="先处理上一章",
            primary_intent="previous_chapter",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )
    if action == "resolve_deferred_backlog":
        return ProductionDecision(
            status="blocked_by_deferred_backlog",
            stage="revise",
            label="先清回炉队列",
            headline="上一生产段仍有暂存章，不能进入下一段",
            next_step="先切回最早的暂存回炉章，把它修到正式通过；清账后再继续生产下一段。",
            primary_label="先回炉暂存章",
            primary_intent="refresh",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )
    if action == "deferred_revision_backlog":
        return ProductionDecision(
            status="deferred_backlog",
            stage="revise",
            label="回炉队列",
            headline="当前章未通过，必须先回炉",
            next_step="这章不能发布，也不能作为跳过当前章的依据；请先回炉到正式通过。",
            primary_label="继续回炉当前章",
            primary_intent="refresh",
            can_continue=False,
            needs_author=False,
            reason=reason,
        )

    if action == "review_chapter":
        return ProductionDecision(
            status="draft_ready_for_review",
            stage="review",
            label="待审核",
            headline="当前草稿已生成，等待质量审核",
            next_step="点击继续生产，系统会审核当前草稿；通过后进入连续性记录，不通过则生成明确修订合同。",
            primary_label="审核当前草稿",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )

    if action in {"create_chapter_brief", "draft_chapter", "enqueue_draft_chapter", "create_revision_brief"}:
        return ProductionDecision(
            status="can_continue",
            stage="produce",
            label="可自动推进",
            headline="可以继续生产当前章",
            next_step="点击继续生产，让系统创建必要说明或生成章节草稿。",
            primary_label="生成章节草稿",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )

    if action == "record_chapter_continuity":
        return ProductionDecision(
            status="continuity_ready",
            stage="continuity",
            label="待自动回写",
            headline="当前章已过质检，准备自动记录连续性",
            next_step="点击继续生产，系统会自动记录连续性并进入下一步。",
            primary_label="自动记录连续性",
            primary_intent="continue",
            can_continue=True,
            needs_author=False,
            reason=reason,
        )

    if action == "approve_chapter":
        return ProductionDecision(
            status="needs_confirmation",
            stage="approve",
            label="待采用确认",
            headline="主编准定稿已完成，等待采用确认",
            next_step="确认采用当前章后，流程官会进入发布准备；不采用则交回主笔继续修订。",
            primary_label="确认采用当前章",
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
            headline="流程官已完成发布准备",
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
            primary_label="生成章节草稿",
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
