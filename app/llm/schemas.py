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


# ---------------------------------------------------------------------------
# 成文判据 (prose_judgement_v1 J1-J5) 判卷输出
#
# 与 ReviewOutput 的分工：ReviewOutput 是主编审稿 (打分+放行判断，接 editorial_gate)；
# ProseJudgementOutput 是缺口表 (只列缺口，不打分、不放行/拦截) —— prose_judgement_v1
# 明确规定成文判据"无自动 FAIL，不自动拦稿"，所以这里没有 verdict/score 字段。
# 判据有效性规则：判不出原文锚点的判定无效 (解析时丢弃并计数 dropped_no_anchor)。
# ---------------------------------------------------------------------------

PROSE_JUDGEMENT_CRITERIA = ("J1", "J2", "J3", "J4", "J5")


@dataclass
class ProseJudgementGap:
    criterion: str  # J1-J5
    anchor: str  # 原文锚点 (verbatim 引用，必填，空则该条无效)
    explanation: str = ""  # 白话解释
    fix_direction: str = ""  # 修法方向 (不代写正文)

    def to_dict(self) -> dict[str, Any]:
        return {
            "criterion": self.criterion,
            "anchor": self.anchor,
            "explanation": self.explanation,
            "fix_direction": self.fix_direction,
        }


@dataclass
class ProseJudgementOutput:
    gaps: list[ProseJudgementGap] = field(default_factory=list)
    summary: str = ""
    dropped_no_anchor: int = 0  # 因缺原文锚点被判无效的条数

    def to_dict(self) -> dict[str, Any]:
        return {
            "gaps": [gap.to_dict() for gap in self.gaps],
            "gap_count": len(self.gaps),
            "dropped_no_anchor": self.dropped_no_anchor,
            "summary": self.summary,
        }


def _normalize_criterion(value: Any) -> str | None:
    """容忍 'J1' / 'j1' / 'J1 读者入口' 等写法，归一到 J1-J5；无法识别返回 None。"""
    text = str(value or "").strip().upper()
    match = re.match(r"^J([1-5])\b", text)
    if not match:
        return None
    return f"J{match.group(1)}"


def parse_prose_judgement_output(text: str) -> ProseJudgementOutput:
    data = _extract_json(text)
    raw_gaps = data.get("gaps")
    if not isinstance(raw_gaps, list):
        raise StructuredOutputError("prose judgement output gaps must be an array")
    gaps: list[ProseJudgementGap] = []
    dropped = 0
    for item in raw_gaps:
        if not isinstance(item, dict):
            dropped += 1
            continue
        criterion = _normalize_criterion(item.get("criterion"))
        anchor = str(item.get("anchor") or "").strip()
        if criterion is None or not anchor:
            # prose_judgement_v1：判不出锚点的判定无效
            dropped += 1
            continue
        gaps.append(
            ProseJudgementGap(
                criterion=criterion,
                anchor=anchor,
                explanation=str(item.get("explanation") or "").strip(),
                fix_direction=str(item.get("fix_direction") or "").strip(),
            )
        )
    return ProseJudgementOutput(
        gaps=gaps,
        summary=str(data.get("summary") or "").strip(),
        dropped_no_anchor=dropped,
    )
