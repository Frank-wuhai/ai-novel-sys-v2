from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterBrief, ChapterVersion


@dataclass(frozen=True)
class ChapterArchiveResult:
    archived_versions: int
    superseded_revision_briefs: int

    def to_dict(self) -> dict:
        return {
            "archived_versions": self.archived_versions,
            "superseded_revision_briefs": self.superseded_revision_briefs,
        }


def archive_chapter_history_after_readable(
    session: Session,
    *,
    chapter_id: int,
    readable_version_id: int,
) -> ChapterArchiveResult:
    readable = session.get(ChapterVersion, readable_version_id)
    if not readable or readable.chapter_id != chapter_id or readable.status not in {"reviewed_pass", "approved"}:
        return ChapterArchiveResult(archived_versions=0, superseded_revision_briefs=0)

    archived = 0
    for version in session.scalars(
        select(ChapterVersion)
        .where(
            ChapterVersion.chapter_id == chapter_id,
            ChapterVersion.id < readable_version_id,
            ChapterVersion.status == "needs_revision",
        )
        .order_by(ChapterVersion.id.desc())
    ):
        source = str(version.source or "")
        if source.startswith("archived:"):
            continue
        if not (
            source.startswith("revision:")
            or source.startswith("revision_recovery:")
            or source.startswith("editorial_rollback:")
        ):
            continue
        version.source = "archived:" + source
        archived += 1

    superseded = 0
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"
        superseded += 1
    if archived or superseded:
        session.flush()
    return ChapterArchiveResult(archived_versions=archived, superseded_revision_briefs=superseded)
