from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models.entities import Character, CharacterState, Chapter, ChapterVersion, Foreshadow, PlotThread


@dataclass(frozen=True)
class ContinuityResult:
    chapter_id: int
    character_state_ids: list[int]
    new_foreshadow_ids: list[int]
    paid_foreshadow_ids: list[int]
    updated_plot_thread_ids: list[int]


def record_chapter_continuity(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    summary: str,
    character_states: list[tuple[int, str]] | None = None,
    new_foreshadows: list[str] | None = None,
    payoffs: list[tuple[int, str]] | None = None,
    plot_thread_updates: list[tuple[int, str]] | None = None,
) -> ContinuityResult:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    # Sprint 2 P2-Ch29: prefer the promoted reviewed_pass/approved version
    # over the id-max version. accept_early_stop can promote an earlier
    # version (v26) while later versions (v27..v31) remain needs_revision;
    # the id-max lookup would then trip the "quality pass or approval" gate
    # even though the chapter has a legitimate promoted version.
    latest = session.scalar(
        select(ChapterVersion)
        .where(
            ChapterVersion.chapter_id == chapter.id,
            ChapterVersion.status.in_(["reviewed_pass", "approved"]),
        )
        .order_by(ChapterVersion.id.desc())
    )
    if not latest:
        latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not latest:
        raise ValueError("chapter version not found")
    if latest.status not in {"reviewed_pass", "approved"}:
        raise ValueError("continuity can only be recorded after chapter quality pass or approval")

    chapter.summary = summary
    chapter.status = "continuity_recorded"

    character_state_ids: list[int] = []
    for character_id, state_text in character_states or []:
        character = session.get(Character, character_id)
        if not character or character.book_id != book_id:
            raise ValueError(f"character does not belong to book: {character_id}")
        state = CharacterState(
            character_id=character_id,
            chapter_id=chapter.id,
            state_text=state_text,
            source="continuity",
        )
        session.add(state)
        session.flush()
        character_state_ids.append(state.id)

    new_foreshadow_ids: list[int] = []
    for setup_text in new_foreshadows or []:
        foreshadow = Foreshadow(book_id=book_id, setup_text=setup_text, status="open")
        session.add(foreshadow)
        session.flush()
        new_foreshadow_ids.append(foreshadow.id)

    paid_foreshadow_ids: list[int] = []
    for foreshadow_id, payoff_text in payoffs or []:
        foreshadow = session.get(Foreshadow, foreshadow_id)
        if not foreshadow or foreshadow.book_id != book_id:
            raise ValueError(f"foreshadow does not belong to book: {foreshadow_id}")
        foreshadow.payoff_text = payoff_text
        foreshadow.status = "paid_off"
        paid_foreshadow_ids.append(foreshadow.id)

    updated_plot_thread_ids: list[int] = []
    for thread_id, status in plot_thread_updates or []:
        thread = session.get(PlotThread, thread_id)
        if not thread or thread.book_id != book_id:
            raise ValueError(f"plot thread does not belong to book: {thread_id}")
        thread.status = status
        updated_plot_thread_ids.append(thread.id)

    _ensure_chapter_exit_state(session, chapter=chapter, version=latest, summary=summary)

    session.flush()
    return ContinuityResult(
        chapter_id=chapter.id,
        character_state_ids=character_state_ids,
        new_foreshadow_ids=new_foreshadow_ids,
        paid_foreshadow_ids=paid_foreshadow_ids,
        updated_plot_thread_ids=updated_plot_thread_ids,
    )


def _ensure_chapter_exit_state(
    session: Session,
    *,
    chapter: Chapter,
    version: ChapterVersion,
    summary: str,
) -> None:
    _ensure_exit_state_table(session)
    existing = session.execute(
        text(
            "SELECT id FROM chapter_exit_states "
            "WHERE chapter_id=:chapter_id AND chapter_version_id=:version_id LIMIT 1"
        ),
        {"chapter_id": chapter.id, "version_id": version.id},
    ).first()
    if existing:
        return
    state = _derive_exit_state(version.content or "", summary=summary, title=version.title or chapter.title or "")
    session.execute(
        text(
            """INSERT INTO chapter_exit_states
            (chapter_id, chapter_version_id, source_version_number,
             main_character_state, relationship_delta, plot_hook, new_facts,
             physical_location, time_marker, raw_summary, hook_keywords)
            VALUES (:chapter_id, :version_id, :version_number,
                    :main_character_state, :relationship_delta, :plot_hook, :new_facts,
                    :physical_location, :time_marker, :raw_summary, :hook_keywords)"""
        ),
        {
            "chapter_id": chapter.id,
            "version_id": version.id,
            "version_number": version.version_number,
            **state,
        },
    )


