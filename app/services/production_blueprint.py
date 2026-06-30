from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from app.services.chapter_standards import extract_max_chars, extract_min_chars


@dataclass(frozen=True)
class ProductionBlueprint:
    prompt_block: str
    goal: str
    required_beats: str
    constraints: str
    target_min_chars: int
    target_max_chars: int
    target_unit_count: int
    audit: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "required_beats": self.required_beats,
            "constraints": self.constraints,
            "target_min_chars": self.target_min_chars,
            "target_max_chars": self.target_max_chars,
            "target_unit_count": self.target_unit_count,
            "prompt_block_chars": len(self.prompt_block),
            "audit": self.audit,
        }


def build_production_blueprint(
    *,
    chapter_number: int,
    mode: str,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str,
    canon_context: str,
    author_preferences: str,
    chapter_unit_plan: dict[str, Any],
    book_aesthetic_standard: dict[str, Any],
    style_contract: dict[str, Any] | None = None,
    quality_report: str | None = None,
    previous_content: str = "",
    fresh_rewrite: bool = False,
    rewrite_mode: bool = False,
) -> ProductionBlueprint:
    target_min = extract_min_chars(goal, required_beats, constraints, default=3000)
    target_max = extract_max_chars(goal, required_beats, constraints, default=4500)
    target_min = max(3000, target_min)
    target_max = min(max(target_min, target_max), 5200)
    target_units = _target_units(chapter_unit_plan, target_min=target_min, target_max=target_max)
    compact_goal = _first_useful([goal], 180) or f"第{chapter_number}章完整成章"
    beats = _select_beats(required_beats, limit=8)
    constraints_rows = _select_constraints(constraints, limit=8)
    previous = _tail(previous_chapter_context, 700)
    canon = _select_canon(canon_context, limit=6)
    prefs = _select_preferences(author_preferences, book_aesthetic_standard, limit=6)
    style_rows = _style_contract_rows(style_contract or {}, limit=14)
    quality = _quality_focus(quality_report)
    old_draft = _previous_content_focus(previous_content, enabled=bool(previous_content and (fresh_rewrite or rewrite_mode)))
    unit_rows = _unit_rows(chapter_unit_plan, limit=target_units)
    prompt_lines = [
        "【稳定生产蓝图｜最高优先级】",
        f"- 章节: 第{chapter_number}章；模式:{mode}。",
        f"- 字数硬范围: {target_min}-{target_max} 中文字符；超过上限视为失败，不能用多余支线凑字数。",
        f"- 结构硬范围: 正文写成 {target_units} 个连续小单元；不要膨胀成 10 个以上片段。",
        f"- 本章目标: {compact_goal}",
    ]
    if previous:
        prompt_lines.extend(["- 前章承接: " + previous])
    if beats:
        prompt_lines.append("- 必须兑现:")
        prompt_lines.extend(f"  {idx}. {item}" for idx, item in enumerate(beats, start=1))
    if unit_rows:
        prompt_lines.append("- 小单元蓝图:")
        prompt_lines.extend(f"  {idx}. {item}" for idx, item in enumerate(unit_rows, start=1))
    if canon:
        prompt_lines.append("- Canon/设定只保留这些硬锚点:")
        prompt_lines.extend(f"  - {item}" for item in canon)
    if prefs:
        prompt_lines.append("- 作者口味:")
        prompt_lines.extend(f"  - {item}" for item in prefs)
    if style_rows:
        prompt_lines.append("- 风格/DNA/命名硬契约:")
        prompt_lines.extend(f"  - {item}" for item in style_rows)
    if quality:
        prompt_lines.append("- 本轮失败焦点:")
        prompt_lines.extend(f"  - {item}" for item in quality)
    if old_draft:
        prompt_lines.append("- 旧稿仅保留:")
        prompt_lines.extend(f"  - {item}" for item in old_draft)
    if constraints_rows:
        prompt_lines.append("- 禁止/底线:")
        prompt_lines.extend(f"  - {item}" for item in constraints_rows)
    prompt_lines.extend(
        [
            "- 写法: 先确认本章主角此刻想解决的小问题，再用阻碍、动作、反应、后果推进；不要输出导演单、验收单或系统说明。",
            "- 失败处理预防: 如果旧质检要求和本蓝图冲突，以本蓝图为准；宁可删掉支线，也不要突破字数和单元数。",
            "【稳定生产蓝图结束】",
        ]
    )
    prompt_block = "\n".join(prompt_lines)
    return ProductionBlueprint(
        prompt_block=prompt_block,
        goal=compact_goal,
        required_beats="；".join(beats),
        constraints="\n".join([f"正文字数:{target_min}-{target_max}中文字符；目标小单元:{target_units}", *constraints_rows]),
        target_min_chars=target_min,
        target_max_chars=target_max,
        target_unit_count=target_units,
        audit={
            "schema": "production_blueprint_v1",
            "mode": mode,
            "input_goal_chars": len(goal or ""),
            "input_required_beats_chars": len(required_beats or ""),
            "input_constraints_chars": len(constraints or ""),
            "previous_context_chars": len(previous_chapter_context or ""),
            "canon_context_chars": len(canon_context or ""),
            "quality_report_chars": len(quality_report or ""),
            "previous_content_chars": len(previous_content or ""),
            "selected_beats": len(beats),
            "selected_constraints": len(constraints_rows),
            "selected_canon": len(canon),
            "selected_preferences": len(prefs),
            "selected_style_contract": len(style_rows),
            "selected_quality_focus": len(quality),
        },
    )


