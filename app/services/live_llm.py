from __future__ import annotations

from dataclasses import dataclass

from app.llm.providers import ArkOpenAIProvider
from app.services.llm_errors import classify_exception


@dataclass(frozen=True)
class LiveLLMSmokeResult:
    passed: bool
    provider: str
    model: str
    request_id: str
    estimated_total_tokens: int
    elapsed_ms: int
    text: str
    error_category: str = ""
    error: str = ""


def run_live_llm_smoke() -> LiveLLMSmokeResult:
    try:
        provider = ArkOpenAIProvider()
        response = provider.generate('只回复 JSON: {"ok": true}', max_tokens=20)
    except Exception as exc:
        classification = classify_exception(exc)
        return LiveLLMSmokeResult(
            passed=False,
            provider="ark_openai_compatible",
            model="",
            request_id="",
            estimated_total_tokens=0,
            elapsed_ms=0,
            text="",
            error_category=classification.category,
            error=f"{type(exc).__name__}: {exc}",
        )
    ok = '"ok"' in response.text or "ok" in response.text.lower()
    return LiveLLMSmokeResult(
        passed=ok,
        provider=response.provider,
        model=response.model,
        request_id=response.request_id,
        estimated_total_tokens=response.estimated_prompt_tokens + response.estimated_response_tokens,
        elapsed_ms=response.elapsed_ms,
        text=response.text[:200],
        error_category="" if ok else "unexpected_response",
        error="" if ok else "live smoke response did not contain ok",
    )
