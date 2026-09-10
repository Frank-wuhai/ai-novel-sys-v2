from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy import select

from app.models.entities import Chapter, ChapterVersion


REALITY_ANCHORS = ("现实", "出租屋", "硬板床", "头盔", "床", "手机", "房租", "矿泉水", "右手")
JIANGHU_ANCHORS = ("老大夫", "药铺", "镖行", "山口", "山道", "城门", "客栈", "道观")


@dataclass
class ChapterContinuityReport:
    passed: bool
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    previous_tail: str = ""
    current_opening: str = ""

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "issues": self.issues,
            "warnings": self.warnings,
            "previous_tail": self.previous_tail[-240:],
            "current_opening": self.current_opening[:240],
        }


def evaluate_chapter_continuity(session, *, book_id: int | None, chapter_number: int | None, current_text: str) -> ChapterContinuityReport:
    if not session or not book_id or not chapter_number or chapter_number <= 1:
        return ChapterContinuityReport(passed=True)
    previous = _load_previous_approved(session, book_id=book_id, chapter_number=chapter_number - 1)
    if not previous:
        return ChapterContinuityReport(passed=True, warnings=["previous_approved_version_missing"])
    return evaluate_opening_continuity(previous.content or "", current_text or "")


def evaluate_opening_continuity(previous_text: str, current_text: str) -> ChapterContinuityReport:
    previous_tail = (previous_text or "")[-600:]
    current_opening = (current_text or "")[:600]
    issues: list[str] = []
    warnings: list[str] = []

    if _has_any(previous_tail, REALITY_ANCHORS) and _has_any(current_opening, JIANGHU_ANCHORS) and not _has_any(current_opening, REALITY_ANCHORS):
        issues.append("opening_location_jump_after_reality_tail")

    repeated = _repeated_opening_phrase(previous_text, current_opening)
    if repeated:
        issues.append(f"repeated_previous_scene:{repeated[:30]}")

    if _has_unresolved_tail(previous_tail) and not _shares_tail_anchor(previous_tail, current_opening):
        issues.append("opening_ignores_previous_tail_hook")

    return ChapterContinuityReport(
        passed=not issues,
        issues=issues,
        warnings=warnings,
        previous_tail=previous_tail,
        current_opening=current_opening,
    )


def _load_previous_approved(session, *, book_id: int, chapter_number: int):
    stmt = (
        select(ChapterVersion)
        .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
        .where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
        .where(ChapterVersion.status.in_(("approved", "reviewed_pass")))
        .order_by(ChapterVersion.created_at.desc(), ChapterVersion.id.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def _has_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in (text or "") for term in terms)


def _repeated_opening_phrase(previous_text: str, current_opening: str) -> str:
    opening = current_opening or ""
    for phrase in re.split(r"[。！？!?；;\n]+", opening):
        phrase = phrase.strip(" ，,。“”\"'：:")
        if len(phrase) >= 10 and phrase in (previous_text or "")[:-200]:
            return phrase
    return ""


def _has_unresolved_tail(tail: str) -> bool:
    return any(marker in (tail or "") for marker in ("忽然", "门外", "响", "盯着", "发现", "还在", "没有消失", "下一次"))


def _shares_tail_anchor(tail: str, opening: str) -> bool:
    tail_terms = [term for term in REALITY_ANCHORS + JIANGHU_ANCHORS if term in (tail or "")]
    return any(term in (opening or "") for term in tail_terms[:6])
