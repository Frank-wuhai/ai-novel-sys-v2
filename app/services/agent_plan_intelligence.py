from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.exc import OperationalError
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.entities import (
    Book,
    Character,
    Chapter,
    ChapterVersion,
    EvidenceSource,
    KnowledgeEmbedding,
    MarketSignal,
    PlatformFeedback,
    StoryBible,
    StoryFoundation,
    VisualAsset,
    WorldRule,
)
from app.services.aesthetic_profile import strip_aesthetic_profile_blocks
from app.services.canon import format_canon_context


@dataclass(frozen=True)
class RetrievalHit:
    embedding_id: int
    source_type: str
    source_ref_id: str
    source_label: str
    score: float
    text: str


def format_semantic_memory_context(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    query: str,
    limit: int = 5,
) -> tuple[str, list[int]]:
    try:
        summary = ensure_semantic_memory_for_production(session, book_id=book_id)
        hits = retrieve_book_knowledge(
            session,
            book_id=book_id,
            query=f"第{chapter_number}章 {query}",
            limit=limit,
            dry_run=not _should_use_live_embedding_query(summary),
        )
    except (OperationalError, RuntimeError, httpx.HTTPError, ValueError):
        session.rollback()
        return "", []
    if not hits:
        return "", []
    lines = []
    for hit in hits:
        text = strip_aesthetic_profile_blocks(hit.text)
        if hit.score > 0.15 and text:
            lines.append(f"- memory#{hit.embedding_id} score={hit.score:.3f} type={hit.source_type} ref={hit.source_ref_id} label={hit.source_label}: {text[:420]}")
    if not lines:
        return "", []
    return "语义记忆召回（只用于防止遗漏，不得覆盖正式 Canon）：\n" + "\n".join(lines), [hit.embedding_id for hit in hits]


def ensure_semantic_memory_for_production(session: Session, *, book_id: int) -> dict:
    summary = summarize_semantic_memory(session, book_id=book_id)
    expected_count = int(summary.get("expected_count") or 0)
    models = set(summary.get("models") or [])
    has_live_index = settings.ark_embedding_model in models
    needs_rebuild = bool(summary.get("stale")) or int(summary.get("indexed_count") or 0) == 0 or not has_live_index
    if not needs_rebuild:
        summary["auto_live_embedding_action"] = "ready"
        return summary
    if not settings.auto_live_embedding:
        summary["auto_live_embedding_action"] = "disabled"
        return _ensure_dry_run_memory(session, book_id=book_id, summary=summary)
    if not settings.ark_agent_plan_api_key:
        summary["auto_live_embedding_action"] = "missing_api_key"
        return _ensure_dry_run_memory(session, book_id=book_id, summary=summary)
    if expected_count > settings.auto_live_embedding_max_chunks:
        summary["auto_live_embedding_action"] = f"chunk_limit:{expected_count}>{settings.auto_live_embedding_max_chunks}"
        return _ensure_dry_run_memory(session, book_id=book_id, summary=summary)
    index_book_knowledge(session, book_id=book_id, dry_run=False, reset=True)
    refreshed = summarize_semantic_memory(session, book_id=book_id)
    refreshed["auto_live_embedding_action"] = "rebuilt_live"
    return refreshed


def _ensure_dry_run_memory(session: Session, *, book_id: int, summary: dict) -> dict:
    if int(summary.get("indexed_count") or 0) == 0 or bool(summary.get("stale")):
        index_book_knowledge(session, book_id=book_id, dry_run=True, reset=True)
        refreshed = summarize_semantic_memory(session, book_id=book_id)
        refreshed["auto_live_embedding_action"] = summary.get("auto_live_embedding_action", "rebuilt_dry_run")
        return refreshed
    return summary


def _should_use_live_embedding_query(summary: dict) -> bool:
    return settings.ark_embedding_model in set(summary.get("models") or [])


