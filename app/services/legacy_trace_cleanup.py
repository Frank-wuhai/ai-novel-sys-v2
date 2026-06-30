from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief


@dataclass(frozen=True)
class LegacyTraceCleanupResult:
    inspected_count: int
    changed_count: int
    changed_brief_ids: tuple[int, ...]


def cleanup_active_production_traces(session: Session, *, book_id: int | None = None) -> LegacyTraceCleanupResult:
    stmt = select(ChapterBrief).where(ChapterBrief.status == "revision_ready")
    if book_id is not None:
        stmt = stmt.join(Chapter, ChapterBrief.chapter_id == Chapter.id).where(Chapter.book_id == book_id)
    inspected = 0
    changed: list[int] = []
    for brief in session.scalars(stmt.order_by(ChapterBrief.id)):
        inspected += 1
        if normalize_active_revision_brief(brief):
            changed.append(brief.id)
    if changed:
        session.flush()
    return LegacyTraceCleanupResult(inspected, len(changed), tuple(changed))


def normalize_active_revision_brief(brief: ChapterBrief) -> bool:
    if brief.status != "revision_ready":
        return False
    required = brief.required_beats or ""
    constraints = brief.constraints or ""
    full_text = "\n".join([brief.goal or "", required, constraints])
    if "system_revision_budget_recovery" in full_text:
        new_required = _normalize_budget_required_beats(required)
        new_constraints = _normalize_budget_constraints(constraints)
        changed = new_required != required or new_constraints != constraints
        if changed:
            brief.required_beats = new_required
            brief.constraints = new_constraints
        return changed
    return False


def _normalize_budget_required_beats(text: str) -> str:
    lines = _clean_lines(text)
    cleaned: list[str] = []
    has_rewrite = False
    for line in lines:
        normalized = line.replace("：", ":")
        if normalized.startswith(("revision_mode:local_patch", "修订模式:local_patch", "revision_mode:fresh", "修订模式:fresh", "revision_mode:targeted", "修订模式:targeted")):
            continue
        if normalized.startswith(("reading_assessment_contract:", "reading_assessment_auto_quality#")):
            continue
        if normalized.startswith(("revision_mode:rewrite", "修订模式:rewrite")):
            has_rewrite = True
        cleaned.append(line)
    if not has_rewrite:
        cleaned.insert(0, "修订模式:rewrite；预算恢复后按结构修订执行，不沿用旧局部补丁合同。")
    return "\n".join(dict.fromkeys(cleaned))


def _normalize_budget_constraints(text: str) -> str:
    cleaned: list[str] = []
    for line in _clean_lines(text):
        normalized = line.replace("：", ":")
        if normalized.startswith(("revision_mode:local_patch", "修订模式:local_patch", "revision_mode:fresh", "修订模式:fresh", "revision_mode:targeted", "修订模式:targeted")):
            continue
        if normalized.startswith(("reading_assessment_contract:", "reading_assessment_auto_quality#")):
            continue
        if "阅读评估自动修订" in line or "当前阅读层级" in line or "源版本锁定" in line or "fresh 重建" in line:
            continue
        cleaned.append(line)
    return "\n".join(dict.fromkeys(cleaned))


def _clean_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]
