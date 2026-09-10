"""Force rebuild helper — 让已批准/已发布章节能重新进入 rebuild_candidates 流程。

用途场景：产品 prompt 变更后（比如字数上限降低、加分段约束），需要把已 approved 或 published_review 的章节全部回炉重写。

安全性：
- 只改 chapter_versions.status 和 chapter_briefs.status（可逆）
- 不删除任何数据 · 只做状态倒回
- 有 PublishJob 处于 published/published_review 的会拒绝（避免污染已发布）
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Chapter,
    ChapterBrief,
    ChapterVersion,
    PublishJob,
)


def force_prepare_rebuild(session: Session, *, book_id: int, chapter_number: int) -> dict:
    """把章节状态倒回 needs_revision · 让 rebuild_candidates 能接手。

    Returns: {"ok": bool, "message": str, "version_id": int|None, "brief_id": int|None}
    """
    chapter = session.scalar(
        select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
    )
    if not chapter:
        return {"ok": False, "message": f"chapter not found: book={book_id} ch={chapter_number}"}

    # 1. 检查是否已发布到平台（有 published/published_review 的 PublishJob）
    published_job = session.scalar(
        select(PublishJob).join(ChapterVersion, PublishJob.chapter_version_id == ChapterVersion.id)
        .where(
            ChapterVersion.chapter_id == chapter.id,
            PublishJob.status.in_(["published", "published_review"]),
        )
    )
    if published_job:
        return {
            "ok": False,
            "message": f"chapter has published job#{published_job.id} (status={published_job.status}) · 拒绝回炉已发布章",
        }

    # 2. 找当前 non-discarded 最新 version
    version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )
    if not version:
        return {"ok": False, "message": f"chapter {chapter_number} has no non-discarded version"}

    old_status = version.status
    if version.status != "needs_revision":
        version.status = "needs_revision"

    # 3. 复活最新 brief · 状态 → revision_ready
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id)
        .order_by(ChapterBrief.id.desc())
    )
    if not brief:
        return {
            "ok": False,
            "message": f"chapter {chapter_number} has no brief · cannot rebuild",
            "version_id": version.id,
        }

    old_brief_status = brief.status
    if brief.status != "revision_ready":
        brief.status = "revision_ready"

    # 4. chapter 主状态也需要重置（避免 needs_confirmation 卡住）
    old_chapter_status = chapter.status
    if chapter.status in ("needs_confirmation", "approved"):
        chapter.status = "needs_revision"

    session.commit()

    return {
        "ok": True,
        "message": (
            f"prepared ch#{chapter_number}: "
            f"version#{version.id} {old_status}→needs_revision · "
            f"brief#{brief.id} {old_brief_status}→revision_ready · "
            f"chapter {old_chapter_status}→{chapter.status}"
        ),
        "version_id": version.id,
        "brief_id": brief.id,
    }