def create_market_research_pack(
    session: Session,
    *,
    genre: str,
    query: str,
    platform: str = "番茄小说",
) -> dict:
    book_examples = _book_examples_for_genre(session, genre=genre)
    queries = [
        query,
        f"{platform} {genre} 热门 榜单 读者 评价",
        f"{platform} {genre} 开篇 卖点 爽点 避雷",
        f"{platform} {genre} 最新 爆款 题材 趋势",
    ]
    payload = {
        "genre": genre,
        "platform": platform,
        "queries": _dedupe(queries),
        "book_examples": book_examples,
        "expected_result_schema": {
            "results": [
                {
                    "title": "source title",
                    "url": "https://...",
                    "snippet": "short evidence excerpt or summary",
                    "signals": ["market signal derived from this source"],
                    "reliability": 3,
                    "confidence": 70,
                }
            ]
        },
        "instructions": [
            "Use Agent Plan web search Harness or MCP to search the queries.",
            "Prefer current platform/ranking/editorial/community sources.",
            "Return concise JSON in expected_result_schema.",
            "Do not invent sources or URLs.",
        ],
    }
    path = _write_artifact("market_research", f"{_safe_name(genre)}-{_stamp()}.json", payload)
    payload["artifact_path"] = str(path)
    return payload


def ingest_market_research_results(
    session: Session,
    *,
    genre: str,
    result_json: str,
    source_prefix: str = "agent-search",
) -> dict:
    payload = _loads_json(result_json)
    rows = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("result_json must contain a results list or be a JSON list")
    created_sources: list[int] = []
    created_signals: list[int] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        title = str(row.get("title") or f"Agent search source {index}").strip()
        url = str(row.get("url") or "").strip()
        snippet = str(row.get("snippet") or row.get("summary") or "").strip()
        reliability = _bounded_int(row.get("reliability"), default=3, low=1, high=5)
        source_key = f"{source_prefix}-{_safe_name(genre)}-{_short_hash(url or title or str(index))}"
        source = _upsert_evidence_source(
            session,
            source_key=source_key,
            title=title,
            url=url,
            reliability=reliability,
        )
        created_sources.append(source.id)
        signals = row.get("signals") or []
        if isinstance(signals, str):
            signals = [signals]
        confidence = _bounded_int(row.get("confidence"), default=70, low=0, high=100)
        for signal_text in signals:
            text = str(signal_text).strip()
            if not text:
                continue
            signal = MarketSignal(source_id=source.id, genre=genre, signal_text=text, confidence=confidence)
            session.add(signal)
            session.flush()
            created_signals.append(signal.id)
        if snippet and not signals:
            signal = MarketSignal(source_id=source.id, genre=genre, signal_text=snippet[:500], confidence=confidence)
            session.add(signal)
            session.flush()
            created_signals.append(signal.id)
    return {"source_ids": created_sources, "market_signal_ids": created_signals}


def index_book_knowledge(
    session: Session,
    *,
    book_id: int,
    dry_run: bool = False,
    reset: bool = False,
    limit_chapters: int = 80,
) -> dict:
    book = _book(session, book_id)
    chunks = _knowledge_chunks(session, book=book, limit_chapters=limit_chapters)
    if reset:
        session.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id))
        session.flush()
    created: list[int] = []
    for chunk in chunks:
        vector = _embed_text(chunk["text"], dry_run=dry_run)
        item = KnowledgeEmbedding(
            book_id=book_id,
            source_type=chunk["source_type"],
            source_ref_id=chunk["source_ref_id"],
            source_label=chunk["source_label"],
            text=chunk["text"],
            embedding_json=json.dumps(vector, ensure_ascii=False),
            model="dry-run-hash" if dry_run else settings.ark_embedding_model,
            dimensions=len(vector),
        )
        session.add(item)
        session.flush()
        created.append(item.id)
    return {"book_id": book_id, "indexed_count": len(created), "embedding_ids": created}


def retrieve_book_knowledge(
    session: Session,
    *,
    book_id: int,
    query: str,
    limit: int = 8,
    dry_run: bool = False,
) -> list[RetrievalHit]:
    query_vector = _embed_text(query, dry_run=dry_run)
    rows = list(session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id).order_by(KnowledgeEmbedding.id)))
    hits: list[RetrievalHit] = []
    for row in rows:
        vector = _loads_vector(row.embedding_json)
        if not vector:
            continue
        score = _cosine(query_vector, vector)
        hits.append(
            RetrievalHit(
                embedding_id=row.id,
                source_type=row.source_type,
                source_ref_id=row.source_ref_id,
                source_label=row.source_label,
                score=score,
                text=row.text[:800],
            )
        )
    return sorted(hits, key=lambda item: item.score, reverse=True)[:limit]


