from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.entities import LLMRequestLog


@dataclass(frozen=True)
class LLMUsageSummary:
    book_id: int | None
    request_count: int
    completed_count: int
    failed_count: int
    estimated_total_tokens: int
    elapsed_ms: int


def record_llm_request(
    session: Session,
    *,
    book_id: int,
    task_type: str,
    generation_task_id: int | None,
    provider: str,
    model: str,
    request_id: str = "",
    prompt_template: str = "",
    prompt_chars: int = 0,
    response_chars: int = 0,
    estimated_prompt_tokens: int = 0,
    estimated_response_tokens: int = 0,
    elapsed_ms: int = 0,
    status: str = "completed",
    error_category: str = "",
) -> LLMRequestLog:
    log = LLMRequestLog(
        generation_task_id=generation_task_id,
        book_id=book_id,
        task_type=task_type,
        provider=provider,
        model=model,
        request_id=request_id,
        prompt_template=prompt_template,
        prompt_chars=prompt_chars,
        response_chars=response_chars,
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_response_tokens=estimated_response_tokens,
        estimated_total_tokens=estimated_prompt_tokens + estimated_response_tokens,
        elapsed_ms=elapsed_ms,
        status=status,
        error_category=error_category,
    )
    session.add(log)
    session.flush()
    return log


def list_llm_request_logs(
    session: Session,
    *,
    book_id: int | None = None,
    status: str = "",
    limit: int = 20,
) -> list[LLMRequestLog]:
    stmt = select(LLMRequestLog).order_by(LLMRequestLog.id.desc()).limit(limit)
    if book_id is not None:
        stmt = stmt.where(LLMRequestLog.book_id == book_id)
    if status:
        stmt = stmt.where(LLMRequestLog.status == status)
    return list(session.scalars(stmt))


def summarize_llm_usage(session: Session, *, book_id: int | None = None) -> LLMUsageSummary:
    stmt = select(
        func.count(LLMRequestLog.id),
        func.sum(LLMRequestLog.estimated_total_tokens),
        func.sum(LLMRequestLog.elapsed_ms),
        func.sum(case((LLMRequestLog.status == "completed", 1), else_=0)),
        func.sum(case((LLMRequestLog.status == "failed", 1), else_=0)),
    )
    if book_id is not None:
        stmt = stmt.where(LLMRequestLog.book_id == book_id)
    row = session.execute(stmt).one()
    return LLMUsageSummary(
        book_id=book_id,
        request_count=int(row[0] or 0),
        estimated_total_tokens=int(row[1] or 0),
        elapsed_ms=int(row[2] or 0),
        completed_count=int(row[3] or 0),
        failed_count=int(row[4] or 0),
    )
