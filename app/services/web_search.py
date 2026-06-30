from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class WebSearchResult:
    provider: str
    status: str
    query: str
    result_json: str
    used_credits: int
    usage: dict[str, Any]
    raw: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "status": self.status,
            "query": self.query,
            "result_json": self.result_json,
            "used_credits": self.used_credits,
            "usage": self.usage,
            "raw": self.raw,
        }


def run_market_web_search(
    *,
    query: str,
    provider: str = "auto",
    max_results: int = 5,
    search_depth: str = "basic",
) -> WebSearchResult:
    query = query.strip()
    if not query:
        raise ValueError("search query is required")
    providers = _provider_order(provider)
    errors: list[str] = []
    for candidate in providers:
        if candidate == "tavily":
            if not settings.tavily_api_key:
                errors.append("tavily_missing_key")
                continue
            if not _within_monthly_budget("tavily", settings.tavily_search_monthly_limit, cost=1 if search_depth != "advanced" else 2):
                errors.append("tavily_monthly_limit_reached")
                continue
            try:
                result = _search_tavily(query=query, max_results=max_results, search_depth=search_depth)
                _record_search_usage("tavily", result.used_credits)
                return result
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                errors.append(f"tavily_http_{status}")
                if status not in {429, 432, 433}:
                    raise
            except httpx.HTTPError as exc:
                errors.append(f"tavily_network:{exc.__class__.__name__}")
                continue
        elif candidate == "agent_plan":
            if not settings.ark_search_api_key:
                errors.append("agent_plan_missing_key")
                continue
            if not settings.ark_search_base_url:
                errors.append("agent_plan_search_base_url_missing")
                continue
            if not _within_monthly_budget("agent_plan", settings.agent_plan_search_monthly_limit, cost=1):
                errors.append("agent_plan_monthly_limit_reached")
                continue
            try:
                result = _search_agent_plan(query=query, max_results=max_results, search_depth=search_depth)
                _record_search_usage("agent_plan", result.used_credits)
                return result
            except httpx.HTTPStatusError as exc:
                errors.append(f"agent_plan_http_{exc.response.status_code}")
                continue
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"agent_plan_error:{exc.__class__.__name__}")
                continue
        elif candidate == "agent_plan_manual":
            errors.append("agent_plan_search_is_manual_pack")
            continue
        elif candidate in {"manual", "pack"}:
            errors.append("manual_provider")
            continue
        else:
            errors.append(f"unknown_provider:{candidate}")
    raise RuntimeError("no_live_search_provider_available: " + ", ".join(errors))


def web_search_status() -> dict[str, Any]:
    return {
        "provider_order": _provider_order("auto"),
        "providers": {
            "tavily": {
                "configured": bool(settings.tavily_api_key),
                "monthly_limit": settings.tavily_search_monthly_limit,
                "used_this_month": _usage_count("tavily"),
            },
            "agent_plan_manual": {
                "configured": bool(settings.ark_search_api_key),
                "live_configured": bool(settings.ark_search_api_key and settings.ark_search_base_url),
                "monthly_limit": settings.agent_plan_search_monthly_limit,
                "used_this_month": _usage_count("agent_plan"),
            },
        },
    }


def _search_tavily(*, query: str, max_results: int, search_depth: str) -> WebSearchResult:
    max_results = max(1, min(10, int(max_results or 5)))
    depth = search_depth if search_depth in {"basic", "advanced", "fast", "ultra-fast"} else "basic"
    response = httpx.post(
        "https://api.tavily.com/search",
        headers={
            "Authorization": f"Bearer {settings.tavily_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "search_depth": depth,
            "topic": "general",
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "auto_parameters": False,
        },
        timeout=45,
    )
    response.raise_for_status()
    raw = response.json()
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    used_credits = int(usage.get("credits") or (2 if depth == "advanced" else 1))
    normalized = {
        "results": [
            {
                "title": str(item.get("title") or "Tavily search source").strip(),
                "url": str(item.get("url") or "").strip(),
                "snippet": str(item.get("content") or "").strip(),
                "signals": _signals_from_tavily_item(item),
                "reliability": 3,
                "confidence": _confidence_from_score(item.get("score")),
            }
            for item in raw.get("results", [])
            if isinstance(item, dict)
        ]
    }
    return WebSearchResult(
        provider="tavily",
        status="completed",
        query=query,
        result_json=json.dumps(normalized, ensure_ascii=False),
        used_credits=used_credits,
        usage=usage,
        raw={"request_id": raw.get("request_id"), "response_time": raw.get("response_time")},
    )


