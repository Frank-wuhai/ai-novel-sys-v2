from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterBrief, ChapterVersion, QualityReport
from app.services.production_optimization import predict_revision_pass


@dataclass(frozen=True)
class ProductionStrategyAssessment:
    action: str = ""
    intent: str = ""
    reason: str = ""
    category: str = "normal"
    confidence: int = 0
    evidence: tuple[str, ...] = field(default_factory=tuple)


def assess_production_strategy(
    session: Session,
    *,
    chapter_id: int,
    latest_version: ChapterVersion | None,
    latest_quality: QualityReport | None,
    revision_brief: ChapterBrief | None,
    has_sample_adoption: bool = False,
    has_continuity_context: bool = False,
) -> ProductionStrategyAssessment:
    if not latest_version or latest_version.status != "needs_revision":
        return ProductionStrategyAssessment()

    rows = _recent_failed_quality_rows(session, chapter_id=chapter_id, limit=8)
    scores = [int(quality.score or 0) for _version, quality, _report in rows]
    sources = [str(version.source or "") for version, _quality, _report in rows]
    brief_text = _brief_text(revision_brief)
    evidence = _base_evidence(scores=scores, sources=sources, brief_text=brief_text)

    if _active_budget_recovery_state(latest_version=latest_version, brief_text=brief_text):
        return ProductionStrategyAssessment(
            action="",
            intent="continue_active_budget_recovery",
            category="active_recovery",
            confidence=80,
            reason="当前已有预算恢复稿和恢复合同，下一步应执行修订，不重复恢复。",
            evidence=evidence,
        )

    if _active_trend_recovery_state(latest_version=latest_version, brief_text=brief_text):
        return ProductionStrategyAssessment(
            action="",
            intent="continue_active_trend_recovery",
            category="active_recovery",
            confidence=80,
            reason="当前已有趋势恢复稿和恢复合同，下一步应执行修订，不重复切换策略。",
            evidence=evidence,
        )

    if _pending_trend_recovery_contract(brief_text):
        return ProductionStrategyAssessment(
            action="",
            intent="continue_pending_trend_recovery",
            category="active_recovery",
            confidence=78,
            reason="当前已有趋势恢复合同，下一步交由趋势恢复路由处理，不被平台期策略抢占。",
            evidence=evidence,
        )

    if latest_quality and _latest_failure_is_narrow_and_repairable(latest_quality):
        if not _should_defer_for_later(session, chapter_id=chapter_id, latest_quality=latest_quality):
            return ProductionStrategyAssessment(
                action="",
                intent="continue_narrow_repairable_gate",
                category="narrow_repairable_gate",
                confidence=84,
                reason="最新稿已把阻断收敛为单一可修门禁，继续定向修订，不重新打散为多候选。",
                evidence=evidence,
            )

    if latest_quality and _selected_rebuild_candidate_regressed(
        session,
        chapter_id=chapter_id,
        latest_version=latest_version,
        latest_quality=latest_quality,
    ):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="recover_regressed_rebuild_candidate",
            category="rebuild_candidate_regressed",
            confidence=96,
            reason="当前多候选择优稿低于历史最佳稿，停止沿低分稿继续修订；重新候选择优并启用历史最佳稿兜底。",
            evidence=evidence,
        )

    if _active_rebuild_candidate_state(latest_version=latest_version, brief_text=brief_text):
        if not _should_defer_for_later(session, chapter_id=chapter_id, latest_quality=latest_quality):
            return ProductionStrategyAssessment(
                action="",
                intent="continue_selected_rebuild_candidate",
                category="active_rebuild_candidate",
                confidence=82,
                reason="当前已有多候选择优稿，下一步应修选中稿，不重复生成候选或回到旧修订合同。",
                evidence=evidence,
            )

    if revision_brief and _should_defer_for_later(session, chapter_id=chapter_id, latest_quality=latest_quality):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="force_rebuild_blocked_chapter",
            category="readable_revision_deadlock",
            confidence=93,
            reason="当前章多轮修订和候选重建仍未关闭质量门禁；禁止未通过转入下一章，改用多候选回炉重建直到正式通过。",
            evidence=evidence,
        )

    if revision_brief and _comparison_restore_loop(session, chapter_id=chapter_id):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="escape_comparison_restore_loop",
            category="comparison_restore_loop",
            confidence=94,
            reason="连续定点修订低于当前最佳稿并被自动回退，继续修只会反复回到同一分数；改为多候选重建并择优。",
            evidence=evidence,
        )

    if revision_brief and _budget_recovery_pingpong(rows=rows, brief_text=brief_text):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="escape_budget_recovery_pingpong",
            category="strategy_loop",
            confidence=95,
            reason="预算恢复后仍连续低分未通过，继续单稿修订只会涨版号；改为多候选重建并自动择优。",
            evidence=evidence,
        )

    if revision_brief and _score_plateau_near_gate(scores):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="escape_near_gate_plateau",
            category="score_plateau",
            confidence=92,
            reason="最近多版卡在准合格区间但无法通过，说明不是局部措辞问题；改为多候选重建寻找可通过结构。",
            evidence=evidence,
        )

    if revision_brief and _linear_revision_exhausted(rows=rows, scores=scores):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="escape_linear_revision_exhaustion",
            category="linear_revision_exhausted",
            confidence=90,
            reason="连续正文修订未产生有效改善，停止线性烧修订，改为多候选重建。",
            evidence=evidence,
        )

    if revision_brief and _contract_is_conflicted(brief_text):
        return ProductionStrategyAssessment(
            action="revision_budget_recovery",
            intent="repair_conflicted_revision_contract",
            category="contract_conflict",
            confidence=86,
            reason="当前修订合同混入互相冲突的旧策略标记，先重写为单一结构修订合同再继续。",
            evidence=evidence,
        )

    if (
        latest_quality
        and not latest_quality.passed
        and revision_brief
        and _latest_report_requires_rebuild(latest_quality)
        and _can_strategy_escalate_reading_rebuild(rows=rows, brief_text=brief_text)
    ):
        return ProductionStrategyAssessment(
            action="generate_rebuild_candidates",
            intent="respect_quality_rebuild_signal",
            category="quality_requires_rebuild",
            confidence=88,
            reason="最新质检已指向结构重建，直接生成多候选稿，避免继续微修。",
            evidence=evidence,
        )

    if latest_quality and revision_brief:
        prediction = predict_revision_pass(_loads_json(latest_quality.report), chapter_number=0)
        if prediction.should_rebuild and prediction.confidence >= 88:
            return ProductionStrategyAssessment(
                action="generate_rebuild_candidates",
                intent="respect_pass_prediction_rebuild",
                category="pass_prediction_rebuild",
                confidence=prediction.confidence,
                reason="通过预测显示继续线性修订收益低，改为多候选重建以缩短达标时间。",
                evidence=(*evidence, *prediction.reasons),
            )

    if has_sample_adoption or has_continuity_context:
        return ProductionStrategyAssessment(
            action="",
            intent="protect_inputs_during_revision",
            category="protected_context",
            confidence=70,
            reason="继续修订时必须继承已采用小样和上一章承接。",
            evidence=evidence,
        )

    return ProductionStrategyAssessment(evidence=evidence)


