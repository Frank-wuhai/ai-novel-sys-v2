from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ProductionRouteDecision:
    intent: str
    action: str
    reason: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    protected_inputs: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ProductionSituation:
    chapter_number: int
    chapter_status: str
    has_brief: bool
    latest_version_status: str
    latest_quality_passed: bool | None
    publish_action: str = ""
    publish_reason: str = ""
    draft_queue_status: str = ""
    draft_queue_id: int | None = None
    revision_queue_status: str = ""
    revision_queue_id: int | None = None
    has_revision_brief: bool = False
    has_sample_adoption: bool = False
    has_continuity_context: bool = False
    budget_blocker: str = ""
    trend_blocker: str = ""
    should_generate_rebuild_candidates: bool = False
    reading_assessment_requires_revision: bool = False
    protected_review_contract_passed: bool = False
    feedback_marker_without_quality: bool = False
    revision_matches_quality_or_feedback: bool = False
    story_clean_revision_brief: bool = False
    strategy_action: str = ""
    strategy_intent: str = ""
    strategy_reason: str = ""
    strategy_category: str = ""
    strategy_confidence: int = 0
    strategy_evidence: tuple[str, ...] = field(default_factory=tuple)


def decide_production_route(situation: ProductionSituation) -> ProductionRouteDecision:
    s = situation
    evidence = _evidence(s)
    protected_inputs = _protected_inputs(s)

    if not s.has_brief:
        return ProductionRouteDecision(
            intent="repair_missing_brief",
            action="create_chapter_brief",
            reason="chapter exists but brief is missing",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if not s.latest_version_status or s.latest_version_status == "missing":
        if s.draft_queue_status:
            return ProductionRouteDecision(
                intent="wait_draft_queue",
                action="wait_generation_task",
                reason=f"draft generation task {s.draft_queue_id} is {s.draft_queue_status}",
                evidence=evidence,
                protected_inputs=protected_inputs,
            )
        return ProductionRouteDecision(
            intent="draft_from_brief",
            action="draft_chapter",
            reason="brief is ready and no chapter version exists",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.latest_version_status == "draft":
        return ProductionRouteDecision(
            intent="review_draft",
            action="review_chapter",
            reason="latest version is draft",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.latest_version_status == "needs_revision":
        return _decide_revision_route(s, evidence=evidence, protected_inputs=protected_inputs)
    if s.latest_version_status == "reviewed_pass":
        if s.chapter_status != "continuity_recorded":
            return ProductionRouteDecision(
                intent="record_continuity",
                action="record_chapter_continuity",
                reason="quality passed but continuity has not been recorded",
                evidence=evidence,
                protected_inputs=protected_inputs,
            )
        return ProductionRouteDecision(
            intent="approve_after_continuity",
            action="approve_chapter",
            reason="quality and continuity are complete",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.latest_version_status == "approved":
        return ProductionRouteDecision(
            intent="publish_flow",
            action=s.publish_action or "done",
            reason=s.publish_reason or "approved chapter has no pending publish action",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    return ProductionRouteDecision(
        intent="inspect_unknown_state",
        action="inspect_manually",
        reason=f"unhandled latest version status: {s.latest_version_status}",
        evidence=evidence,
        protected_inputs=protected_inputs,
    )


def _decide_revision_route(
    s: ProductionSituation,
    *,
    evidence: tuple[str, ...],
    protected_inputs: tuple[str, ...],
) -> ProductionRouteDecision:
    if s.revision_queue_status:
        return ProductionRouteDecision(
            intent="wait_revision_queue",
            action="wait_generation_task",
            reason=f"revision generation task {s.revision_queue_id} is {s.revision_queue_status}",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.strategy_action in {"generate_rebuild_candidates", "revision_budget_recovery", "revision_trend_recovery"}:
        return ProductionRouteDecision(
            intent=s.strategy_intent or "production_strategy_override",
            action=s.strategy_action,
            reason=s.strategy_reason or "生产策略层判定当前路径无效，自动切换下一步。",
            evidence=(*evidence, *s.strategy_evidence),
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.budget_blocker:
        return ProductionRouteDecision(
            intent="recover_revision_budget",
            action="revision_budget_recovery",
            reason=s.budget_blocker,
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.trend_blocker:
        return ProductionRouteDecision(
            intent="recover_revision_trend",
            action="revision_trend_recovery",
            reason=s.trend_blocker,
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.should_generate_rebuild_candidates:
        return ProductionRouteDecision(
            intent="generate_rebuild_candidates",
            action="generate_rebuild_candidates",
            reason="连续重建/修订未通过，改为多候选生成并自动择优，停止单稿线性烧修订。",
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.reading_assessment_requires_revision:
        return ProductionRouteDecision(
            intent="revise_from_reading_assessment",
            action="revise_chapter",
            reason=_revision_reason(s, "阅读评估已自动生成修订合同，继续修到可读候选稿"),
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.protected_review_contract_passed:
        return ProductionRouteDecision(
            intent="revise_protected_review_contract",
            action="revise_chapter",
            reason=_revision_reason(s, "基础质检虽通过，但主编准定稿标准未关闭当前合同，继续自动定点修订。"),
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.feedback_marker_without_quality:
        return ProductionRouteDecision(
            intent="revise_feedback_or_recovery",
            action="revise_chapter",
            reason=_revision_reason(s, "latest version has a feedback/recovery brief and should continue revision"),
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.revision_matches_quality_or_feedback:
        return ProductionRouteDecision(
            intent="revise_matching_contract",
            action="revise_chapter",
            reason=_revision_reason(s, "latest version needs revision and revision brief exists"),
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    if s.has_revision_brief and s.story_clean_revision_brief:
        return ProductionRouteDecision(
            intent="revise_story_clean_contract",
            action="revise_chapter",
            reason=_revision_reason(s, "latest version needs revision and clean revision brief exists"),
            evidence=evidence,
            protected_inputs=protected_inputs,
        )
    return ProductionRouteDecision(
        intent="create_revision_brief",
        action="create_revision_brief",
        reason="latest version failed quality needs fresh revision brief",
        evidence=evidence,
        protected_inputs=protected_inputs,
    )


def _revision_reason(s: ProductionSituation, base: str) -> str:
    details: list[str] = []
    if s.has_sample_adoption:
        details.append("已采用小样方向必须继承")
    if s.has_continuity_context:
        details.append("必须承接上一章后果")
    return base if not details else f"{base}；{'；'.join(details)}"


def _evidence(s: ProductionSituation) -> tuple[str, ...]:
    rows = [
        f"chapter_status={s.chapter_status or 'unknown'}",
        f"version_status={s.latest_version_status or 'missing'}",
    ]
    if s.latest_quality_passed is not None:
        rows.append(f"quality_passed={bool(s.latest_quality_passed)}")
    if s.budget_blocker:
        rows.append(f"budget={s.budget_blocker}")
    if s.trend_blocker:
        rows.append(f"trend={s.trend_blocker}")
    if s.strategy_category:
        rows.append(f"strategy={s.strategy_category}:{s.strategy_confidence}")
    return tuple(rows)


def _protected_inputs(s: ProductionSituation) -> tuple[str, ...]:
    rows: list[str] = []
    if s.has_sample_adoption:
        rows.append("chapter_sample_adoption")
    if s.has_continuity_context:
        rows.append("previous_chapter_continuity")
    if s.has_revision_brief:
        rows.append("active_revision_brief")
    return tuple(rows)