def summarize_semantic_memory(session: Session, *, book_id: int) -> dict:
    rows = list(session.scalars(select(KnowledgeEmbedding).where(KnowledgeEmbedding.book_id == book_id)))
    chunks = _knowledge_chunks(session, book=_book(session, book_id), limit_chapters=80)
    expected_keys = {(chunk["source_type"], chunk["source_ref_id"]) for chunk in chunks}
    indexed_keys = {(row.source_type, row.source_ref_id) for row in rows}
    missing_keys = sorted(expected_keys - indexed_keys)
    indexed_count = len(rows)
    latest_embedding_at = max((row.created_at for row in rows if row.created_at), default=None)
    source_types = sorted({row.source_type for row in rows if row.source_type})
    models = sorted({row.model for row in rows if row.model})
    dimensions = sorted({row.dimensions for row in rows if row.dimensions})
    latest_chapter_version_at = session.scalar(
        select(func.max(ChapterVersion.created_at))
        .join(Chapter, ChapterVersion.chapter_id == Chapter.id)
        .where(Chapter.book_id == book_id)
    )
    stale_by_time = bool(indexed_count and latest_chapter_version_at and latest_embedding_at and latest_embedding_at < latest_chapter_version_at)
    stale = stale_by_time or bool(missing_keys)
    ready = indexed_count > 0 and not stale
    return {
        "book_id": book_id,
        "ready": ready,
        "indexed_count": indexed_count,
        "expected_count": len(expected_keys),
        "missing_sources": [f"{source_type}:{source_ref_id}" for source_type, source_ref_id in missing_keys[:20]],
        "source_types": source_types,
        "models": models,
        "dimensions": dimensions,
        "latest_embedding_at": latest_embedding_at.isoformat() if latest_embedding_at else "",
        "latest_chapter_version_at": latest_chapter_version_at.isoformat() if latest_chapter_version_at else "",
        "stale_by_time": stale_by_time,
        "stale": stale,
    }


def run_agent_plan_enhancement_cycle(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
    market_query: str = "",
    platform: str = "",
    dry_run: bool = True,
    rebuild_memory: bool = True,
    create_visuals: bool = True,
    auto_market_search: bool = False,
) -> dict:
    book = _book(session, book_id)
    query = market_query.strip() or f"{book.target_platform or '番茄小说'} {book.genre or '网文'} 最新爆款 趋势 开篇 卖点 避雷"
    target_platform = platform.strip() or book.target_platform or "番茄小说"
    steps: list[dict[str, Any]] = []

    market_step = ensure_market_research_evidence(
        session,
        genre=book.genre or "未分类",
        query=query,
        platform=target_platform,
        auto_search=auto_market_search,
    )
    research_pack = market_step.get("research_pack") or {}
    steps.append(market_step["step"])

    memory_before = summarize_semantic_memory(session, book_id=book_id)
    memory_after = memory_before
    if rebuild_memory:
        index_result = index_book_knowledge(session, book_id=book_id, dry_run=dry_run, reset=True)
        memory_after = summarize_semantic_memory(session, book_id=book_id)
        steps.append(
            {
                "name": "semantic_memory",
                "status": "rebuilt",
                "indexed_count": index_result["indexed_count"],
                "dry_run": dry_run,
            }
        )
    else:
        steps.append({"name": "semantic_memory", "status": "skipped", "indexed_count": memory_before["indexed_count"]})

    visual_asset_ids: list[int] = []
    if create_visuals:
        cover = create_visual_asset(session, book_id=book_id, asset_type="cover", dry_run=True)
        visual_asset_ids.append(cover.id)
        chapter_asset = None
        if chapter_number:
            chapter_asset = create_visual_asset(
                session,
                book_id=book_id,
                asset_type="chapter_illustration",
                chapter_number=chapter_number,
                dry_run=True,
            )
            visual_asset_ids.append(chapter_asset.id)
        steps.append(
            {
                "name": "visual_assets",
                "status": "planned",
                "asset_ids": visual_asset_ids,
                "chapter_number": chapter_number,
            }
        )
    else:
        steps.append({"name": "visual_assets", "status": "skipped", "asset_ids": []})

    return {
        "book_id": book_id,
        "book_title": book.title,
        "chapter_number": chapter_number,
        "dry_run": dry_run,
        "steps": steps,
        "market_research_artifact": research_pack.get("artifact_path", ""),
        "market_research": market_step,
        "semantic_memory_before": memory_before,
        "semantic_memory_after": memory_after,
        "visual_asset_ids": visual_asset_ids,
        "next_actions": [
            "Market evidence is checked before production; if auto search is unavailable, run the generated search pack and import JSON.",
            "Use semantic-memory-status before long production runs; rebuild if stale.",
            "Open visual asset artifacts and pass prompts to the Agent Plan image model when ready.",
        ],
    }


