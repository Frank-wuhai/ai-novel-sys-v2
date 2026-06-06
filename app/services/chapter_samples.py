from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import estimate_tokens, get_provider
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterReview,
    ChapterVersion,
    GenerationTask,
    PlatformFeedback,
    QualityReport,
    StoryFoundation,
)
from app.services.book_profile import build_book_profile
from app.services.feedback import REVISION_MODE_FRESH, submit_revision_suggestion
from app.services.llm_audit import record_llm_request
from app.services.llm_errors import classify_exception
from app.services.production_packet import build_chapter_production_packet
from app.services.writer_loop import sample_failure_director

TASK_TYPE_CHAPTER_SAMPLE = "chapter_sample_lab"
PROMPT_TEMPLATE = "chapter_sample_lab@v5"
SAMPLE_DIVERSITY_THRESHOLD = 65
USABLE_SAMPLE_THRESHOLD = 78
DEFAULT_SAMPLE_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class AdoptedChapterSample:
    feedback_id: int
    feedback_adjustment_id: int
    brief_id: int
    chapter_version_id: int | None
    chapter_version_status: str


@dataclass(frozen=True)
class SampleLearningSync:
    recorded_count: int
    preference_ids: list[int]


def generate_chapter_samples(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    sample_count: int = 3,
    focus: str = "opening",
    dry_run: bool = False,
    max_attempts: int = DEFAULT_SAMPLE_MAX_ATTEMPTS,
) -> GenerationTask:
    if sample_count < 1 or sample_count > 5:
        raise ValueError("sample_count must be between 1 and 5")
    max_attempts = max(1, min(5, int(max_attempts or DEFAULT_SAMPLE_MAX_ATTEMPTS)))
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError(f"chapter not found: {chapter_number}")
    brief = session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id).order_by(ChapterBrief.id.desc()))
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    prompt_context = _build_sample_prompt_context(
        session,
        book=book,
        chapter=chapter,
        brief=brief,
        foundation=foundation,
        sample_count=sample_count,
        focus=focus,
    )
    max_tokens = min(max(settings.llm_draft_max_tokens // 2, 3200), 5200)
    model = settings.llm_planning_model
    temperature = max(settings.llm_planning_temperature, 0.72)
    input_data = {
        "chapter_number": chapter_number,
        "dry_run": dry_run,
        "prompt_template": PROMPT_TEMPLATE,
        "sample_count": sample_count,
        "focus": focus,
        "max_attempts": max_attempts,
        "llm_parameters": {
            "provider_mode": "dry_run" if dry_run else "live",
            "requested_model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        "brief_id": brief.id if brief else None,
        "director_sheet": prompt_context["director_sheet"],
        "production_packet_audit": prompt_context.get("production_packet_audit", {}),
        "production_context_audit": prompt_context.get("production_context_audit", {}),
    }
    task = GenerationTask(
        book_id=book_id,
        task_type=TASK_TYPE_CHAPTER_SAMPLE,
        status="running",
        input_json=_dumps_json(input_data),
        output_json="{}",
    )
    session.add(task)
    session.flush()
    task_id = task.id
    session.commit()

    if dry_run:
        dry_samples = _dry_run_samples(book=book, chapter_number=chapter_number, sample_count=sample_count)
        dry_report = _sample_diversity_report(dry_samples)
        task = session.get(GenerationTask, task_id)
        task.status = "completed"
        task.output_json = _dumps_json(
            {
                "provider": "dry_run",
                "model": "dry-run",
                "samples": dry_samples,
                "diversity_report": dry_report,
                "gate_threshold": SAMPLE_DIVERSITY_THRESHOLD,
                "gate_passed": _sample_gate_passed(dry_report),
                "attempts": [],
                "failure_director": sample_failure_director(dry_report, chapter_number=chapter_number),
                "usage": {
                    "estimated_prompt_tokens": estimate_tokens(prompt_context["prompt"]),
                    "estimated_response_tokens": 0,
                },
            }
        )
        session.flush()
        return task

    try:
        provider = get_provider(False)
        attempts: list[dict] = []
        best_samples: list[dict] = []
        best_report: dict = {"score": 0, "status": "empty", "issues": ["missing_samples"]}
        best_response = None
        best_repair: dict = {}
        best_failure_director: dict = {}
        retry_feedback = ""
        base_prompt = prompt_context["prompt"]
        for attempt_index in range(1, max_attempts + 1):
            prompt = _prompt_with_retry_feedback(base_prompt, retry_feedback, attempt_index=attempt_index)
            response = provider.generate(
                prompt,
                max_tokens=max_tokens,
                temperature=min(0.95, temperature + (attempt_index - 1) * 0.06),
                model=model,
                response_format={"type": "json_object"},
            )
            data, repair = _parse_or_repair_sample_output(
                provider,
                response_text=response.text,
                original_prompt=prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                model=model,
            )
            samples = _normalize_samples(data.get("samples"), limit=sample_count)
            if not samples:
                raise ValueError("LLM did not return usable chapter samples")
            diversity_report = _sample_diversity_report(samples)
            if len(samples) < sample_count:
                diversity_report.setdefault("issues", []).append(f"sample_count_low:{len(samples)}<{sample_count}")
                diversity_report["status"] = "attention"
                diversity_report["score"] = max(0, int(diversity_report.get("score") or 0) - 8)
            gate_passed = _sample_gate_passed(diversity_report)
            group_usable = _sample_group_usable(diversity_report)
            attempts.append(
                {
                    "attempt": attempt_index,
                    "score": int(diversity_report.get("score") or 0),
                    "status": diversity_report.get("status", ""),
                    "gate_passed": gate_passed,
                    "usable": group_usable,
                    "recommended_sample_index": diversity_report.get("recommended_sample_index"),
                    "issues": list(diversity_report.get("issues") or [])[:8],
                    "repeated_motifs": list(diversity_report.get("repeated_motifs") or [])[:8],
                    "max_pair_overlap": diversity_report.get("max_pair_overlap"),
                    "request_id": response.request_id,
                }
            )
            if int(diversity_report.get("score") or 0) > int(best_report.get("score") or 0):
                best_samples = samples
                best_report = diversity_report
                best_response = response
                best_repair = repair or {}
                best_failure_director = sample_failure_director(diversity_report, chapter_number=chapter_number)
            record_llm_request(
                session,
                book_id=book_id,
                task_type=TASK_TYPE_CHAPTER_SAMPLE,
                generation_task_id=task_id,
                provider=response.provider,
                model=response.model,
                request_id=response.request_id,
                prompt_template=f"{PROMPT_TEMPLATE}#attempt{attempt_index}",
                prompt_chars=len(prompt),
                response_chars=len(response.text),
                estimated_prompt_tokens=response.estimated_prompt_tokens,
                estimated_response_tokens=response.estimated_response_tokens,
                actual_prompt_tokens=int((response.usage or {}).get("prompt_tokens") or 0),
                actual_response_tokens=int((response.usage or {}).get("completion_tokens") or 0),
                actual_total_tokens=int((response.usage or {}).get("total_tokens") or 0),
                elapsed_ms=response.elapsed_ms,
                status="completed" if gate_passed else ("usable" if group_usable else "needs_retry"),
            )
            if gate_passed or (attempt_index >= 2 and group_usable):
                best_samples = samples
                best_report = diversity_report
                best_response = response
                best_repair = repair or {}
                best_failure_director = sample_failure_director(diversity_report, chapter_number=chapter_number)
                break
            retry_feedback = _sample_retry_feedback(
                diversity_report,
                samples,
                director=sample_failure_director(diversity_report, chapter_number=chapter_number),
            )
        if not best_samples or best_response is None:
            raise ValueError("LLM did not return usable chapter samples")
        task = session.get(GenerationTask, task_id)
        task.status = "completed"
        task.output_json = _dumps_json(
            {
                "provider": best_response.provider,
                "model": best_response.model,
                "request_id": best_response.request_id,
                "samples": best_samples,
                "diversity_report": best_report,
                "gate_threshold": SAMPLE_DIVERSITY_THRESHOLD,
                "gate_passed": _sample_gate_passed(best_report),
                "attempts": attempts,
                "failure_director": best_failure_director,
                "raw_summary": _compact(best_response.text, 1200),
                "json_repair": best_repair,
                "usage": {
                    "prompt_chars": len(base_prompt),
                    "response_chars": len(best_response.text),
                    "estimated_prompt_tokens": best_response.estimated_prompt_tokens,
                    "estimated_response_tokens": best_response.estimated_response_tokens,
                    "estimated_total_tokens": best_response.estimated_prompt_tokens + best_response.estimated_response_tokens,
                    "elapsed_ms": best_response.elapsed_ms,
                    "usage": best_response.usage,
                },
            }
        )
        session.flush()
        return task
    except Exception as exc:
        classification = classify_exception(exc)
        task = session.get(GenerationTask, task_id)
        task.status = "failed"
        task.output_json = _dumps_json(
            {
                "error_category": classification.category,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "retryable": classification.retryable,
            }
        )
        record_llm_request(
            session,
            book_id=book_id,
            task_type=TASK_TYPE_CHAPTER_SAMPLE,
            generation_task_id=task.id,
            provider="live",
            model=model,
            prompt_template=PROMPT_TEMPLATE,
            prompt_chars=len(prompt_context["prompt"]),
            estimated_prompt_tokens=estimate_tokens(prompt_context["prompt"]),
            status="failed",
            error_category=classification.category,
        )
        session.flush()
        return task


def latest_chapter_samples(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    limit: int = 3,
) -> dict:
    learning = build_chapter_sample_learning(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        limit=8,
    )
    tasks = session.scalars(
        select(GenerationTask)
        .where(GenerationTask.book_id == book_id, GenerationTask.task_type == TASK_TYPE_CHAPTER_SAMPLE)
        .order_by(GenerationTask.id.desc())
        .limit(12)
    )
    latest_failed: dict | None = None
    latest_completed: dict | None = None
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        if task.status == "completed":
            samples = _normalize_samples(output_data.get("samples"), limit=limit)
            diversity_report = _sample_diversity_report(samples)
            adoption_by_index = {
                int(item.get("sample_index") or 0): item
                for item in learning.get("recent_adoptions", [])
                if int(item.get("task_id") or 0) == task.id
            }
            for sample in samples:
                adoption = adoption_by_index.get(int(sample.get("index") or 0))
                if adoption:
                    sample["adoption"] = adoption
            latest_completed = {
                "task_id": task.id,
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "provider": output_data.get("provider", ""),
                "model": output_data.get("model", ""),
                "samples": samples,
                "diversity_report": diversity_report,
                "gate_threshold": int(output_data.get("gate_threshold") or SAMPLE_DIVERSITY_THRESHOLD),
                "gate_passed": _sample_gate_passed(diversity_report),
                "attempts": output_data.get("attempts") or [],
                "failure_director": output_data.get("failure_director")
                or sample_failure_director(diversity_report, chapter_number=chapter_number),
            }
            if latest_failed:
                latest_failed["fallback_task_id"] = latest_completed["task_id"]
                latest_failed["fallback_created_at"] = latest_completed["created_at"]
                latest_failed["fallback_samples"] = samples
                latest_failed["fallback_diversity_report"] = latest_completed["diversity_report"]
                latest_failed["fallback_gate_passed"] = latest_completed["gate_passed"]
                latest_failed["fallback_attempts"] = latest_completed["attempts"]
                latest_failed["fallback_failure_director"] = latest_completed["failure_director"]
                return latest_failed
            return latest_completed
        if latest_failed is None:
            latest_failed = {
                "task_id": task.id,
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else "",
                "provider": output_data.get("provider", ""),
                "model": output_data.get("model", ""),
                "samples": [],
                "error": output_data.get("error", ""),
                "error_category": output_data.get("error_category", ""),
            }
    return latest_failed or latest_completed or {"task_id": None, "status": "empty", "samples": []}


def adopt_chapter_sample(
    session: Session,
    *,
    task_id: int,
    sample_index: int,
    revision_mode: str = REVISION_MODE_FRESH,
) -> AdoptedChapterSample:
    task = session.get(GenerationTask, task_id)
    if not task:
        raise ValueError(f"chapter sample task not found: {task_id}")
    if task.task_type != TASK_TYPE_CHAPTER_SAMPLE:
        raise ValueError(f"not a chapter sample task: {task.task_type}")
    input_data = _loads_json(task.input_json)
    output_data = _loads_json(task.output_json)
    chapter_number = int(input_data.get("chapter_number") or 0)
    diversity_report = _sample_diversity_report(_normalize_samples(output_data.get("samples"), limit=10))
    samples = _normalize_samples(output_data.get("samples"), limit=10)
    sample = next((item for item in samples if int(item.get("index") or 0) == sample_index), None)
    if not sample:
        raise ValueError(f"sample index not found: {sample_index}")
    suggestion = _sample_adoption_text(task_id=task.id, sample=sample)
    if not _sample_gate_passed(diversity_report):
        issues = "；".join(str(item) for item in diversity_report.get("issues", [])[:5]) or "多样性不足"
        score = int(diversity_report.get("score") or 0)
        suggestion += (
            "\n\n小样采用风险：本组小样多样性评分"
            f"{score}/{SAMPLE_DIVERSITY_THRESHOLD}，问题：{issues}。"
            "允许采用本小样作为作者方向选择，但整章重写时必须主动规避上述重复问题。"
        )
    feedback, adjustment, brief, version = submit_revision_suggestion(
        session,
        book_id=task.book_id,
        chapter_number=chapter_number,
        platform="chapter_sample_lab",
        suggestion_text=suggestion,
        revision_mode=revision_mode,
    )
    _append_adoption_record(
        task,
        sample_index=sample_index,
        sample=sample,
        chapter_number=chapter_number,
        feedback_id=feedback.id,
        adjustment_id=adjustment.id,
        brief_id=brief.id,
        source_version_id=version.id if version else None,
    )
    session.flush()
    return AdoptedChapterSample(
        feedback_id=feedback.id,
        feedback_adjustment_id=adjustment.id,
        brief_id=brief.id,
        chapter_version_id=version.id if version else None,
        chapter_version_status=version.status if version else "",
    )


def build_chapter_sample_learning(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
    limit: int = 12,
) -> dict:
    adoptions = _sample_adoption_records(session, book_id=book_id, chapter_number=chapter_number)
    enriched = []
    for index, adoption in enumerate(adoptions):
        next_adoption_at = next(
            (
                item["adopted_at_dt"]
                for item in adoptions[index + 1 :]
                if item["chapter_number"] == adoption["chapter_number"]
            ),
            None,
        )
        enriched.append(
            _enrich_adoption_outcome(
                session,
                adoption=adoption,
                next_adoption_at=next_adoption_at,
            )
        )
    recent = list(reversed(enriched))[:limit]
    successful = [
        item
        for item in enriched
        if item["outcome"] == "human_approved"
    ]
    weak = [
        item
        for item in enriched
        if item["outcome"] in {"needs_more_work", "quality_failed"}
    ]
    return {
        "total_adoptions": len(enriched),
        "human_approved_count": sum(1 for item in enriched if item["outcome"] == "human_approved"),
        "quality_passed_count": sum(1 for item in enriched if item["outcome"] == "quality_passed"),
        "pending_count": sum(1 for item in enriched if item["outcome"] == "pending"),
        "recent_adoptions": recent,
        "successful_patterns": _sample_patterns(successful, limit=5),
        "weak_patterns": _sample_patterns(weak, limit=4),
    }


def sync_chapter_sample_learning(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
) -> SampleLearningSync:
    return SampleLearningSync(recorded_count=0, preference_ids=[])


def _append_adoption_record(
    task: GenerationTask,
    *,
    sample_index: int,
    sample: dict,
    chapter_number: int,
    feedback_id: int,
    adjustment_id: int,
    brief_id: int,
    source_version_id: int | None,
) -> None:
    output_data = _loads_json(task.output_json)
    adoptions = output_data.get("adoptions")
    if not isinstance(adoptions, list):
        adoptions = []
    adoptions.append(
        {
            "book_id": task.book_id,
            "sample_index": sample_index,
            "sample_title": sample.get("title", ""),
            "direction": sample.get("direction", ""),
            "chapter_number": chapter_number,
            "feedback_id": feedback_id,
            "feedback_adjustment_id": adjustment_id,
            "brief_id": brief_id,
            "source_version_id": source_version_id,
            "adopted_at": datetime.utcnow().isoformat(),
        }
    )
    output_data["adoptions"] = adoptions
    output_data["adopted_sample_index"] = sample_index
    task.output_json = _dumps_json(output_data)


def _sample_adoption_records(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None,
) -> list[dict]:
    records: list[dict] = []
    seen_feedback_ids: set[int] = set()
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type == TASK_TYPE_CHAPTER_SAMPLE)
            .order_by(GenerationTask.created_at)
        )
    )
    for task in tasks:
        input_data = _loads_json(task.input_json)
        task_chapter_number = int(input_data.get("chapter_number") or 0)
        if chapter_number and task_chapter_number != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        samples = {
            int(item.get("index") or 0): item
            for item in _normalize_samples(output_data.get("samples"), limit=10)
        }
        adoptions = output_data.get("adoptions") if isinstance(output_data.get("adoptions"), list) else []
        for raw in adoptions:
            if not isinstance(raw, dict):
                continue
            record = _adoption_record_from_raw(
                task=task,
                task_chapter_number=task_chapter_number,
                raw=raw,
                samples=samples,
            )
            if not record:
                continue
            if record.get("feedback_id"):
                seen_feedback_ids.add(int(record["feedback_id"]))
            records.append(record)

    return sorted(records, key=lambda item: item["adopted_at_dt"])


