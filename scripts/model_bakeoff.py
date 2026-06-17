from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_scope
from app.llm.providers import ArkOpenAIProvider
from app.models.entities import Book, Chapter, ChapterBrief
from app.services.canon import format_canon_context
from app.services.quality import evaluate_chapter


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "outputs" / "model_bakeoff"

DEFAULT_MODELS = [
    "deepseek-v4-pro",
    "deepseek-v4-pro-260425",
    "deepseek-v4-flash",
    "deepseek-v4-flash-260425",
    "doubao-seed-2.0-pro",
    "doubao-seed-2.0-lite",
    "glm-4-7-251222",
    "deepseek-v3-2-251201",
    "minimax-m2.7",
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a controlled live model bakeoff for chapter prose quality.")
    parser.add_argument("--book-id", type=int, required=True)
    parser.add_argument("--chapter-number", type=int, default=1)
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--id-check-only", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=1800)
    parser.add_argument("--temperature", type=float, default=0.55)
    parser.add_argument("--min-chars", type=int, default=700)
    parser.add_argument("--limit-models", type=int, default=0)
    parser.add_argument("--request-timeout-seconds", type=float, default=90)
    args = parser.parse_args()

    models = [item.strip() for item in args.models.split(",") if item.strip()]
    if args.limit_models:
        models = models[: args.limit_models]
    if not models:
        raise SystemExit("no models provided")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    provider = ArkOpenAIProvider(timeout=args.request_timeout_seconds)
    id_results = [_check_model_id(provider, model) for model in models]
    available = [item["model"] for item in id_results if item["status"] == "ok"]

    path = OUT_DIR / (f"id-check-{stamp}.json" if args.id_check_only else f"bakeoff-{stamp}.json")
    payload: dict = {
        "book_id": args.book_id,
        "chapter_number": args.chapter_number,
        "base_url": settings.ark_base_url,
        "request_timeout_seconds": args.request_timeout_seconds,
        "id_results": id_results,
        "samples": [],
    }
    if args.id_check_only:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": "completed", "artifact": str(path), "available": available, "id_results": id_results}, ensure_ascii=False, indent=2))
        return 0

    with session_scope() as session:
        prompt_context = _prompt_context(session, book_id=args.book_id, chapter_number=args.chapter_number)
    for model in available:
        sample = _run_sample(
            provider,
            model=model,
            prompt_context=prompt_context,
            max_tokens=args.max_tokens,
            temperature=args.temperature,
            min_chars=args.min_chars,
        )
        payload["samples"].append(sample)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    payload["ranking"] = _ranking(payload["samples"])
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "completed", "artifact": str(path), "ranking": payload["ranking"], "id_results": id_results}, ensure_ascii=False, indent=2))
    return 0


def _check_model_id(provider: ArkOpenAIProvider, model: str) -> dict:
    prompt = "请只回答一个 JSON：{\"ok\":true}"
    started = time.perf_counter()
    try:
        response = provider.generate(prompt, max_tokens=40, temperature=0, model=model, response_format={"type": "json_object"})
    except Exception as exc:
        return {
            "model": model,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:500],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }
    return {
        "model": model,
        "status": "ok",
        "provider_model": response.model,
        "request_id": response.request_id,
        "actual_total_tokens": int((response.usage or {}).get("total_tokens") or 0),
        "estimated_total_tokens": response.estimated_prompt_tokens + response.estimated_response_tokens,
        "elapsed_ms": response.elapsed_ms,
        "text": response.text[:120],
    }


def _prompt_context(session, *, book_id: int, chapter_number: int) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    brief = None
    if chapter:
        brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    canon_context, _ = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    goal = brief.goal if brief else f"写第{chapter_number}章开篇样本，体现主角目标、压力、选择和代价。"
    required_beats = brief.required_beats if brief else "开场进入具体场景；主角做出行动；外部阻碍逼近；选择带来代价；章末留下自然钩子。"
    constraints = brief.constraints if brief else "不要写后台说明；不要生造怪名词；不要用系统奖励替代因果；代价必须具体可信。"
    prompt = f"""
你是中文男频网文作者。请基于同一生产 brief 写一个 900-1200 汉字的正文小样，用来测试模型正文能力。

作品：{book.title}
类型：{book.genre}
平台：{book.target_platform}
章节：第{chapter_number}章

章节目标：
{goal}

必写节拍：
{required_beats}

限制：
{constraints}

Canon/上下文：
{canon_context[:3000]}

输出要求：
- 只输出 JSON 对象，不要解释。
- JSON 字段：title, content。
- content 必须是正文，不要写大纲、分析、系统提示或自评。
- 语言要自然，避免怪异搭配；不要出现“靛蓝味”“拿嘴买命”等生硬表达。
- 代价要来自制度、利益、身体伤害或关系债，不能硬凑。
- 场景氛围必须影响人物判断或行动。
""".strip()
    return {
        "book_title": book.title,
        "genre": book.genre,
        "target_platform": book.target_platform,
        "goal": goal,
        "required_beats": required_beats,
        "constraints": constraints,
        "canon_context": canon_context,
        "prompt": prompt,
    }


