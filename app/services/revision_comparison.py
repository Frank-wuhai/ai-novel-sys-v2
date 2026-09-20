from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterBrief, ChapterVersion, GenerationTask, QualityReport


WATCHED_DIMENSIONS = (
    "readability",
    "author_intent",
    "prose_voice",
    "dialogue_fullness",
    "character_voice",
    "scene_atmosphere",
    "paragraph_aesthetic",
    "chapter_unit_flow",
    "writer_craft",
    "brief_coverage",
)


@dataclass(frozen=True)
class RevisionComparisonResult:
    status: str
    source_version_id: int | None
    current_version_id: int
    restored_version_id: int | None
    score_delta: int
    degraded_dimensions: list[str]
    decision: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "source_version_id": self.source_version_id,
            "current_version_id": self.current_version_id,
            "restored_version_id": self.restored_version_id,
            "score_delta": self.score_delta,
            "degraded_dimensions": self.degraded_dimensions,
            "decision": self.decision,
        }


USER_ADJUDICATION_MARKER = "用户裁决"


def compare_and_restore_if_regressed(
    session: Session,
    *,
    current_version: ChapterVersion,
    current_quality: QualityReport,
    allow_restore: bool = True,
) -> RevisionComparisonResult:
    """修订稿 vs 源稿对比；回退时恢复源稿。

    2026-09-20 第 4.5 步两处收紧（QC 只读化 + 用户裁决保护）：
    - allow_restore=False（QC 评审路径）：只记录对比结论，不改版本状态。
      恢复动作收归修订管线 revise-chapter 入口——QC 命令必须只读，
      且恢复阈值（score_delta<=-5）曾小于门禁噪声（同文两次评审差 26 分），
      在评审路径自动恢复等于让测量抖动直接改写版本状态。
    - 含「用户裁决」指令的修订稿（创意门禁已终审）任何路径都禁止被分数对比
      单方面逆转，只能人工确认后处置。
    """
    if not str(current_version.source or "").startswith("revision:"):
        return RevisionComparisonResult("skipped", None, current_version.id, None, 0, [], "不是修订稿，不做版本对比。")
    if str(current_version.source or "").startswith(("revision_compare_restore:", "revision_recovery:", "editorial_rollback:")):
        return RevisionComparisonResult("skipped", None, current_version.id, None, 0, [], "恢复稿不再触发恢复。")
    source_version = _source_version_for_revision(session, version_id=current_version.id)
    if not source_version:
        return RevisionComparisonResult("missing_source", None, current_version.id, None, 0, [], "找不到源版本，无法对比。")
    source_quality = _latest_quality(session, version_id=source_version.id)
    if not source_quality:
        return RevisionComparisonResult("missing_source_quality", source_version.id, current_version.id, None, 0, [], "源版本缺少质检报告。")
    source_data = _loads_json(source_quality.report)
    current_data = _loads_json(current_quality.report)
    score_delta = int(current_quality.score or 0) - int(source_quality.score or 0)
    degraded = _degraded_dimensions(source_data, current_data)
    source_base_passed = bool(source_data.get("base_quality_passed", source_data.get("passed", source_quality.passed)))
    current_base_passed = bool(current_data.get("base_quality_passed", current_data.get("passed", current_quality.passed)))
    should_restore = (
        (not current_quality.passed and bool(source_quality.passed))
        or (source_base_passed and not current_base_passed)
        or score_delta <= -5
        or len(degraded) >= 3
    )
    result_status = "regressed" if should_restore else "improved_or_stable"
    restored_id = None
    decision = "修订稿未明显变差，保留当前稿继续流程。"
    if should_restore and _is_user_adjudicated_revision(session, version_id=current_version.id):
        result_status = "regressed_protected"
        decision = (
            "修订稿低于源稿，但产出该稿的修订简报含用户裁决指令——创意门禁的裁决"
            "禁止被分数对比单方面逆转，未自动恢复；请人工核验（注意排除评审噪声误判）后处置。"
        )
    elif should_restore and not allow_restore:
        result_status = "regressed_readonly"
        decision = (
            "修订稿低于源稿；QC 评审路径只读（2026-09-20 第 4.5 步），未自动恢复。"
            "如需回退请进入修订管线（revise-chapter 入口会先做回退恢复再修订）。"
        )
    elif should_restore:
        restored = _restore_source_version(
            session,
            source_version=source_version,
            source_quality=source_quality,
            failed_version=current_version,
            failed_quality=current_quality,
            score_delta=score_delta,
            degraded=degraded,
        )
        restored_id = restored.id
        decision = "修订稿低于源稿，已自动恢复到源稿，避免沿更差版本继续修。"
    result = RevisionComparisonResult(
        result_status,
        source_version.id,
        current_version.id,
        restored_id,
        score_delta,
        degraded,
        decision,
    )
    _attach_comparison(current_quality, result)
    session.flush()
    return result


def _is_user_adjudicated_revision(session: Session, *, version_id: int) -> bool:
    """产出该版本的修订简报是否含「用户裁决」指令（创意门禁终审标记）。"""
    for candidate in session.scalars(
        select(GenerationTask)
        .where(GenerationTask.task_type == "revise_chapter", GenerationTask.status == "completed")
        .order_by(GenerationTask.id.desc())
        .limit(80)
    ):
        output = _loads_json(candidate.output_json)
        if int(output.get("version_id") or 0) != version_id:
            continue
        input_data = _loads_json(candidate.input_json)
        brief_id = int(input_data.get("revision_brief_id") or 0)
        brief = session.get(ChapterBrief, brief_id) if brief_id else None
        if brief is None:
            return False
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        return USER_ADJUDICATION_MARKER in text
    return False


