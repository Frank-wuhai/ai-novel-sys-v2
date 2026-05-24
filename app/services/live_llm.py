from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import ArkOpenAIProvider
from app.models.entities import Book, LLMRequestLog
from app.services.llm_errors import classify_exception
from app.services.llm_audit import record_llm_request


@dataclass(frozen=True)
class LiveLLMSmokeResult:
    passed: bool
    provider: str
    model: str
    request_id: str
    estimated_total_tokens: int
    elapsed_ms: int
    text: str
    llm_request_log_id: int | None = None
    error_category: str = ""
    error: str = ""


def run_live_llm_smoke(session: Session | None = None, *, book_id: int | None = None) -> LiveLLMSmokeResult:
    prompt = '只回复 JSON: {"ok": true}'
    try:
        provider = ArkOpenAIProvider()
        response = provider.generate(
            prompt,
            max_tokens=settings.llm_smoke_max_tokens,
            temperature=0,
        )
    except Exception as exc:
        classification = classify_exception(exc)
        log = _record_smoke_log(
            session,
            book_id=book_id,
            provider="ark_openai_compatible",
            model=settings.model_name,
            request_id="",
            prompt=prompt,
            response_text="",
            estimated_prompt_tokens=0,
            estimated_response_tokens=0,
            actual_prompt_tokens=0,
            actual_response_tokens=0,
            actual_total_tokens=0,
            elapsed_ms=0,
            status="failed",
            error_category=classification.category,
        )
        result = LiveLLMSmokeResult(
            passed=False,
            provider="ark_openai_compatible",
            model=settings.model_name,
            request_id="",
            estimated_total_tokens=0,
            elapsed_ms=0,
            text="",
            llm_request_log_id=log.id if log else None,
            error_category=classification.category,
            error=f"{type(exc).__name__}: {exc}",
        )
        return result
    ok = '"ok"' in response.text or "ok" in response.text.lower()
    actual_prompt, actual_response, actual_total = _actual_usage_tokens(response.usage)
    log = _record_smoke_log(
        session,
        book_id=book_id,
        provider=response.provider,
        model=response.model,
        request_id=response.request_id,
        prompt=prompt,
        response_text=response.text,
        estimated_prompt_tokens=response.estimated_prompt_tokens,
        estimated_response_tokens=response.estimated_response_tokens,
        actual_prompt_tokens=actual_prompt,
        actual_response_tokens=actual_response,
        actual_total_tokens=actual_total,
        elapsed_ms=response.elapsed_ms,
        status="completed" if ok else "failed",
        error_category="" if ok else "unexpected_response",
    )
    return LiveLLMSmokeResult(
        passed=ok,
        provider=response.provider,
        model=response.model,
        request_id=response.request_id,
        estimated_total_tokens=response.estimated_prompt_tokens + response.estimated_response_tokens,
        elapsed_ms=response.elapsed_ms,
        text=response.text[:200],
        llm_request_log_id=log.id if log else None,
        error_category="" if ok else "unexpected_response",
        error="" if ok else "live smoke response did not contain ok",
    )


def _record_smoke_log(
    session: Session | None,
    *,
    book_id: int | None,
    provider: str,
    model: str,
    request_id: str,
    prompt: str,
    response_text: str,
    estimated_prompt_tokens: int,
    estimated_response_tokens: int,
    actual_prompt_tokens: int,
    actual_response_tokens: int,
    actual_total_tokens: int,
    elapsed_ms: int,
    status: str,
    error_category: str,
) -> LLMRequestLog | None:
    if session is None or not book_id:
        return None
    if not session.get(Book, book_id):
        raise ValueError(f"book not found: {book_id}")
    return record_llm_request(
        session,
        book_id=book_id,
        task_type="live_llm_smoke",
        generation_task_id=None,
        provider=provider,
        model=model,
        request_id=request_id,
        prompt_template="live_llm_smoke@v1",
        prompt_chars=len(prompt),
        response_chars=len(response_text),
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_response_tokens=estimated_response_tokens,
        actual_prompt_tokens=actual_prompt_tokens,
        actual_response_tokens=actual_response_tokens,
        actual_total_tokens=actual_total_tokens,
        elapsed_ms=elapsed_ms,
        status=status,
        error_category=error_category,
    )


def _actual_usage_tokens(usage: dict | None) -> tuple[int, int, int]:
    if not usage:
        return 0, 0, 0
    prompt = int(usage.get("prompt_tokens") or 0)
    response = int(usage.get("completion_tokens") or usage.get("response_tokens") or 0)
    total = int(usage.get("total_tokens") or prompt + response)
    return prompt, response, total
