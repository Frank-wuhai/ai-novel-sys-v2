from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import ChapterVersion, GenerationTask, ProductionRunReview


def record_production_run_review(
    session: Session,
    *,
    book_id: int,
    chapter_id: int,
    chapter_number: int,
    version: ChapterVersion,
    task: GenerationTask,
    output_data: dict[str, Any],
) -> ProductionRunReview:
    payload = build_production_run_review_payload(
        chapter_number=chapter_number,
        task_type=task.task_type,
        version_id=version.id,
        task_id=task.id,
        output_data=output_data,
    )
    row = ProductionRunReview(
        book_id=book_id,
        chapter_id=chapter_id,
        chapter_version_id=version.id,
        generation_task_id=task.id,
        status="attention" if payload.get("issues") else "pass",
        review_json=json.dumps(payload, ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row


def latest_production_run_review(session: Session, *, chapter_id: int) -> ProductionRunReview | None:
    return session.scalar(
        select(ProductionRunReview)
        .where(ProductionRunReview.chapter_id == chapter_id)
        .order_by(ProductionRunReview.id.desc())
    )


def production_run_review_payload(row: ProductionRunReview | None) -> dict[str, Any] | None:
    if not row:
        return None
    data = _loads_json(row.review_json)
    return {
        "id": row.id,
        "status": row.status,
        "chapter_version_id": row.chapter_version_id,
        "generation_task_id": row.generation_task_id,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "data": data,
    }


def build_production_pattern_memory(
    session: Session,
    *,
    book_id: int,
    chapter_number: int | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(ProductionRunReview)
            .where(ProductionRunReview.book_id == book_id)
            .order_by(ProductionRunReview.id.desc())
            .limit(max(limit, 1))
        )
    )
    issue_counts: Counter[str] = Counter()
    weak_issue_counts: Counter[str] = Counter()
    repair_counts: Counter[str] = Counter()
    unit_count_gaps: list[int] = []
    scores: list[int] = []
    source_chapters: list[int] = []
    for row in rows:
        data = _loads_json(row.review_json)
        if chapter_number and int(data.get("chapter_number") or 0) >= chapter_number:
            continue
        source_chapters.append(int(data.get("chapter_number") or 0))
        issue_counts.update(str(item) for item in data.get("issues") or [])
        repair_counts.update([str(data.get("repair_mode") or "none")])
        score = int(data.get("unit_flow_score") or 0)
        if score:
            scores.append(score)
        alignment = data.get("plan_alignment") if isinstance(data.get("plan_alignment"), dict) else {}
        expected = int(alignment.get("expected_unit_count") or 0)
        actual = int(alignment.get("actual_unit_count") or data.get("unit_count") or 0)
        if expected and actual:
            unit_count_gaps.append(expected - actual)
        for unit in data.get("weak_units") or []:
            if isinstance(unit, dict):
                weak_issue_counts.update(str(item) for item in unit.get("issues") or [])
    top_issues = _top(issue_counts)
    top_weak = _top(weak_issue_counts)
    avg_score = round(sum(scores) / len(scores)) if scores else 0
    avg_gap = round(sum(unit_count_gaps) / len(unit_count_gaps), 1) if unit_count_gaps else 0
    recommendations = _pattern_recommendations(top_issues=top_issues, top_weak=top_weak, avg_gap=avg_gap, avg_score=avg_score)
    return {
        "schema": "production_pattern_memory_v1",
        "source_review_count": len(source_chapters),
        "source_chapters": source_chapters[:limit],
        "avg_unit_flow_score": avg_score,
        "avg_unit_count_gap": avg_gap,
        "top_issues": top_issues,
        "top_weak_unit_issues": top_weak,
        "repair_mode_counts": dict(repair_counts),
        "headline": _pattern_headline(top_issues=top_issues, top_weak=top_weak, avg_gap=avg_gap, avg_score=avg_score),
        "recommendations": recommendations,
        "prompt_block": format_production_pattern_memory(
            {
                "source_review_count": len(source_chapters),
                "avg_unit_flow_score": avg_score,
                "avg_unit_count_gap": avg_gap,
                "top_issues": top_issues,
                "top_weak_unit_issues": top_weak,
                "recommendations": recommendations,
            }
        ),
    }


def format_production_pattern_memory(memory: dict[str, Any] | None) -> str:
    if not memory or int(memory.get("source_review_count") or 0) <= 0:
        return ""
    lines = [
        "生产复盘记忆（用于本章避坑，不要写进正文）：",
        f"- 最近复盘数：{memory.get('source_review_count')}；平均单元流：{memory.get('avg_unit_flow_score') or 0}；平均计划/实际差：{memory.get('avg_unit_count_gap') or 0}",
    ]
    top_issues = _label_counts(memory.get("top_issues") or [])
    top_weak = _label_counts(memory.get("top_weak_unit_issues") or [])
    if top_issues:
        lines.append(f"- 常见计划兑现问题：{top_issues}")
    if top_weak:
        lines.append(f"- 常见弱单元问题：{top_weak}")
    for item in memory.get("recommendations") or []:
        lines.append(f"- {item}")
    return "\n".join(lines)


def build_production_run_review_payload(
    *,
    chapter_number: int,
    task_type: str,
    version_id: int,
    task_id: int,
    output_data: dict[str, Any],
) -> dict[str, Any]:
    unit_repair = output_data.get("unit_flow_repair") if isinstance(output_data.get("unit_flow_repair"), dict) else {}
    alignment = output_data.get("unit_plan_alignment") if isinstance(output_data.get("unit_plan_alignment"), dict) else {}
    before = unit_repair.get("before") if isinstance(unit_repair.get("before"), dict) else {}
    after = unit_repair.get("after") if isinstance(unit_repair.get("after"), dict) else before
    repair_mode = _repair_mode(unit_repair)
    weak_units = _weak_units(after or before)
    issues: list[str] = []
    if alignment and not alignment.get("passed"):
        issues.extend(str(item) for item in alignment.get("issues") or [])
    if after and int(after.get("score") or 0) < 70:
        issues.append(f"unit_flow_low:{after.get('score')}")
    headline = _headline(alignment=alignment, unit_report=after or before, repair_mode=repair_mode, issues=issues)
    recommendations = _recommendations(alignment=alignment, unit_report=after or before, repair_mode=repair_mode, weak_units=weak_units)
    return {
        "schema": "production_run_review_v1",
        "chapter_number": chapter_number,
        "task_type": task_type,
        "version_id": version_id,
        "generation_task_id": task_id,
        "headline": headline,
        "status": "attention" if issues else "pass",
        "repair_mode": repair_mode,
        "unit_flow_score": int((after or before).get("score") or 0) if (after or before) else 0,
        "unit_count": int((after or before).get("unit_count") or 0) if (after or before) else 0,
        "plan_alignment": alignment,
        "weak_units": weak_units,
        "issues": list(dict.fromkeys(issues))[:8],
        "recommendations": recommendations,
        "repair_summary": _repair_summary(unit_repair),
    }


def _repair_mode(unit_repair: dict[str, Any]) -> str:
    if not unit_repair:
        return "none"
    if not unit_repair.get("attempted"):
        return "none"
    if unit_repair.get("mode") == "local_units":
        return "local_units"
    if unit_repair.get("mode") == "whole_chapter":
        return "whole_chapter"
    local = unit_repair.get("local_repair")
    if isinstance(local, dict) and local.get("accepted"):
        return "local_units"
    return "whole_chapter" if unit_repair.get("accepted") else "failed"


def _headline(*, alignment: dict[str, Any], unit_report: dict[str, Any], repair_mode: str, issues: list[str]) -> str:
    expected = int(alignment.get("expected_unit_count") or 0)
    actual = int(alignment.get("actual_unit_count") or unit_report.get("unit_count") or 0)
    score = int(unit_report.get("score") or 0)
    repair_label = {
        "none": "未触发返修",
        "local_units": "已局部返修失败单元",
        "whole_chapter": "已整章返修",
        "failed": "返修未被接受",
    }.get(repair_mode, repair_mode)
    if issues:
        if expected:
            return f"本章需关注：计划 {expected} 个小单元，实际 {actual} 个，单元流 {score} 分；{repair_label}。"
        return f"本章需关注：单元流 {score} 分；{repair_label}。"
    if expected:
        return f"本章单元生产正常：计划 {expected} 个，实际 {actual} 个，单元流 {score} 分；{repair_label}。"
    return f"本章单元生产正常：单元流 {score} 分；{repair_label}。"


def _recommendations(*, alignment: dict[str, Any], unit_report: dict[str, Any], repair_mode: str, weak_units: list[dict[str, Any]]) -> list[str]:
    rows: list[str] = []
    if alignment and not alignment.get("passed"):
        rows.append("继续写作会优先按计划-正文对账补足缺失单元或弱单元。")
    if repair_mode == "local_units":
        rows.append("局部返修已替换失败单元，建议下一步复检规则质检和主编审稿。")
    elif repair_mode == "whole_chapter":
        rows.append("整章返修已执行，建议观察是否仍反复出现同类单元问题。")
    elif repair_mode == "failed":
        rows.append("返修未被接受，下一步应降低单次修订范围或改为整章重写。")
    if weak_units:
        indexes = "、".join(str(item.get("index")) for item in weak_units[:5])
        rows.append(f"下轮生产重点关注第 {indexes} 单元。")
    if int(unit_report.get("score") or 0) < 70:
        rows.append("单元流仍低于 70 分，下一轮生产应优先优化单元衔接；当前章是否通过以质检结论和人工阅读为准。")
    return list(dict.fromkeys(rows))[:6]


def _weak_units(unit_report: dict[str, Any]) -> list[dict[str, Any]]:
    units = unit_report.get("units") if isinstance(unit_report.get("units"), list) else []
    return [
        {
            "index": item.get("index"),
            "score": item.get("score"),
            "issues": item.get("issues", []),
            "summary": item.get("summary", ""),
        }
        for item in units
        if isinstance(item, dict) and int(item.get("score") or 0) < 70
    ][:8]


def _repair_summary(unit_repair: dict[str, Any]) -> dict[str, Any]:
    if not unit_repair:
        return {"attempted": False}
    unit_results = unit_repair.get("unit_results")
    if not isinstance(unit_results, list):
        local = unit_repair.get("local_repair")
        unit_results = local.get("unit_results") if isinstance(local, dict) and isinstance(local.get("unit_results"), list) else []
    return {
        "attempted": bool(unit_repair.get("attempted")),
        "accepted": bool(unit_repair.get("accepted")),
        "mode": _repair_mode(unit_repair),
        "unit_results": unit_results[:8],
    }


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _top(counter: Counter[str], limit: int = 5) -> list[dict[str, Any]]:
    return [
        {"name": name, "count": count, "label": _issue_label(name)}
        for name, count in counter.most_common(limit)
        if name
    ]


def _label_counts(items: list[dict[str, Any]]) -> str:
    return "，".join(f"{item.get('label') or item.get('name')}×{item.get('count')}" for item in items[:5])


def _pattern_headline(*, top_issues: list[dict[str, Any]], top_weak: list[dict[str, Any]], avg_gap: float, avg_score: int) -> str:
    fragments: list[str] = []
    if top_weak:
        fragments.append("、".join(str(item.get("label") or item.get("name")) for item in top_weak[:2]))
    if avg_gap > 1:
        fragments.append("实际单元数偏少")
    if avg_score and avg_score < 70:
        fragments.append("单元流均分偏低")
    if not fragments and top_issues:
        fragments.append("、".join(str(item.get("label") or item.get("name")) for item in top_issues[:2]))
    if not fragments:
        return "近期生产复盘没有稳定失败模式。"
    return "系统记住了：最近主要问题是" + "、".join(fragments) + "，下一章会自动加强。"


def _pattern_recommendations(
    *,
    top_issues: list[dict[str, Any]],
    top_weak: list[dict[str, Any]],
    avg_gap: float,
    avg_score: int,
) -> list[str]:
    names = {str(item.get("name")) for item in [*top_issues, *top_weak]}
    rows: list[str] = []
    if avg_gap > 1:
        rows.append("下一章单元计划必须明确 6-8 个单元，并要求每单元末交出可接后果。")
    if "handoff" in names or any(name.startswith("unit_count_low") for name in names):
        rows.append("强化承接：每个单元第一句接上一单元后果，最后一句交给下一单元。")
    if "reaction" in names:
        rows.append("强化人物反应：每个关键动作后补犹豫、疼痛、误判、沉默、试探或情绪变化。")
    if "action" in names:
        rows.append("强化动作链：每个单元至少有一个可见动作改变局面。")
    if "obstacle" in names:
        rows.append("强化阻碍：用人物、环境、伤势、利益、规矩或误判制造阻力。")
    if "consequence" in names:
        rows.append("强化后果：主角动作必须换来收益、损失、暴露、误会或更大麻烦。")
    if "info_gain" in names:
        rows.append("强化信息增量：每个单元给读者一条新线索、规则、身份、代价或局面变化。")
    if avg_score and avg_score < 70:
        rows.append("本章生成后不建议直接审批，优先复检小单元流。")
    return list(dict.fromkeys(rows))[:6]


def _issue_label(name: str) -> str:
    labels = {
        "handoff": "承接断裂",
        "reaction": "人物反应弱",
        "action": "动作链弱",
        "obstacle": "阻碍不足",
        "consequence": "后果没落地",
        "info_gain": "信息增量弱",
        "goal": "目标不清",
        "length": "长度不稳",
        "precision": "表达逻辑风险",
    }
    if name.startswith("unit_count_low"):
        return "实际单元数偏少"
    if name.startswith("unit_flow_low"):
        return "单元流偏低"
    if name.startswith("weak_units"):
        return "存在弱单元"
    return labels.get(name, name)