def _legacy_feedback_adoption_records(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None,
    known_feedback_ids: set[int],
) -> list[dict]:
    rows = list(
        session.scalars(
            select(PlatformFeedback)
            .where(
                PlatformFeedback.book_id == book_id,
                PlatformFeedback.platform == "chapter_sample_lab",
                PlatformFeedback.metric_name == "revision_suggestion",
            )
            .order_by(PlatformFeedback.collected_at)
        )
    )
    records = []
    for feedback in rows:
        if feedback.id in known_feedback_ids:
            continue
        match = re.search(r"采用章节小样\s*#(\d+)-(\d+)", feedback.raw_text or "")
        if not match:
            continue
        task_id = int(match.group(1))
        sample_index = int(match.group(2))
        task = session.get(GenerationTask, task_id)
        if not task or task.book_id != book_id:
            continue
        input_data = _loads_json(task.input_json)
        task_chapter_number = int(input_data.get("chapter_number") or 0)
        if chapter_number and task_chapter_number != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        samples = {
            int(item.get("index") or 0): item
            for item in _normalize_samples(output_data.get("samples"), limit=10)
        }
        sample = samples.get(sample_index, {})
        adopted_at = feedback.collected_at or task.created_at or datetime.utcnow()
        records.append(
            {
                "task_id": task.id,
                "book_id": task.book_id,
                "sample_index": sample_index,
                "chapter_number": task_chapter_number,
                "sample_title": sample.get("title", ""),
                "direction": sample.get("direction", ""),
                "adoption_note": sample.get("adoption_note", ""),
                "why_it_works": sample.get("why_it_works", ""),
                "feedback_id": feedback.id,
                "feedback_adjustment_id": None,
                "brief_id": None,
                "source_version_id": None,
                "adopted_at": adopted_at.isoformat(),
                "adopted_at_dt": adopted_at,
            }
        )
    return records


