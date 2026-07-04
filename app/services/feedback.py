from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, FeedbackAdjustment, PlatformFeedback
from app.services.brief_sanitizer import sanitize_chapter_brief_fields
from app.services.evidence import add_evidence_source, add_market_signal
from app.services.revision_intent import (
    decide_revision_intent,
    extract_revision_decision,
    normalize_revision_mode as normalize_revision_intent_mode,
)
from app.workflows.state_machine import move

REVISION_MODE_POLISH = "polish"
REVISION_MODE_LOCAL_PATCH = "local_patch"
REVISION_MODE_TARGETED = "targeted"
REVISION_MODE_REWRITE = "rewrite"
REVISION_MODE_FRESH = "fresh"
REVISION_MODES = {REVISION_MODE_POLISH, REVISION_MODE_LOCAL_PATCH, REVISION_MODE_TARGETED, REVISION_MODE_REWRITE, REVISION_MODE_FRESH}
AUTHOR_PREFERENCE_METRIC = "author_preference"


@dataclass(frozen=True)
class FeedbackSummary:
    total: int
    by_metric: dict[str, int]
    by_platform: dict[str, int]
    latest: list[PlatformFeedback]


def record_platform_feedback(
    session: Session,
    *,
    book_id: int,
    platform: str,
    metric_name: str,
    metric_value: str = "",
    raw_text: str = "",
    chapter_number: int | None = None,
) -> PlatformFeedback:
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    chapter_id = None
    if chapter_number is not None:
        chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
        if not chapter:
            raise ValueError(f"chapter not found: {chapter_number}")
        chapter_id = chapter.id
    feedback = PlatformFeedback(
        book_id=book_id,
        chapter_id=chapter_id,
        platform=platform,
        metric_name=metric_name,
        metric_value=metric_value,
        raw_text=raw_text,
    )
    session.add(feedback)
    session.flush()
    return feedback


def list_platform_feedback(
    session: Session,
    *,
    book_id: int,
    platform: str = "",
    metric_name: str = "",
    limit: int = 20,
) -> list[PlatformFeedback]:
    stmt = select(PlatformFeedback).where(PlatformFeedback.book_id == book_id).order_by(PlatformFeedback.id.desc()).limit(limit)
    if platform:
        stmt = stmt.where(PlatformFeedback.platform == platform)
    if metric_name:
        stmt = stmt.where(PlatformFeedback.metric_name == metric_name)
    return list(session.scalars(stmt))


def summarize_platform_feedback(session: Session, *, book_id: int, limit: int = 20) -> FeedbackSummary:
    items = list_platform_feedback(session, book_id=book_id, limit=limit)
    return FeedbackSummary(
        total=len(items),
        by_metric=dict(Counter(item.metric_name for item in items)),
        by_platform=dict(Counter(item.platform for item in items)),
        latest=items[:5],
    )


def record_author_preference(
    session: Session,
    *,
    book_id: int,
    category: str,
    preference_text: str,
) -> PlatformFeedback:
    text = preference_text.strip()
    if not text:
        raise ValueError("preference text is required")
    value = (category or "general").strip()[:120] or "general"
    return record_platform_feedback(
        session,
        book_id=book_id,
        platform="author",
        metric_name=AUTHOR_PREFERENCE_METRIC,
        metric_value=value,
        raw_text=text,
    )


def format_author_preference_context(session: Session, *, book_id: int, limit: int = 12) -> str:
    rows = list_platform_feedback(
        session,
        book_id=book_id,
        platform="author",
        metric_name=AUTHOR_PREFERENCE_METRIC,
        limit=limit,
    )
    if not rows:
        return "未登记作者口味；以最新生产骨架、章节 brief 和本轮修订方向为准。"
    labels = {
        "like": "喜欢",
        "dislike": "讨厌",
        "must": "必须保留/强化",
        "avoid": "绝对避免",
        "style": "文风偏好",
        "general": "一般偏好",
    }
    lines = ["作者口味库（低于本轮修订合同，高于泛化市场建议）："]
    seen: set[tuple[str, str]] = set()
    for item in rows:
        text = item.raw_text.strip()
        if not text:
            continue
        category = item.metric_value or "general"
        key = (category, text)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"- {labels.get(category, category)}：{text}")
    return "\n".join(lines) if len(lines) > 1 else "未登记作者口味；以最新生产骨架、章节 brief 和本轮修订方向为准。"