def _run_sample(provider: ArkOpenAIProvider, *, model: str, prompt_context: dict, max_tokens: int, temperature: float, min_chars: int) -> dict:
    started = time.perf_counter()
    try:
        response = provider.generate(
            prompt_context["prompt"],
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"},
        )
        data = _loads_json_object(response.text)
        content = str(data.get("content") or response.text)
        quality = evaluate_chapter(
            content,
            min_chars=min_chars,
            max_chars=2600,
            goal=prompt_context["goal"],
            required_beats=prompt_context["required_beats"],
            constraints=prompt_context["constraints"],
            canon_context=prompt_context["canon_context"],
        )
        report = json.loads(quality.report)
        return {
            "model": model,
            "status": "completed",
            "title": str(data.get("title") or ""),
            "content": content,
            "raw_response": response.text[:12000],
            "content_chars": len(content),
            "quality_passed": quality.passed,
            "quality_score": quality.score,
            "key_dimensions": _key_dimensions(report.get("dimensions") or {}),
            "issues": quality.issues,
            "warnings": list(report.get("warnings") or [])[:20],
            "usage": {
                "actual_total_tokens": int((response.usage or {}).get("total_tokens") or 0),
                "estimated_total_tokens": response.estimated_prompt_tokens + response.estimated_response_tokens,
                "elapsed_ms": response.elapsed_ms or round((time.perf_counter() - started) * 1000),
                "request_id": response.request_id,
            },
        }
    except Exception as exc:
        return {
            "model": model,
            "status": "failed",
            "error_type": type(exc).__name__,
            "error": str(exc)[:1000],
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
        }


def _loads_json_object(text: str) -> dict:
    try:
        data = json.loads(_json_object_text(text))
        if isinstance(data, str):
            try:
                data = json.loads(_json_object_text(data))
            except json.JSONDecodeError:
                return {"content": data}
        return data if isinstance(data, dict) else {"content": text}
    except json.JSONDecodeError:
        return {"content": text}


def _json_object_text(value: str) -> str:
    text = value.strip()
    text = re.sub(r"^```(?:json)?", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text


def _key_dimensions(dimensions: dict) -> dict:
    keys = [
        "brief_coverage",
        "reader_momentum",
        "conflict_pressure",
        "choice_and_cost",
        "visual_staging",
        "narrative_logic",
        "cost_plausibility",
        "scene_atmosphere",
        "prose_voice",
        "expression_precision",
        "object_verb_collocation",
        "chapter_unit_flow",
        "writer_craft",
    ]
    return {key: dimensions.get(key) for key in keys if key in dimensions}


def _ranking(samples: list[dict]) -> list[dict]:
    rows = []
    for sample in samples:
        if sample.get("status") != "completed":
            rows.append({"model": sample.get("model"), "status": sample.get("status"), "score": -1, "reason": sample.get("error", "")[:120]})
            continue
        dims = sample.get("key_dimensions") or {}
        blocker_count = len(sample.get("issues") or [])
        score = int(sample.get("quality_score") or 0)
        score += min(8, int(dims.get("object_verb_collocation") or 0) // 12)
        score += min(6, int(dims.get("cost_plausibility") or 0) // 15)
        score -= blocker_count * 4
        rows.append(
            {
                "model": sample.get("model"),
                "status": sample.get("status"),
                "rank_score": score,
                "quality_score": sample.get("quality_score"),
                "quality_passed": sample.get("quality_passed"),
                "blocker_count": blocker_count,
                "actual_total_tokens": (sample.get("usage") or {}).get("actual_total_tokens", 0),
                "key_dimensions": dims,
            }
        )
    return sorted(rows, key=lambda item: item.get("rank_score", -1), reverse=True)


if __name__ == "__main__":
    raise SystemExit(main())
