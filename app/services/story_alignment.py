from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    Character,
    PlatformFeedback,
    PowerSystem,
    StoryBible,
    StoryFoundation,
    WorldRule,
)
from app.services.book_profile import build_book_profile
from app.services.skeleton_governance import SkeletonGovernanceReport, audit_story_skeleton


DEFAULT_MUST_MARKERS = (
    "真实武侠",
    "真实存在",
    "穿越",
    "有血有肉",
    "门派",
    "恩怨",
    "修炼",
    "拜师",
    "交易",
    "冒险",
    "江湖",
    "套路触发",
    "生活逻辑",
)

DEFAULT_AVOID_MARKERS = (
    "打怪升级",
    "刷经验",
    "刷副本",
    "经验值",
    "任务 NPC",
    "机械 NPC",
    "系统任务",
    "任务大厅",
    "杀毒软件",
    "觉醒者",
)

STALE_CONTEXT_MARKERS = (
    "依据质检报告",
    "上次质检分数",
    "采纳二审建议",
    "修复质检问题",
)


@dataclass(frozen=True)
class AlignmentSource:
    name: str
    kind: str
    positive_hits: list[str]
    negative_hits: list[str]
    stale_hits: list[str]
    preview: str


@dataclass(frozen=True)
class ChapterAlignment:
    chapter_number: int
    brief_id: int | None
    brief_status: str
    brief_positive_hits: list[str]
    brief_negative_hits: list[str]
    brief_stale_hits: list[str]
    latest_version_id: int | None
    latest_version_status: str
    content_positive_hits: list[str]
    content_negative_hits: list[str]
    content_chars: int


@dataclass(frozen=True)
class StoryAlignmentAudit:
    book_id: int
    book_title: str
    score: int
    status: str
    blockers: list[str]
    recommendations: list[str]
    source_summary: list[AlignmentSource]
    chapters: list[ChapterAlignment]


def build_story_alignment_audit(
    session: Session,
    *,
    book_id: int,
    chapter_limit: int = 8,
    must_markers: tuple[str, ...] | None = None,
    avoid_markers: tuple[str, ...] | None = None,
) -> StoryAlignmentAudit:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    profile = build_book_profile(session, book_id=book_id)
    active_must_markers = must_markers or profile.core_markers or DEFAULT_MUST_MARKERS
    active_avoid_markers = avoid_markers or profile.avoid_markers or DEFAULT_AVOID_MARKERS
    governance = audit_story_skeleton(session, book_id=book_id)

    sources = _collect_sources(session, book)
    source_summary = [
        AlignmentSource(
            name=name,
            kind=kind,
            positive_hits=_hits(text, active_must_markers),
            negative_hits=_negative_hits(text, active_avoid_markers),
            stale_hits=_hits(text, STALE_CONTEXT_MARKERS),
            preview=_preview(text),
        )
        for kind, name, text in sources
    ]
    chapters = _chapter_alignment(
        session,
        book_id=book_id,
        chapter_limit=chapter_limit,
        must_markers=active_must_markers,
        avoid_markers=active_avoid_markers,
    )
    blockers = _blockers(source_summary, chapters, governance=governance)
    recommendations = _recommendations(source_summary, chapters, governance=governance)
    score = _score(source_summary, chapters, blockers)
    status = "aligned" if score >= 80 and not blockers else ("blocked" if blockers else "attention")
    return StoryAlignmentAudit(
        book_id=book.id,
        book_title=book.title,
        score=score,
        status=status,
        blockers=blockers,
        recommendations=recommendations,
        source_summary=source_summary,
        chapters=chapters,
    )