def format_chapter_sample_adoption_context(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    limit: int = 2,
) -> str:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return ""
    rows = list(
        session.scalars(
            select(PlatformFeedback)
            .where(
                PlatformFeedback.book_id == book_id,
                PlatformFeedback.chapter_id == chapter.id,
                PlatformFeedback.platform == "chapter_sample_lab",
                PlatformFeedback.metric_name == "revision_suggestion",
            )
            .order_by(PlatformFeedback.id.desc())
            .limit(max(1, int(limit or 1)))
        )
    )
    if not rows:
        return ""
    lines = ["本章已采用小样方向（高于普通作者偏好；后续 draft/revision/recovery 不得丢失）："]
    seen: set[str] = set()
    for item in rows:
        text = _compact_text(item.raw_text or "", limit=520)
        if not text or text in seen:
            continue
        seen.add(text)
        lines.append(f"- {text}")
    return "\n".join(lines) if len(lines) > 1 else ""


def convert_feedback_to_market_signal(
    session: Session,
    *,
    feedback_id: int,
    genre: str,
    signal_text: str,
    confidence: int = 65,
    source_status: str = "verified",
    source_reliability: int = 3,
) -> tuple[int, int]:
    feedback = session.get(PlatformFeedback, feedback_id)
    if not feedback:
        raise ValueError(f"feedback not found: {feedback_id}")
    source_key = f"feedback-{feedback.id}"
    source = add_evidence_source(
        session,
        source_id=source_key,
        title=f"{feedback.platform} feedback #{feedback.id}",
        url="",
        reliability=source_reliability,
        status=source_status,
    )
    signal = add_market_signal(
        session,
        source_key=source.source_id,
        genre=genre,
        signal_text=signal_text,
        confidence=confidence,
    )
    return source.id, signal.id


def create_feedback_adjustment(
    session: Session,
    *,
    book_id: int,
    target_chapter_number: int,
    feedback_ids: list[int],
    adjustment_text: str = "",
    status: str = "ready",
) -> FeedbackAdjustment:
    if target_chapter_number < 1:
        raise ValueError("target chapter number must be >= 1")
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    feedback_items = _feedback_items(session, book_id=book_id, feedback_ids=feedback_ids)
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == target_chapter_number))
    text = adjustment_text or _default_adjustment_text(feedback_items, target_chapter_number)
    routed_text = _auto_route_adjustment_text(
        session,
        book_id=book_id,
        chapter_number=target_chapter_number,
        text=text,
    )
    adjustment = FeedbackAdjustment(
        book_id=book_id,
        chapter_id=chapter.id if chapter else None,
        target_chapter_number=target_chapter_number,
        feedback_ids=",".join(str(item.id) for item in feedback_items),
        adjustment_text=routed_text,
        status=status,
    )
    session.add(adjustment)
    session.flush()
    return adjustment


def list_feedback_adjustments(
    session: Session,
    *,
    book_id: int,
    status: str = "",
    limit: int = 20,
) -> list[FeedbackAdjustment]:
    stmt = (
        select(FeedbackAdjustment)
        .where(FeedbackAdjustment.book_id == book_id)
        .order_by(FeedbackAdjustment.id.desc())
        .limit(limit)
    )
    if status:
        stmt = stmt.where(FeedbackAdjustment.status == status)
    return list(session.scalars(stmt))


def _latest_non_local_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    briefs = list(
        session.scalars(
            select(ChapterBrief)
            .where(ChapterBrief.chapter_id == chapter_id)
            .order_by(ChapterBrief.id.desc())
            .limit(12)
        )
    )
    for brief in briefs:
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if "system_revision_loop_guard" in text:
            continue
        if "修订模式:local_patch" in text or "修订模式：local_patch" in text:
            continue
        if str(brief.goal or "").startswith("局部修订"):
            continue
        return brief
    return None


