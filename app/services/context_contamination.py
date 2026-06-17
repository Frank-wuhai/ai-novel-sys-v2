from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Character, PowerSystem, StoryBible, StoryFoundation, WorldRule


@dataclass(frozen=True)
class ContextContaminationReport:
    passed: bool
    authority_terms: dict[str, list[str]]
    stale_terms: list[str]
    blockers: list[str]
    warnings: list[str]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "authority_terms": self.authority_terms,
            "stale_terms": self.stale_terms,
            "blockers": self.blockers,
            "warnings": self.warnings,
        }


def audit_context_contamination(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief_text: str = "",
    canon_context: str = "",
    semantic_memory_context: str = "",
    previous_content: str = "",
    fresh_rewrite: bool = False,
) -> ContextContaminationReport:
    authority = _authority_text(session, book_id=book_id)
    terms = _authority_terms(authority)
    stale = _stale_terms(session, book_id=book_id, authority=authority, terms=terms)
    blockers: list[str] = []
    warnings: list[str] = []
    sources = {
        "canon": canon_context,
        "brief": brief_text,
        "semantic_memory": semantic_memory_context,
    }
    if not fresh_rewrite:
        sources["previous_content"] = previous_content
    for source_name, text in sources.items():
        hits = [term for term in stale if term and term in (text or "")]
        if hits:
            blockers.append(f"{source_name} 含旧设定锚点: " + "、".join(hits[:8]))
    for source_name, text in {"brief": brief_text, "canon": canon_context}.items():
        missing = _missing_required_terms(text or "", terms, include_ability=source_name == "canon")
        if missing:
            blockers.append(f"{source_name} 未承接当前骨架锚点: " + "、".join(missing[:8]))
    brief_missing_ability = _missing_required_terms(brief_text or "", terms, include_core=False, include_ability=True)
    if brief_missing_ability:
        warnings.append("brief 未显式写入能力锚点: " + "、".join(brief_missing_ability[:4]))
    if not terms.get("protagonists"):
        warnings.append("未能从 StoryFoundation/StoryBible 识别主角名，建议补清骨架主角锚点。")
    if not terms.get("world_titles"):
        warnings.append("未能从 StoryFoundation/StoryBible 识别书中作品/世界名，建议补清骨架世界锚点。")
    return ContextContaminationReport(
        passed=not blockers,
        authority_terms=terms,
        stale_terms=stale,
        blockers=blockers,
        warnings=warnings,
    )


def assert_context_not_contaminated(report: ContextContaminationReport) -> None:
    if report.passed:
        return
    raise ValueError("生产上下文污染未通过，不能继续生成：" + "；".join(report.blockers))


def context_anchor_lines(session: Session, *, book_id: int) -> list[str]:
    terms = _authority_terms(_authority_text(session, book_id=book_id))
    rows: list[str] = []
    if terms.get("protagonists"):
        rows.append("当前主角锚点:" + "、".join(terms["protagonists"]))
    if terms.get("world_titles"):
        rows.append("当前世界/作品锚点:" + "、".join(f"《{item}》" for item in terms["world_titles"]))
    if terms.get("ability_terms"):
        rows.append("当前能力/卖点锚点:" + "、".join(terms["ability_terms"]))
    return rows


def _authority_text(session: Session, *, book_id: int) -> str:
    book = session.get(Book, book_id)
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    chunks = [book.title if book else ""]
    if foundation:
        chunks.extend(
            [
                foundation.premise,
                foundation.reader_promise,
                foundation.world_engine,
                foundation.protagonist_engine,
                foundation.conflict_engine,
            ]
        )
    if bible:
        chunks.extend(
            [
                bible.positioning,
                bible.reader_promise,
                bible.main_plot,
                bible.protagonist_arc,
                bible.power_curve,
                bible.forbidden_rules,
                bible.style_guide,
            ]
        )
    return "\n".join(str(item or "") for item in chunks)


def _authority_terms(text: str) -> dict[str, list[str]]:
    protagonists = _extract_protagonists(text)
    world_titles = _extract_world_titles(text)
    ability_terms = _extract_ability_terms(text)
    return {
        "protagonists": protagonists[:3],
        "world_titles": world_titles[:4],
        "ability_terms": ability_terms[:5],
    }