def ensure_market_research_evidence(
    session: Session,
    *,
    genre: str,
    query: str,
    platform: str = "番茄小说",
    auto_search: bool = False,
    freshness_days: int = 14,
    min_signals: int = 3,
) -> dict:
    genre = genre or "未分类"
    recent_count = _recent_market_signal_count(session, genre=genre, freshness_days=freshness_days)
    if recent_count >= max(1, min_signals):
        return {
            "step": {
                "name": "market_research",
                "status": "fresh",
                "provider": "cached",
                "recent_signal_count": recent_count,
                "freshness_days": freshness_days,
            },
            "research_pack": {},
        }
    if auto_search:
        try:
            from app.services.web_search import run_market_web_search

            search = run_market_web_search(query=query, provider="auto", max_results=5, search_depth="basic")
            ingest_result = ingest_market_research_results(
                session,
                genre=genre,
                result_json=search.result_json,
                source_prefix=f"{search.provider}-search",
            )
            return {
                "step": {
                    "name": "market_research",
                    "status": "searched",
                    "provider": search.provider,
                    "used_credits": search.used_credits,
                    "created_signal_count": len(ingest_result.get("market_signal_ids") or []),
                    "recent_signal_count_before": recent_count,
                },
                "search": search.to_dict(),
                "ingest": ingest_result,
                "research_pack": {},
            }
        except (RuntimeError, httpx.HTTPError, ValueError) as exc:
            session.rollback()
            pack = create_market_research_pack(session, genre=genre, query=query, platform=platform)
            return {
                "step": {
                    "name": "market_research",
                    "status": "manual_pack_created",
                    "provider": "agent_plan_manual",
                    "reason": str(exc),
                    "artifact_path": pack["artifact_path"],
                    "query_count": len(pack["queries"]),
                    "recent_signal_count_before": recent_count,
                },
                "research_pack": pack,
            }
    pack = create_market_research_pack(session, genre=genre, query=query, platform=platform)
    return {
        "step": {
            "name": "market_research",
            "status": "manual_pack_created",
            "provider": "agent_plan_manual",
            "artifact_path": pack["artifact_path"],
            "query_count": len(pack["queries"]),
            "recent_signal_count_before": recent_count,
        },
        "research_pack": pack,
    }


def create_visual_asset(
    session: Session,
    *,
    book_id: int,
    asset_type: str,
    chapter_number: int | None = None,
    style: str = "",
    dry_run: bool = True,
) -> VisualAsset:
    book = _book(session, book_id)
    chapter = _chapter(session, book_id=book_id, chapter_number=chapter_number) if chapter_number else None
    prompt = _visual_prompt(session, book=book, chapter=chapter, asset_type=asset_type, style=style)
    metadata = {
        "book_title": book.title,
        "genre": book.genre,
        "target_platform": book.target_platform,
        "chapter_number": chapter.chapter_number if chapter else None,
        "style": style,
        "mode": "prompt_artifact" if dry_run else "live_requested",
        "agent_plan_models": {
            "vision": settings.ark_vision_model,
            "image": settings.ark_image_model,
            "video": settings.ark_video_model,
        },
    }
    path = _write_artifact("visual_assets", f"{book.id}-{asset_type}-{chapter_number or 'book'}-{_stamp()}.json", {"prompt": prompt, **metadata})
    asset = VisualAsset(
        book_id=book_id,
        chapter_id=chapter.id if chapter else None,
        asset_type=asset_type,
        prompt=prompt,
        model=settings.ark_image_model,
        status="planned" if dry_run else "pending_external_generation",
        artifact_path=str(path),
        metadata_json=json.dumps(metadata, ensure_ascii=False),
    )
    session.add(asset)
    session.flush()
    return asset