def _adoption_record_from_raw(
    *,
    task: GenerationTask,
    task_chapter_number: int,
    raw: dict,
    samples: dict[int, dict],
) -> dict | None:
    sample_index = _safe_int(raw.get("sample_index"), 0)
    if not sample_index:
        return None
    sample = samples.get(sample_index, {})
    adopted_at = _parse_datetime(raw.get("adopted_at")) or task.created_at or datetime.utcnow()
    return {
        "task_id": task.id,
        "book_id": task.book_id,
        "sample_index": sample_index,
        "chapter_number": _safe_int(raw.get("chapter_number"), task_chapter_number),
        "sample_title": raw.get("sample_title") or sample.get("title", ""),
        "direction": raw.get("direction") or sample.get("direction", ""),
        "adoption_note": sample.get("adoption_note", ""),
        "why_it_works": sample.get("why_it_works", ""),
        "feedback_id": raw.get("feedback_id"),
        "feedback_adjustment_id": raw.get("feedback_adjustment_id"),
        "brief_id": raw.get("brief_id"),
        "source_version_id": raw.get("source_version_id"),
        "adopted_at": adopted_at.isoformat(),
        "adopted_at_dt": adopted_at,
    }


def _enrich_adoption_outcome(
    session: Session,
    *,
    adoption: dict,
    next_adoption_at: datetime | None,
) -> dict:
    version = _resulting_version_for_adoption(session, adoption=adoption, next_adoption_at=next_adoption_at)
    quality = _latest_quality_for_version(session, version.id) if version else None
    human_approved = bool(version and (version.status == "approved" or _approved_review_exists(session, version.id)))
    outcome = _sample_outcome(version=version, quality=quality, human_approved=human_approved)
    return {
        "task_id": adoption["task_id"],
        "sample_index": adoption["sample_index"],
        "chapter_number": adoption["chapter_number"],
        "sample_title": adoption.get("sample_title", ""),
        "direction": adoption.get("direction", ""),
        "adoption_note": adoption.get("adoption_note", ""),
        "why_it_works": adoption.get("why_it_works", ""),
        "adopted_at": adoption.get("adopted_at", ""),
        "feedback_id": adoption.get("feedback_id"),
        "brief_id": adoption.get("brief_id"),
        "result_version_id": version.id if version else None,
        "result_version_status": version.status if version else "",
        "quality_score": quality.score if quality else 0,
        "quality_passed": bool(quality.passed) if quality else False,
        "human_approved": human_approved,
        "outcome": outcome,
    }


