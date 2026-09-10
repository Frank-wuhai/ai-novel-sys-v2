from __future__ import annotations

from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterVersion
from app.services.bias import evaluate_generation_bias
from app.services.quality import chinese_chars


def build_publish_preflight(session: Session, *, version_id: int) -> dict:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    # 取 book profile 让检查跟 hard_gate 一致
    book_profile = None
    try:
        chapter = session.get(Chapter, version.chapter_id)
        if chapter:
            from app.services.book_profile import build_book_profile
            book_profile = build_book_profile(session, book_id=chapter.book_id)
    except Exception:
        book_profile = None
    allow_system_panel = bool(book_profile and "系统任务" not in (book_profile.avoid_markers or ()))

    blockers: list[str] = []
    warnings: list[str] = []
    content = version.content or ""
    if version.status != "approved":
        blockers.append(f"版本状态不是 approved: {version.status}")
    chars = chinese_chars(content)
    # 番茄硬下限约 2000 · 用 1900 略微保守（Ch1-3 已过审 chars 2172-2777）
    import os
    min_chars = int(os.environ.get("PUBLISH_MIN_CHARS", "1900"))
    if chars < min_chars:
        blockers.append(f"正文过短: {chars}")
    # 元信息硬拦（但"系统提示"要走 book_profile 判断）
    hard_meta_markers = ("修订模式", "修订合同", "作为AI")
    for marker in hard_meta_markers:
        if marker in content:
            blockers.append("正文含后台/模型元信息")
            break
    # "系统提示" 走 quality 的 meta_leak 检测·允许抽奖爽文的叙述性面板
    if "系统提示" in content:
        from app.services.quality import _has_forbidden_marker
        if _has_forbidden_marker(content, "系统提示", allow_system_panel=allow_system_panel):
            blockers.append("正文含后台/模型元信息")
    bias = evaluate_generation_bias(content=content, profile=book_profile)
    # 只在 book profile 有明确 model_drift_markers 时才 hard block
    # （bias.blockers 已按 profile 判定过·避免用 model_bias_hits 走 fallback markers 误伤）
    if bias.blockers:
        for b in bias.blockers:
            if b.startswith("model_default_drift"):
                blockers.append("正文仍含模型默认套路词: " + b.split(":", 1)[1])
                break
    if not version.title:
        warnings.append("章节标题为空")
    return {
        "passed": not blockers,
        "version_id": version.id,
        "status": version.status,
        "title": version.title,
        "chinese_chars": chars,
        "blockers": blockers,
        "warnings": warnings,
        "export_preview": _format_export(version)[:1200],
    }


def _format_export(version: ChapterVersion) -> str:
    title = version.title or f"第{version.version_number}版"
    return f"{title}\n\n{version.content or ''}".strip()
