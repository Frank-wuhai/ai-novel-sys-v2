from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Character, PowerSystem, StoryBible, StoryFoundation, WorldRule


@dataclass(frozen=True)
class NamingGovernanceReport:
    score: int
    allowed_terms: list[str]
    new_terms: list[str]
    ungrounded_terms: list[str]
    issues: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "allowed_terms": self.allowed_terms,
            "new_terms": self.new_terms,
            "ungrounded_terms": self.ungrounded_terms,
            "issues": self.issues,
            "recommendations": self.recommendations,
        }


TERM_SUFFIXES = (
    "剑派",
    "镖局",
    "药王谷",
    "药篓道",
    "盐道",
    "血印",
    "铜铃",
    "令牌",
    "腰牌",
    "法器",
    "灵符",
    "秘卷",
    "石门",
    "古井",
    "山庄",
    "堂口",
    "寨",
    "谷",
    "城",
    "镇",
    "村",
)
GENERIC_PREFIXES = ("这个", "那个", "这本", "那本", "一座", "一处", "一道", "一块", "两处", "三家", "不是", "所谓")
CONTEXT_TRIM_MARKERS = (
    "告诉他",
    "给我一个能让",
    "连滚带爬退回",
    "带进",
    "害得我",
    "第二声",
    "不是",
    "若是",
    "只有",
    "还有",
    "知道",
    "看见",
    "听见",
    "递给",
    "交给",
    "拿出",
    "走进",
    "来自",
    "属于",
    "牵连",
    "提到",
    "问起",
    "说起",
    "封过",
    "封",
    "撑不到",
    "负伤入",
    "三家",
    "一只",
    "一条",
    "最靠",
    "留你",
    "不出",
    "能让",
    "传来",
    "而是",
    "借",
    "让",
    "有",
    "过",
    "到",
    "看",
    "守",
    "入",
    "从",
    "把",
    "替",
    "和",
    "在",
)
GROUNDING_MARKERS = (
    "来自",
    "源自",
    "归",
    "属于",
    "来源",
    "用途",
    "规矩",
    "代价",
    "欠",
    "仇",
    "血",
    "裂",
    "旧",
    "铜",
    "铁",
    "药",
    "账",
    "印",
    "门",
    "帮",
    "派",
    "谁在乎",
    "为何",
    "因为",
    "用来",
    "换",
    "救",
    "害",
    "追",
    "封",
    "掌管",
    "抵押",
    "凭据",
    "证据",
)


def build_naming_governance_block(session: Session, *, book_id: int, chapter_number: int | None = None) -> str:
    terms = allowed_naming_terms(session, book_id=book_id)
    term_text = "、".join(terms[:30]) or "暂无已登记专名"
    chapter_line = f"第{chapter_number}章" if chapter_number else "本章"
    return "\n".join(
        [
            "命名治理（必须执行，不要原样输出标题）：",
            f"- 已登记专名白名单：{term_text}",
            f"- {chapter_line}优先使用白名单中的人名、地名、组织名、物品名和力量名。",
            "- 不要临时生造人名、地名、物品名、门派名、令牌名、法器名或玄学名词来填坑。",
            "- 如剧情必须新增名称，最多新增1-2个，并立刻给出来源、功能、利益关系或可见证据；普通路人和普通物件优先用身份/用途称呼。",
            "- 新名称必须读起来像本书世界自然长出来的词，避免堆叠青、玄、幽、魄、灵、天、古、神等泛玄幻字眼。",
        ]
    )


def allowed_naming_terms(session: Session, *, book_id: int) -> list[str]:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chunks: list[str] = [book.title or ""]
    foundation = session.scalar(
        select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc())
    )
    if foundation:
        chunks.extend(
            [
                foundation.premise or "",
                foundation.reader_promise or "",
                foundation.world_engine or "",
                foundation.protagonist_engine or "",
                foundation.conflict_engine or "",
            ]
        )
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    if bible:
        chunks.extend(
            [
                bible.positioning or "",
                bible.main_plot or "",
                bible.protagonist_arc or "",
                bible.relationship_arc or "",
                bible.power_curve or "",
                bible.forbidden_rules or "",
                bible.style_guide or "",
            ]
        )
    for character in session.scalars(select(Character).where(Character.book_id == book_id).order_by(Character.id)):
        chunks.append(character.name or "")
    for rule in session.scalars(select(WorldRule).where(WorldRule.book_id == book_id).order_by(WorldRule.id)):
        chunks.extend([rule.category or "", rule.rule_text or ""])
    for power in session.scalars(select(PowerSystem).where(PowerSystem.book_id == book_id).order_by(PowerSystem.id)):
        chunks.extend([power.name or "", power.rules or "", power.costs or "", power.limits or ""])
    return _dedupe(_extract_candidate_terms("\n".join(chunks)))


