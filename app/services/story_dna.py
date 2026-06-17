from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, StoryBible, StoryFoundation
from app.services.aesthetic_profile import profile_from_story_text, strip_aesthetic_profile_blocks


DNA_START = "【作品DNA】"
DNA_END = "【作品DNA结束】"
_DNA_LABEL_RE = re.compile(r"作品\s*DNA|作品\s*ＤＮＡ", re.IGNORECASE)
_DNA_END_RE = re.compile(r"作品\s*DNA\s*结束|作品\s*ＤＮＡ\s*结束", re.IGNORECASE)
_OTHER_BLOCK_RE = re.compile(r"^【(?!作品\s*DNA|作品\s*ＤＮＡ)(.+?)】$")
_OTHER_HEADING_RE = re.compile(r"^#{1,6}\s+(?!作品\s*DNA|作品\s*ＤＮＡ).+")
_OTHER_FORM_LABELS = (
    "一句话核心设定",
    "读者承诺",
    "审美画像",
    "题材主味",
    "主角动力",
    "成长弧",
    "世界规则",
    "能力曲线",
    "长期冲突",
    "文风指南",
    "禁忌规则",
    "第一卷摘要",
)


def build_story_dna_block(
    *,
    genre_flavor: str = "",
    prose_style: str = "",
    atmosphere: str = "",
    story_route: str = "",
    core_hook: str = "",
    goldfinger: str = "",
    world_rule: str = "",
    conflict: str = "",
    must_have: str = "",
    must_not: str = "",
    chapter_engines: list[str] | None = None,
) -> str:
    engines = _clean_engines(chapter_engines) or _derive_chapter_engines(
        "\n".join([genre_flavor, core_hook, goldfinger, world_rule, conflict, story_route])
    )
    return "\n".join(
        [
            DNA_START,
            f"- 题材主味: {_line(genre_flavor) or '明确题材主味，不能被通用悬疑、冷硬写实或后台规则吞掉。'}",
            f"- 笔触: {_line(prose_style) or '明快、有画面、有角色声线；紧张可以局部使用，但不能长期冷硬克制。'}",
            f"- 氛围: {_line(atmosphere) or '有烟火气、人物趣味和场面变化；压抑感只服务具体危机。'}",
            f"- 故事路线: {_line(story_route) or '以主角主动破局、奇遇/收益兑现、关系变化和章末期待推进。'}",
            f"- 核心钩子: {_line(core_hook) or '每章都要让核心卖点以具体场景出现，而不是停留在设定说明。'}",
            f"- 金手指机制: {_line(goldfinger) or '能力必须有触发条件、成长方式、误判风险、失败后果和补救路径。'}",
            f"- 世界收益规则: {_line(world_rule) or '收益来自行动、选择、规则约束和人物因果；不能白拿。'}",
            f"- 长线压力: {_line(conflict) or '长期冲突必须逐步升级，并改变主角的资源、关系、身份或现实处境。'}",
            f"- 必须保留: {_line(must_have) or '章节要保留本书独有的题材主味、爽点结构和人物关系变化。'}",
            f"- 禁止滑坡: {_line(must_not) or '禁止回到通用模板、机械任务链、单一桥段重复、阴冷悬疑默认化。'}",
            "- 章节发动机库: " + "；".join(engines[:10]),
            "- 执行要求: 生成 brief、导演单、小单元和正文时，先选一个章节发动机，再安排目标、阻碍、动作、代价、收益和章末钩子。",
            DNA_END,
        ]
    )


def build_story_dna_from_development(draft: dict) -> str:
    candidates = draft.get("creative_candidates") if isinstance(draft.get("creative_candidates"), list) else []
    chosen = str(draft.get("chosen_creative_engine") or "")
    if not chosen and candidates:
        chosen = str(candidates[0].get("name") or "") if isinstance(candidates[0], dict) else ""
    goldfinger_rows = []
    for item in candidates[:3]:
        if not isinstance(item, dict):
            continue
        row = " / ".join(
            part
            for part in [
                item.get("name"),
                item.get("goldfinger_form"),
                item.get("mechanism_principle"),
                item.get("cost_logic"),
                item.get("failure_trigger"),
            ]
            if part
        )
        if row:
            goldfinger_rows.append(row)
    return build_story_dna_block(
        genre_flavor=str(draft.get("genre") or ""),
        prose_style=str(draft.get("prose_style") or ""),
        atmosphere=str(draft.get("atmosphere") or ""),
        story_route=str(draft.get("story_route") or ""),
        core_hook=str(draft.get("premise") or ""),
        goldfinger=chosen or "；".join(goldfinger_rows),
        world_rule=str(draft.get("world_engine") or ""),
        conflict=str(draft.get("conflict_engine") or ""),
        must_have=str(draft.get("reader_promise") or ""),
        must_not=str(draft.get("style_must_not") or ""),
        chapter_engines=_derive_chapter_engines(
            "\n".join(
                [
                    str(draft.get("premise") or ""),
                    str(draft.get("world_engine") or ""),
                    str(draft.get("conflict_engine") or ""),
                    str(draft.get("volume_summary") or ""),
                    str(draft.get("arc_goal") or ""),
                    str(draft.get("arc_climax") or ""),
                    str(draft.get("arc_turn") or ""),
                ]
            )
        ),
    )


