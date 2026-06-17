from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.services.skeleton_governance import audit_skeleton_sources
from app.services.story import upsert_story_bible


PROFILE_START = "【作品审美画像】"
PROFILE_END = "【作品审美画像结束】"
_PROFILE_LABEL_RE = re.compile(r"作品\s*审美画像|审美画像|题材主味", re.IGNORECASE)
_PROFILE_END_RE = re.compile(r"作品\s*审美画像\s*结束|审美画像\s*结束", re.IGNORECASE)
_OTHER_BLOCK_RE = re.compile(r"^【(?!作品\s*审美画像|审美画像|题材主味)(.+?)】$")
_OTHER_HEADING_RE = re.compile(r"^#{1,6}\s+(?!作品\s*审美画像|审美画像|题材主味).+")
_OTHER_FORM_LABELS = (
    "一句话核心设定",
    "读者承诺",
    "作品DNA",
    "作品 DNA",
    "主角动力",
    "成长弧",
    "世界规则",
    "能力曲线",
    "长期冲突",
    "文风指南",
    "禁忌规则",
    "第一卷摘要",
)


def build_aesthetic_profile_block(
    *,
    prose_style: str = "",
    atmosphere: str = "",
    story_route: str = "",
    must_have: str = "",
    must_not: str = "",
) -> str:
    prose_style = prose_style.strip() or "明快、有画面、有角色声线，避免长期冷硬克制。"
    atmosphere = atmosphere.strip() or "有烟火气、有热闹江湖和人物趣味，压抑感只能局部使用。"
    story_route = story_route.strip() or "以主角主动破局、奇遇冒险、爽点回报和人情交锋推进。"
    must_have = must_have.strip() or "每章至少有一个可感知的正向回报：见识、招式、关系、资源、名声、机会或局面主动权。"
    must_not = must_not.strip() or "不要把旧案追查、血迹盘问、阴雨冷场、旧债逼迫或悬疑线索当成默认章节发动机。"
    return "\n".join(
        [
            PROFILE_START,
            f"- 笔触: {prose_style}",
            f"- 氛围: {atmosphere}",
            f"- 路线: {story_route}",
            f"- 必须保留: {must_have}",
            f"- 禁止惯性: {must_not}",
            "- 执行要求: 如果市场证据、质量规则或旧稿倾向与本画像冲突，以本画像为准；悬疑只能作为辅料，不能吞掉题材主味。",
            PROFILE_END,
        ]
    )


def build_aesthetic_profile_from_idea(idea: str) -> str:
    idea = _compact(idea)
    if not idea:
        return ""
    markers = (
        "笔触",
        "氛围",
        "路线",
        "冷硬",
        "阴冷",
        "压抑",
        "悬疑",
        "热闹",
        "烟火",
        "奇遇",
        "爽文",
        "武侠",
        "轻松",
        "吐槽",
    )
    if not any(marker in idea for marker in markers):
        return ""
    return build_aesthetic_profile_block(
        prose_style="按作者意见执行，形成可辨识笔触；不要让默认质量规则把语言收敛成单一冷硬写实。作者原话：" + idea[:220],
        atmosphere="以作者指定氛围为准；紧张可以局部存在，但不能覆盖全书主味。",
        story_route="以作者指定路线为主发动机；章节冲突、爽点和章末期待都要服务这条路线。",
        must_have="每章都要能看出本书独有的笔触、氛围和路线，而不是复用系统默认模板。",
        must_not="不得回到作者明确否定的惯性写法。作者原话：" + idea[:220],
    )


def profile_from_story_text(*, style_guide: str = "", forbidden_rules: str = "") -> str:
    blocks = _extract_aesthetic_profile_blocks("\n".join([style_guide or "", forbidden_rules or ""]))
    return blocks[0] if blocks else ""


def strip_aesthetic_profile_blocks(text: str) -> str:
    return _strip_aesthetic_profile_blocks(text)


def merge_style_with_aesthetic_profile(style_guide: str, profile: str) -> str:
    clean_style = strip_aesthetic_profile_blocks(style_guide)
    profile = str(profile or "").strip()
    return "\n\n".join(item for item in [clean_style, profile] if item)


def story_bible_display_fields(*, style_guide: str = "", forbidden_rules: str = "") -> dict[str, str]:
    return {
        "forbidden_rules": strip_aesthetic_profile_blocks(forbidden_rules),
        "style_guide": strip_aesthetic_profile_blocks(style_guide),
        "aesthetic_profile": profile_from_story_text(style_guide=style_guide, forbidden_rules=forbidden_rules),
    }


def apply_aesthetic_profile(
    session: Session,
    *,
    book_id: int,
    prose_style: str = "",
    atmosphere: str = "",
    story_route: str = "",
    must_have: str = "",
    must_not: str = "",
) -> str:
    block = build_aesthetic_profile_block(
        prose_style=prose_style,
        atmosphere=atmosphere,
        story_route=story_route,
        must_have=must_have,
        must_not=must_not,
    )
    bible = upsert_story_bible(session, book_id=book_id)
    bible.style_guide = _replace_profile_block(bible.style_guide or "", block)
    from app.services.story_dna import strip_story_dna_blocks

    bible.forbidden_rules = strip_story_dna_blocks(strip_aesthetic_profile_blocks(bible.forbidden_rules or ""))
    session.flush()
    return block


