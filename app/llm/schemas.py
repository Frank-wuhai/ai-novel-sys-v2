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


def _find_balanced_json_objects(text: str) -> list[str]:
    """扫描文本,返回所有括号配平的顶层 {...} 子串(按出现顺序)。

    thinking 模型常把 JSON 埋在思维链中间/末尾,前后带解释文字。简单的
    text.find('{')..rfind('}') 会把多个对象或对象外的杂散花括号一起截进来导致
    解析失败。这里做括号配平扫描(尊重字符串内的转义与花括号),稳健切出候选对象。
    """
    objs: list[str] = []
    depth = 0
    start = -1
    in_str = False
    escape = False
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    objs.append(text[start : i + 1])
                    start = -1
    return objs


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    # 1) 首选:整串就是合法 JSON
    try:
        data = json.loads(stripped)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    # 2) 兜底:从"思维链+JSON混杂"文本里提取 balanced 对象。
    #    优先取含 title+content 的对象(草稿结构);否则取最后一个能解析的对象。
    candidates = _find_balanced_json_objects(stripped)
    best: dict[str, Any] | None = None
    for cand in candidates:
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if "title" in obj and "content" in obj:
            best = obj  # 草稿结构优先,继续找更靠后的(取最终定稿)
        elif best is None:
            best = obj
    if best is not None:
        return best
    raise StructuredOutputError("LLM output is not valid JSON (no balanced object found)")


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
