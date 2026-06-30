from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.entities import Book
from app.services.data_governance import audit_book_data_governance
from app.services.llm_queue import build_generation_queue_health
from app.services.model_strategy import build_model_strategy
from app.services.planning import build_human_decision_package, plan_chapters
from app.services.production_decision import decide_chapter_production
from app.services.readiness import check_production_readiness
from app.services.revision_supervisor import supervise_revision_trend
from app.services.story_alignment import build_story_alignment_audit


@dataclass(frozen=True)
class ProductionControlReport:
    status: str
    status_label: str
    summary: str
    blockers: list[str]
    warnings: list[str]
    next_actions: list[str]
    metrics: dict[str, int | bool | str]

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "status_label": self.status_label,
            "summary": self.summary,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "next_actions": self.next_actions,
            "metrics": self.metrics,
        }

    @property
    def lines(self) -> list[str]:
        rows = [
            f"status={self.status}",
            f"status_label={self.status_label}",
            f"summary={self.summary}",
        ]
        rows.extend(f"blocker={item}" for item in self.blockers)
        rows.extend(f"warning={item}" for item in self.warnings)
        rows.extend(f"next_action={item}" for item in self.next_actions)
        rows.extend(f"metric\t{name}={value}" for name, value in sorted(self.metrics.items()))
        return rows