def _supersede_previous_revision_briefs(session: Session, *, chapter_id: int, keep_id: int) -> None:
    briefs = session.scalars(
        select(ChapterBrief).where(
            ChapterBrief.chapter_id == chapter_id,
            ChapterBrief.status == "revision_ready",
            ChapterBrief.id != keep_id,
        )
    )
    for brief in briefs:
        brief.status = "superseded"


def apply_feedback_adjustment_to_brief(
    session: Session,
    *,
    adjustment_id: int,
    brief_status: str = "ready",
) -> ChapterBrief:
    adjustment = session.get(FeedbackAdjustment, adjustment_id)
    if not adjustment:
        raise ValueError(f"feedback adjustment not found: {adjustment_id}")
    chapter = _get_or_create_chapter(
        session,
        book_id=adjustment.book_id,
        chapter_number=adjustment.target_chapter_number,
    )
    latest = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    label = f"修订方向#{adjustment.id}"
    mode, clean_text = _extract_revision_mode(adjustment.adjustment_text)
    brief_contract = build_brief_revision_contract(adjustment.adjustment_text, chapter_number=adjustment.target_chapter_number)
    if latest:
        if mode in {REVISION_MODE_LOCAL_PATCH, REVISION_MODE_POLISH}:
            goal = f"局部修订第{adjustment.target_chapter_number}章：{_compact_text(clean_text, limit=220)}"
            required_beats = f"revision_mode:{mode}；保留当前可用结构，只处理修订方向命中的最低风险问题。"
        elif mode == REVISION_MODE_FRESH:
            goal = f"按最新设定重写第{adjustment.target_chapter_number}章：{_compact_text(clean_text, limit=260)}"
            required_beats = (
                f"revision_mode:{mode}；旧稿只作为禁用反例，不沿用旧稿段落顺序、旧场景推进、旧人物名或旧桥段。"
                "必须以最新 Story Bible、Canon、作品DNA和修订方向为准重新生成。"
            )
        elif mode == REVISION_MODE_REWRITE:
            goal = f"结构重写第{adjustment.target_chapter_number}章：{_compact_text(clean_text, limit=260)}"
            required_beats = f"revision_mode:{mode}；按修订方向重做章节结构，不沿用旧恢复底稿。"
        else:
            latest = _latest_non_local_brief(session, chapter_id=chapter.id) or latest
            goal = latest.goal
            required_beats = _append_unique(
                _strip_revision_beats(latest.required_beats),
                f"revision_mode:{mode}；按修订方向定点处理，不扩大修改范围",
            )
        constraints = _append_unique(_strip_feedback_constraints(latest.constraints), f"{label}:\n{brief_contract}")
    else:
        goal = f"根据修订方向处理第{adjustment.target_chapter_number}章"
        required_beats = f"revision_mode:{mode}；按修订方向定点处理，不扩大修改范围"
        constraints = f"{label}:\n{brief_contract}"
    goal, required_beats, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=adjustment.book_id,
        chapter_number=adjustment.target_chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
    )
    brief = ChapterBrief(
        chapter_id=chapter.id,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        status=brief_status,
    )
    session.add(brief)
    adjustment.chapter_id = chapter.id
    adjustment.status = "applied"
    _supersede_previous_revision_briefs(session, chapter_id=chapter.id, keep_id=brief.id)
    session.flush()
    return brief


