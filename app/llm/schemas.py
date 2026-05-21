from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


class StructuredOutputError(ValueError):
    pass


@dataclass
class DraftOutput:
    title: str
    content: str
    self_check: list[str] = field(default_factory=list)
    used_brief_points: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(
            {
                "title": self.title,
                "content": self.content,
                "self_check": self.self_check,
                "used_brief_points": self.used_brief_points,
            },
            ensure_ascii=False,
    )


@dataclass
class ReviewOutput:
    verdict: str
    score: int
    strengths: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    revision_suggestions: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "score": self.score,
            "strengths": self.strengths,
            "issues": self.issues,
            "revision_suggestions": self.revision_suggestions,
            "risk_flags": self.risk_flags,
        }


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise StructuredOutputError(f"LLM output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise StructuredOutputError("LLM output JSON must be an object")
    return data


def parse_draft_output(text: str) -> DraftOutput:
    data = _extract_json(text)
    title = str(data.get("title") or "").strip()
    content = str(data.get("content") or "").strip()
    self_check_raw = data.get("self_check") or []
    used_points_raw = data.get("used_brief_points") or []
    if not title:
        raise StructuredOutputError("draft output missing title")
    if not content:
        raise StructuredOutputError("draft output missing content")
    if not isinstance(self_check_raw, list):
        raise StructuredOutputError("self_check must be a list")
    if not isinstance(used_points_raw, list):
        raise StructuredOutputError("used_brief_points must be a list")
    return DraftOutput(
        title=title,
        content=content,
        self_check=[str(item) for item in self_check_raw],
        used_brief_points=[str(item) for item in used_points_raw],
    )


def parse_review_output(text: str) -> ReviewOutput:
    data = _extract_json(text)
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in {"pass", "needs_revision", "fail"}:
        raise StructuredOutputError("review output verdict must be pass, needs_revision, or fail")
    try:
        score = int(data.get("score"))
    except (TypeError, ValueError) as exc:
        raise StructuredOutputError("review output score must be an integer") from exc
    score = max(0, min(100, score))
    return ReviewOutput(
        verdict=verdict,
        score=score,
        strengths=_string_list(data.get("strengths")),
        issues=_string_list(data.get("issues")),
        revision_suggestions=_string_list(data.get("revision_suggestions")),
        risk_flags=_string_list(data.get("risk_flags")),
    )


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise StructuredOutputError("review output list fields must be arrays")
    return [str(item) for item in value]
