from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import Book, EvidenceSource, KnowledgeEmbedding, MarketSignal, VisualAsset
from app.services.agent_plan_intelligence import summarize_semantic_memory
from app.services.evidence import list_market_signals
from app.services.model_strategy import build_model_strategy


def build_agent_plan_utilization_report(session: Session, *, book_id: int) -> dict[str, Any]:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    sections = [
        _configuration_section(),
        _model_routing_section(),
        _market_evidence_section(session, book=book),
        _semantic_memory_section(session, book_id=book_id),
        _lesson_memory_section(session, book_id=book_id),
        _visual_assets_section(session, book_id=book_id),
    ]
    score = round(sum(section["score"] for section in sections) / max(1, len(sections)))
    gaps = [gap for section in sections for gap in section.get("gaps", [])]
    next_actions = [action for section in sections for action in section.get("next_actions", [])]
    return {
        "book_id": book_id,
        "book_title": book.title,
        "score": score,
        "status": _status(score),
        "sections": sections,
        "gaps": gaps,
        "next_actions": _dedupe(next_actions)[:10],
        "operating_rule": (
            "Agent Plan 负责市场证据、语义记忆、多模态资产和失败复盘；"
            "章节状态机、质量门禁、修订策略仍由 AI Novel System v2 控制。"
        ),
    }


def _configuration_section() -> dict[str, Any]:
    gaps: list[str] = []
    actions: list[str] = []
    if settings.llm_plan != "agent_plan":
        gaps.append("LLM_PLAN 未设置为 agent_plan")
        actions.append("将 LLM_PLAN 设置为 agent_plan。")
    if settings.ark_base_url.rstrip("/") != "https://ark.cn-beijing.volces.com/api/plan/v3":
        gaps.append("ARK_BASE_URL 未指向 Agent Plan 专属 /api/plan/v3")
        actions.append("把 ARK_BASE_URL 设置为 https://ark.cn-beijing.volces.com/api/plan/v3。")
    if not settings.ark_agent_plan_api_key:
        gaps.append("缺少 ARK_AGENT_PLAN_API_KEY")
        actions.append("在 .env 配置 Agent Plan 专属 Key，不要复用普通 Ark Key。")
    if not settings.ark_search_api_key:
        gaps.append("缺少 ARK_SEARCH_API_KEY，Agent Plan 搜索只能走手动包或其他 provider")
        actions.append("配置 ARK_SEARCH_API_KEY；若官方 live search endpoint 不开放，继续使用任务包导入。")
    score = 100 - len(gaps) * 25
    return _section("configuration", max(0, score), gaps, actions)


def _model_routing_section() -> dict[str, Any]:
    strategy = build_model_strategy()
    roles = strategy.get("roles") or {}
    models = [str(item.get("model") or "") for item in roles.values() if isinstance(item, dict)]
    distinct_models = len({model for model in models if model and model != "none"})
    gaps = list(strategy.get("warnings") or [])
    actions = list(strategy.get("recommendations") or [])
    if distinct_models < 3:
        gaps.append("规划、正文、修订、审稿模型分层不足")
        actions.append("规划/审稿用低温轻模型，正文/整章重写用强模型，局部补丁不用模型。")
    score = 90 if distinct_models >= 3 and not gaps else 70 if distinct_models >= 2 else 45
    return {"name": "model_routing", "score": score, "gaps": gaps, "next_actions": actions, "detail": roles}


def _market_evidence_section(session: Session, *, book: Book) -> dict[str, Any]:
    genre = book.genre or ""
    signals = list_market_signals(session, genre=genre, usable_only=True, min_confidence=60) if genre else []
    recent_count = (
        session.scalar(
            select(func.count(MarketSignal.id)).where(
                MarketSignal.genre == genre,
                MarketSignal.confidence >= 60,
                MarketSignal.created_at >= datetime.now() - timedelta(days=14),
            )
        )
        or 0
    )
    source_count = session.scalar(select(func.count(EvidenceSource.id)).where(EvidenceSource.status == "verified", EvidenceSource.reliability >= 3)) or 0
    gaps: list[str] = []
    actions: list[str] = []
    if len(signals) < 5:
        gaps.append(f"可用市场信号不足：{len(signals)}/5")
        actions.append(
            f"执行 Agent Plan 搜索并导入 5-8 条市场信号：{book.target_platform or '番茄小说'} {genre or '网文'} 最新爆款 开篇 爽点 追读 避雷。"
        )
    if recent_count < 3:
        gaps.append(f"近 14 天高置信市场信号不足：{recent_count}/3")
        actions.append("每个新剧情段开始前刷新一次市场证据，不要每章都联网搜索。")
    score = 100 if len(signals) >= 8 and recent_count >= 3 else 75 if len(signals) >= 5 else 45 if signals else 20
    return _section(
        "market_evidence",
        score,
        gaps,
        actions,
        detail={"genre": genre, "usable_signals": len(signals), "recent14d": recent_count, "verified_sources": source_count},
    )


