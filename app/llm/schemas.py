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

