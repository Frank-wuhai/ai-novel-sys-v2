from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class ProductionContext:
    market_evidence: str
    canon_context: str
    author_preferences: str
    previous_chapter_context: str
    quality_report: str
    previous_content: str
    audit: dict


def build_production_context(
    *,
    market_evidence: str = "",
    canon_context: str = "",
    author_preferences: str = "",
    previous_chapter_context: str = "",
    quality_report: str | None = None,
    previous_content: str = "",
    revision_mode: str = "draft",
    fresh_rewrite: bool = False,
    rewrite_mode: bool = False,
) -> ProductionContext:
    """Keep generation context short, current, and role-specific."""
    quality = sanitize_quality_report(quality_report, fresh_rewrite=fresh_rewrite)
    old_text = select_previous_content(previous_content, fresh_rewrite=fresh_rewrite, rewrite_mode=rewrite_mode)
    audit = {
        "market_chars": len(market_evidence or ""),
        "canon_chars": len(canon_context or ""),
        "author_preference_chars": len(author_preferences or ""),
        "previous_chapter_chars": len(previous_chapter_context or ""),
        "quality_report_chars": len(quality or ""),
        "previous_content_chars": len(old_text or ""),
        "revision_mode": revision_mode,
        "fresh_rewrite": fresh_rewrite,
        "rewrite_mode": rewrite_mode,
        "policy": "short_director_first_context",
    }
    return ProductionContext(
        market_evidence=_clip(market_evidence, 1800),
        canon_context=_clip(canon_context, 2600),
        author_preferences=_clip(author_preferences, 1400),
        previous_chapter_context=_clip(previous_chapter_context, 1600, tail=True),
        quality_report=quality,
        previous_content=old_text,
        audit=audit,
    )


def sanitize_quality_report(report: str | None, *, fresh_rewrite: bool = False) -> str:
    if fresh_rewrite:
        return (
            "旧质检仅表示上一版失败；不要采纳其中与最新生产骨架冲突的具体桥段、名词、能力表现或场景建议。"
            "本轮以导演单、修订合同、最新 Canon 和作者口味为准。"
        )
    if not report:
        return "本轮没有可用旧质检；按导演单、修订合同和 Canon 执行。"
    try:
        data = json.loads(report)
    except json.JSONDecodeError:
        return _clip(report, 1200)
    if not isinstance(data, dict):
        return _clip(report, 1200)
    llm = data.get("llm_review") if isinstance(data.get("llm_review"), dict) else {}
    essentials = {
        "score": data.get("score"),
        "issues": _list(data.get("issues"))[:6],
        "warnings": _list(data.get("warnings"))[:6],
        "review_issues": _list(llm.get("issues"))[:5],
        "revision_suggestions": _list(llm.get("revision_suggestions"))[:5],
        "risk_flags": _list(llm.get("risk_flags"))[:5],
        "bias_report": data.get("bias_report") if isinstance(data.get("bias_report"), dict) else {},
    }
    return json.dumps(essentials, ensure_ascii=False)


def select_previous_content(content: str, *, fresh_rewrite: bool = False, rewrite_mode: bool = False) -> str:
    if fresh_rewrite:
        return (
            "旧稿已废弃，本次按最新导演单重启本章。"
            "不要参考旧稿段落顺序、旧场景推进、旧句式或旧桥段；只保留数据库 Canon 中仍有效的必要事实。"
        )
    if not content:
        return ""
    if not rewrite_mode:
        return _clip(content, 6200, tail=True)
    excerpt = "\n".join(content.splitlines()[:80])
    return (
        "以下旧稿只用于保留必要 Canon 和避免断裂；禁止照抄旧稿句子，禁止沿用旧稿段落顺序。\n\n"
        + _clip(excerpt, 4200)
    )


def _clip(value: str, limit: int, *, tail: bool = False) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    if tail:
        return "…\n" + text[-limit:]
    return text[:limit] + "\n…"


def _list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]
