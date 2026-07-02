"""Live-LLM production baseline aggregator.

Reads the audit tables (llm_request_logs, chapter_versions, quality_reports,
platform_feedback) and prints a per-chapter production profile plus an
aggregated summary.

Does NOT create new tables — everything is derived from existing audit records.

Metrics per chapter:
  - versions:            total ChapterVersion rows
  - first_pass_version:  smallest version_number whose QualityReport.passed=True
  - final_score:         score of the latest QualityReport
  - verdict:             three-tier verdict of the latest QualityReport
  - early_stopped:       any PlatformFeedback metric_name='revision_early_stop'
  - final_chars:         char count of the final content
  - tokens_prompt/resp/total: sum over all LLMRequestLog rows for the chapter
  - elapsed_seconds:     sum over all LLMRequestLog.elapsed_ms / 1000
  - est_cost_cny:        billable_tokens × price_per_1m (from settings)

Aggregate rollup:
  - avg / p50 / p95 of each metric across the sampled chapters
  - first_pass_rate:     fraction of chapters where first_pass_version=1
  - avg_revision_versions: avg versions
  - projected_daily_10k:   externalize to 3 books × 10k chars/day (chars/final_chars ratio)
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func

from app.core.config import settings
from app.db.session import configure_database, session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterVersion,
    QualityReport,
    LLMRequestLog,
    PlatformFeedback,
    GenerationTask,
)


@dataclass
class ChapterProfile:
    chapter_number: int
    chapter_id: int
    versions: int
    first_pass_version: int | None
    final_score: int | None
    final_verdict: str
    passed: bool
    early_stopped: bool
    final_chars: int
    prompt_tokens: int
    response_tokens: int
    total_tokens: int
    elapsed_seconds: float
    est_cost_cny: float
    llm_requests: int


def _verdict_from_report(report_json: str) -> str:
    try:
        return json.loads(report_json).get("verdict", "")
    except Exception:
        return ""


def collect_chapter_profile(session, chapter: Chapter) -> ChapterProfile:
    versions = list(
        session.execute(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter.id)
            .order_by(ChapterVersion.version_number)
        ).scalars()
    )
    if not versions:
        return ChapterProfile(
            chapter_number=chapter.chapter_number,
            chapter_id=chapter.id,
            versions=0,
            first_pass_version=None,
            final_score=None,
            final_verdict="",
            passed=False,
            early_stopped=False,
            final_chars=0,
            prompt_tokens=0,
            response_tokens=0,
            total_tokens=0,
            elapsed_seconds=0.0,
            est_cost_cny=0.0,
            llm_requests=0,
        )

    version_ids = [v.id for v in versions]

    # Quality reports keyed by version_number
    qr_rows = session.execute(
        select(QualityReport)
        .where(QualityReport.chapter_version_id.in_(version_ids))
        .order_by(QualityReport.created_at)
    ).scalars().all()

    # Build version_number -> latest QR
    version_num_by_id = {v.id: v.version_number for v in versions}
    qr_by_version_num: dict[int, QualityReport] = {}
    for qr in qr_rows:
        vnum = version_num_by_id.get(qr.chapter_version_id)
        if vnum is not None:
            qr_by_version_num[vnum] = qr  # last-write-wins by created_at order

    first_pass_version = next(
        (n for n, qr in sorted(qr_by_version_num.items()) if qr.passed),
        None,
    )
    latest_version = versions[-1]
    latest_qr = qr_by_version_num.get(latest_version.version_number)
    final_score = latest_qr.score if latest_qr else None
    final_verdict = _verdict_from_report(latest_qr.report) if latest_qr else ""
    passed = bool(latest_qr and latest_qr.passed)

    early_stopped = session.execute(
        select(func.count(PlatformFeedback.id))
        .where(
            PlatformFeedback.chapter_id == chapter.id,
            PlatformFeedback.metric_name == "revision_early_stop",
        )
    ).scalar_one() > 0

    # LLM usage — attribute by chapter_number stored in GenerationTask.input_json.
    # (GenerationTask does not have chapter_id FK; the chapter number is in input_json.)
    import json as _json
    task_rows = session.execute(
        select(GenerationTask.id, GenerationTask.input_json)
        .where(GenerationTask.book_id == chapter.book_id)
    ).all()
    task_ids: list[int] = []
    for tid, input_json in task_rows:
        try:
            data = _json.loads(input_json or "{}")
        except Exception:
            continue
        cn = data.get("chapter_number")
        if cn is None:
            cn = data.get("chapter_id")  # fallback
        if cn == chapter.chapter_number:
            task_ids.append(tid)

    if task_ids:
        usage_row = session.execute(
            select(
                func.coalesce(func.sum(LLMRequestLog.actual_prompt_tokens), 0),
                func.coalesce(func.sum(LLMRequestLog.actual_response_tokens), 0),
                func.coalesce(func.sum(LLMRequestLog.actual_total_tokens), 0),
                func.coalesce(func.sum(LLMRequestLog.elapsed_ms), 0),
                func.count(LLMRequestLog.id),
            ).where(LLMRequestLog.generation_task_id.in_(task_ids))
        ).one()
        prompt_tokens, response_tokens, total_tokens, elapsed_ms, requests = usage_row
    else:
        prompt_tokens = response_tokens = total_tokens = elapsed_ms = requests = 0

    input_price = settings.llm_input_price_per_1m_tokens
    output_price = settings.llm_output_price_per_1m_tokens
    est_cost = (
        prompt_tokens / 1_000_000 * input_price
        + response_tokens / 1_000_000 * output_price
    )

    return ChapterProfile(
        chapter_number=chapter.chapter_number,
        chapter_id=chapter.id,
        versions=len(versions),
        first_pass_version=first_pass_version,
        final_score=final_score,
        final_verdict=final_verdict,
        passed=passed,
        early_stopped=early_stopped,
        final_chars=len(latest_version.content or ""),
        prompt_tokens=int(prompt_tokens),
        response_tokens=int(response_tokens),
        total_tokens=int(total_tokens),
        elapsed_seconds=round(int(elapsed_ms) / 1000, 2),
        est_cost_cny=round(est_cost, 4),
        llm_requests=int(requests),
    )


def build_report(session, *, book_id: int) -> dict:
    book = session.get(Book, book_id)
    if not book:
        raise SystemExit(f"book_id={book_id} not found")

    chapters = list(
        session.execute(
            select(Chapter)
            .where(Chapter.book_id == book_id)
            .order_by(Chapter.chapter_number)
        ).scalars()
    )

    profiles = [collect_chapter_profile(session, ch) for ch in chapters]
    profiled = [p for p in profiles if p.versions > 0]

    def _agg(values: list[float], key: str) -> dict:
        if not values:
            return {"n": 0}
        return {
            "n": len(values),
            "min": min(values),
            "avg": round(statistics.mean(values), 2),
            "p50": round(statistics.median(values), 2),
            "max": max(values),
        }

    aggregate = {
        "chapters_with_versions": len(profiled),
        "versions": _agg([p.versions for p in profiled], "versions"),
        "first_pass_rate": round(
            sum(1 for p in profiled if p.first_pass_version == 1) / len(profiled), 3
        ) if profiled else 0.0,
        "eventual_pass_rate": round(
            sum(1 for p in profiled if p.passed) / len(profiled), 3
        ) if profiled else 0.0,
        "final_score": _agg([p.final_score for p in profiled if p.final_score is not None], "final_score"),
        "final_chars": _agg([p.final_chars for p in profiled], "final_chars"),
        "tokens_total": _agg([p.total_tokens for p in profiled], "tokens_total"),
        "elapsed_seconds": _agg([p.elapsed_seconds for p in profiled], "elapsed_seconds"),
        "est_cost_cny": _agg([p.est_cost_cny for p in profiled], "est_cost_cny"),
        "llm_requests": _agg([p.llm_requests for p in profiled], "llm_requests"),
    }

    # 外推：3 本书日更 10000 字
    if profiled:
        avg_chars = aggregate["final_chars"]["avg"]
        avg_cost = aggregate["est_cost_cny"]["avg"]
        avg_seconds = aggregate["elapsed_seconds"]["avg"]
        avg_tokens = aggregate["tokens_total"]["avg"]
        chapters_per_book_per_day = max(1, round(10000 / avg_chars)) if avg_chars > 0 else 0
        projected = {
            "target": "3 books × 10000 chars/day",
            "chapters_per_book_per_day": chapters_per_book_per_day,
            "chapters_per_day_total": chapters_per_book_per_day * 3,
            "cost_per_book_per_day_cny": round(avg_cost * chapters_per_book_per_day, 2),
            "cost_per_day_total_cny": round(avg_cost * chapters_per_book_per_day * 3, 2),
            "cost_per_month_cny": round(avg_cost * chapters_per_book_per_day * 3 * 30, 2),
            "wall_seconds_per_book_per_day": round(avg_seconds * chapters_per_book_per_day, 1),
            "tokens_per_day_total": int(avg_tokens * chapters_per_book_per_day * 3),
        }
    else:
        projected = {}

    return {
        "book": {"id": book.id, "title": book.title, "platform": book.target_platform},
        "model": settings.model_name,
        "draft_model": getattr(settings, "llm_draft_model", ""),
        "input_price_per_1m": settings.llm_input_price_per_1m_tokens,
        "output_price_per_1m": settings.llm_output_price_per_1m_tokens,
        "chapters": [asdict(p) for p in profiles],
        "aggregate": aggregate,
        "projection": projected,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--book-id", type=int, required=True)
    parser.add_argument("--json", action="store_true", help="output json only")
    parser.add_argument("--output", type=str, default="", help="also write json to file")
    args = parser.parse_args()

    configure_database(settings.database_url)
    with session_scope() as session:
        report = build_report(session, book_id=args.book_id)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")

    if args.json:
        print(text)
        return 0

    # Human-readable output
    print(f"=== Production Baseline · book={report['book']['title']} (id={report['book']['id']}) ===")
    print(f"Model: {report['draft_model'] or report['model']}")
    print(f"Price: in ${report['input_price_per_1m']}/1M · out ${report['output_price_per_1m']}/1M")
    print()
    print(f"{'ch':>3} {'ver':>4} {'1st':>4} {'sc':>4} {'verdict':>10} {'ES':>3} {'chars':>6} {'tokens':>7} {'sec':>7} {'cny':>7} {'req':>4}")
    print("-" * 78)
    for p in report["chapters"]:
        print(
            f"{p['chapter_number']:>3} {p['versions']:>4} "
            f"{p['first_pass_version'] or '-':>4} {p['final_score'] or '-':>4} "
            f"{p['final_verdict']:>10} {'Y' if p['early_stopped'] else '-':>3} "
            f"{p['final_chars']:>6} {p['total_tokens']:>7} "
            f"{p['elapsed_seconds']:>7.1f} {p['est_cost_cny']:>7.4f} {p['llm_requests']:>4}"
        )
    print()
    agg = report["aggregate"]
    print("=== Aggregate ===")
    print(f"chapters_with_versions:  {agg['chapters_with_versions']}")
    print(f"first_pass_rate:         {agg['first_pass_rate']} (first version passes quality gate)")
    print(f"eventual_pass_rate:      {agg['eventual_pass_rate']} (any version passes)")
    for k in ("versions", "final_score", "final_chars", "tokens_total", "elapsed_seconds", "est_cost_cny", "llm_requests"):
        v = agg.get(k, {})
        if v.get("n"):
            print(f"{k:22s} min={v['min']} avg={v['avg']} p50={v['p50']} max={v['max']}")
    print()
    if report["projection"]:
        pj = report["projection"]
        print(f"=== Projection · {pj['target']} ===")
        print(f"chapters/book/day:       {pj['chapters_per_book_per_day']}")
        print(f"chapters/day total:      {pj['chapters_per_day_total']}")
        print(f"cost/book/day (CNY):     {pj['cost_per_book_per_day_cny']}")
        print(f"cost/day total (CNY):    {pj['cost_per_day_total_cny']}")
        print(f"cost/month (CNY):        {pj['cost_per_month_cny']}")
        print(f"wall-sec/book/day:       {pj['wall_seconds_per_book_per_day']} (sequential)")
        print(f"tokens/day total:        {pj['tokens_per_day_total']:,}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
