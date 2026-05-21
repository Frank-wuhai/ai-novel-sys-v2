from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.llm_audit import LLMUsageSummary, summarize_llm_usage


@dataclass(frozen=True)
class LLMCostSummary:
    book_id: int | None
    model: str
    request_count: int
    billable_prompt_tokens: int
    billable_response_tokens: int
    billable_total_tokens: int
    input_price_per_1m_tokens: float
    output_price_per_1m_tokens: float
    estimated_cost: float
    currency: str = "USD"


def summarize_llm_cost(
    session: Session,
    *,
    book_id: int | None = None,
    input_price_per_1m_tokens: float | None = None,
    output_price_per_1m_tokens: float | None = None,
) -> LLMCostSummary:
    usage = summarize_llm_usage(session, book_id=book_id)
    input_price = settings.llm_input_price_per_1m_tokens if input_price_per_1m_tokens is None else input_price_per_1m_tokens
    output_price = settings.llm_output_price_per_1m_tokens if output_price_per_1m_tokens is None else output_price_per_1m_tokens
    return _build_cost_summary(usage, input_price=input_price, output_price=output_price)


def _build_cost_summary(usage: LLMUsageSummary, *, input_price: float, output_price: float) -> LLMCostSummary:
    input_cost = usage.billable_prompt_tokens / 1_000_000 * input_price
    output_cost = usage.billable_response_tokens / 1_000_000 * output_price
    return LLMCostSummary(
        book_id=usage.book_id,
        model=settings.model_name,
        request_count=usage.request_count,
        billable_prompt_tokens=usage.billable_prompt_tokens,
        billable_response_tokens=usage.billable_response_tokens,
        billable_total_tokens=usage.billable_total_tokens,
        input_price_per_1m_tokens=input_price,
        output_price_per_1m_tokens=output_price,
        estimated_cost=round(input_cost + output_cost, 6),
    )
