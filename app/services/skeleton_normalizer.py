from __future__ import annotations

import re

from app.services.aesthetic_profile import strip_aesthetic_profile_blocks
from app.services.story_dna import story_dna_display_fields, strip_story_dna_blocks


SKELETON_FIELD_ALIASES = {
    "premise": ("一句话核心设定", "核心设定", "故事前提", "前提"),
    "reader_promise": ("读者承诺", "读者期待", "爽点承诺", "阅读承诺"),
    "world_engine": ("世界规则", "能力曲线", "世界与收益机制", "世界机制", "收益机制"),
    "protagonist_engine": ("主角动力", "成长弧", "主角发动机", "主角机制"),
    "conflict_engine": ("长期冲突", "主线", "故事推进", "冲突引擎"),
    "forbidden_rules": ("禁忌规则", "写作边界", "避雷", "禁止事项", "不能写"),
    "style_guide": ("文风指南", "文风", "笔触", "正文风格"),
    "aesthetic_profile": ("审美画像", "题材主味", "氛围边界"),
    "story_dna": ("作品DNA", "作品 DNA", "章节发动机库", "章节发动机"),
    "volume_summary": ("第一卷摘要", "卷摘要", "第一卷"),
    "arc_goal": ("剧情段目标", "前五章目标", "开局目标"),
    "arc_climax": ("剧情段高潮", "前五章高潮", "开局高潮"),
    "arc_turn": ("剧情段转折", "前五章转折", "开局转折"),
}

_ALIAS_TO_FIELD = {
    re.sub(r"\s+", "", alias): field
    for field, aliases in SKELETON_FIELD_ALIASES.items()
    for alias in aliases
}


def normalize_story_skeleton_payload(payload: dict) -> dict[str, str]:
    raw = relocate_labeled_sections({key: str(value or "").strip() for key, value in (payload or {}).items()})
    extracted_from_common = story_dna_display_fields(
        style_guide="\n\n".join(item for item in [raw.get("style_guide", ""), raw.get("aesthetic_profile", "")] if item),
        forbidden_rules=raw.get("forbidden_rules", ""),
    )["story_dna"]
    explicit_dna = story_dna_display_fields(style_guide=raw.get("story_dna", ""), forbidden_rules="")["story_dna"]
    cleaned = dict(raw)
    cleaned["style_guide"] = strip_aesthetic_profile_blocks(strip_story_dna_blocks(raw.get("style_guide", "")))
    cleaned["forbidden_rules"] = strip_aesthetic_profile_blocks(strip_story_dna_blocks(raw.get("forbidden_rules", "")))
    if cleaned.get("aesthetic_profile"):
        cleaned["aesthetic_profile"] = strip_story_dna_blocks(cleaned["aesthetic_profile"])
    cleaned["story_dna"] = explicit_dna or raw.get("story_dna", "") or extracted_from_common
    return cleaned


def relocate_labeled_sections(payload: dict[str, str]) -> dict[str, str]:
    relocated = {key: str(value or "").strip() for key, value in payload.items()}
    additions: dict[str, list[str]] = {key: [] for key in SKELETON_FIELD_ALIASES}
    for source_key, value in list(relocated.items()):
        if not value or source_key not in SKELETON_FIELD_ALIASES:
            continue
        own_parts: list[str] = []
        current_field = source_key
        current_lines: list[str] = []
        saw_label = False
        protected_block = ""
        for raw_line in value.splitlines():
            if protected_block:
                current_lines.append(raw_line)
                if _is_protected_block_end(raw_line, protected_block):
                    protected_block = ""
                continue
            block_type = _protected_block_start(raw_line)
            if block_type:
                current_lines.append(raw_line)
                protected_block = "" if _is_protected_block_end(raw_line, block_type) else block_type
                continue
            label_field, tail = _split_label_line(raw_line)
            if label_field:
                if current_lines:
                    _append_section(source_key, current_field, current_lines, own_parts, additions)
                current_field = label_field
                current_lines = [tail] if tail else []
                saw_label = True
                continue
            current_lines.append(raw_line)
        if current_lines:
            _append_section(source_key, current_field, current_lines, own_parts, additions)
        if saw_label:
            relocated[source_key] = "\n".join(line for line in own_parts if line.strip()).strip()
    for field, values in additions.items():
        relocated[field] = _dedupe_paragraphs("\n\n".join(item for item in [relocated.get(field, ""), *values] if item))
    return relocated


def _split_label_line(line: str) -> tuple[str, str]:
    text = str(line or "").strip()
    if not text:
        return "", ""
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^[-*]\s*", "", text)
    text = re.sub(r"^【(.+?)】\s*", r"\1：", text)
    for separator in ("：", ":"):
        if separator not in text:
            continue
        label, tail = text.split(separator, 1)
        field = _ALIAS_TO_FIELD.get(re.sub(r"\s+", "", label.strip()))
        if field:
            return field, tail.strip()
    return "", ""


def _protected_block_start(line: str) -> str:
    compact = re.sub(r"\s+", "", str(line or "").strip())
    if not compact:
        return ""
    if compact.startswith(("【作品DNA】", "【作品ＤＮＡ】", "#作品DNA", "##作品DNA", "###作品DNA", "#作品ＤＮＡ", "##作品ＤＮＡ", "作品DNA:", "作品DNA：", "作品ＤＮＡ:", "作品ＤＮＡ：")):
        return "dna"
    if compact.startswith(("【作品审美画像】", "【审美画像】", "【题材主味】", "#作品审美画像", "##作品审美画像", "###作品审美画像", "#审美画像", "##审美画像", "审美画像:", "审美画像：", "题材主味:", "题材主味：")):
        return "profile"
    return ""


def _is_protected_block_end(line: str, block_type: str) -> bool:
    compact = re.sub(r"\s+", "", str(line or "").strip())
    if block_type == "dna":
        return "作品DNA结束" in compact or "作品ＤＮＡ结束" in compact
    if block_type == "profile":
        return "审美画像结束" in compact
    return False


def _append_section(source_key: str, target_key: str, lines: list[str], own_parts: list[str], additions: dict[str, list[str]]) -> None:
    text = "\n".join(line.rstrip() for line in lines).strip()
    if not text:
        return
    if target_key == source_key:
        own_parts.append(text)
    else:
        additions.setdefault(target_key, []).append(text)


def _dedupe_paragraphs(text: str) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for raw in str(text or "").split("\n\n"):
        part = raw.strip()
        if not part or part in seen:
            continue
        seen.add(part)
        parts.append(part)
    return "\n\n".join(parts)