def _recent_failed_quality_rows(
    session: Session,
    *,
    chapter_id: int,
    limit: int,
) -> list[tuple[ChapterVersion, QualityReport, dict]]:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    rows: list[tuple[ChapterVersion, QualityReport, dict]] = []
    for version in versions:
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        if quality and not quality.passed:
            rows.append((version, quality, _loads_json(quality.report)))
    return rows


def _budget_recovery_pingpong(*, rows: list[tuple[ChapterVersion, QualityReport, dict]], brief_text: str) -> bool:
    if "system_revision_budget_recovery" not in brief_text:
        return False
    failed_after_recovery = 0
    seen_recovery = False
    for version, quality, _report in rows:
        source = str(version.source or "")
        if source.startswith("revision_budget_recovery:"):
            seen_recovery = True
            continue
        if not seen_recovery or not source.startswith("revision:"):
            continue
        if int(quality.score or 0) < 70:
            failed_after_recovery += 1
    return failed_after_recovery >= 2


def _score_plateau_near_gate(scores: list[int]) -> bool:
    recent = [score for score in scores[:4] if score > 0]
    if len(recent) >= 2 and min(recent[:2]) >= 70 and max(recent[:2]) < 80 and max(recent[:2]) - min(recent[:2]) <= 2:
        return True
    if len(recent) < 3:
        return False
    return min(recent) >= 70 and max(recent) < 80 and max(recent) - min(recent) <= 3