def build_story_dna_from_skeleton(skeleton: dict, *, genre: str = "") -> str:
    style_guide = strip_aesthetic_profile_blocks(str(skeleton.get("style_guide") or ""))
    aesthetic = profile_from_story_text(style_guide=str(skeleton.get("style_guide") or ""), forbidden_rules=str(skeleton.get("forbidden_rules") or ""))
    return build_story_dna_block(
        genre_flavor=genre,
        prose_style=style_guide or _profile_field(aesthetic, "笔触"),
        atmosphere="",
        story_route=str(skeleton.get("reader_promise") or ""),
        core_hook=str(skeleton.get("premise") or ""),
        goldfinger=str(skeleton.get("protagonist_engine") or ""),
        world_rule=str(skeleton.get("world_engine") or ""),
        conflict=str(skeleton.get("conflict_engine") or ""),
        must_have=str(skeleton.get("reader_promise") or ""),
        must_not=str(skeleton.get("forbidden_rules") or ""),
        chapter_engines=_derive_chapter_engines("\n".join(str(value or "") for value in skeleton.values())),
    )


def extract_story_dna_block(*, style_guide: str = "", forbidden_rules: str = "") -> str:
    blocks = _extract_story_dna_blocks("\n".join([style_guide or "", forbidden_rules or ""]))
    return blocks[0] if blocks else ""


def strip_story_dna_blocks(text: str) -> str:
    return _strip_story_dna_blocks(text)


def merge_style_with_story_dna(style_guide: str, dna_block: str) -> str:
    clean = strip_story_dna_blocks(style_guide)
    dna = str(dna_block or "").strip()
    return "\n\n".join(item for item in [clean, dna] if item)


def story_dna_display_fields(*, style_guide: str = "", forbidden_rules: str = "") -> dict[str, str]:
    story_dna = extract_story_dna_block(style_guide=style_guide, forbidden_rules=forbidden_rules)
    return {
        "style_guide": strip_story_dna_blocks(style_guide),
        "forbidden_rules": strip_story_dna_blocks(forbidden_rules),
        "story_dna": story_dna,
    }


def story_dna_for_book(session: Session, *, book_id: int) -> str:
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id).order_by(StoryBible.id.desc()))
    if bible:
        existing = extract_story_dna_block(style_guide=bible.style_guide or "", forbidden_rules=bible.forbidden_rules or "")
        if existing:
            return existing
    book = session.get(Book, book_id)
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    if not foundation and not bible:
        return ""
    aesthetic = profile_from_story_text(style_guide=bible.style_guide if bible else "", forbidden_rules=bible.forbidden_rules if bible else "")
    return build_story_dna_block(
        genre_flavor=book.genre if book else "",
        prose_style=_profile_field(aesthetic, "笔触") or strip_aesthetic_profile_blocks(bible.style_guide if bible else ""),
        story_route=foundation.reader_promise if foundation else (bible.reader_promise if bible else ""),
        core_hook=foundation.premise if foundation else (bible.positioning if bible else ""),
        goldfinger=foundation.protagonist_engine if foundation else (bible.protagonist_arc if bible else ""),
        world_rule=foundation.world_engine if foundation else (bible.power_curve if bible else ""),
        conflict=foundation.conflict_engine if foundation else (bible.main_plot if bible else ""),
        must_not=bible.forbidden_rules if bible else "",
    )


def chapter_engine_for_number(story_dna: str, chapter_number: int) -> str:
    engines = parse_chapter_engines(story_dna)
    if not engines:
        engines = _fallback_engines()
    index = max(0, int(chapter_number or 1) - 1) % len(engines)
    return engines[index]


def parse_chapter_engines(story_dna: str) -> list[str]:
    for line in str(story_dna or "").splitlines():
        if "章节发动机库" not in line:
            continue
        value = line.split(":", 1)[-1].split("：", 1)[-1]
        return _clean_engines(re.split(r"[；;、,，]", value))
    return []