def _resulting_version_for_adoption(
    session: Session,
    *,
    adoption: dict,
    next_adoption_at: datetime | None,
) -> ChapterVersion | None:
    task = session.get(GenerationTask, adoption["task_id"])
    if not task:
        return None
    chapter = session.scalar(
        select(Chapter).where(
            Chapter.book_id == task.book_id,
            Chapter.chapter_number == adoption["chapter_number"],
        )
    )
    if not chapter:
        return None
    stmt = select(ChapterVersion).where(
        ChapterVersion.chapter_id == chapter.id,
        ChapterVersion.created_at >= adoption["adopted_at_dt"],
    )
    if next_adoption_at:
        stmt = stmt.where(ChapterVersion.created_at < next_adoption_at)
    return session.scalar(stmt.order_by(ChapterVersion.created_at.desc(), ChapterVersion.id.desc()))


def _latest_quality_for_version(session: Session, version_id: int) -> QualityReport | None:
    return session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == version_id)
        .order_by(QualityReport.id.desc())
    )


def _approved_review_exists(session: Session, version_id: int) -> bool:
    return bool(
        session.scalar(
            select(ChapterReview)
            .where(ChapterReview.chapter_version_id == version_id, ChapterReview.verdict == "approved")
            .order_by(ChapterReview.id.desc())
        )
    )


def _sample_outcome(
    *,
    version: ChapterVersion | None,
    quality: QualityReport | None,
    human_approved: bool,
) -> str:
    if not version:
        return "pending"
    if human_approved:
        return "human_approved"
    if version.status == "needs_revision":
        return "needs_more_work"
    if quality and quality.passed:
        return "quality_passed"
    if quality and not quality.passed:
        return "quality_failed"
    return "pending"


def _sample_patterns(items: list[dict], *, limit: int) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for item in reversed(items):
        text = _compact(
            f"第{item['chapter_number']}章《{item.get('sample_title') or '未命名小样'}》："
            f"{item.get('direction') or item.get('adoption_note') or item.get('why_it_works')}",
            180,
        )
        if not text or text in seen:
            continue
        seen.add(text)
        rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def _sample_learning_prompt(learning: dict) -> str:
    if not learning.get("human_approved_count"):
        return "暂无经人工明确验证的小样学习；近期采用记录只作为试写历史，不得当作偏好复刻。"
    lines = [
        f"- 经人工验证的小样 {learning.get('human_approved_count', 0)} 次；采用但未明确验证的记录不得当作作者偏好。",
    ]
    for item in learning.get("successful_patterns", [])[:4]:
        lines.append(f"- 人工确认方向：{item}")
    return "\n".join(lines)


def _recent_sample_avoidance(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    limit: int = 3,
) -> str:
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type == TASK_TYPE_CHAPTER_SAMPLE)
            .order_by(GenerationTask.id.desc())
            .limit(20)
        )
    )
    rows: list[str] = []
    motifs: set[str] = set()
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        for sample in _normalize_samples(output_data.get("samples"), limit=6):
            title = sample.get("title", "")
            direction = sample.get("direction", "")
            opening = sample.get("opening", "")
            rows.append(_compact(f"《{title}》：{direction}", 180))
            motifs.update(_sample_motifs(f"{title}\n{direction}\n{opening}"))
            if len(rows) >= limit:
                break
        if len(rows) >= limit:
            break
    if not rows:
        return "暂无同章近期小样；本轮仍必须主动拉开结构差异。"
    motif_text = "、".join(sorted(motifs)) or "无明显模式"
    return "\n".join(
        [
            "同章近期小样旧模板警戒：",
            *[f"- {row}" for row in rows],
            f"- 已出现模式：{motif_text}",
            "- 本轮不得只换地点或道具复刻这些模式；必须换主角目标、冲突来源、信息释放方式和章末诱因。",
            "- 如果近期已出现某个现实入口、职业履历、登录方式或固定桥段，本轮第1个小样不要再把它当开场发动机；若属于本书设定，只能作为背景压力或代价出现。",
        ]
    )