def classify_quality_failure(report_data: dict[str, Any]) -> dict[str, Any]:
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    issues = [str(item) for item in report_data.get("issues") or []]
    warnings = [str(item) for item in report_data.get("warnings") or []]
    chapter_units = report_data.get("chapter_unit_report") if isinstance(report_data.get("chapter_unit_report"), dict) else {}
    count = int(report_data.get("chinese_chars") or 0)
    thresholds = report_data.get("thresholds") if isinstance(report_data.get("thresholds"), dict) else {}
    max_chars = int(thresholds.get("max_chars") or 0)
    unit_count = int(chapter_units.get("unit_count") or 0)
    unit_score = int(chapter_units.get("score") or dimensions.get("chapter_unit_flow") or 0)
    structural_reasons: list[str] = []
    local_reasons: list[str] = []
    if any(issue.startswith(("too_long", "too_short")) for issue in issues):
        structural_reasons.append("length_out_of_range")
    if max_chars and count > max_chars:
        structural_reasons.append("over_target_max_chars")
    if unit_count >= 10:
        structural_reasons.append("unit_count_exploded")
    if unit_score and unit_score < 65:
        structural_reasons.append("unit_flow_structural")
    if int(dimensions.get("brief_coverage") or 100) < 55:
        structural_reasons.append("brief_coverage_structural")
    for name in ("dialogue_fullness", "imageable_paragraphs", "payoff_grounding", "scene_atmosphere"):
        if int(dimensions.get(name) or 100) < 60:
            local_reasons.append(name)
    if any("chapter_type_gate" in item for item in issues + warnings):
        structural_reasons.append("chapter_type_gate")
    category = "structure_rewrite" if structural_reasons else ("local_patch" if local_reasons else "targeted_revision")
    return {
        "schema": "quality_failure_classification_v1",
        "category": category,
        "structural_reasons": list(dict.fromkeys(structural_reasons)),
        "local_reasons": list(dict.fromkeys(local_reasons))[:6],
        "recommended_revision_mode": "rewrite" if category == "structure_rewrite" else "targeted",
    }


def _target_units(plan: dict[str, Any], *, target_min: int, target_max: int) -> int:
    raw = int(plan.get("target_unit_count") or 0) if isinstance(plan, dict) else 0
    if raw:
        return max(6, min(8, raw))
    midpoint = (target_min + target_max) // 2
    return max(6, min(8, round(midpoint / 560)))


def _select_beats(text: str, *, limit: int) -> list[str]:
    blocked = (
        "质检报告 #",
        "验收清单",
        "修订合同",
        "reading_assessment",
        "weak_",
        "score=",
        "系统自动",
        "修订模式",
        "revision_mode",
        "系统修订判定",
        "处理强度",
        "置信度",
        "意见理解规则",
        "禁止项",
    )
    return _select_lines(text, limit=limit, max_chars=130, blocked=blocked)


def _select_constraints(text: str, *, limit: int) -> list[str]:
    important = []
    for item in _split(text):
        if any(marker in item for marker in ("不要", "禁止", "不得", "字数", "Canon", "系统", "面板", "自检", "主角行动链", "章末")):
            important.append(_one_line(item, 130))
    return list(dict.fromkeys(important))[:limit]


def _select_canon(text: str, *, limit: int) -> list[str]:
    rows = []
    for item in _split(text):
        if any(marker in item for marker in ("主角", "能力", "门派", "世界", "上一章", "未解决", "信物", "账册", "赵乾", "林北")):
            rows.append(_one_line(item, 130))
    return list(dict.fromkeys(rows))[:limit]


def _select_preferences(author_preferences: str, standard: dict[str, Any], *, limit: int) -> list[str]:
    rows = _select_lines(author_preferences, limit=limit, max_chars=130, blocked=("反馈调整#", "JSON"))
    for key in ("narrative_flavor", "scene_density", "forbidden_tone"):
        for item in standard.get(key) or []:
            rows.append(_one_line(str(item), 130))
    return list(dict.fromkeys([item for item in rows if item]))[:limit]