def _extract_world_titles(text: str) -> list[str]:
    rows: list[str] = []
    for match in re.finditer(r"《([^》]{2,24})》", text or ""):
        title = match.group(1).strip()
        prefix = (text or "")[max(0, match.start() - 12) : match.start()]
        if any(marker in prefix for marker in ("借鉴", "参考", "类似", "致敬", "像", "读过")):
            continue
        rows.append(title)
    return _dedupe(rows)


def _extract_ability_terms(text: str) -> list[str]:
    rows = [
        *re.findall(r"[“\"‘']([^“”\"‘’']{2,16})(?:能力|系统|金手指)?[”\"’']", text or ""),
        *re.findall(r"激活(?:了|的)?([\u4e00-\u9fff]{2,12}?)(?:能力|系统|金手指|，|。|；)", text or ""),
    ]
    blocked_exact = {"核心能力", "核心卖点", "能力", "系统", "金手指"}
    cleaned: list[str] = []
    for term in rows:
        item = str(term or "").strip(" -—，。；：:、")
        if not item or item in blocked_exact:
            continue
        if item.startswith(("于", "在", "第一次", "首次")):
            continue
        if any(marker in item for marker in ("场景", "资格", "世界", "小说", "章节", "后遗症", "现代", "入侵")):
            continue
        if item.endswith(("资格", "世界", "小说")):
            continue
        cleaned.append(item)
    return _dedupe(cleaned)


def _extract_protagonists(text: str) -> list[str]:
    candidates: list[str] = []
    patterns = (
        r"(?:主角|大学生|少年|青年|少女|底层少年)([\u4e00-\u9fff]{2,3})(?:获得|激活|进入|发现|在|必须)",
        r"([\u4e00-\u9fff]{2,3})(?:获得|激活)(?:全真|虚拟|武侠|网游|能力|金手指)",
        r"(?:看|围绕)([\u4e00-\u9fff]{2,3})(?:在|用|靠|进入)",
    )
    for pattern in patterns:
        candidates.extend(re.findall(pattern, text or ""))
    blocked = {"主角", "大学", "学生", "少年", "青年", "少女", "读者", "玩家", "现实", "游戏"}
    normalized: list[str] = []
    for item in candidates:
        value = str(item or "").strip()
        if value.startswith("生") and len(value) == 3:
            value = value[1:]
        if value and value not in blocked:
            normalized.append(value)
    return _dedupe(normalized)


def _stale_terms(session: Session, *, book_id: int, authority: str, terms: dict[str, list[str]]) -> list[str]:
    stale: list[str] = []
    current_names = set(terms.get("protagonists") or [])
    generic_names = {"主角", "男主", "女主", "配角", "反派", "旁白", "作者", "玩家", "NPC"}
    for character in session.scalars(select(Character).where(Character.book_id == book_id).order_by(Character.id)):
        name = character.name or ""
        if name in generic_names:
            continue
        if name and name not in current_names and name not in authority and character.role in {"主角", "protagonist", "男主", "女主"}:
            stale.append(name)
    current_abilities = set(terms.get("ability_terms") or [])
    generic_abilities = {"核心能力", "核心卖点", "金手指", "能力体系", "主能力", "主角能力"}
    for power in session.scalars(select(PowerSystem).where(PowerSystem.book_id == book_id).order_by(PowerSystem.id)):
        name = power.name or ""
        if name in generic_abilities:
            continue
        if name and current_abilities and name not in current_abilities and name not in authority:
            stale.append(name)
    current_titles = set(terms.get("world_titles") or [])
    for rule in session.scalars(select(WorldRule).where(WorldRule.book_id == book_id).order_by(WorldRule.id)):
        for title in re.findall(r"《([^》]{2,24})》", rule.rule_text or ""):
            if current_titles and title not in current_titles and title not in authority:
                stale.append(title)
    return _dedupe(stale)


def _missing_required_terms(
    text: str,
    terms: dict[str, list[str]],
    *,
    include_core: bool = True,
    include_ability: bool = False,
) -> list[str]:
    required: list[str] = []
    keys = []
    if include_core:
        keys.extend(["protagonists", "world_titles"])
    if include_ability:
        keys.append("ability_terms")
    for key in keys:
        values = terms.get(key) or []
        if values and not any(value in text for value in values):
            required.append(values[0])
    return required


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        rows.append(item)
    return rows