def _build_sample_prompt_context(
    session: Session,
    *,
    book: Book,
    chapter: Chapter,
    brief: ChapterBrief | None,
    foundation: StoryFoundation | None,
    sample_count: int,
    focus: str,
) -> dict:
    chapter_number = chapter.chapter_number
    profile = build_book_profile(session, book_id=book.id)
    sample_learning = build_chapter_sample_learning(session, book_id=book.id, chapter_number=chapter_number, limit=8)
    goal = brief.goal if brief else f"第{chapter_number}章需要承接全书设定，写出可读的推进。"
    required_beats = brief.required_beats if brief else "承接前情；主角主动选择；形成新阻碍；章末留下钩子"
    constraints = brief.constraints if brief else "禁止系统提示、质检术语、元叙事进入正文。"
    revision_mode = "fresh" if "修订模式:fresh" in constraints or "修订模式：fresh" in constraints else ""
    packet = build_chapter_production_packet(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        mode="fresh" if revision_mode == "fresh" else "sample",
        revision_goal=goal if revision_mode else "",
        revision_required_beats=required_beats if revision_mode else "",
        revision_constraints=constraints if revision_mode else "",
        revision_context_mode=revision_mode or "sample",
        fresh_rewrite=revision_mode == "fresh",
        rewrite_mode=bool(revision_mode),
    )
    prompt = f"""
你是网文作者的“试写小样工位”。你的任务不是生成整章，而是为第{chapter_number}章给出 {sample_count} 个可供作者选择的正文方向小样。

作品信息：
- 书名：{book.title}
- 类型：{book.genre}
- 平台：{book.target_platform}
- 一句话核心：{foundation.premise if foundation else book.title}
- 读者承诺：{foundation.reader_promise if foundation else "强冲突、强钩子、持续推进"}

当前章节生产说明：
- 本章目标：{goal}
- 必要节拍：{required_beats}
- 约束：{packet.constraints}

作者口味库：
{packet.context.author_preferences}

前章承接：
{packet.context.previous_chapter_context}

人工验证学习：
{_sample_learning_prompt(sample_learning)}

同章反模板约束：
{_recent_sample_avoidance(session, book_id=book.id, chapter_number=chapter_number)}

Canon 与世界规则：
{packet.context.canon_context}

{profile.sample_prompt_block()}

章节导演单：
{packet.director_sheet}

小样要求：
- focus={focus}。每个小样写 350-550 个中文字符的“可直接放进正文的开场/关键场景片段”，不是提纲。
- 本轮目标是探索，不是复刻已采用小样。采用记录只代表试写历史，不代表作者偏好；除非人工明确验证，否则不得学习为固定模板。
- 三个小样必须先各自声明 exploration_axis 和 experiment_hypothesis：分别测试不同叙事发动机，例如人物处境、关系压力、规则误判、场景奇观、道德选择、信息悬疑；不得只是三个不同地点的同一套开场。
- 三个小样必须先在脑中完成“发动机分配”：短期目标、主压力、配角功能、秘密来源、章末诱因五项不能成套复用。若两个小样都靠欠账/盘问/追杀/演技观察推动，即为失败。
- 采用小样只代表本次试写方向，不进入长期学习；只有人工明确说“这个方向以后保留”，才可沉淀为作者偏好。
- 第二章及以后必须先承接上一章最后动作的后果、情绪或未解决问题，再选择适合本章的切入法推进。
- 每个小样必须使用不同开篇策略，且不得复用“写作智能上下文”中最近开篇记忆的地点、第一动作、第一矛盾和章末钩子形态。
- 如果本书 Book Profile 指定题材偏差护栏，必须按护栏执行；不要把别的书的默认套路套进来。
- 三个小样必须方向明显不同：主角短期目标、开场场域、冲突来源、关键配角功能、信息释放方式、章末诱因至少四项不同。
- 禁止三个小样都写成“被盘问/欠账/被追 -> 主角观察 -> 规矩压人 -> 章末受伤或奇遇”。这属于固定模板。
- 第1章小样尤其要拉开：样本一必须从本书核心场景内的具体困境或陌生关系压力起步，不要把近期小样的旧入口当开场发动机。
- 若 Book Profile 给出旧模板警戒词，本轮 opening 不能把这些词当作捷径、万能解法或默认开场；若它们属于已登记设定，只能作为背景压力、误读代价或被纠正的旧经验出现。
- 每个小样必须说明 difference_from_existing：它和最近同章小样相比，究竟换了什么结构，不允许只写“更有代入感”。
- 每个小样必须说明 anti_ai_flavor_strategy：如何避免临时设定、抽象场景、翻译腔和功能对白。
- 每个小样必须说明 pov_strategy：本小样如何贴住角色视角，让读者从角色的听、看、闻、触感、误判和身体反应进入场景。
- opening 不得像摄像头客观扫景；必须至少两处写出角色当下感知或身体反应。
- 每个小样必须说明 precision_strategy：如何保证物件、动词、视线条件和推理链准确；角色不能凭空看见鞋底、不能把刀穗写成腰上别着，也不能让强判断缺少“如果/若是/多半”等条件。
- scene_plan 只写低成本方案，不扩写整章；被采用后再进入整章生产。
- direction、why_it_works、difference_from_existing、anti_ai_flavor_strategy、pov_strategy、precision_strategy、adoption_note 每项控制在 80 个中文字符以内。
- scene_plan 每项控制在 40 个中文字符以内。
- 不要输出系统说明、质检术语、导演单标题、JSON 外文本。
- 字符串内部如需换行必须使用合法 JSON 转义；不要在字符串中插入未转义双引号。

严格输出 JSON：
{{
  "samples": [
    {{
      "index": 1,
      "title": "小样名",
      "exploration_axis": "本小样所属探索轴",
      "experiment_hypothesis": "本小样测试的叙事发动机",
      "direction": "这个方向的读者体验",
      "opening": "350-550中文字符正文小样",
      "scene_plan": ["后续场景推进1", "后续场景推进2", "章末钩子"],
      "why_it_works": "为什么它更贴合本书方向",
      "difference_from_existing": "和近期同章小样相比的结构差异",
      "anti_ai_flavor_strategy": "本小样如何去AI味儿",
      "pov_strategy": "如何贴住角色视角写感官、身体反应和误判",
      "precision_strategy": "如何保证物件动词、观察条件和推理链准确",
      "risks": ["可能风险1", "可能风险2"],
      "adoption_note": "如果采用，下一版整章必须怎么写"
    }}
  ]
}}
""".strip()
    return {
        "prompt": prompt,
        "director_sheet": packet.director_sheet,
        "production_packet_audit": packet.audit,
        "production_context_audit": packet.context.audit,
    }


def _sample_adoption_text(*, task_id: int, sample: dict) -> str:
    plan = "；".join(_list(sample.get("scene_plan"))[:4])
    risks = "；".join(_list(sample.get("risks"))[:3])
    engine_contract = _sample_engine_contract(sample)
    return "\n".join(
        [
            f"采用章节小样 #{task_id}-{sample.get('index')} 作为本章新版方向。",
            f"小样名：{sample.get('title', '')}",
            f"叙事实验：{sample.get('experiment_hypothesis', '')}",
            f"读者体验方向：{sample.get('direction', '')}",
            "必须继承的叙事发动机合同：",
            *engine_contract,
            "必须保留的小样气质和开场方法：",
            _compact(str(sample.get("opening") or ""), 900),
            f"后续场景推进：{plan}",
            f"结构差异：{sample.get('difference_from_existing', '')}",
            f"去AI味儿策略：{sample.get('anti_ai_flavor_strategy', '')}",
            f"贴身视角策略：{sample.get('pov_strategy', '')}",
            f"表达准确策略：{sample.get('precision_strategy', '')}",
            f"采用说明：{sample.get('adoption_note', '')}",
            f"注意规避：{risks}",
            "验收方式：下一版整章必须能逐项对应叙事发动机合同；开篇、场景推进、配角作用、信息释放和章末钩子都要沿用同一套压力逻辑，不得只替换几个名词，也不得退回旧稿的机械化推进。",
        ]
    )