def ensure_chapter_exit_state_table(session: Session) -> None:
    _ensure_exit_state_table(session)


def _ensure_exit_state_table(session: Session) -> None:
    session.execute(
        text(
            """CREATE TABLE IF NOT EXISTS chapter_exit_states (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chapter_id INTEGER NOT NULL,
                chapter_version_id INTEGER NOT NULL,
                source_version_number INTEGER NOT NULL,
                main_character_state TEXT NOT NULL,
                relationship_delta TEXT NOT NULL,
                plot_hook TEXT NOT NULL,
                new_facts TEXT NOT NULL,
                physical_location TEXT NOT NULL,
                time_marker TEXT NOT NULL,
                raw_summary TEXT NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                hook_keywords TEXT
            )"""
        )
    )


def _derive_exit_state(content: str, *, summary: str, title: str) -> dict[str, str]:
    compact = _compact(content)
    tail = compact[-420:] if len(compact) > 420 else compact
    raw_summary = _clip(_last_sentences(tail, limit=2) or summary or tail or title, 140)
    plot_hook = _derive_plot_hook(tail, summary=summary, raw_summary=raw_summary)
    keywords = _derive_hook_keywords(tail + " " + plot_hook + " " + summary)
    return {
        "main_character_state": _clip(_derive_main_character_state(tail, raw_summary=raw_summary), 180),
        "relationship_delta": _clip(_derive_relationship_delta(tail), 180),
        "plot_hook": _clip(plot_hook, 180),
        "new_facts": _clip(_derive_new_facts(tail, raw_summary=raw_summary), 220),
        "physical_location": _clip(_derive_location(tail), 40),
        "time_marker": _clip(_derive_time_marker(tail), 40),
        "raw_summary": raw_summary,
        "hook_keywords": json.dumps(keywords, ensure_ascii=False),
    }


def _derive_plot_hook(tail: str, *, summary: str, raw_summary: str) -> str:
    for marker in ("必须", "决定", "得先", "还要", "不能", "要查", "约定", "消息", "禁", "追", "找上门"):
        sentence = _sentence_with(tail, marker)
        if sentence:
            return sentence
    return raw_summary or _clip(summary, 160) or "章末状态已记录，下一章必须承接当前后果。"


def _derive_main_character_state(tail: str, *, raw_summary: str) -> str:
    state_terms = [term for term in ("受伤", "流血", "疼", "冷汗", "疲惫", "精气", "欠款", "禁登", "掌心", "手机", "短信") if term in tail]
    if state_terms:
        return f"主角处于章末后果中：{raw_summary} 关键状态：" + "、".join(state_terms[:6]) + "。"
    return "主角处于上一章章末后果中：" + raw_summary


def _derive_relationship_delta(tail: str) -> str:
    names = _proper_terms(tail)[:4]
    if names:
        return "章末涉及关系/压力对象：" + "、".join(names) + "；下章需承接其态度或后续动作。"
    return "本章关系变化已进入章末后果，下章需承接人物态度和压力。"


def _derive_new_facts(tail: str, *, raw_summary: str) -> str:
    fact_sentences = []
    for marker in ("检测到", "当前", "发现", "原来", "不是", "会", "禁止", "任务", "奖励", "代价", "同步"):
        sentence = _sentence_with(tail, marker)
        if sentence and sentence not in fact_sentences:
            fact_sentences.append(sentence)
        if len(fact_sentences) >= 2:
            break
    return "；".join(fact_sentences) if fact_sentences else raw_summary


def _derive_location(tail: str) -> str:
    location_patterns = (
        r"在([^，。！？]{1,12}(?:门口|院里|殿前|宿舍|走廊|窗前|床上|桌前|山门|街口|屋里))",
        r"走到([^，。！？]{1,12})",
    )
    for pattern in location_patterns:
        match = re.search(pattern, tail)
        if match:
            return match.group(1)
    for fallback in ("宿舍走廊", "宿舍", "清虚观", "山门", "殿前", "街口"):
        if fallback in tail:
            return fallback
    return "章末现场"