def apply_revision_idea_to_repair_payload(payload: dict, *, revision_idea: str) -> dict:
    skeleton = payload.get("skeleton") or payload.get("repaired_skeleton") or {}
    if not isinstance(skeleton, dict):
        return payload
    repaired = apply_revision_idea_to_skeleton(skeleton, revision_idea=revision_idea)
    if repaired == skeleton:
        return payload
    after = audit_skeleton_sources({f"author_idea.{key}": str(value or "") for key, value in repaired.items()})
    updated = dict(payload)
    updated["skeleton"] = repaired
    updated["repaired_skeleton"] = repaired
    updated["passed"] = after.passed
    updated["score"] = after.score
    updated["after"] = after.to_dict()
    updated["author_revision_idea_applied"] = True
    return updated


def apply_revision_idea_to_skeleton(skeleton: dict, *, revision_idea: str) -> dict:
    block = build_aesthetic_profile_from_idea(revision_idea)
    if not block:
        return skeleton
    repaired = dict(skeleton)
    repaired["style_guide"] = merge_style_with_aesthetic_profile(repaired.get("style_guide", ""), block)
    repaired["forbidden_rules"] = strip_aesthetic_profile_blocks(repaired.get("forbidden_rules", ""))
    repaired["aesthetic_profile"] = block
    return repaired


def _replace_profile_block(text: str, block: str) -> str:
    text = (text or "").strip()
    start = text.find(PROFILE_START)
    end = text.find(PROFILE_END)
    if start >= 0 and end >= start:
        prefix = text[:start].strip()
        suffix = text[end + len(PROFILE_END) :].strip()
        return "\n\n".join(item for item in [prefix, block, suffix] if item)
    return "\n\n".join(item for item in [text, block] if item)


def _extract_aesthetic_profile_blocks(text: str) -> list[str]:
    lines = str(text or "").splitlines()
    blocks: list[str] = []
    current: list[str] = []
    in_block = False
    for raw in lines:
        line = raw.strip()
        if not in_block and not _is_profile_start(line):
            continue
        if not in_block:
            in_block = True
            current = []
            tail = _profile_start_tail(line)
            if tail:
                current.append(tail)
            if _is_profile_end(line):
                blocks.append(_normalize_profile_block(current))
                in_block = False
                current = []
            continue
        if _is_profile_end(line):
            blocks.append(_normalize_profile_block(current))
            in_block = False
            current = []
            continue
        if _is_non_profile_section_start(line):
            blocks.append(_normalize_profile_block(current))
            in_block = False
            current = []
            continue
        current.append(raw.rstrip())
    if in_block:
        blocks.append(_normalize_profile_block(current))
    return [block for block in blocks if _profile_body(block)]


def _strip_aesthetic_profile_blocks(text: str) -> str:
    lines = str(text or "").splitlines()
    kept: list[str] = []
    in_block = False
    for raw in lines:
        line = raw.strip()
        if not in_block and _is_profile_start(line):
            in_block = True
            if _is_profile_end(line):
                in_block = False
            continue
        if in_block:
            if _is_profile_end(line):
                in_block = False
                continue
            if _is_non_profile_section_start(line):
                in_block = False
                kept.append(raw)
            continue
        kept.append(raw)
    return "\n\n".join(part.strip() for part in "\n".join(kept).split("\n\n") if part.strip())


def _is_profile_start(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    if not compact:
        return False
    if compact in {"【作品审美画像】", "【审美画像】", "【题材主味】"}:
        return True
    if compact.startswith(("##作品审美画像", "###作品审美画像", "#作品审美画像", "作品审美画像:", "作品审美画像：")):
        return True
    if compact.startswith(("##审美画像", "###审美画像", "#审美画像", "审美画像:", "审美画像：", "题材主味:", "题材主味：")):
        return True
    return bool(_PROFILE_LABEL_RE.match(line)) and (":" in line or "：" in line)


def _is_profile_end(line: str) -> bool:
    compact = re.sub(r"\s+", "", line or "")
    return compact in {"【作品审美画像结束】", "【审美画像结束】", "作品审美画像结束", "审美画像结束"} or bool(_PROFILE_END_RE.search(line))


def _profile_start_tail(line: str) -> str:
    value = re.sub(r"^#{1,6}\s*", "", line or "").strip()
    value = re.sub(r"^【\s*(?:作品\s*)?审美画像\s*】", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^【\s*题材主味\s*】", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^(?:作品\s*)?审美画像\s*(?:/[^:：]*)?[:：]?", "", value, flags=re.IGNORECASE).strip()
    value = re.sub(r"^题材主味\s*(?:/[^:：]*)?[:：]?", "", value, flags=re.IGNORECASE).strip()
    value = value.replace(PROFILE_END, "").strip()
    return value


def _is_non_profile_section_start(line: str) -> bool:
    if not line:
        return False
    if _OTHER_BLOCK_RE.match(line) or _OTHER_HEADING_RE.match(line):
        return True
    return any(line.startswith(label) or line.startswith(f"{label}:") or line.startswith(f"{label}：") for label in _OTHER_FORM_LABELS)


def _normalize_profile_block(lines: list[str]) -> str:
    body = "\n".join(line.rstrip() for line in lines).strip()
    return "\n".join([PROFILE_START, body, PROFILE_END]) if body else ""


def _profile_body(block: str) -> str:
    text = str(block or "").strip()
    if text.startswith(PROFILE_START):
        text = text[len(PROFILE_START) :].strip()
    if text.endswith(PROFILE_END):
        text = text[: -len(PROFILE_END)].strip()
    return text


def _compact(value: str) -> str:
    return " ".join(str(value or "").strip().split())


def _append_text(value: str, addition: str) -> str:
    base = str(value or "").strip()
    extra = str(addition or "").strip()
    if not extra or extra in base:
        return base
    return f"{base} {extra}".strip()