def evaluate_naming_governance(text: str, *, allowed_terms: list[str] | None = None, canon_context: str = "") -> NamingGovernanceReport:
    allowed = _dedupe([*(allowed_terms or []), *_extract_candidate_terms(canon_context or "")])
    terms = _extract_candidate_terms(text or "")
    new_terms = [term for term in terms if term not in allowed]
    ungrounded = [term for term in new_terms if not _term_grounded(text or "", term)]
    score = 88
    score -= max(0, len(new_terms) - 2) * 8
    score -= len(ungrounded) * 10
    if _has_fantasy_stack(new_terms):
        score -= 10
    score = max(0, min(100, score))
    issues: list[str] = []
    if len(new_terms) > 2:
        issues.append("too_many_new_names:" + ",".join(new_terms[:8]))
    if ungrounded:
        issues.append("ungrounded_new_names:" + ",".join(ungrounded[:8]))
    if _has_fantasy_stack(new_terms):
        issues.append("fantasy_syllable_stack")
    recommendations = []
    if new_terms:
        recommendations.append("删掉非必要新专名；必须新增时，在同段写清来源、功能、谁在乎它、会改变什么局面。")
    if ungrounded:
        recommendations.append("这些名称缺少锚点：" + "、".join(ungrounded[:6]))
    return NamingGovernanceReport(
        score=score,
        allowed_terms=allowed[:40],
        new_terms=new_terms[:16],
        ungrounded_terms=ungrounded[:12],
        issues=issues,
        recommendations=recommendations,
    )


def _extract_candidate_terms(text: str) -> list[str]:
    source = text or ""
    terms: list[str] = []
    for suffix in TERM_SUFFIXES:
        pattern = rf"[\u4e00-\u9fff]{{1,8}}{re.escape(suffix)}"
        for match in re.finditer(pattern, source):
            term = _normalize_candidate(match.group(0), suffix=suffix)
            if term and term not in terms and not _context_fragment(match.group(0), term):
                terms.append(term)
    return terms


def _normalize_candidate(candidate: str, *, suffix: str) -> str:
    value = candidate
    for marker in CONTEXT_TRIM_MARKERS:
        if marker in value:
            value = value.split(marker)[-1]
    value = value.strip(" ，。；：、“”‘’（）()")
    if not value.endswith(suffix):
        return ""
    prefix = value[: -len(suffix)]
    if _ordinary_location_or_object(prefix, suffix=suffix):
        return ""
    if not prefix or len(prefix) > 6:
        prefix = prefix[-6:]
    term = prefix + suffix
    if len(term) < 3 or any(term.startswith(prefix) for prefix in GENERIC_PREFIXES):
        return ""
    if term in {"一座城", "一道桥", "一间铺", "这座城", "那个村", "一块腰牌", "这块腰牌"}:
        return ""
    return term[-10:]


def _context_fragment(candidate: str, term: str) -> bool:
    if candidate == term:
        return False
    prefix = candidate[: -len(term)]
    if not prefix:
        return False
    return any(marker in prefix for marker in ("不是", "能让", "只有", "还有", "传来", "退回", "告诉", "害得", "留你", "带进", "不出"))


def _ordinary_location_or_object(prefix: str, *, suffix: str) -> bool:
    ordinary_prefixes = (
        "后",
        "前",
        "本",
        "小",
        "破",
        "旧",
        "新",
        "这",
        "那",
        "几",
        "一",
        "二",
        "三",
        "外",
        "内",
        "东",
        "西",
        "南",
        "北",
        "街角",
        "门外",
        "反派",
    )
    if prefix.startswith(("反派", "主角", "配角", "作者")):
        return True
    if suffix in {"城", "镇", "村", "谷", "寨"} and prefix.endswith(("新手", "外乡", "真实", "游戏")):
        return True
    return prefix in ordinary_prefixes or prefix.endswith(ordinary_prefixes)


def _term_grounded(text: str, term: str) -> bool:
    index = (text or "").find(term)
    if index < 0:
        return False
    window = text[max(0, index - 80) : min(len(text), index + len(term) + 100)]
    if any(marker in window for marker in ("没问来历", "不知来历", "没有来历", "来历不明")):
        return False
    return any(marker in window for marker in GROUNDING_MARKERS)


def _has_fantasy_stack(terms: list[str]) -> bool:
    stack_chars = ("玄", "幽", "魄", "灵", "天", "古", "神", "圣", "魔", "炎", "寒", "冥")
    return any(sum(1 for ch in term if ch in stack_chars) >= 3 for term in terms)


def _dedupe(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