def submit_revision_suggestion(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    suggestion_text: str,
    platform: str = "manual",
    revision_mode: str = "auto",
) -> tuple[PlatformFeedback, FeedbackAdjustment, ChapterBrief, ChapterVersion | None]:
    text = suggestion_text.strip()
    if not text:
        raise ValueError("suggestion text is required")
    decision = decide_revision_intent(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        suggestion_text=text,
        requested_mode=revision_mode,
    )
    mode = decision.mode
    _supersede_previous_adjustments(session, book_id=book_id, chapter_number=chapter_number)
    feedback = record_platform_feedback(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform=platform or "manual",
        metric_name="revision_suggestion",
        metric_value=mode,
        raw_text=text,
    )
    adjustment = create_feedback_adjustment(
        session,
        book_id=book_id,
        target_chapter_number=chapter_number,
        feedback_ids=[feedback.id],
        adjustment_text=f"{decision.contract_prefix()}\n{text}",
    )
    brief = apply_feedback_adjustment_to_brief(session, adjustment_id=adjustment.id, brief_status="revision_ready")
    latest_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == brief.chapter_id)
        .order_by(ChapterVersion.id.desc())
    )
    # Sprint 2 P1-3 stage-9 (unified via chapter_state helper P2-Ch27):
    # never demote a reviewed_pass version when the chapter has entered a
    # post-accept terminal state. accept_early_stop promotes the
    # best_version_number to reviewed_pass and flips chapter.status to
    # needs_confirmation; if we still ran the demote below
    # (feedback_reopen), the just-promoted version would silently slide
    # back to needs_revision and continuity gates on Ch+1 would block
    # indefinitely. Root-caused on book=3 Ch10/Ch12/Ch15 (stage-9)
    # and Ch27 (P2-Ch27, where 4 sibling demote paths were still open).
    from app.services.chapter_state import chapter_is_in_closed_state
    _chapter_closed = chapter_is_in_closed_state(session, brief.chapter_id)
    if (
        latest_version
        and not _chapter_closed
        and latest_version.status in {"draft", "reviewed_pass", "approved"}
    ):
        latest_version.status = move("chapter_version", latest_version.status, "needs_revision", "feedback_reopen")
    session.flush()
    return feedback, adjustment, brief, latest_version


def build_rewrite_contract(suggestion_text: str, *, chapter_number: int) -> str:
    return build_revision_contract(suggestion_text, chapter_number=chapter_number)


def build_brief_revision_contract(suggestion_text: str, *, chapter_number: int) -> str:
    text = suggestion_text.strip()
    if not text:
        raise ValueError("suggestion text is required")
    mode, clean_text = _extract_revision_mode(text)
    scope = {
        REVISION_MODE_LOCAL_PATCH: "只修改明确命中的句子、词语或短段落，必须按最小范围处理，保留其余正文。",
        REVISION_MODE_TARGETED: "保留可用结构和有效场景，只重写明确不合格的部分。",
        REVISION_MODE_POLISH: "保留剧情结构，只润色表达、节奏密度和现场反应。",
        REVISION_MODE_FRESH: "按最新生产骨架重启本章，旧稿不作为段落参照。",
        REVISION_MODE_REWRITE: "允许结构性重写，以最新生产骨架和 Canon 为准。",
    }.get(mode, "以最新生产骨架和修订方向为准。")
    return "\n".join(
        [
            "修订方向说明:",
            f"revision_mode:{mode}",
            f"范围:{scope}",
            "读感目标:",
            _compact_text(clean_text, limit=900),
            "主编验收:",
            "- 保留当前最佳稿中已通过基础质量底线的主事件、因果链和章末事实。",
            "- 禁止把修订说明、质检术语、合同条目或系统信息写进正文。",
            f"- 第{chapter_number}章下一版必须在最低读感维度上有可见改善，不能以整章换方向逃避定点问题。",
        ]
    )