def _style_contract_rows(contract: dict[str, Any], *, limit: int) -> list[str]:
    rows: list[str] = []
    profile = str(contract.get("aesthetic_profile") or "").strip()
    if profile:
        rows.append("【作品审美画像】")
        rows.extend(_contract_block_rows(profile, max_rows=5, max_chars=150))
    standard = contract.get("book_aesthetic_standard") if isinstance(contract.get("book_aesthetic_standard"), dict) else {}
    standard_rows: list[str] = []
    for key in ("narrative_flavor", "scene_density", "character_voice", "forbidden_tone"):
        for item in standard.get(key) or []:
            standard_rows.append(_one_line(str(item), 140))
    if standard_rows:
        rows.append("【作品级审美标尺】")
        rows.extend(list(dict.fromkeys(item for item in standard_rows if item))[:3])
    story_dna = str(contract.get("story_dna") or "").strip()
    chapter_engine = str(contract.get("chapter_engine") or "").strip()
    if story_dna:
        rows.append("【本书作品DNA / 本章发动机】")
        rows.extend(_contract_block_rows(story_dna, max_rows=2, max_chars=160))
    if chapter_engine:
        rows.append(f"本章优先发动机: {chapter_engine}")
    naming = str(contract.get("naming_governance") or "").strip()
    if naming:
        rows.append("命名治理")
        rows.extend(_contract_block_rows(naming, max_rows=3, max_chars=160))
    return list(dict.fromkeys(item for item in rows if item))[:limit]


def _contract_block_rows(text: str, *, max_rows: int, max_chars: int) -> list[str]:
    rows = []
    for line in str(text or "").splitlines():
        compact = line.strip()
        if not compact or compact in {"【作品审美画像】", "【作品审美画像结束】", "【作品DNA】", "【作品DNA结束】"}:
            continue
        if compact.startswith("【") and compact.endswith("】"):
            continue
        compact = re.sub(r"^[-*]\s*", "", compact)
        rows.append(_one_line(compact, max_chars))
    return list(dict.fromkeys(rows))[:max_rows]


def _quality_focus(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError:
        return _select_lines(value, limit=5, max_chars=140, blocked=())
    if not isinstance(data, dict):
        return []
    rows: list[str] = []
    failure = data.get("production_failure_classification") if isinstance(data.get("production_failure_classification"), dict) else {}
    if failure:
        rows.append(f"失败类型:{failure.get('category')}；建议:{failure.get('recommended_revision_mode')}")
        rows.extend(str(item) for item in failure.get("structural_reasons") or [])
    for item in data.get("issues") or []:
        rows.append(str(item))
    dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
    weak = sorted(((name, int(score)) for name, score in dimensions.items() if isinstance(score, int) and score < 65), key=lambda row: row[1])
    rows.extend(f"{name}={score}" for name, score in weak[:5])
    return [_one_line(item, 140) for item in list(dict.fromkeys(rows))[:8]]


def _previous_content_focus(value: str, *, enabled: bool) -> list[str]:
    if not enabled:
        return []
    text = str(value or "")
    if not text:
        return []
    return [_one_line(text[:260], 260), _one_line(text[-360:], 360)]


def _unit_rows(plan: dict[str, Any], *, limit: int) -> list[str]:
    rows = []
    units = plan.get("units") if isinstance(plan.get("units"), list) else []
    for item in units[:limit]:
        if not isinstance(item, dict):
            continue
        rows.append(
            _one_line(
                f"{item.get('role')}:目标={item.get('goal')}；阻碍={item.get('obstacle')}；动作={item.get('action')}；承接={item.get('handoff')}",
                180,
            )
        )
    return rows


def _select_lines(text: str, *, limit: int, max_chars: int, blocked: tuple[str, ...]) -> list[str]:
    rows = []
    for item in _split(text):
        if len(item) < 4:
            continue
        if any(marker in item for marker in blocked):
            continue
        rows.append(_one_line(item, max_chars))
    return list(dict.fromkeys(rows))[:limit]


def _split(text: str) -> list[str]:
    normalized = re.sub(r"\s+", " ", str(text or "").strip())
    parts = re.split(r"[；;\n]+|(?<=。)|(?<=！)|(?<=？)", normalized)
    return [part.strip(" -\t") for part in parts if part.strip(" -\t")]


def _first_useful(values: list[str], max_chars: int) -> str:
    for value in values:
        for item in _split(value):
            if len(item) >= 4 and not any(marker in item for marker in ("质检", "系统自动", "修订合同")):
                return _one_line(item, max_chars)
    return ""


def _tail(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return "…" + value[-limit:]


def _one_line(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