def list_visual_assets(session: Session, *, book_id: int, asset_type: str = "", limit: int = 20) -> list[VisualAsset]:
    stmt = select(VisualAsset).where(VisualAsset.book_id == book_id).order_by(VisualAsset.id.desc()).limit(limit)
    if asset_type:
        stmt = stmt.where(VisualAsset.asset_type == asset_type)
    return list(session.scalars(stmt))


def _recent_market_signal_count(session: Session, *, genre: str, freshness_days: int) -> int:
    threshold = datetime.now() - timedelta(days=max(1, int(freshness_days or 14)))
    return int(
        session.scalar(
            select(func.count(MarketSignal.id)).where(
                MarketSignal.genre == genre,
                MarketSignal.confidence >= 60,
                MarketSignal.created_at >= threshold,
            )
        )
        or 0
    )


def _embed_text(text: str, *, dry_run: bool) -> list[float]:
    text = text.strip()
    if not text:
        return []
    if dry_run:
        return _hash_embedding(text)
    if not settings.ark_agent_plan_api_key:
        raise RuntimeError("ARK_AGENT_PLAN_API_KEY is required for live embedding calls")
    url = settings.ark_base_url.rstrip("/") + "/embeddings/multimodal"
    response = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {settings.ark_agent_plan_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.ark_embedding_model,
            "encoding_format": "float",
            "dimensions": 1024,
            "input": [{"type": "text", "text": text[:12000]}],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json().get("data")
    if isinstance(data, list) and data:
        embedding = data[0].get("embedding") if isinstance(data[0], dict) else getattr(data[0], "embedding", None)
    elif isinstance(data, dict):
        embedding = data.get("embedding")
    else:
        embedding = None
    if not isinstance(embedding, list):
        raise ValueError("embedding response did not contain data.embedding")
    return [float(item) for item in embedding]


def _knowledge_chunks(session: Session, *, book: Book, limit_chapters: int) -> list[dict[str, str]]:
    chunks: list[dict[str, str]] = []
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book.id).order_by(StoryFoundation.id.desc()))
    if foundation:
        chunks.append(_chunk("foundation", str(foundation.id), "Story Foundation", foundation.premise + "\n" + foundation.reader_promise))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book.id).order_by(StoryBible.id.desc()))
    if bible:
        chunks.append(
            _chunk(
                "story_bible",
                str(bible.id),
                "Story Bible",
                "\n".join(
                    [
                        bible.positioning,
                        bible.reader_promise,
                        bible.main_plot,
                        bible.protagonist_arc,
                        bible.relationship_arc,
                        bible.power_curve,
                        strip_aesthetic_profile_blocks(bible.forbidden_rules),
                        strip_aesthetic_profile_blocks(bible.style_guide),
                    ]
                ),
            )
        )
    for character in session.scalars(select(Character).where(Character.book_id == book.id).order_by(Character.id)):
        chunks.append(_chunk("character", str(character.id), character.name, f"{character.role}\n{character.personality}\n{character.ability}\n{character.background}"))
    for rule in session.scalars(select(WorldRule).where(WorldRule.book_id == book.id).order_by(WorldRule.id)):
        chunks.append(_chunk("world_rule", str(rule.id), rule.category, rule.rule_text))
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.book_id == book.id).order_by(Chapter.chapter_number).limit(limit_chapters)
        )
    )
    for chapter in chapters:
        latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
        text = "\n".join([chapter.title, chapter.summary, latest.content[:3000] if latest else ""]).strip()
        if text:
            chunks.append(_chunk("chapter", str(chapter.id), f"chapter {chapter.chapter_number}", text))
    for feedback in session.scalars(select(PlatformFeedback).where(PlatformFeedback.book_id == book.id).order_by(PlatformFeedback.id.desc()).limit(80)):
        chunks.append(_chunk("feedback", str(feedback.id), f"{feedback.platform}:{feedback.metric_name}", feedback.raw_text or feedback.metric_value))
    return [chunk for chunk in chunks if chunk["text"].strip()]


