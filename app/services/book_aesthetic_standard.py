from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterVersion, QualityReport, StoryBible
from app.services.aesthetic_profile import profile_from_story_text


@dataclass(frozen=True)
class BookAestheticStandard:
    status: str
    narrative_flavor: list[str]
    prose_rhythm: list[str]
    scene_density: list[str]
    character_voice: list[str]
    forbidden_tone: list[str]
    benchmark_fragments: list[str]
    taste_memory: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "narrative_flavor": self.narrative_flavor,
            "prose_rhythm": self.prose_rhythm,
            "scene_density": self.scene_density,
            "character_voice": self.character_voice,
            "forbidden_tone": self.forbidden_tone,
            "benchmark_fragments": self.benchmark_fragments,
            "taste_memory": self.taste_memory,
        }

    def prompt_block(self) -> str:
        lines = ["【作品级审美标尺】"]
        lines.extend(f"- 叙事气质: {item}" for item in self.narrative_flavor[:5])
        lines.extend(f"- 句段节奏: {item}" for item in self.prose_rhythm[:4])
        lines.extend(f"- 场景密度: {item}" for item in self.scene_density[:5])
        lines.extend(f"- 人物声线: {item}" for item in self.character_voice[:4])
        lines.extend(f"- 禁止笔触: {item}" for item in self.forbidden_tone[:6])
        if self.taste_memory:
            lines.append("【好稿记忆】")
            lines.extend(f"- {item}" for item in self.taste_memory[:6])
        if self.benchmark_fragments:
            lines.append("【本书标杆片段】")
            lines.extend(f"- {item}" for item in self.benchmark_fragments[:3])
        lines.append("执行: 正文必须优先对齐本书标尺，不得为了通过质检压成冷硬、概括、装深沉的短句。")
        lines.append("【作品级审美标尺结束】")
        return "\n".join(lines)


def build_book_aesthetic_standard(session: Session, *, book_id: int) -> BookAestheticStandard:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    profile = profile_from_story_text(
        style_guide=bible.style_guide if bible else "",
        forbidden_rules=bible.forbidden_rules if bible else "",
    )
    context = "\n".join(
        [
            book.title or "",
            book.genre or "",
            bible.positioning if bible else "",
            bible.reader_promise if bible else "",
            bible.style_guide if bible else "",
            bible.forbidden_rules if bible else "",
            profile,
        ]
    )
    passed = _passed_quality_rows(session, book_id=book_id, limit=8)
    fragments = [_benchmark_fragment(version.content or "") for version, _quality in passed]
    return BookAestheticStandard(
        status="ready",
        narrative_flavor=_narrative_flavor(context),
        prose_rhythm=_prose_rhythm(context),
        scene_density=_scene_density(context),
        character_voice=_character_voice(context),
        forbidden_tone=_forbidden_tone(context),
        benchmark_fragments=[item for item in fragments if item][:3],
        taste_memory=_taste_memory(passed),
    )


def _passed_quality_rows(session: Session, *, book_id: int, limit: int) -> list[tuple[ChapterVersion, QualityReport]]:
    rows = (
        session.execute(
            select(ChapterVersion, QualityReport)
            .join(QualityReport, QualityReport.chapter_version_id == ChapterVersion.id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id, QualityReport.passed.is_(True))
            .order_by(QualityReport.score.desc(), ChapterVersion.id.desc())
            .limit(limit)
        )
        .all()
    )
    return [(version, quality) for version, quality in rows]


def _narrative_flavor(context: str) -> list[str]:
    rows = []
    if any(marker in context for marker in ("武侠", "江湖", "门派")):
        rows.append("江湖要有烟火、人情、门槛、门派规矩和可见的场面感。")
    if any(marker in context for marker in ("网游", "游戏", "玩家")):
        rows.append("游戏感只提供入口和误读，正文仍按真实世界的人物因果写。")
    if any(marker in context for marker in ("仙侠", "升维", "修仙")):
        rows.append("世界从武侠向仙侠升维，奇观必须伴随规则变化和代价。")
    if any(marker in context for marker in ("热闹", "烟火", "轻松", "吐槽")):
        rows.append("基调要有热闹和人物趣味，紧张不能长期压成阴冷悬疑。")
    return rows or ["以主角主动选择、具体阻碍、收益代价和章末期待推动读者继续读。"]


def _prose_rhythm(context: str) -> list[str]:
    rows = ["中等句长为主，动作、观察、反应、对白交替推进。"]
    if any(marker in context for marker in ("明快", "轻快", "爽")):
        rows.append("表达要明快，少用冷硬单词式概括。")
    rows.append("重要氛围不能只用一个形容词，要落到空间、物件、声音、气味和人物动作。")
    return rows


def _scene_density(_context: str) -> list[str]:
    return [
        "每 500 字至少出现一次局面变化或信息增量。",
        "每个关键场景必须有目标、阻碍、动作、反应、后果。",
        "对白不能只解释设定，必须承担试探、遮掩、交易、威胁或情绪变化。",
    ]


def _character_voice(_context: str) -> list[str]:
    return [
        "主角说话要体现当下目标和临场判断，不只做旁白结论。",
        "配角先是有利益和顾虑的人，再承担设定功能。",
        "人物反应链必须可见：误判、迟疑、试探、改口、行动。"
    ]


def _forbidden_tone(context: str) -> list[str]:
    rows = ["冷硬装酷式精炼", "只用抽象词替代场景展开", "对白只解释设定", "系统面板直接解题"]
    if any(marker in context for marker in ("不要", "禁止", "不得", "避免")):
        rows.append("作者明确否定过的惯性写法不得作为默认发动机。")
    return rows


def _taste_memory(rows: list[tuple[ChapterVersion, QualityReport]]) -> list[str]:
    memory: list[str] = []
    for version, quality in rows[:6]:
        data = _loads_json(quality.report)
        chief = data.get("editor_in_chief") if isinstance(data.get("editor_in_chief"), dict) else {}
        level = chief.get("draft_level") or ("通过稿" if quality.passed else "未通过稿")
        memory.append(f"v{version.id} {level} score={quality.score}: {version.title or '未命名章节'}")
    return memory


def _benchmark_fragment(text: str) -> str:
    paragraphs = [re.sub(r"\s+", " ", item).strip() for item in str(text or "").splitlines() if len(item.strip()) >= 80]
    if not paragraphs:
        return ""
    return max(paragraphs[:12], key=len)[:220]


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
