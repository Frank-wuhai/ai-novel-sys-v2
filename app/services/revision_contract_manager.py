from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, QualityReport


REVISION_MODES = {"local_patch", "targeted", "rewrite", "fresh", "polish"}


@dataclass(frozen=True)
class RevisionContractAudit:
    active_brief_id: int
    revision_mode: str
    superseded_count: int
    normalized: bool

    def to_dict(self) -> dict:
        return {
            "active_brief_id": self.active_brief_id,
            "revision_mode": self.revision_mode,
            "superseded_count": self.superseded_count,
            "normalized": self.normalized,
        }


def prepare_new_revision_contract(session: Session, *, chapter_id: int) -> int:
    superseded = 0
    for brief in session.scalars(
        select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
    ):
        brief.status = "superseded"
        superseded += 1
    session.flush()
    return superseded


def normalize_active_revision_contract(
    session: Session,
    *,
    chapter_id: int,
    preferred_mode: str = "",
    quality: QualityReport | None = None,
) -> RevisionContractAudit | None:
    active = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not active:
        return None
    superseded = 0
    for older in session.scalars(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready", ChapterBrief.id != active.id)
    ):
        older.status = "superseded"
        superseded += 1
    mode = _effective_mode(active, preferred_mode=preferred_mode, quality=quality)
    before = _brief_text(active)
    active.goal = _strip_mode_lines(active.goal or "")
    active.required_beats = _strip_mode_lines(active.required_beats or "")
    active.constraints = _normalize_constraints(active.constraints or "", mode=mode)
    after = _brief_text(active)
    session.flush()
    return RevisionContractAudit(
        active_brief_id=active.id,
        revision_mode=mode,
        superseded_count=superseded,
        normalized=before != after or superseded > 0,
    )


def latest_revision_mode(text: str) -> str:
    normalized = (text or "").replace("：", ":")
    matches = list(re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", normalized))
    if not matches:
        return ""
    mode = matches[-1].group(1).strip().lower()
    return mode if mode in REVISION_MODES else ""


def quality_prefers_rewrite(quality: QualityReport | None) -> bool:
    if not quality:
        return False
    import json

    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    failure = data.get("production_failure_classification") if isinstance(data.get("production_failure_classification"), dict) else {}
    if failure.get("category") == "structure_rewrite":
        return True
    prediction = data.get("revision_pass_prediction") if isinstance(data.get("revision_pass_prediction"), dict) else {}
    return bool(prediction.get("should_rebuild"))


def _effective_mode(brief: ChapterBrief, *, preferred_mode: str, quality: QualityReport | None) -> str:
    if quality_prefers_rewrite(quality):
        return "rewrite"
    if preferred_mode in REVISION_MODES:
        return preferred_mode
    text = _brief_text(brief)
    mode = latest_revision_mode(text)
    return mode or "targeted"


def _normalize_constraints(text: str, *, mode: str) -> str:
    rows = [line for line in _strip_mode_lines(text).splitlines() if line.strip()]
    return "\n".join([*rows, f"revision_mode:{mode}"]).strip()


def _strip_mode_lines(text: str) -> str:
    rows = []
    for line in (text or "").splitlines():
        normalized = line.strip().replace("：", ":")
        if normalized.startswith(("revision_mode:", "修订模式:")):
            continue
        rows.append(line)
    return "\n".join(rows).strip()


def _brief_text(brief: ChapterBrief) -> str:
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])


def normalize_current_chapter_contract(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    quality: QualityReport | None = None,
    preferred_mode: str = "",
) -> RevisionContractAudit | None:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return None
    return normalize_active_revision_contract(session, chapter_id=chapter.id, quality=quality, preferred_mode=preferred_mode)