def build_revision_contract(suggestion_text: str, *, chapter_number: int) -> str:
    text = suggestion_text.strip()
    if not text:
        raise ValueError("suggestion text is required")
    mode, clean_text = _extract_revision_mode(text)
    lines = [
        line.strip(" -\t")
        for line in re.split(r"[\n。；;]+", clean_text.replace("\r\n", "\n"))
        if line.strip(" -\t")
    ]
    must: list[str] = []
    must_not: list[str] = []
    target: list[str] = []
    for line in lines:
        normalized = line.lstrip("0123456789.、)） ")
        if _looks_like_forbidden(normalized):
            must_not.append(normalized)
        elif _looks_like_requirement(normalized):
            must.append(normalized)
        else:
            target.append(normalized)
    if not must:
        must.append(f"回应修订方向：{clean_text}")
    if not target:
        target.append(f"第{chapter_number}章修订后必须更贴近建议指定的读者体验。")
    if mode == REVISION_MODE_POLISH:
        mode_line = "修订模式:polish（小修：保留当前剧情结构，只改表达、节奏密度和局部自然度）"
        must_not.extend(
            [
                "不要改变已成立的核心剧情走向、关键设定和章节结尾事实",
                "不要把修订说明、质检术语、合同条目或系统信息写进正文",
                "不要用总结式心理活动代替可见行动、对话、压力和后果",
            ]
        )
    elif mode == REVISION_MODE_LOCAL_PATCH:
        mode_line = "修订模式:local_patch（最小补丁：只修改明确命中的句子或段落，不改章节结构、场景顺序和章末事实）"
        must.append("只修复建议命中的具体句子、词语或短段落，保留其余正文")
        must_not.extend(
            [
                "不要整章重写",
                "不要重排场景",
                "不要改变已成立的剧情事实、人物关系和章末钩子",
                "不要扩大修改范围",
                "不要把修订说明、质检术语、合同条目或系统信息写进正文",
            ]
        )
    elif mode == REVISION_MODE_TARGETED:
        mode_line = "修订模式:targeted（定点修订：保留已可用的章节结构和有效场景，只重写明确不合格的部分）"
        must.append("保留用户认为可用的段落、场景、人物行动链和章末钩子，除非它们违反最新骨架或 Canon")
        must_not.extend(
            [
                "不要彻底重写整章，不要替换用户已经认可的有效内容",
                "不要改变已成立的核心剧情走向、关键设定和章节结尾事实，除非修订方向明确要求",
                "不要把局部问题扩大成整章重做",
                "不要把修订说明、质检术语、合同条目或系统信息写进正文",
                "不要用总结式心理活动代替可见行动、对话、压力和后果",
            ]
        )
    elif mode == REVISION_MODE_FRESH:
        mode_line = "修订模式:fresh（按最新生产骨架重启本章：旧稿已废弃，不进入创作参照）"
        must_not.extend(
            [
                "不要参考旧稿段落顺序、旧场景推进、旧句式和旧桥段",
                "不要采纳旧质检里已经被最新生产骨架推翻的具体名词、能力表现或场景建议",
                "不要只做局部润色，必须重新设计开篇牵引、主角行动链、信息释放顺序、主要场景推进和章末钩子",
                "不要把修订说明、质检术语、合同条目或系统信息写进正文",
                "不要用总结式心理活动代替可见行动、对话、压力和后果",
            ]
        )
    else:
        mode_line = "修订模式:rewrite（结构性重写：以最新生产骨架为准，旧稿只作 Canon 弱参考）"
        must_not.extend(
            [
                "不要只做局部润色，允许重排场景、重写开头、替换无效桥段",
                "不要沿用旧稿段落顺序、句式和场景推进方式；旧稿只作 Canon 参考",
                "不要把修订说明、质检术语、合同条目或系统信息写进正文",
                "不要用总结式心理活动代替可见行动、对话、压力和后果",
            ]
        )
    if mode in {REVISION_MODE_LOCAL_PATCH, REVISION_MODE_POLISH}:
        understanding_rules = [
            "先判断用户真正不满意的是词句、动作、对白、画面、承接还是局部读感。",
            "如果修订方向只命中句子、词语或短段落，必须按最小范围处理，不要扩大成整章结构调整。",
            "修完后上下文必须读得顺，不能留下半句话、断动作或前后称谓不一致。",
        ]
        checks = [
            "建议命中的句子、词语、短段落或局部读感是否已修复",
            "未被点名的问题段落、场景顺序、人物关系和章末事实是否保持不变",
            "修订后上下文是否自然承接，没有新增解释腔、合同条目或系统信息",
            "禁止项是否完全没有进入正文",
        ]
    elif mode == REVISION_MODE_TARGETED:
        understanding_rules = [
            "先判断用户真正不满意的是读者体验、人物动机、场景选择、节奏、爽点、设定兑现、文风还是章末期待。",
            "把修订方向转化为明确的局部或场景级变化：替换哪些问题段落，补强哪些行动、对白、画面或承接。",
            "不要把局部问题扩大成整章重做；除非修订方向明确说整章方向废弃。",
        ]
        checks = [
            "是否保留可用结构和有效场景，只处理明确不合格的部分",
            "修订方向中的每个必须项是否在正文中有可见兑现",
            "主角行动、场景承接、对白或画面问题是否被定点修复",
            "禁止项是否完全没有进入正文",
            "章末事实是否未被无故改变",
        ]
    else:
        understanding_rules = [
            "先判断用户真正不满意的是读者体验、人物动机、场景选择、节奏、爽点、设定兑现、文风还是章末期待。",
            "如果修订方向是抽象判断，必须转化为正文里的可见变化：新增/删除/替换哪些场景，主角做什么不同选择，读者会获得什么不同感受。",
            "不要只替换几个名词或句子；必须让正文结果能被主编验收维度验证。",
        ]
        checks = [
            "是否把修订方向转化成正文里的可见变化，而不是只复述关键词",
            "开篇是否在前500字内进入具体处境，并用人物欲望、关系张力、异常细节、利益交换、行动后果或悬念形成牵引",
            "主角是否做出主动选择，并产生收益、代价或后果",
            "修订方向中的每个必须项是否在正文中有可见兑现",
            "禁止项是否完全没有进入正文",
            "章末是否留下具体危险、发现、转折或未解决压力",
        ]
    return "\n".join(
        [
            "修订合同:",
            mode_line,
            "修订方向:",
            clean_text,
            "意见理解规则:",
            *_bullet_lines(understanding_rules),
            "目标读者体验:",
            *_bullet_lines(target[:6]),
            "必须满足:",
            *_bullet_lines(_dedupe(must)[:10]),
            "禁止:",
            *_bullet_lines(_dedupe(must_not)[:10]),
            "验收清单:",
            *_bullet_lines(checks),
        ]
    )