def _derive_chapter_engines(text: str) -> list[str]:
    source = str(text or "")
    engines: list[str] = []
    candidates = [
        ("求医疗伤", ("求医", "伤", "经络", "医疗", "神经")),
        ("门派规矩试炼", ("门派", "规矩", "师门", "试炼")),
        ("护送与失物", ("护送", "失物", "镖", "送")),
        ("身份误认", ("身份", "误认", "暴露", "伪装")),
        ("资源交易", ("资源", "交易", "人情", "交换")),
        ("擂台/切磋", ("擂台", "切磋", "比武", "招式")),
        ("玩家竞争", ("玩家", "账号", "内测", "录屏")),
        ("现实异常外泄", ("现实", "同步", "登录舱", "监测", "舆论", "科技")),
        ("桥段复刻变形", ("桥段", "演绎", "复刻", "经典")),
        ("因果债追偿", ("因果", "债", "好感", "关系")),
    ]
    for label, markers in candidates:
        if any(marker in source for marker in markers):
            engines.append(label)
    for item in _fallback_engines():
        if item not in engines:
            engines.append(item)
    return engines[:10]


def _fallback_engines() -> list[str]:
    return ["具体处境破局", "人物关系交锋", "资源交换", "外部追查", "身份暴露", "规则惩罚", "收益兑现", "章末新钩子"]


def _clean_engines(values) -> list[str]:
    result = []
    for value in values or []:
        item = _line(str(value or ""), limit=28)
        if item and item not in result:
            result.append(item)
    return result


def _line(value: str, limit: int = 180) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    return text[:limit].strip()


def _profile_field(profile: str, label: str) -> str:
    prefix = f"- {label}:"
    for raw in str(profile or "").splitlines():
        line = raw.strip()
        if line.startswith(prefix):
            return line.split(":", 1)[-1].split("：", 1)[-1].strip()
    return ""


def _extract_story_dna_blocks(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    blocks: list[str] = []
    current: list[str] = []
    in_block = False

    for raw in lines:
        line = raw.strip()
        if not in_block and not _is_story_dna_start(line):
            continue
        if not in_block:
            in_block = True
            current = []
            tail = _story_dna_start_tail(line)
            if tail:
                current.append(tail)
            if _is_story_dna_end(line):
                blocks.append(_normalize_story_dna_block(current))
                in_block = False
                current = []
            continue
        if _is_story_dna_end(line):
            blocks.append(_normalize_story_dna_block(current))
            in_block = False
            current = []
            continue
        if _is_non_dna_section_start(line):
            blocks.append(_normalize_story_dna_block(current))
            in_block = False
            current = []
            continue
        current.append(raw.rstrip())

    if in_block:
        blocks.append(_normalize_story_dna_block(current))
    return [block for block in blocks if _story_dna_body(block)]


def _strip_story_dna_blocks(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    in_block = False
    for raw in lines:
        line = raw.strip()
        if not in_block and _is_story_dna_start(line):
            in_block = True
            if _is_story_dna_end(line):
                in_block = False
            continue
        if in_block:
            if _is_story_dna_end(line):
                in_block = False
                continue
            if _is_non_dna_section_start(line):
                in_block = False
                kept.append(raw)
            continue
        kept.append(raw)
    return "\n\n".join(part.strip() for part in "\n".join(kept).split("\n\n") if part.strip())


def _is_story_dna_start(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    if not compact:
        return False
    if compact in {"【作品DNA】", "【作品ＤＮＡ】"}:
        return True
    if compact.startswith(("##作品DNA", "###作品DNA", "#作品DNA", "作品DNA:", "作品DNA：", "作品DNA/")):
        return True
    if compact.startswith(("##作品ＤＮＡ", "###作品ＤＮＡ", "#作品ＤＮＡ", "作品ＤＮＡ:", "作品ＤＮＡ：", "作品ＤＮＡ/")):
        return True
    return bool(_DNA_LABEL_RE.match(line)) and (":" in line or "：" in line)


def _is_story_dna_end(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    return compact in {"【作品DNA结束】", "【作品ＤＮＡ结束】", "作品DNA结束", "作品ＤＮＡ结束"} or bool(_DNA_END_RE.search(line))


def _story_dna_start_tail(line: str) -> str:
    value = re.sub(r"^#{1,6}\s*", "", line or "").strip()
    value = re.sub(r"^【\s*作品\s*(?:DNA|ＤＮＡ)\s*】", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^作品\s*(?:DNA|ＤＮＡ)\s*(?:/[^:：]*)?[:：]?", "", value, flags=re.IGNORECASE).strip()
    value = value.replace(DNA_END, "").strip()
    return value


def _is_non_dna_section_start(line: str) -> bool:
    if not line:
        return False
    if _OTHER_BLOCK_RE.match(line) or _OTHER_HEADING_RE.match(line):
        return True
    return any(line.startswith(label) or line.startswith(f"{label}:") or line.startswith(f"{label}：") for label in _OTHER_FORM_LABELS)


def _normalize_story_dna_block(lines: list[str]) -> str:
    body = "\n".join(line.rstrip() for line in lines).strip()
    return "\n".join([DNA_START, body, DNA_END]) if body else ""


def _story_dna_body(block: str) -> str:
    text = str(block or "").strip()
    if text.startswith(DNA_START):
        text = text[len(DNA_START) :].strip()
    if text.endswith(DNA_END):
        text = text[: -len(DNA_END)].strip()
    return text