def _sample_engine_contract(sample: dict) -> list[str]:
    axis = _compact(str(sample.get("exploration_axis") or ""), 80)
    experiment = _compact(str(sample.get("experiment_hypothesis") or ""), 140)
    direction = _compact(str(sample.get("direction") or ""), 140)
    opening = str(sample.get("opening") or "")
    plan = [_compact(str(item), 80) for item in _list(sample.get("scene_plan"))[:4] if str(item).strip()]
    risks = [_compact(str(item), 80) for item in _list(sample.get("risks"))[:3] if str(item).strip()]
    lines = [
        f"- 必须保留探索轴：{axis or '以本小样开场压力为准'}。",
        f"- 必须保留叙事实验：{experiment or '用同一套叙事发动机推进整章'}。",
        f"- 必须保留读者体验：{direction or '让读者感到主角在具体压力下主动选择并付出代价'}。",
        f"- 必须让开篇压力在前500字内落地：短期目标、阻碍、身体反应和误判至少各出现一次。",
        f"- 必须让整章后续场景从小样压力自然长出来，不得换成无关任务链或旧稿默认桥段。",
        f"- 必须让关键配角承担功能：制造阻碍、暴露利益、纠正误判或递出代价，不能只负责解释设定。",
        f"- 必须按小样的信息释放方式推进：先给可见证据，再给角色误判，最后用人物反应或新物证修正判断。",
        f"- 必须让章末钩子来自本章行动后果，而不是突兀追杀、坠崖、陌生人硬塞秘密或系统提示。",
    ]
    if plan:
        lines.append(f"- 必须沿用后续推进骨架：{'；'.join(plan)}。")
    if risks:
        lines.append(f"- 必须规避采用风险：{'；'.join(risks)}。")
    if _uses_actor_shortcut(opening):
        lines.append("- 禁止把演员、龙套、导演教过或表演经验当作解决冲突的钥匙。")
    if _sample1_uses_banned_entry(opening):
        lines.append("- 禁止把现实片场、出租屋头盔或内测资格复刻成开场发动机。")
    return lines


def _normalize_samples(value, *, limit: int) -> list[dict]:
    if not isinstance(value, list):
        return []
    rows = []
    for fallback_index, raw in enumerate(value[:limit], start=1):
        if not isinstance(raw, dict):
            continue
        index = _safe_int(raw.get("index"), fallback_index)
        opening = _compact(str(raw.get("opening") or raw.get("content") or ""), 1600)
        if not opening:
            continue
        rows.append(
            {
                "index": index,
                "title": _compact(str(raw.get("title") or f"小样{index}"), 80),
                "exploration_axis": _compact(str(raw.get("exploration_axis") or raw.get("axis") or ""), 80),
                "experiment_hypothesis": _compact(str(raw.get("experiment_hypothesis") or ""), 160),
                "direction": _compact(str(raw.get("direction") or ""), 180),
                "opening": opening,
                "scene_plan": _list(raw.get("scene_plan"))[:5],
                "why_it_works": _compact(str(raw.get("why_it_works") or ""), 260),
                "difference_from_existing": _compact(str(raw.get("difference_from_existing") or ""), 260),
                "anti_ai_flavor_strategy": _compact(str(raw.get("anti_ai_flavor_strategy") or ""), 260),
                "pov_strategy": _compact(str(raw.get("pov_strategy") or ""), 260),
                "precision_strategy": _compact(str(raw.get("precision_strategy") or ""), 260),
                "risks": _list(raw.get("risks"))[:4],
                "adoption_note": _compact(str(raw.get("adoption_note") or ""), 260),
            }
        )
    return rows


def _sample_gate_passed(report: dict) -> bool:
    return (
        int(report.get("score") or 0) >= SAMPLE_DIVERSITY_THRESHOLD
        and not report.get("issues")
        and str(report.get("status") or "") == "pass"
    )


def _sample_group_usable(report: dict) -> bool:
    return bool(report.get("recommended_sample_index")) and int(report.get("score") or 0) >= SAMPLE_DIVERSITY_THRESHOLD


def _prompt_with_retry_feedback(base_prompt: str, retry_feedback: str, *, attempt_index: int) -> str:
    if attempt_index <= 1 or not retry_feedback:
        return base_prompt
    return "\n\n".join(
        [
            base_prompt,
            "上一轮小样没有通过多样性门禁，请按以下失败原因整体换发动机，不得局部改词：",
            retry_feedback,
            "本轮重试要求：三个小样的主角短期目标、冲突来源、配角功能、信息释放方式、章末诱因必须重新分配；样本一不要把上轮旧入口或旧职业履历当开场发动机。",
        ]
    )


def _sample_retry_feedback(report: dict, samples: list[dict], *, director: dict | None = None) -> str:
    motif_lines = []
    for row in report.get("motifs_by_sample") or []:
        motifs = "、".join(row.get("motifs") or []) or "无明显母题"
        motif_lines.append(f"- 样本{row.get('index')}《{row.get('title', '')}》：{motifs}")
    issue_text = "、".join(str(item) for item in report.get("issues", [])[:8]) or "结构差异不足"
    repeated = "、".join(str(item) for item in report.get("repeated_motifs", [])[:8]) or "无"
    titles = "、".join(str(sample.get("title") or "") for sample in samples)
    director = director or {}
    director_lines = [str(item) for item in director.get("rewrite_directives", [])[:6]]
    blocked_lines = [str(item) for item in director.get("blocked_patterns", [])[:6]]
    return "\n".join(
        [
            f"- 上轮分数：{int(report.get('score') or 0)}/{SAMPLE_DIVERSITY_THRESHOLD}",
            f"- 失败问题：{issue_text}",
            f"- 重复母题：{repeated}",
            f"- 上轮标题：{titles}",
            *motif_lines,
            *[f"- 编辑导演单：{item}" for item in director_lines],
            *[f"- 避免复刻：{item}" for item in blocked_lines],
            "- 下一轮不要复刻这些标题、入口、职业困境、盘问结构、追杀坠崖、人情交易和规矩打脸组合；已登记设定可以保留，但不能当偷懒解法。",
        ]
    )