def normalize_revision_mode(mode: str) -> str:
    value = normalize_revision_intent_mode(mode)
    if value:
        return value
    value = (mode or "").strip().lower()
    aliases = {
        "minor": REVISION_MODE_POLISH,
        "light": REVISION_MODE_POLISH,
        "partial": REVISION_MODE_TARGETED,
        "local": REVISION_MODE_TARGETED,
        "patch": REVISION_MODE_LOCAL_PATCH,
        "local_patch": REVISION_MODE_LOCAL_PATCH,
        "minimal": REVISION_MODE_LOCAL_PATCH,
        "minimal_patch": REVISION_MODE_LOCAL_PATCH,
        "target": REVISION_MODE_TARGETED,
        "targeted_revision": REVISION_MODE_TARGETED,
        "rebuild": REVISION_MODE_REWRITE,
        "structural": REVISION_MODE_REWRITE,
        "restart": REVISION_MODE_FRESH,
        "fresh_rewrite": REVISION_MODE_FRESH,
        "latest_skeleton": REVISION_MODE_FRESH,
    }
    value = aliases.get(value, value)
    if value not in REVISION_MODES:
        return REVISION_MODE_TARGETED
    return value


def _revision_mode_prefix(mode: str) -> str:
    return f"修订模式:{normalize_revision_mode(mode)}\n"


def _extract_revision_mode(text: str) -> tuple[str, str]:
    match = re.match(r"\s*修订模式\s*[:：]\s*([a-zA-Z_]+)\s*\n?", text)
    if not match:
        return REVISION_MODE_TARGETED, text
    mode = normalize_revision_mode(match.group(1))
    return mode, text[match.end() :].strip()


def _supersede_previous_adjustments(session: Session, *, book_id: int, chapter_number: int) -> None:
    previous = session.scalars(
        select(FeedbackAdjustment).where(
            FeedbackAdjustment.book_id == book_id,
            FeedbackAdjustment.target_chapter_number == chapter_number,
            FeedbackAdjustment.status.in_(["ready", "applied"]),
        )
    )
    for adjustment in previous:
        adjustment.status = "superseded"