def _collect_sources(session: Session, book: Book) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    foundation = session.scalar(
        select(StoryFoundation).where(StoryFoundation.book_id == book.id).order_by(StoryFoundation.id.desc())
    )
    if foundation:
        rows.append(
            (
                "foundation",
                f"StoryFoundation#{foundation.id}",
                "\n".join(
                    [
                        foundation.premise or "",
                        foundation.reader_promise or "",
                        foundation.world_engine or "",
                        foundation.protagonist_engine or "",
                        foundation.conflict_engine or "",
                    ]
                ),
            )
        )
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book.id).order_by(StoryBible.id.desc()))
    if bible:
        rows.append(
            (
                "bible",
                f"StoryBible#{bible.id}",
                "\n".join(
                    [
                        bible.positioning or "",
                        bible.reader_promise or "",
                        bible.main_plot or "",
                        bible.protagonist_arc or "",
                        bible.power_curve or "",
                        bible.forbidden_rules or "",
                        bible.style_guide or "",
                    ]
                ),
            )
        )
    for rule in session.scalars(select(WorldRule).where(WorldRule.book_id == book.id).order_by(WorldRule.id)):
        rows.append(("world_rule", f"WorldRule#{rule.id}:{rule.category}", rule.rule_text or ""))
    for power in session.scalars(select(PowerSystem).where(PowerSystem.book_id == book.id).order_by(PowerSystem.id)):
        rows.append(
            (
                "power",
                f"PowerSystem#{power.id}:{power.name}",
                "\n".join([power.rules or "", power.costs or "", power.limits or ""]),
            )
        )
    for character in session.scalars(select(Character).where(Character.book_id == book.id).order_by(Character.id)):
        rows.append(
            (
                "character",
                f"Character#{character.id}:{character.name}",
                "\n".join([character.personality or "", character.ability or "", character.background or ""]),
            )
        )
    for feedback in session.scalars(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book.id, PlatformFeedback.metric_name == "author_preference")
        .order_by(PlatformFeedback.id.desc())
        .limit(8)
    ):
        rows.append(("author_preference", f"PlatformFeedback#{feedback.id}:{feedback.metric_value}", feedback.raw_text or ""))
    return rows


def _chapter_alignment(
    session: Session,
    *,
    book_id: int,
    chapter_limit: int,
    must_markers: tuple[str, ...],
    avoid_markers: tuple[str, ...],
) -> list[ChapterAlignment]:
    chapters = session.scalars(
        select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number).limit(chapter_limit)
    )
    results: list[ChapterAlignment] = []
    for chapter in chapters:
        brief = session.scalar(
            select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc())
        )
        version = session.scalar(
            select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc())
        )
        brief_text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]) if brief else ""
        content = version.content or "" if version else ""
        results.append(
            ChapterAlignment(
                chapter_number=chapter.chapter_number,
                brief_id=brief.id if brief else None,
                brief_status=brief.status if brief else "",
                brief_positive_hits=_hits(brief_text, must_markers),
                brief_negative_hits=_negative_hits(brief_text, avoid_markers),
                brief_stale_hits=_hits(brief_text, STALE_CONTEXT_MARKERS),
                latest_version_id=version.id if version else None,
                latest_version_status=version.status if version else "",
                content_positive_hits=_hits(content, must_markers),
                content_negative_hits=_negative_hits(content, avoid_markers),
                content_chars=_chinese_chars(content),
            )
        )
    return results


def _blockers(
    sources: list[AlignmentSource],
    chapters: list[ChapterAlignment],
    *,
    governance: SkeletonGovernanceReport | None = None,
) -> list[str]:
    blockers: list[str] = []
    if governance and not governance.passed:
        codes = ",".join(issue.code for issue in governance.issues[:4])
        blockers.append(f"骨架治理未通过: score={governance.score} issues={codes}")
    if not any(source.positive_hits for source in sources if source.kind in {"foundation", "bible"}):
        blockers.append("故事地基/圣经没有明确写入核心作者意图")
    polluted_sources = [source.name for source in sources if source.negative_hits and source.kind in {"foundation", "bible", "world_rule", "power"}]
    if polluted_sources:
        blockers.append("核心设定源含有反方向词: " + ",".join(polluted_sources[:4]))
    stale_chapters = [str(item.chapter_number) for item in chapters if item.brief_stale_hits]
    if stale_chapters:
        blockers.append("最新章节 brief 仍含旧质检/旧修订合同残留: " + ",".join(stale_chapters))
    missing_brief_chapters = [str(item.chapter_number) for item in chapters if not item.brief_positive_hits]
    if missing_brief_chapters:
        blockers.append("章节 brief 未显式承接核心作者意图: " + ",".join(missing_brief_chapters))
    negative_content = [str(item.chapter_number) for item in chapters if item.content_negative_hits]
    if negative_content:
        blockers.append("最新正文仍出现反方向表达: " + ",".join(negative_content))
    return blockers