def _visual_prompt(session: Session, *, book: Book, chapter: Chapter | None, asset_type: str, style: str) -> str:
    canon, _ = format_canon_context(session, book_id=book.id, chapter_number=chapter.chapter_number if chapter else None, limit=8)
    chapter_text = ""
    if chapter:
        latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
        chapter_text = f"章节：第{chapter.chapter_number}章 {chapter.title}\n摘要：{chapter.summary}\n正文片段：{latest.content[:1200] if latest else ''}"
    target = "书籍封面" if asset_type == "cover" else "章节插图"
    style_line = style or "番茄小说男频商业封面质感，强主体，清晰光影，移动端小图可读"
    return "\n".join(
        [
            f"为《{book.title}》生成{target}视觉方案。",
            f"类型：{book.genre}；平台：{book.target_platform}。",
            f"视觉风格：{style_line}。",
            "必须遵守 Canon，不得添加未登记的核心设定、武器、人物关系或能力。",
            "画面要求：主体明确，构图有冲突压力，避免廉价 AI 感，避免文字乱码，避免过度堆元素。",
            "Canon 摘要：",
            canon[:2400],
            chapter_text,
            "输出应适合用于 Agent Plan 生图模型 prompt。",
        ]
    ).strip()


def _book(session: Session, book_id: int) -> Book:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    return book


def _chapter(session: Session, *, book_id: int, chapter_number: int | None) -> Chapter | None:
    if not chapter_number:
        return None
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError(f"chapter not found: {chapter_number}")
    return chapter


def _chunk(source_type: str, source_ref_id: str, source_label: str, text: str) -> dict[str, str]:
    return {"source_type": source_type, "source_ref_id": source_ref_id, "source_label": source_label, "text": text.strip()}


def _upsert_evidence_source(session: Session, *, source_key: str, title: str, url: str, reliability: int) -> EvidenceSource:
    source = session.scalar(select(EvidenceSource).where(EvidenceSource.source_id == source_key))
    if source:
        source.title = title or source.title
        source.url = url or source.url
        source.reliability = reliability
        source.status = "verified"
        session.flush()
        return source
    source = EvidenceSource(source_id=source_key, title=title, url=url, reliability=reliability, status="verified")
    session.add(source)
    session.flush()
    return source


def _hash_embedding(text: str, dimensions: int = 128) -> list[float]:
    values: list[float] = []
    seed = text.encode("utf-8")
    counter = 0
    while len(values) < dimensions:
        digest = hashlib.sha256(seed + str(counter).encode("ascii")).digest()
        for byte in digest:
            values.append((byte / 127.5) - 1.0)
            if len(values) >= dimensions:
                break
        counter += 1
    norm = math.sqrt(sum(item * item for item in values)) or 1.0
    return [item / norm for item in values]


def _cosine(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[i] * right[i] for i in range(size))
    left_norm = math.sqrt(sum(left[i] * left[i] for i in range(size))) or 1.0
    right_norm = math.sqrt(sum(right[i] * right[i] for i in range(size))) or 1.0
    return dot / (left_norm * right_norm)


def _loads_vector(raw: str) -> list[float]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [float(item) for item in data if isinstance(item, (int, float))]


def _loads_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _book_examples_for_genre(session: Session, *, genre: str) -> list[str]:
    books = list(session.scalars(select(Book).where(Book.genre == genre).order_by(Book.id.desc()).limit(5)))
    return [book.title for book in books]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = item.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _write_artifact(kind: str, filename: str, payload: dict) -> Path:
    directory = settings.outputs_dir / "agent_plan" / kind
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", value.strip())
    return cleaned.strip("-")[:80] or "item"


def _short_hash(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def _stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%d-%H%M%S")