def _sample_diversity_report(samples: list[dict]) -> dict:
    if not samples:
        return {"score": 0, "status": "empty", "issues": ["missing_samples"], "motifs_by_sample": []}
    motif_rows = []
    motif_sets = []
    sample_texts = []
    for sample in samples:
        text = "\n".join(
            str(sample.get(key) or "")
            for key in (
                "title",
                "exploration_axis",
                "experiment_hypothesis",
                "direction",
                "opening",
                "scene_plan",
            )
        )
        sample_texts.append(text)
        motifs = sorted(_sample_motifs(text))
        motif_sets.append(set(motifs))
        motif_rows.append({"index": sample.get("index"), "title": sample.get("title", ""), "motifs": motifs})

    pair_scores = []
    for left_index, left in enumerate(motif_sets):
        for right in motif_sets[left_index + 1 :]:
            union = left | right
            intersection = left & right
            pair_scores.append(len(intersection) / len(union) if union else 0)
    max_overlap = max(pair_scores) if pair_scores else 0
    unique_motifs = len(set().union(*motif_sets)) if motif_sets else 0
    repeated_motifs = sorted(
        motif
        for motif in set().union(*motif_sets)
        if sum(1 for row in motif_sets if motif in row) >= 2
    )
    score = 100 - round(max_overlap * 55) - max(0, len(samples) * 3 - unique_motifs) * 3 - min(18, len(repeated_motifs) * 3)
    issues = []
    axes = [str(sample.get("exploration_axis") or "").strip() for sample in samples]
    if any(not item for item in axes):
        issues.append("missing_exploration_axis")
    if len({item[:10] for item in axes if item}) < min(len(samples), 3):
        issues.append("exploration_axis_overlap")
        score -= 8
    experiments = [str(sample.get("experiment_hypothesis") or "").strip() for sample in samples]
    if any(not item for item in experiments):
        issues.append("missing_experiment_hypothesis")
    if len({item[:12] for item in experiments if item}) < min(len(samples), 3):
        issues.append("experiment_hypothesis_overlap")
    if max_overlap >= 0.45:
        issues.append(f"sample_overlap_high:{max_overlap:.2f}")
    if repeated_motifs:
        issues.append("repeated_motifs:" + ",".join(repeated_motifs[:8]))
    first_motifs = motif_sets[0] if motif_sets else set()
    if first_motifs and "演员观察" in first_motifs and (
        _sample1_uses_banned_entry(sample_texts[0]) or _uses_actor_shortcut(sample_texts[0])
    ):
        issues.append("sample1_reuses_reality_actor_template")
        score -= 10
    if sample_texts and _sample1_uses_banned_entry(sample_texts[0]):
        issues.append("sample1_uses_banned_old_entry")
        score -= 10
    actor_shortcut_indices = [
        str(index + 1)
        for index, text in enumerate(sample_texts)
        if _uses_actor_shortcut(text)
    ]
    if actor_shortcut_indices:
        issues.append("actor_shortcut_reused:samples=" + ",".join(actor_shortcut_indices))
        score -= min(18, 6 * len(actor_shortcut_indices))
    if any(not sample.get("difference_from_existing") for sample in samples):
        issues.append("missing_difference_from_existing")
    if any(not sample.get("anti_ai_flavor_strategy") for sample in samples):
        issues.append("missing_anti_ai_flavor_strategy")
    if any(not sample.get("pov_strategy") for sample in samples):
        issues.append("missing_pov_strategy")
        score -= 6
    if any(not sample.get("precision_strategy") for sample in samples):
        issues.append("missing_precision_strategy")
        score -= 6
    sample_scores = [
        _sample_quality_score(sample, motifs=motif_sets[index], repeated_motifs=set(repeated_motifs))
        for index, sample in enumerate(samples)
    ]
    usable_sample_indices = [
        int(row.get("index") or 0)
        for row in sample_scores
        if _sample_score_is_usable(row)
    ]
    recommended_sample = max(
        (row for row in sample_scores if _sample_score_is_usable(row)),
        key=lambda row: int(row.get("score") or 0),
        default=None,
    )
    score = max(0, min(100, score))
    status = "pass" if score >= 65 and not issues else "attention"
    if status != "pass" and score >= 65 and recommended_sample:
        status = "usable"
    return {
        "score": score,
        "status": status,
        "max_pair_overlap": round(max_overlap, 3),
        "unique_motif_count": unique_motifs,
        "repeated_motifs": repeated_motifs[:12],
        "sample_scores": sample_scores,
        "usable_sample_indices": usable_sample_indices,
        "recommended_sample_index": int(recommended_sample.get("index") or 0) if recommended_sample else None,
        "recommended_sample_score": int(recommended_sample.get("score") or 0) if recommended_sample else 0,
        "usability_note": _sample_usability_note(
            status=status,
            recommended_sample=recommended_sample,
            repeated_motifs=repeated_motifs,
        ),
        "experiments": experiments,
        "axes": axes,
        "issues": issues,
        "motifs_by_sample": motif_rows,
    }


def _sample_score_is_usable(row: dict) -> bool:
    issues = {str(item) for item in row.get("issues", [])}
    blocking = {
        "uses_shortcut_profile",
        "opening_too_thin",
        "motif_too_sparse",
    }
    if any(str(item).startswith("missing:") for item in issues):
        return False
    if issues & blocking:
        return False
    return int(row.get("score") or 0) >= USABLE_SAMPLE_THRESHOLD


def _sample_usability_note(*, status: str, recommended_sample: dict | None, repeated_motifs: list[str]) -> str:
    if status == "pass":
        return "三版小样整体通过，可以直接选方向。"
    if recommended_sample:
        note = f"整体仍有结构重复，但第{int(recommended_sample.get('index') or 0)}个小样单独可用。"
        if repeated_motifs:
            note += " 采用后整章要避开：" + "、".join(repeated_motifs[:4]) + "。"
        return note
    return "暂未出现稳定可用的小样，建议重新生成。"


def _sample_quality_score(sample: dict, *, motifs: set[str], repeated_motifs: set[str]) -> dict:
    score = 100
    issues: list[str] = []
    opening = str(sample.get("opening") or "")
    required_fields = (
        "exploration_axis",
        "experiment_hypothesis",
        "difference_from_existing",
        "anti_ai_flavor_strategy",
        "pov_strategy",
        "precision_strategy",
    )
    for field in required_fields:
        if not str(sample.get(field) or "").strip():
            score -= 8
            issues.append(f"missing:{field}")
    if len(opening) < 260:
        score -= 10
        issues.append("opening_too_thin")
    if len(motifs) < 2:
        score -= 8
        issues.append("motif_too_sparse")
    if motifs & repeated_motifs:
        score -= min(18, len(motifs & repeated_motifs) * 6)
        issues.append("shares_repeated_motif")
    if _uses_actor_shortcut(opening):
        score -= 18
        issues.append("uses_shortcut_profile")
    return {
        "index": sample.get("index"),
        "score": max(0, min(100, score)),
        "issues": issues,
    }


def _sample_motifs(text: str) -> set[str]:
    source = str(text or "")
    motif_map = {
        "现实片场": ("横店", "片场", "导演", "副导演", "群演", "替身", "剧组", "道具"),
        "出租屋登录": ("出租屋", "头盔", "内测卡", "登录", "内测资格"),
        "茶棚欠账": ("茶棚", "老板娘", "饭钱", "茶钱", "赊账", "欠账", "劈柴换饭"),
        "渡口盘问": ("渡口", "船夫", "船帮", "路引", "差役", "验身"),
        "被盘问": ("哪来的", "路引", "报名", "盘问", "官爷", "查验"),
        "追杀逼近": ("追", "追杀", "山犬", "火把", "围捕", "别让", "灭口"),
        "受伤女子": ("受伤", "女子", "女侠", "梅霜", "二小姐", "短箭"),
        "坠崖奇遇": ("坠崖", "断魂崖", "山崖", "崖", "藤", "坠落"),
        "重伤获救": ("重伤获救", "托孤", "救人", "药王谷", "沈青梧", "梅引"),
        "规矩打脸": ("规矩", "保正", "官规", "江湖规矩", "不是游戏"),
        "演员观察": ("演员", "表演", "演技", "龙套", "落难书生", "临场"),
        "人情交易": ("人情", "交易", "价钱", "欠下", "账房", "押了"),
        "帮派压迫": ("青竹帮", "黑虎寨", "青河剑派", "帮派", "门派", "寨主", "堂口"),
    }
    motifs = {
        name
        for name, markers in motif_map.items()
        if any(marker in source for marker in markers)
    }
    return motifs


def _sample1_uses_banned_entry(text: str) -> bool:
    source = str(text or "")
    reality_markers = ("横店", "剧组", "片场", "副导演", "替身费")
    login_markers = ("出租屋", "头盔", "内测资格", "内测卡", "登录")
    opening_markers = ("醒来", "睁眼", "刚登录", "戴上", "摘下", "推开出租屋")
    reality_hits = sum(1 for marker in reality_markers if marker in source)
    login_hits = sum(1 for marker in login_markers if marker in source)
    if reality_hits and login_hits:
        return True
    if login_hits and any(marker in source[:220] for marker in opening_markers):
        return True
    return reality_hits >= 2 and any(marker in source[:220] for marker in ("刚下戏", "副导演", "替身费"))