def _recommendations(
    sources: list[AlignmentSource],
    chapters: list[ChapterAlignment],
    *,
    governance: SkeletonGovernanceReport | None = None,
) -> list[str]:
    recs: list[str] = []
    if governance and not governance.passed:
        recs.append("先处理骨架治理 blocker：生成修复草案、二次审计通过并重新确认后，再继续小样或正文生产。")
    kinds = Counter(source.kind for source in sources if source.positive_hits)
    if kinds.get("author_preference", 0) and not kinds.get("foundation", 0):
        recs.append("作者口味已记录，但故事地基未同步；先更新 StoryFoundation/StoryBible，再重写章节。")
    if any(item.brief_stale_hits for item in chapters):
        recs.append("先清理最新章节 brief 中的旧质检合同，再继续生产，避免旧建议覆盖新方向。")
    if any(item.brief_positive_hits and not item.content_positive_hits and item.latest_version_id for item in chapters):
        recs.append("brief 已对齐但正文未兑现，问题更可能在正文模型、prompt 执行力或旧稿参考权重。")
    if any(item.content_negative_hits for item in chapters):
        recs.append("正文出现反方向表达时，先跑章节偏差审计；只有词句偏差用 local_patch，方向整体偏离再用 fresh。")
    if not recs:
        recs.append("当前上下文基本同向；下一步应小样本生产一章，再用审计结果比较 brief 与正文兑现差距。")
    return recs


def _score(sources: list[AlignmentSource], chapters: list[ChapterAlignment], blockers: list[str]) -> int:
    score = 100
    score -= min(35, len(blockers) * 10)
    score -= min(20, sum(1 for source in sources if source.negative_hits) * 4)
    score -= min(20, sum(1 for chapter in chapters if chapter.brief_stale_hits) * 8)
    score -= min(15, sum(1 for chapter in chapters if not chapter.brief_positive_hits) * 4)
    return max(0, score)


def _hits(text: str, markers: tuple[str, ...]) -> list[str]:
    return [marker for marker in markers if marker and marker in (text or "")]


def _negative_hits(text: str, markers: tuple[str, ...]) -> list[str]:
    value = text or ""
    hits: list[str] = []
    for marker in markers:
        if not marker:
            continue
        start = 0
        unsafe = False
        while True:
            index = value.find(marker, start)
            if index < 0:
                break
            if not _is_negated_warning_context(value, index):
                unsafe = True
                break
            start = index + len(marker)
        if unsafe:
            hits.append(marker)
    return hits


def _is_negated_warning_context(text: str, marker_index: int) -> bool:
    prefix = text[max(0, marker_index - 48) : marker_index]
    suffix = text[marker_index : marker_index + 32]
    warning_prefixes = (
        "禁止",
        "不是",
        "不靠",
        "不能",
        "不要",
        "不得",
        "没有",
        "避免",
        "禁",
        "而不是",
        "别写成",
        "不要写成",
        "不能写成",
        "不写成",
        "不可写成",
        "需要删除",
        "需要替换",
        "删除或替换",
        "清除",
        "修复",
        "禁止出现",
    )
    warning_suffixes = (
        "的主成长线",
        "或机械任务链",
        "的方向",
        "网游数值爽文",
    )
    return any(item in prefix for item in warning_prefixes) or any(item in suffix for item in warning_suffixes)


def _preview(text: str, *, limit: int = 120) -> str:
    compact = " ".join((text or "").split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _chinese_chars(text: str) -> int:
    return sum(1 for char in text or "" if "\u4e00" <= char <= "\u9fff")