def _linear_revision_exhausted(*, rows: list[tuple[ChapterVersion, QualityReport, dict]], scores: list[int]) -> bool:
    revision_rows = [row for row in rows if str(row[0].source or "").startswith("revision:")]
    if len(revision_rows) < 4 or len(scores) < 4:
        return False
    recent = scores[:4]
    return max(recent) < 76 or recent[0] <= max(recent[1:])


def _contract_is_conflicted(text: str) -> bool:
    normalized = text.replace("：", ":")
    modes = set()
    for marker, mode in (
        ("revision_mode:local_patch", "local_patch"),
        ("修订模式:local_patch", "local_patch"),
        ("revision_mode:fresh", "fresh"),
        ("修订模式:fresh", "fresh"),
        ("revision_mode:rewrite", "rewrite"),
        ("修订模式:rewrite", "rewrite"),
        ("revision_mode:targeted", "targeted"),
        ("修订模式:targeted", "targeted"),
    ):
        if marker in normalized:
            modes.add(mode)
    if len(modes) >= 2 and not (modes == {"rewrite", "targeted"} and "system_revision_budget_recovery" in normalized):
        return True
    return "system_revision_budget_recovery" in normalized and "reading_assessment_auto_quality#" in normalized


def _active_budget_recovery_state(*, latest_version: ChapterVersion, brief_text: str) -> bool:
    source = str(latest_version.source or "")
    if not source.startswith(("revision_budget_recovery:", "revision_budget_readable_restore:")):
        return False
    return True


def _active_trend_recovery_state(*, latest_version: ChapterVersion, brief_text: str) -> bool:
    source = str(latest_version.source or "")
    return source.startswith("revision_recovery:") and "system_revision_trend_recovery" in brief_text


def _pending_trend_recovery_contract(brief_text: str) -> bool:
    return "system_revision_trend_recovery" in brief_text


def _active_rebuild_candidate_state(*, latest_version: ChapterVersion, brief_text: str) -> bool:
    source = str(latest_version.source or "")
    return source.startswith(("rebuild_candidate_selected:", "rebuild_candidate_incumbent_restore:")) and (
        "reading_assessment_auto_quality#" in brief_text
        or "需重建" in brief_text
        or "失败结构不得沿用" in brief_text
        or "revision_mode:rewrite" in brief_text
        or "修订模式:rewrite" in brief_text
        or "本章已采用小样方向" in brief_text
        or "小样名:" in brief_text
        or "小样名：" in brief_text
    )


def _selected_rebuild_candidate_regressed(
    session: Session,
    *,
    chapter_id: int,
    latest_version: ChapterVersion,
    latest_quality: QualityReport,
) -> bool:
    source = str(latest_version.source or "")
    if not source.startswith("rebuild_candidate_selected:"):
        return False
    latest_score = int(latest_quality.score or 0)
    best_prior_score = 0
    for version in session.scalars(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id, ChapterVersion.id != latest_version.id)
        .order_by(ChapterVersion.id.desc())
        .limit(24)
    ):
        if version.status == "candidate":
            continue
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        if quality and quality.score is not None:
            best_prior_score = max(best_prior_score, int(quality.score or 0))
    return best_prior_score > latest_score