def _derive_time_marker(tail: str) -> str:
    for marker in ("清晨", "深夜", "夜里", "天亮", "三天后", "周五", "晚八点", "次日", "现在"):
        if marker in tail:
            return marker
    return "章末当下"


# 系统摘要词 blocklist · 这些词永远不能作为 hook_keywords
# 它们是审核维度/状态摘要, 不是剧情/正文/结构/节拍里的具体钩子
_HOOK_KEYWORD_BLOCKLIST = frozenset({
    "第2章已通过质检", "第3章已通过质检", "第1章已通过质检",
    "第N章已通过质检", "章末后果", "主角状态", "未解压力",
    "审核", "质检", "通过", "待审", "评审",
    "状态快照", "承接", "下一章", "本章",
    "硬门禁", "硬拦截", "提示", "要求", "必须",
    "系统", "面板", "作者", "修订",
})

def _is_blocked_hook_keyword(item: str) -> bool:
    """判断 item 是否为系统摘要词/审核术语 (blocklist 命中)"""
    if item in _HOOK_KEYWORD_BLOCKLIST:
        return True
    # 含 "已通过" / "审核" / "质检" 任何子串
    for blocked_substr in ("已通过", "审核", "质检", "已发布", "已批准"):
        if blocked_substr in item:
            return True
    return False


def _derive_hook_keywords(text_value: str) -> list[str]:
    """从正文/exit_state/plot_hook 提取 1-6 个具体钩子关键词.
    只允许: 具体名词, 动作, 地点, 异常, 人物关系.
    禁止: 系统摘要词 (章末后果/主角状态/未解压力/第X章已通过质检).
    返回空列表 OK — quality hook_missing 会自动 bypass.
    """
    terms = []
    for term in _proper_terms(text_value):
        if term in terms:
            continue
        if _is_blocked_hook_keyword(term):
            continue
        terms.append(term)
    # 不再 fallback 系统摘要词
    return terms[:6]


def _proper_terms(text_value: str) -> list[str]:
    candidates = re.findall(r"[《“]?([一-鿿A-Za-z0-9]{2,8})[》”]?", text_value or "")
    stop = {"主角", "当前", "已经", "一个", "这才", "不能", "必须", "正文", "时候", "自己", "什么", "一样", "那里", "他们", "没有", "还是"}
    rows = []
    for item in candidates:
        if item in stop or item.isdigit():
            continue
        if len(item) <= 1:
            continue
        if any(ch.isdigit() for ch in item) or item.startswith(("第", "v")):
            rows.append(item)
        elif item in text_value and _looks_like_keyword(item):
            rows.append(item)
    return rows


def _looks_like_keyword(item: str) -> bool:
    return any(marker in item for marker in ("观", "门", "值", "痕", "血", "哥", "任务", "系统", "短信", "掌", "剑", "内力", "精气", "地点", "身份", "名单", "约"))


def _sentence_with(text_value: str, marker: str) -> str:
    for sentence in re.split(r"(?<=[。！？!?])", text_value or ""):
        stripped = sentence.strip()
        if marker in stripped:
            return _clip(stripped, 160)
    return ""


def _last_sentences(text_value: str, *, limit: int) -> str:
    sentences = [item.strip() for item in re.split(r"(?<=[。！？!?])", text_value or "") if item.strip()]
    return "".join(sentences[-limit:])


def _compact(text_value: str) -> str:
    return " ".join(str(text_value or "").split())


def _clip(text_value: str, limit: int) -> str:
    value = _compact(text_value)
    return value[:limit]


def latest_version_for_chapter(session: Session, *, book_id: int, chapter_number: int) -> ChapterVersion:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(
        select(ChapterVersion)
        .where(
            ChapterVersion.chapter_id == chapter.id,
            ChapterVersion.status.in_(["reviewed_pass", "approved"]),
        )
        .order_by(ChapterVersion.id.desc())
    )
    if not version:
        version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    return version


def default_chapter_continuity_summary(session: Session, *, book_id: int, chapter_number: int) -> str:
    version = latest_version_for_chapter(session, book_id=book_id, chapter_number=chapter_number)
    compact = " ".join(version.content.split())
    ending = compact[-260:] if len(compact) > 260 else compact
    return f"第{chapter_number}章已通过质检，最新版本《{version.title}》进入连续性记录。章末后果/下一章承接：{ending}"