def build_production_control_report(
    session: Session,
    *,
    book_id: int,
    start: int = 1,
    count: int = 8,
) -> ProductionControlReport:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")

    readiness = check_production_readiness(session, book_id=book_id, start=start, count=count, live_llm=False)
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
    decisions = build_human_decision_package(session, book_id=book_id, start=start, count=count, apply_state_repairs=False)
    queue = build_generation_queue_health(session)
    alignment = build_story_alignment_audit(session, book_id=book_id, chapter_limit=count)
    governance = audit_book_data_governance(session, book_id=book_id, chapter_limit=count)
    model_strategy = build_model_strategy()
    revision_report = supervise_revision_trend(session, book_id=book_id, chapter_number=start)

    decisions_by_chapter = {item.chapter_number: decide_chapter_production(item) for item in plan_items}
    auto_ready = [item for item in plan_items if decisions_by_chapter[item.chapter_number].can_continue]
    confirmation_waiting = [item for item in plan_items if decisions_by_chapter[item.chapter_number].needs_author]
    inspect = [item for item in plan_items if item.next_action == "inspect_manually"]
    missing_versions = [item for item in plan_items if item.latest_version_status in {"missing", "no_version"}]
    approved = [item for item in plan_items if item.latest_version_status == "approved"]
    reviewed_pass = [item for item in plan_items if item.latest_version_status == "reviewed_pass"]

    blockers: list[str] = []
    warnings: list[str] = []
    next_actions: list[str] = []

    auto_repairable_blockers = [item for item in alignment.blockers if _repairable_brief_blocker(item)]
    hard_alignment_blockers = [
        item
        for item in alignment.blockers
        if not _repairable_brief_blocker(item) and not item.startswith("骨架治理未通过")
    ]
    if hard_alignment_blockers:
        blockers.extend(hard_alignment_blockers)
    if auto_repairable_blockers:
        warnings.append("检测到章节 brief 内部状态可自动修复；继续生产时会先清理再执行。")
    if not governance.get("passed") and not (auto_repairable_blockers and not hard_alignment_blockers):
        warnings.extend(governance.get("warnings", []))
    warnings.extend(model_strategy.get("warnings", []))
    if revision_report.status == "degrading":
        warnings.append("自动修订趋势劣化；系统将回退到近期最佳稿并换策略修订。")
    if queue.stale_running_count:
        blockers.append(f"有 {queue.stale_running_count} 个后台任务疑似卡死，先恢复或标记失败。")
    if queue.counts.get("failed", 0):
        warnings.append(f"后台队列有 {queue.counts.get('failed', 0)} 个失败任务，可到后台诊断查看。")
    failed_readiness = [check for check in readiness.checks if not check.passed]
    for check in failed_readiness:
        if check.severity == "blocker":
            blockers.append(f"{check.name}: {check.detail}")
        elif check.name not in {"human_decisions", "team_decisions"}:
            warnings.append(f"{check.name}: {check.detail}")
    if inspect:
        blockers.append(f"有 {len(inspect)} 章处于未知状态，需要查看后台状态。")

    if blockers:
        status = "blocked"
        status_label = "先处理阻断"
        next_actions.append("先处理总控阻断项，不要继续烧正文 token。")
    elif queue.running_count:
        status = "running"
        status_label = "后台运行中"
        next_actions.append("等待当前后台任务完成，或在后台诊断查看运行时间。")
    elif queue.counts.get("pending", 0):
        status = "queued"
        status_label = "待启动后台"
        next_actions.append("启动后台生产队列。")
    elif confirmation_waiting:
        status = "needs_confirmation"
        status_label = "等待确认"
        first = confirmation_waiting[0]
        first_decision = decisions_by_chapter[first.chapter_number]
        next_actions.append(f"第 {first.chapter_number} 章：{first_decision.next_step}")
    elif auto_ready:
        status = "can_produce"
        status_label = "可以继续生产"
        first = auto_ready[0]
        first_decision = decisions_by_chapter[first.chapter_number]
        next_actions.append(f"第 {first.chapter_number} 章：{first_decision.next_step}")
    else:
        status = "idle"
        status_label = "暂无动作"
        next_actions.append("当前范围没有可自动执行或待你处理的章节。")

    if alignment.status != "aligned":
        recommendations = alignment.recommendations
        if auto_repairable_blockers and not hard_alignment_blockers:
            recommendations = [item for item in recommendations if not _auto_handled_recommendation(item)]
        warnings.extend(recommendations)
    if reviewed_pass:
        warnings.append(f"有 {len(reviewed_pass)} 章已通过主编准定稿标准，等待连续性、采用确认或发布准备。")
    if missing_versions and not auto_ready:
        warnings.append(f"有 {len(missing_versions)} 章还没有正文版本。")

    effective_auto_ready = 0 if blockers else len(auto_ready)
    planned_auto_ready = len(plan_items) if blockers else len(auto_ready)
    metrics: dict[str, int | bool | str] = {
        "book_id": book.id,
        "range_start": start,
        "range_count": count,
        "readiness_passed": readiness.passed,
        "alignment_score": alignment.score,
        "auto_ready": effective_auto_ready,
        "planned_auto_ready": planned_auto_ready,
        "confirmation_waiting": len(confirmation_waiting),
        "human_waiting": len(confirmation_waiting),
        "approval_waiting": decisions.approval_count,
        "inspect_waiting": decisions.inspect_count,
        "approved": len(approved),
        "reviewed_pass": len(reviewed_pass),
        "queue_pending": int(queue.counts.get("pending", 0)),
        "queue_running": queue.running_count,
        "queue_failed": int(queue.counts.get("failed", 0)),
        "stale_briefs": len(governance.get("stale_briefs", [])),
        "auto_repairable_brief_blockers": len(auto_repairable_blockers),
        "revision_supervision": revision_report.status,
    }
    summary = _summary(status=status, metrics=metrics)
    return ProductionControlReport(
        status=status,
        status_label=status_label,
        summary=summary,
        blockers=_dedupe(blockers),
        warnings=_dedupe(warnings),
        next_actions=_dedupe(next_actions),
        metrics=metrics,
    )


def _summary(*, status: str, metrics: dict[str, int | bool | str]) -> str:
    if status in {"needs_author", "needs_confirmation"}:
        return f"系统已把内容推到确认点；待确认 {metrics['approval_waiting']} 章。"
    if status == "can_produce":
        return f"当前可自动推进；可生产章节 {metrics['auto_ready']} 章。"
    if status == "running":
        return f"后台正在运行；运行任务 {metrics['queue_running']} 个。"
    if status == "blocked":
        return "存在阻断项；先修系统状态或方向，再继续生产。"
    if status == "queued":
        return f"有待启动生成任务 {metrics['queue_pending']} 个。"
    return "当前范围没有紧急生产动作。"


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _repairable_brief_blocker(value: str) -> bool:
    return (
        "最新章节 brief 仍含旧质检/旧修订合同残留" in value
        or "章节 brief 未显式承接核心作者意图" in value
        or "brief 未承接当前骨架锚点" in value
    )


def _auto_handled_recommendation(value: str) -> bool:
    return "清理最新章节 brief" in value or "旧质检合同" in value
