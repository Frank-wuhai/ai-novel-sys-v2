from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief
from app.services.context_contamination import context_anchor_lines


STALE_BRIEF_MARKERS = (
    "已废弃",
    "旧主角名",
    "旧世界名",
    "旧桥段",
    "江湖志",
    "大江湖",
    "林默",
    "陈默",
    "题材主味: 玄幻脑洞",
    "题材主味：玄幻脑洞",
    "【作品DNA】 - 题材主味: 玄幻脑洞",
)
CONTEXTUAL_STALE_MARKERS = {"江湖志", "大江湖", "林默", "陈默"}
PROMPT_ARTIFACT_MARKERS = (
    "clean_rebuild_contract@",
    "reading_assessment_auto_quality#",
    "system_revision_",
    "editorial_elevation_quality#",
    "质量报告：#",
    "质检报告 #",
    "当前阅读层级",
    "源版本锁定",
    "重建素材来源",
    "合同当前底稿",
    "触发原因",
    "自动修订预算",
    "恢复底稿",
    "废弃劣化稿",
    "最新待修稿",
    "当前最新待修稿",
    "选择最佳底稿",
    "score=",
    "protected_brief",
    "reading_assessment_contract",
    "当前稿不是正式批准稿",
    "阅读评估结论",
    "修订执行摘要",
    "系统修订判定",
    "处理强度",
    "置信度",
    "判定理由",
    "升级规则",
    "原始意见",
    "反馈调整#",
)
PROMPT_ARTIFACT_PREFIXES = (
    "revision_mode:",
    "revision_mode：",
    "修订模式:",
    "修订模式：",
    "源版本：",
    "源版本:",
    "策略：",
    "停滞维度：",
    "低分维度：",
    "失败问题：",
)


def sanitize_chapter_brief_fields(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str = "",
    constraints: str = "",
) -> tuple[str, str, str]:
    anchors = context_anchor_lines(session, book_id=book_id)
    current_context = "\n".join(anchors)
    clean_goal = _sanitize_text(goal, current_context=current_context)
    clean_required = _sanitize_text(required_beats, current_context=current_context)
    clean_constraints = _sanitize_text(constraints, current_context=current_context)
    required_lines = [line for line in clean_required.splitlines() if line.strip()]
    for anchor in anchors:
        if anchor and anchor not in "\n".join(required_lines):
            required_lines.append(anchor)
    if not clean_goal.strip():
        clean_goal = f"第{chapter_number}章：承接当前作品设定推进。"
    return clean_goal.strip(), "\n".join(required_lines).strip(), clean_constraints.strip()


def sanitize_existing_chapter_brief(session: Session, *, book_id: int, brief: ChapterBrief) -> ChapterBrief:
    chapter = session.get(Chapter, brief.chapter_id)
    if not chapter:
        return brief
    goal, required_beats, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=book_id,
        chapter_number=chapter.chapter_number,
        goal=brief.goal,
        required_beats=brief.required_beats,
        constraints=brief.constraints,
    )
    brief.goal = goal
    brief.required_beats = required_beats
    brief.constraints = constraints
    session.flush()
    return brief


def sanitize_prompt_contract_text(text: str) -> str:
    """Return only model-actionable story instructions, not recovery metadata."""
    parts: list[str] = []
    for raw_line in str(text or "").splitlines():
        for raw_part in raw_line.replace("；", "\n").splitlines():
            part = raw_part.strip(" -\t")
            if not part:
                continue
            cleaned = _prompt_safe_part(part)
            if cleaned and cleaned not in parts:
                parts.append(cleaned)
    return "\n".join(parts)


def _sanitize_text(text: str, *, current_context: str) -> str:
    lines = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _line_has_wrong_anchor(line, current_context=current_context):
            continue
        lines.append(line)
    return "\n".join(lines)


def _prompt_safe_part(part: str) -> str:
    part = re.sub(r"，?旧稿\s*v\d+\s*只作素材参考。?", "", part)
    part = re.sub(r"，?旧稿\s*v\d+\s*只保留可用素材。?", "", part)
    part = re.sub(r"，?以\s*v\d+\s*为底稿。?", "", part)
    if _is_prompt_artifact(part):
        return ""
    replacements = (
        ("本轮只解决：", "修订目标："),
        ("必须保留：", "可保留素材："),
        ("可复用素材：", "可保留素材："),
    )
    cleaned = part
    for prefix, replacement in replacements:
        if cleaned.startswith(prefix):
            cleaned = replacement + cleaned[len(prefix):].strip()
            break
    if _is_prompt_artifact(cleaned):
        return ""
    return cleaned


def _is_prompt_artifact(part: str) -> bool:
    text = part.strip()
    if not text:
        return True
    if any(text.startswith(prefix) for prefix in PROMPT_ARTIFACT_PREFIXES):
        return True
    if any(marker in text for marker in PROMPT_ARTIFACT_MARKERS):
        return True
    if "v" in text and any(marker in text for marker in ("底稿", "旧稿", "版本", "恢复", "回退", "待修稿")):
        return True
    return False


def _line_has_wrong_anchor(line: str, *, current_context: str) -> bool:
    has_current_context = bool((current_context or "").strip())
    for marker in STALE_BRIEF_MARKERS:
        if marker not in line:
            continue
        if marker in CONTEXTUAL_STALE_MARKERS and not has_current_context:
            continue
        if marker not in current_context:
            return True
    return False
