from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief
from app.services.context_contamination import context_anchor_lines


STALE_BRIEF_MARKERS = (
    "已废弃",
    "旧主角名",
    "旧世界名",
    "旧桥段",
    "江湖志",
    "大江湖",
    "林默",
    "陈默",
    "题材主味: 玄幻脑洞",
    "题材主味：玄幻脑洞",
    "【作品DNA】 - 题材主味: 玄幻脑洞",
)


def sanitize_chapter_brief_fields(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str = "",
    constraints: str = "",
) -> tuple[str, str, str]:
    anchors = context_anchor_lines(session, book_id=book_id)
    current_context = "\n".join(anchors)
    clean_goal = _sanitize_text(goal, current_context=current_context)
    clean_required = _sanitize_text(required_beats, current_context=current_context)
    clean_constraints = _sanitize_text(constraints, current_context=current_context)
    required_lines = [line for line in clean_required.splitlines() if line.strip()]
    for anchor in anchors:
        if anchor and anchor not in "\n".join(required_lines):
            required_lines.append(anchor)
    if not clean_goal.strip():
        clean_goal = f"第{chapter_number}章：承接当前作品设定推进。"
    return clean_goal.strip(), "\n".join(required_lines).strip(), clean_constraints.strip()


def sanitize_existing_chapter_brief(session: Session, *, book_id: int, brief: ChapterBrief) -> ChapterBrief:
    chapter = session.get(Chapter, brief.chapter_id)
    if not chapter:
        return brief
    goal, required_beats, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=book_id,
        chapter_number=chapter.chapter_number,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
    )
    brief.goal = goal
    brief.required_beats = required_beats
    brief.constraints = constraints
    session.flush()
    return brief


def _sanitize_text(text: str, *, current_context: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _line_has_wrong_anchor(line, current_context=current_context):
            continue
        lines.append(line)
    return "\n".join(lines)


def _line_has_wrong_anchor(line: str, *, current_context: str) -> bool:
    return any(marker in line and marker not in current_context for marker in STALE_BRIEF_MARKERS)