def _strip_feedback_constraints(value: str) -> str:
    if "反馈调整#" not in value and "修订方向#" not in value:
        return value
    cleaned = re.sub(r"(?:^|[，,；;]\s*)(?:反馈调整|修订方向)#\d+:[\s\S]*", "", value).strip(" ，,；;")
    return cleaned


def _strip_internal_revision_markers(value: str) -> str:
    markers = (
        "执行修订合同，逐条兑现主编验收标准",
        "按本次修订要求验收，不扩大修改范围",
    )
    result = value or ""
    for marker in markers:
        result = result.replace(marker, "")
    return result.strip(" ，,；;")


def _strip_revision_beats(value: str) -> str:
    blocked_prefixes = (
        "修订模式:",
        "本章按最新",
        "执行修订合同",
        "按本次修订要求验收",
        "结构重写时",
    )
    keep = []
    for part in (value or "").replace("\n", "；").split("；"):
        item = part.strip(" ，,；;")
        if not item:
            continue
        if item.startswith(blocked_prefixes):
            continue
        keep.append(item)
    return "；".join(keep)


def _compact_text(value: str, *, limit: int = 900) -> str:
    compact = "\n".join(line.strip() for line in (value or "").splitlines() if line.strip())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"


def _looks_like_requirement(line: str) -> bool:
    markers = ("必须", "需要", "要", "前", "结尾", "开头", "主角", "能力", "钩子", "冲突", "代价", "压力")
    return any(marker in line for marker in markers)


def _looks_like_forbidden(line: str) -> bool:
    markers = ("不要", "不能", "禁止", "不得", "删除", "避免", "别")
    return any(marker in line for marker in markers)


def _bullet_lines(items: list[str]) -> list[str]:
    return [f"- {item}" for item in items if item]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _feedback_items(session: Session, *, book_id: int, feedback_ids: list[int]) -> list[PlatformFeedback]:
    if not feedback_ids:
        raise ValueError("at least one feedback id is required")
    items: list[PlatformFeedback] = []
    for feedback_id in feedback_ids:
        feedback = session.get(PlatformFeedback, feedback_id)
        if not feedback:
            raise ValueError(f"feedback not found: {feedback_id}")
        if feedback.book_id != book_id:
            raise ValueError(f"feedback {feedback_id} does not belong to book {book_id}")
        items.append(feedback)
    return items


def _default_adjustment_text(items: list[PlatformFeedback], target_chapter_number: int) -> str:
    metric_counts = Counter(item.metric_name for item in items)
    metric_part = "，".join(f"{name}x{count}" for name, count in sorted(metric_counts.items()))
    raw_parts = [item.raw_text.strip() for item in items if item.raw_text.strip()]
    value_parts = [f"{item.metric_name}={item.metric_value}" for item in items if item.metric_value.strip()]
    evidence = "；".join(raw_parts[:3] or value_parts[:3])
    if evidence:
        return f"第{target_chapter_number}章需回应近期反馈（{metric_part}）：{evidence}"
    return f"第{target_chapter_number}章需回应近期反馈（{metric_part}）。"


def _auto_route_adjustment_text(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    text: str,
) -> str:
    if _extract_revision_mode(text)[0] != REVISION_MODE_TARGETED or text.lstrip().startswith(("修订模式:", "修订模式：")):
        return text
    if extract_revision_decision(text):
        return text
    decision = decide_revision_intent(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        suggestion_text=text,
    )
    return f"{decision.contract_prefix()}\n{text.strip()}"


def _get_or_create_chapter(session: Session, *, book_id: int, chapter_number: int) -> Chapter:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if chapter:
        return chapter
    chapter = Chapter(book_id=book_id, chapter_number=chapter_number, title=f"第{chapter_number}章", status="briefing")
    session.add(chapter)
    session.flush()
    return chapter


def _append_unique(existing: str, addition: str) -> str:
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}，{addition}"