def _source_version_for_revision(session: Session, *, version_id: int) -> ChapterVersion | None:
    for candidate in session.scalars(
        select(GenerationTask)
        .where(GenerationTask.task_type == "revise_chapter", GenerationTask.status == "completed")
        .order_by(GenerationTask.id.desc())
        .limit(80)
    ):
        output = _loads_json(candidate.output_json)
        if int(output.get("version_id") or 0) != version_id:
            continue
        input_data = _loads_json(candidate.input_json)
        source_id = int(input_data.get("source_version_id") or 0)
        return session.get(ChapterVersion, source_id) if source_id else None
    return None


def _latest_quality(session: Session, *, version_id: int) -> QualityReport | None:
    return session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version_id)
        .order_by(QualityReport.id.desc())
    )


def _degraded_dimensions(source_data: dict, current_data: dict) -> list[str]:
    source_dims = source_data.get("dimensions") if isinstance(source_data.get("dimensions"), dict) else {}
    current_dims = current_data.get("dimensions") if isinstance(current_data.get("dimensions"), dict) else {}
    rows: list[str] = []
    for name in WATCHED_DIMENSIONS:
        before = int(source_dims.get(name) or 0)
        after = int(current_dims.get(name) or 0)
        if before and before - after >= 8:
            rows.append(f"{name}:{before}->{after}")
    return rows


def _restore_source_version(
    session: Session,
    *,
    source_version: ChapterVersion,
    source_quality: QualityReport,
    failed_version: ChapterVersion,
    failed_quality: QualityReport,
    score_delta: int,
    degraded: list[str],
) -> ChapterVersion:
    protected_brief = _latest_protected_revision_brief(session, chapter_id=failed_version.chapter_id)
    restored_status = "needs_revision" if protected_brief else ("reviewed_pass" if source_quality.passed else "needs_revision")
    if protected_brief:
        protected_brief.status = "revision_ready"
    restored = ChapterVersion(
        chapter_id=failed_version.chapter_id,
        version_number=_next_version_number(session, failed_version.chapter_id),
        title=source_version.title,
        content=source_version.content,
        status=restored_status,
        source=f"revision_compare_restore:v{source_version.id}",
    )
    session.add(restored)
    session.flush()
    if protected_brief:
        from app.services.reading_assessment import downgrade_rebound_brief_to_targeted, rebind_revision_brief_source

        rebind_revision_brief_source(protected_brief, version_id=restored.id)
        source_report = _loads_json(source_quality.report)
        source_base_passed = bool(source_report.get("base_quality_passed", source_report.get("passed", source_quality.passed)))
        if source_base_passed:
            downgrade_rebound_brief_to_targeted(protected_brief, version_id=restored.id, quality=source_quality)
    source_report = _loads_json(source_quality.report)
    source_report.pop("reading_assessment", None)
    source_report["revision_comparison_restore"] = {
        "failed_version_id": failed_version.id,
        "failed_quality_id": failed_quality.id,
        "source_version_id": source_version.id,
        "source_quality_id": source_quality.id,
        "score_delta": score_delta,
        "degraded_dimensions": degraded,
        "protected_brief_id": protected_brief.id if protected_brief else None,
        "reason": (
            "修订稿低于源稿，但存在未解决的阅读评估/修订合同，恢复源稿为待修订底稿。"
            if protected_brief
            else "修订稿低于源稿，自动恢复源稿作为当前最佳版本。"
        ),
    }
    session.add(
        QualityReport(
            chapter_version_id=restored.id,
            score=source_quality.score,
            passed=source_quality.passed,
            report=json.dumps(source_report, ensure_ascii=False),
        )
    )
    return restored


def _attach_comparison(quality: QualityReport, result: RevisionComparisonResult) -> None:
    data = _loads_json(quality.report)
    data["revision_comparison"] = result.to_dict()
    quality.report = json.dumps(data, ensure_ascii=False)


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.version_number.desc()))
    return (latest.version_number if latest else 0) + 1


def _latest_protected_revision_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    briefs = list(
        session.scalars(
            select(ChapterBrief)
            .where(ChapterBrief.chapter_id == chapter_id)
            .order_by(ChapterBrief.id.desc())
            .limit(16)
        )
    )
    brief_texts = [(brief, "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])) for brief in briefs]
    for brief, text in brief_texts:
        if _is_unit_flow_protected_revision_brief_text(text):
            return brief
    for brief, text in brief_texts:
        if _is_protected_revision_brief_text(text):
            return brief
    return None


def _is_protected_revision_brief_text(text: str) -> bool:
    if any(
        marker in text
        for marker in (
            "reading_assessment_contract",
            "reading_assessment_auto_quality#",
            "阅读评估结论",
            "当前稿不是正式批准稿",
            "修订方向:",
            "clean_rebuild_contract@v1",
        )
    ):
        return True
    return _is_unit_flow_protected_revision_brief_text(text)


def _is_unit_flow_protected_revision_brief_text(text: str) -> bool:
    normalized = text.lower()
    is_unit_flow = "unit_flow" in normalized or "单元流" in text or "小单元" in text
    is_local_contract = "revision_mode:local_patch" in normalized or "revision_mode:targeted" in normalized
    has_explicit_target = any(marker in text for marker in ("只修第", "只重写第", "只替换第", "只改第", "只动第"))
    return is_unit_flow and is_local_contract and has_explicit_target


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
