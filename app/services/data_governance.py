from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief


STALE_BRIEF_MARKERS = (
    "依据质检报告",
    "上次质检分数",
    "采纳二审建议",
    "修复质检问题",
    "执行修订合同",
    "修订合同:",
    "原始人工意见",
    "验收清单",
)


def audit_book_data_governance(session: Session, *, book_id: int, chapter_limit: int = 12) -> dict:
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number).limit(chapter_limit)
        )
    )
    stale_briefs = []
    for chapter in chapters:
        brief = session.scalar(
            select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc())
        )
        if not brief:
            continue
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        hits = [marker for marker in STALE_BRIEF_MARKERS if marker in text]
        if hits:
            stale_briefs.append(
                {
                    "chapter_number": chapter.chapter_number,
                    "brief_id": brief.id,
                    "status": brief.status,
                    "markers": hits,
                }
            )
    warnings = []
    if stale_briefs:
        warnings.append(f"有 {len(stale_briefs)} 个最新 brief 含旧质检/修订合同痕迹。")
    return {
        "passed": not stale_briefs,
        "stale_briefs": stale_briefs,
        "warnings": warnings,
        "recommendations": [
            "历史记录保留在 feedback/quality/task 表里；最新 chapter_brief 只保留当前生产需要的短执行摘要。",
            "如果章节已经从 fresh 转为 local_patch，brief 目标也应同步改成局部验收目标。",
        ],
    }