def _search_agent_plan(*, query: str, max_results: int, search_depth: str) -> WebSearchResult:
    max_results = max(1, min(10, int(max_results or 5)))
    response = httpx.post(
        settings.ark_search_base_url,
        headers={
            "Authorization": f"Bearer {settings.ark_search_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "query": query,
            "max_results": max_results,
            "search_depth": search_depth,
            "expected_schema": {
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
        },
        timeout=60,
    )
    response.raise_for_status()
    raw = response.json()
    rows = _extract_agent_plan_search_rows(raw)
    normalized = {
        "results": [
            {
                "title": str(item.get("title") or item.get("name") or "Agent Plan search source").strip(),
                "url": str(item.get("url") or item.get("link") or "").strip(),
                "snippet": str(item.get("snippet") or item.get("summary") or item.get("content") or "").strip(),
                "signals": _agent_plan_signals(item),
                "reliability": _bounded_int(item.get("reliability"), default=3, low=1, high=5),
                "confidence": _bounded_int(item.get("confidence") or item.get("score"), default=70, low=0, high=100),
            }
            for item in rows[:max_results]
            if isinstance(item, dict)
        ]
    }
    return WebSearchResult(
        provider="agent_plan",
        status="completed",
        query=query,
        result_json=json.dumps(normalized, ensure_ascii=False),
        used_credits=1,
        usage=raw.get("usage") if isinstance(raw.get("usage"), dict) else {},
        raw={"request_id": raw.get("request_id") or raw.get("id") or "", "raw_result_count": len(rows)},
    )


def _extract_agent_plan_search_rows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = [
        raw.get("results"),
        raw.get("items"),
        raw.get("data"),
        raw.get("sources"),
    ]
    data = raw.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("results"), data.get("items"), data.get("sources")])
    for candidate in candidates:
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
    raise ValueError("Agent Plan search response did not contain a results list")


def _agent_plan_signals(item: dict[str, Any]) -> list[str]:
    signals = item.get("signals")
    if isinstance(signals, str):
        return [signals]
    if isinstance(signals, list):
        return [str(value).strip() for value in signals if str(value).strip()]
    text = "；".join(
        part
        for part in [
            str(item.get("title") or item.get("name") or "").strip(),
            str(item.get("snippet") or item.get("summary") or item.get("content") or "").strip()[:300],
        ]
        if part
    )
    return [text] if text else []


def _bounded_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _signals_from_tavily_item(item: dict[str, Any]) -> list[str]:
    title = str(item.get("title") or "").strip()
    content = str(item.get("content") or "").strip()
    text = "；".join(part for part in [title, content[:300]] if part)
    return [text] if text else []


def _confidence_from_score(score: Any) -> int:
    try:
        value = float(score)
    except (TypeError, ValueError):
        return 70
    return max(50, min(95, int(value * 100)))


def _provider_order(provider: str) -> list[str]:
    value = (provider or "auto").strip().lower()
    if value and value != "auto":
        return [value]
    return [
        item.strip().lower()
        for item in settings.web_search_provider_order.split(",")
        if item.strip()
    ] or ["tavily", "agent_plan_manual"]


def _within_monthly_budget(provider: str, monthly_limit: int, *, cost: int) -> bool:
    limit = int(monthly_limit or 0)
    return limit <= 0 or _usage_count(provider) + max(1, int(cost or 1)) <= limit


def _usage_count(provider: str) -> int:
    data = _usage_data()
    return int(data.get(provider, 0) or 0)


def _record_search_usage(provider: str, credits: int) -> None:
    data = _usage_data()
    data[provider] = int(data.get(provider, 0) or 0) + max(1, int(credits or 1))
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _usage_data() -> dict[str, int]:
    path = _usage_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _usage_path() -> Path:
    return settings.outputs_dir / "web_search_usage" / f"{datetime.now().strftime('%Y-%m')}.json"