def _uses_actor_shortcut(text: str) -> bool:
    source = str(text or "")
    markers = (
        "靠演技",
        "靠演员",
        "用演技",
        "凭演技",
        "表演经验",
        "龙套经验",
        "片场经验",
        "导演教过",
        "演过",
        "职业本能",
        "临场表演",
    )
    return any(marker in source for marker in markers)


def _dry_run_samples(*, book: Book, chapter_number: int, sample_count: int) -> list[dict]:
    base = [
        (
            "处境压力",
            "压力切入",
            "从上一章后果直接压住主角，让选择先发生。",
            f"第{chapter_number}章开场，主角没有先看面板，也没有急着找任务。他先听见门外压低的脚步声，随后闻到药铺后堂那股冷掉的血腥气。掌柜攥着账本站在灯下，明明害怕，却仍然把半枚铜钱推到他面前，说江湖规矩不能白救人。主角这才意识到，自己来到的不是能靠刷怪升级的游戏，而是一座每个人都要为选择付账的真实江湖。",
        ),
        (
            "人物关系",
            "人物关系切入",
            "用一个有利益和顾虑的人把世界写活。",
            f"第{chapter_number}章开场，先来找主角的不是系统提示，而是昨夜被他救下的少年。少年袖口还沾着泥，开口却先问他愿不愿意替镖局作证，因为师父死前欠下的银子会落到全家头上。主角看见他害怕，也看见他藏在掌心的短刀，于是明白这个世界的人不会围着玩家转，每个人都有自己的债、仇和活路。",
        ),
        (
            "规则代价",
            "规则代价切入",
            "让提升来自观察规则与承担后果。",
            f"第{chapter_number}章开场，主角照着存档里的旧招式运气，丹田却像被冷针刺了一下。旁边老捕快没有惊讶，只把茶盏扣在桌上，说外来人最容易死在自以为懂规矩的第一夜。想学本事可以，先把昨晚留下的破绽补干净；想求捷径也可以，明日城门口会多一具无名尸。主角第一次知道，提升不是经验条跳动，而是拿命换来一条能活下去的规矩。",
        ),
    ]
    rows = []
    for index, item in enumerate(base[:sample_count], start=1):
        axis, title, direction, opening = item
        rows.append(
            {
                "index": index,
                "title": title,
                "exploration_axis": axis,
                "experiment_hypothesis": ["人物处境型", "关系压力型", "规则代价型"][index - 1],
                "direction": direction,
                "opening": opening,
                "scene_plan": ["承接上一章后果", "让配角带着私心推动冲突", "章末留下更具体的江湖规矩代价"],
                "why_it_works": "先写人和选择，再写规则，因此更接近真实武侠世界。",
                "difference_from_existing": "从不同场域和冲突来源切入，避免复用同一追杀坠崖模板。",
                "anti_ai_flavor_strategy": "用具体生计、人情和动作承载设定，不用抽象解释。",
                "pov_strategy": "每段先落到主角听见、闻到、疼到或误判到的东西，再带出场景。",
                "precision_strategy": "物件动作先核对可见条件和搭配，推断只写角色能证明的部分。",
                "risks": ["需要后续整章继续兑现，不要退回面板化任务链"],
                "adoption_note": "采用后按这个开场气质整章重写，人物动机必须比设定说明更靠前。",
            }
        )
    return rows


def _parse_json_object(text: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        stripped = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start < 0 or end <= start:
                raise
            data = json.loads(stripped[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON object is required")
    return data


def _parse_or_repair_sample_output(
    provider,
    *,
    response_text: str,
    original_prompt: str,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
) -> tuple[dict, dict | None]:
    try:
        return _parse_json_object(response_text), None
    except Exception as first_exc:
        repair_prompt = f"""
你刚才的章节小样输出不是合法 JSON，系统无法保存。

请优先基于“上一轮原始输出”修复格式，重新输出一个完整、合法的 JSON 对象。不要解释，不要 Markdown，不要新增 JSON 外文本。

JSON 格式必须严格为：
{{
  "samples": [
    {{
      "index": 1,
      "title": "小样名",
      "exploration_axis": "本小样所属探索轴",
      "experiment_hypothesis": "本小样测试的叙事发动机",
      "direction": "这个方向的读者体验",
      "opening": "350-550中文字符正文小样",
      "scene_plan": ["后续场景推进1", "后续场景推进2", "章末钩子"],
      "why_it_works": "为什么它更贴合本书方向",
      "difference_from_existing": "和近期同章小样相比的结构差异",
      "anti_ai_flavor_strategy": "本小样如何去AI味儿",
      "pov_strategy": "如何贴住角色视角写感官、身体反应和误判",
      "precision_strategy": "如何保证物件动词、观察条件和推理链准确",
      "risks": ["可能风险1", "可能风险2"],
      "adoption_note": "如果采用，下一版整章必须怎么写"
    }}
  ]
}}

要求：
- samples 必须是数组，保留上一轮的小样数量和主要内容。
- 如果上一轮因为截断缺少样本，请根据原始任务补足到 3 个样本。
- 如果原始任务给出 Book Profile 旧模板警戒词，修复后的小样 opening 不能把这些词当作开场捷径或万能解法；已登记设定可以保留为背景压力。
- 每个样本必须有 exploration_axis 和 experiment_hypothesis，且三个样本的叙事发动机不能重复。
- opening 必须是字符串，不能把正文放到 JSON 外面。
- 字符串内部换行和双引号必须正确转义，保证最终可被 json.loads 解析。
- direction、why_it_works、difference_from_existing、anti_ai_flavor_strategy、adoption_note 每项控制在 80 个中文字符以内。
- pov_strategy 控制在 80 个中文字符以内，且 opening 必须体现该策略。
- precision_strategy 控制在 80 个中文字符以内，且 opening 里的观察和推断必须经得起复盘。
- opening 每个控制在 350-550 个中文字符以内。
- 不要输出系统提示、模型信息、修复说明或第二个 JSON。

上一轮解析错误：
{first_exc}

上一轮原始输出前 5000 字：
{response_text[:5000]}

原始任务前 4000 字：
{original_prompt[:4000]}
""".strip()
        repaired = provider.generate(
            repair_prompt,
            max_tokens=max(max_tokens, 4200),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if getattr(provider, "name", "") != "dry_run" else None,
        )
        data = _parse_json_object(repaired.text)
        return data, {
            "attempted": True,
            "request_id": repaired.request_id,
            "provider": repaired.provider,
            "model": repaired.model,
            "response_chars": len(repaired.text),
            "elapsed_ms": repaired.elapsed_ms,
        }


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        return parsed.replace(tzinfo=None)
    return parsed


def _dumps_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False)


def _list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_compact(str(item), 180) for item in value if str(item).strip()]


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _safe_int(value, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
