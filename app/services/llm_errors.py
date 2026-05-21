from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    retryable: bool


def classify_exception(exc: Exception) -> ErrorClassification:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    combined = f"{name} {text}"
    if isinstance(exc, ValueError):
        return ErrorClassification("validation", False)
    if "structuredoutputerror" in name or "invalid json" in combined or "json" in combined and "parse" in combined:
        return ErrorClassification("structured_output", False)
    if "authentication" in combined or "unauthorized" in combined or "invalid api key" in combined:
        return ErrorClassification("auth", False)
    if "permission" in combined or "forbidden" in combined:
        return ErrorClassification("permission", False)
    if "rate" in combined or "429" in combined:
        return ErrorClassification("rate_limit", True)
    if "timeout" in combined or "timed out" in combined:
        return ErrorClassification("timeout", True)
    if "connection" in combined or "network" in combined or "dns" in combined:
        return ErrorClassification("network", True)
    if "context" in combined and ("length" in combined or "window" in combined or "too long" in combined):
        return ErrorClassification("context_length", False)
    if "api" in combined or "provider" in combined or "server" in combined or "5" in combined:
        return ErrorClassification("provider", True)
    return ErrorClassification("execution", False)