def _semantic_memory_section(session: Session, *, book_id: int) -> dict[str, Any]:
    try:
        summary = summarize_semantic_memory(session, book_id=book_id)
    except OperationalError:
        session.rollback()
        return _section("semantic_memory", 0, ["语义记忆表不可用或迁移未完成"], ["运行迁移后重建语义记忆。"])
    gaps: list[str] = []
    actions: list[str] = []
    if not summary.get("indexed_count"):
        gaps.append("语义记忆为空")
        actions.append("执行 agent-plan-cycle --live-embedding 或 index-book-knowledge --reset --live-embedding。")
    if summary.get("stale"):
        gaps.append("语义记忆已过期")
        actions.append("通过章节后或大改设定后重建语义记忆。")
    if settings.ark_embedding_model not in set(summary.get("models") or []):
        gaps.append("当前语义记忆不是 Agent Plan live embedding 模型生成")
        actions.append("用 --live-embedding 重建，避免长期依赖 dry-run hash 向量。")
    score = 100 if summary.get("ready") and not gaps else 70 if summary.get("indexed_count") else 20
    return _section("semantic_memory", score, gaps, actions, detail=summary)


def _lesson_memory_section(session: Session, *, book_id: int) -> dict[str, Any]:
    rows = list(session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id)))
    source_types = {row.source_type for row in rows}
    quality_count = sum(1 for row in rows if row.source_type == "quality_lesson")
    review_count = sum(1 for row in rows if row.source_type == "production_review")
    gaps: list[str] = []
    actions: list[str] = []
    if "quality_lesson" not in source_types:
        gaps.append("质量/失败经验未进入 Agent Plan 语义记忆")
        actions.append("重建语义记忆，让 quality_lesson 进入 embedding，供后续章节避坑。")
    if review_count == 0:
        gaps.append("生产复盘尚未进入语义记忆")
        actions.append("章节通过或失败恢复后记录 production review，再重建语义记忆。")
    score = 100 if quality_count and review_count else 75 if quality_count else 35
    return _section("lesson_memory", score, gaps, actions, detail={"quality_lesson": quality_count, "production_review": review_count})


def _visual_assets_section(session: Session, *, book_id: int) -> dict[str, Any]:
    assets = list(session.scalars(select(VisualAsset).where(VisualAsset.book_id == book_id).order_by(VisualAsset.id.desc()).limit(20)))
    cover_count = sum(1 for asset in assets if asset.asset_type == "cover")
    chapter_count = sum(1 for asset in assets if asset.asset_type == "chapter_illustration")
    gaps: list[str] = []
    actions: list[str] = []
    if not cover_count:
        gaps.append("尚无 Agent Plan 封面视觉资产")
        actions.append("创建 cover visual asset，作为封面/推广方向，不反向覆盖 Canon。")
    if chapter_count < 1:
        gaps.append("尚无章节插图视觉资产")
        actions.append("为关键章节创建 chapter_illustration visual asset，辅助宣传和画面感校准。")
    score = 100 if cover_count and chapter_count >= 3 else 70 if cover_count or chapter_count else 35
    return _section("visual_assets", score, gaps, actions, detail={"cover": cover_count, "chapter_illustration": chapter_count})


def _section(name: str, score: int, gaps: list[str], actions: list[str], *, detail: Any | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "score": max(0, min(100, int(score))),
        "status": _status(score),
        "gaps": gaps,
        "next_actions": actions,
        "detail": detail or {},
    }


def _status(score: int) -> str:
    if score >= 85:
        return "strong"
    if score >= 65:
        return "usable"
    if score >= 40:
        return "thin"
    return "weak"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result