def _should_defer_for_later(session: Session, *, chapter_id: int, latest_quality: QualityReport | None, limit: int = 18) -> bool:
    if not latest_quality or int(latest_quality.score or 0) < 70:
        return False
    if _latest_failure_is_narrow_and_repairable(latest_quality):
        return False
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    failed_revisions = 0
    rebuild_tasks = 0
    near_readable = 0
    for version in versions:
        source = str(version.source or "")
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        if source.startswith("revision:") and quality and not quality.passed:
            failed_revisions += 1
        if source.startswith(("rebuild_candidate_selected:", "rebuild_candidate_incumbent_restore:")):
            rebuild_tasks += 1
        if quality and not quality.passed and int(quality.score or 0) >= 70:
            near_readable += 1
    if failed_revisions >= 3 and rebuild_tasks >= 1 and near_readable >= 4:
        return True
    return failed_revisions >= 4 and rebuild_tasks >= 2 and near_readable >= 4


def _latest_failure_is_narrow_and_repairable(quality: QualityReport) -> bool:
    if int(quality.score or 0) < 76:
        return False
    report = _loads_json(quality.report)
    issues = [str(item) for item in report.get("issues") or [] if str(item)]
    assessment = report.get("reading_assessment") if isinstance(report.get("reading_assessment"), dict) else {}
    blockers = [str(item) for item in assessment.get("blockers") or [] if str(item)]
    combined = list(dict.fromkeys([*issues, *blockers]))
    if len(combined) != 1:
        return False
    blocker = combined[0]
    repairable_markers = (
        "brief_coverage",
        "dialogue_fullness",
        "payoff_grounding",
        "chapter_unit_flow",
        "imageable_paragraphs",
    )
    return any(marker in blocker for marker in repairable_markers)


def _comparison_restore_loop(session: Session, *, chapter_id: int, limit: int = 8) -> bool:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    restore_count = 0
    failed_revision_count = 0
    for version in versions:
        source = str(version.source or "")
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        if source.startswith("revision_compare_restore:"):
            restore_count += 1
            continue
        if source.startswith("revision:") and quality and not quality.passed:
            failed_revision_count += 1
    return restore_count >= 2 and failed_revision_count >= 2


def _latest_report_requires_rebuild(quality: QualityReport) -> bool:
    report = _loads_json(quality.report)
    assessment = report.get("reading_assessment") if isinstance(report.get("reading_assessment"), dict) else {}
    if assessment.get("action") == "auto_rebuild":
        return True
    return any("重建" in str(item) or "重写" in str(item) for item in report.get("blockers") or [])


def _can_strategy_escalate_reading_rebuild(*, rows: list[tuple[ChapterVersion, QualityReport, dict]], brief_text: str) -> bool:
    if "reading_assessment_auto_quality#" not in brief_text:
        return True
    if any("system_revision_budget_recovery" in str(version.source or "") for version, _quality, _report in rows):
        return True
    return len(rows) >= 3


def _base_evidence(*, scores: list[int], sources: list[str], brief_text: str) -> tuple[str, ...]:
    rows: list[str] = []
    if scores:
        rows.append("recent_scores=" + ",".join(str(score) for score in scores[:5]))
    if sources:
        rows.append("recent_sources=" + ",".join(source for source in sources[:5] if source))
    markers = [marker for marker in ("system_revision_budget_recovery", "reading_assessment_auto_quality#", "clean_rebuild_contract@v1") if marker in brief_text]
    if markers:
        rows.append("brief_markers=" + ",".join(markers))
    return tuple(rows)


def _brief_text(brief: ChapterBrief | None) -> str:
    if not brief:
        return ""
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
