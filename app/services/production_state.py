from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport, StoryFoundation

QUALITY_DIAGNOSTIC_BRIEF_MARKERS = (
    "依据质检报告",
    "上次质检分数",
    "质量门禁",
    "weak_narrative_dimension",
    "修复质检问题",
    "采纳二审建议",
)
REVISION_ARTIFACT_BRIEF_MARKERS = (
    *QUALITY_DIAGNOSTIC_BRIEF_MARKERS,
    "修订合同:",
    "执行修订合同",
    "原始机器修订建议",
    "验收清单",
    "反馈调整#",
    "按本次修订要求验收",
    "不扩大修改范围",
    "reading_assessment_auto_quality#",
    "阅读评估",
    "阅读评估自动修订",
    "当前阅读层级",
    "源版本锁定",
    "本轮只解决",
    "system_revision_",
    "自动修订预算",
    "换策略修订",
    "恢复底稿",
    "editorial_elevation_quality#",
    "升华修订",
    "当前版本层级",
)
LOCAL_REVISION_MODES = {"local_patch", "polish"}
STORY_INTENT_MARKERS = (
    "真实武侠世界",
    "核心作者意图",
    "剧情段",
    "承接",
    "人物",
    "主角",
    "江湖",
    "门派",
    "追兵",
    "章末",
    "压力",
)


def get_or_create_chapter(session: Session, *, book_id: int, chapter_number: int, title: str = "") -> Chapter:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if chapter:
        return chapter
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=title or f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    return chapter


def latest_foundation(session: Session, book_id: int) -> StoryFoundation | None:
    return session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))


def latest_brief(session: Session, chapter_id: int) -> ChapterBrief | None:
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))


def latest_story_brief(session: Session, chapter_id: int, *, search_limit: int = 80) -> ChapterBrief | None:
    rows = list(
        session.scalars(
            select(ChapterBrief)
            .where(ChapterBrief.chapter_id == chapter_id)
            .order_by(ChapterBrief.id.desc())
            .limit(search_limit)
        )
    )
    for brief in rows:
        text = brief_text(brief)
        if brief_is_local_revision(text) or not brief_has_story_intent(text):
            continue
        if not brief_has_revision_artifacts(text):
            return brief
    return None


def brief_text(brief: ChapterBrief) -> str:
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])


def brief_has_revision_artifacts(text: str) -> bool:
    return any(marker in (text or "") for marker in REVISION_ARTIFACT_BRIEF_MARKERS)


def brief_is_quality_diagnostic(text: str) -> bool:
    return any(marker in (text or "") for marker in QUALITY_DIAGNOSTIC_BRIEF_MARKERS)


def brief_revision_mode(text: str) -> str:
    normalized = (text or "").replace("：", ":")
    marker = "修订模式:"
    if marker not in normalized:
        return ""
    tail = normalized.split(marker, 1)[1].strip()
    value = []
    for ch in tail:
        if ch.isascii() and (ch.isalpha() or ch == "_"):
            value.append(ch)
            continue
        break
    return "".join(value)


def brief_is_local_revision(text: str) -> bool:
    mode = brief_revision_mode(text)
    if mode in LOCAL_REVISION_MODES:
        return True
    return (text or "").lstrip().startswith("局部修订")


def brief_has_story_intent(text: str) -> bool:
    return any(marker in (text or "") for marker in STORY_INTENT_MARKERS)


def next_version_number(session: Session, chapter_id: int) -> int:
    current = session.scalar(select(func.max(ChapterVersion.version_number)).where(ChapterVersion.chapter_id == chapter_id))
    return int(current or 0) + 1


def collect_version_scores(session: Session, chapter_id: int) -> list["VersionScore"]:
    """Build chronological (version_number, score, passed) history for early-stop.

    Returned in ascending version_number order. A version without a quality
    report contributes ``score=None, passed=False`` — the early-stop engine
    treats those as unscored and non-passing.
    """

    from app.services.revision_early_stop import VersionScore  # local to avoid cycle

    rows = session.execute(
        select(ChapterVersion.version_number, QualityReport.score, QualityReport.passed)
        .select_from(ChapterVersion)
        .outerjoin(QualityReport, QualityReport.chapter_version_id == ChapterVersion.id)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.version_number.asc())
    ).all()

    scores: list[VersionScore] = []
    for version_number, score, passed in rows:
        scores.append(
            VersionScore(
                version_number=int(version_number),
                score=int(score) if score is not None else None,
                passed=bool(passed) if passed is not None else False,
            )
        )
    return scores
