"""系统体检看板（2026-09-21 人声攻坚元能力第一件）。

背景：用户指出系统缺乏「自己发现问题」的元能力——每章缺陷能被门禁发现，
但跨章/跨版本的趋势不可审计，「这次优化是否真的让系统变强」无法验证。
本模块把趋势从主张变成数据：纯 SQL/JSON 计算、零 LLM 调用、对库零写入。

预登记验收标准见 claude_handoff 报告第九节（先于实现登记）：
只读、全量覆盖、三视图（版本轨迹/慢性低分维度/失败分类）、数字可复算、确定性。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    Chapter,
    ChapterVersion,
    GenerationTask,
    LLMRequestLog,
    QualityReport,
)
from app.services.quality import chinese_chars

# 慢性低分维度判定：维度分 < 60 记为低分命中，命中率 >= 60% 列入慢性榜。
# 60 取各自门禁带（55-65）的中位，是看板趋势口径不是门禁口径——
# 看板只负责呈现趋势，不参与放行判定。
CHRONIC_LOW_SCORE = 60
CHRONIC_HIT_RATE = 0.6


@dataclass(frozen=True)
class VersionPoint:
    version_id: int
    version_number: int
    title: str
    status: str
    source: str
    chars: int
    created_at: str
    quality_score: int | None
    quality_passed: bool | None
    quality_verdict: str
    reading_level: str


@dataclass(frozen=True)
class ChapterTrajectory:
    chapter_number: int
    chapter_status: str
    points: list[VersionPoint] = field(default_factory=list)


@dataclass(frozen=True)
class ChronicDimension:
    name: str
    reports_seen: int
    low_hits: int
    hit_rate: float
    mean_score: float
    min_score: int


@dataclass(frozen=True)
class DimensionRegression:
    """近期塌陷维度：最近几份报告均值显著低于此前均值。

    慢性低分榜（全期命中率）会漏掉「早期及格、近期塌陷」的回归型信号——
    例如 dialogue_fullness 前 5 份报告 65、最近 3 份全 35，全期命中率 50%
    不上慢性榜，但这正是「系统是不是真的在变好」最敏感的反面证据。
    """

    name: str
    earlier_mean: float
    recent_mean: float
    delta: float
    earlier_count: int
    recent_count: int


# 近期窗口：最新 3 份报告；塌陷判定：近期均值 - 早期均值 <= -10。
REGRESSION_RECENT_WINDOW = 3
REGRESSION_DELTA = -10


def _loads_json(value: str | None) -> dict:
    try:
        data = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def build_health_report(session: Session, *, book_id: int) -> dict:
    """组装体检报告。只发 SELECT，对库零写入。"""
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number)
        )
    )
    trajectories: list[ChapterTrajectory] = []
    all_reports: list[tuple[int, int, dict]] = []  # (version_id, report_id, report_json)
    for chapter in chapters:
        versions = list(
            session.scalars(
                select(ChapterVersion)
                .where(ChapterVersion.chapter_id == chapter.id)
                .order_by(ChapterVersion.id)
            )
        )
        points: list[VersionPoint] = []
        for version in versions:
            quality = session.scalar(
                select(QualityReport)
                .where(QualityReport.chapter_version_id == version.id)
                .order_by(QualityReport.id.desc())
            )
            score: int | None = None
            passed: bool | None = None
            verdict = ""
            reading_level = ""
            if quality:
                score = quality.score
                passed = bool(quality.passed)
                data = _loads_json(quality.report)
                verdict = str(data.get("verdict") or "")
                assessment = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
                reading_level = str(assessment.get("level") or "")
                all_reports.append((version.id, quality.id, data))
            points.append(
                VersionPoint(
                    version_id=version.id,
                    version_number=version.version_number,
                    title=version.title or "",
                    status=version.status or "",
                    source=version.source or "",
                    chars=chinese_chars(version.content or ""),
                    created_at=version.created_at.isoformat(sep=" ", timespec="seconds") if version.created_at else "",
                    quality_score=score,
                    quality_passed=passed,
                    quality_verdict=verdict,
                    reading_level=reading_level,
                )
            )
        trajectories.append(
            ChapterTrajectory(
                chapter_number=chapter.chapter_number,
                chapter_status=chapter.status or "",
                points=points,
            )
        )

    chronic = _chronic_dimensions(all_reports)
    regressions = _dimension_regressions(all_reports)
    failures = _failure_breakdown(session, book_id=book_id)
    judge_stats = _judge_stats(session, book_id=book_id, all_reports=all_reports)

    total_quality_reports = len(
        list(session.scalars(select(QualityReport.id).join(ChapterVersion, QualityReport.chapter_version_id == ChapterVersion.id).join(Chapter, ChapterVersion.chapter_id == Chapter.id).where(Chapter.book_id == book_id)))
    )
    return {
        "book_id": book_id,
        "chapter_count": len(chapters),
        "version_count": sum(len(t.points) for t in trajectories),
        "quality_report_count": len(all_reports),
        "quality_report_total": total_quality_reports,
        "trajectories": trajectories,
        "chronic_dimensions": chronic,
        "dimension_regressions": regressions,
        "failures": failures,
        "judge": judge_stats,
        "calibration": {
            "chronic_low_score": CHRONIC_LOW_SCORE,
            "chronic_hit_rate": CHRONIC_HIT_RATE,
        },
    }


def _chronic_dimensions(all_reports: list[tuple[int, int, dict]]) -> list[ChronicDimension]:
    stats: dict[str, list[int]] = {}
    for _version_id, _report_id, data in all_reports:
        dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
        for name, raw in dimensions.items():
            try:
                score = int(raw)
            except (TypeError, ValueError):
                continue
            stats.setdefault(name, []).append(score)
    chronic: list[ChronicDimension] = []
    for name in sorted(stats):
        scores = stats[name]
        low_hits = sum(1 for score in scores if score < CHRONIC_LOW_SCORE)
        hit_rate = low_hits / len(scores)
        if hit_rate >= CHRONIC_HIT_RATE:
            chronic.append(
                ChronicDimension(
                    name=name,
                    reports_seen=len(scores),
                    low_hits=low_hits,
                    hit_rate=round(hit_rate, 3),
                    mean_score=round(sum(scores) / len(scores), 1),
                    min_score=min(scores),
                )
            )
    chronic.sort(key=lambda item: (item.mean_score, item.name))
    return chronic


def _dimension_regressions(all_reports: list[tuple[int, int, dict]]) -> list[DimensionRegression]:
    series: dict[str, list[int]] = {}
    for _version_id, _report_id, data in all_reports:
        dimensions = data.get("dimensions") if isinstance(data.get("dimensions"), dict) else {}
        for name, raw in dimensions.items():
            try:
                score = int(raw)
            except (TypeError, ValueError):
                continue
            series.setdefault(name, []).append(score)
    regressions: list[DimensionRegression] = []
    window = REGRESSION_RECENT_WINDOW
    for name in sorted(series):
        scores = series[name]
        if len(scores) <= window:
            continue
        earlier = scores[:-window]
        recent = scores[-window:]
        earlier_mean = sum(earlier) / len(earlier)
        recent_mean = sum(recent) / len(recent)
        delta = recent_mean - earlier_mean
        if delta <= REGRESSION_DELTA:
            regressions.append(
                DimensionRegression(
                    name=name,
                    earlier_mean=round(earlier_mean, 1),
                    recent_mean=round(recent_mean, 1),
                    delta=round(delta, 1),
                    earlier_count=len(earlier),
                    recent_count=len(recent),
                )
            )
    regressions.sort(key=lambda item: (item.delta, item.name))
    return regressions


def _failure_breakdown(session: Session, *, book_id: int) -> dict:
    tasks = list(session.scalars(select(GenerationTask).where(GenerationTask.book_id == book_id)))
    task_counts: dict[str, dict[str, int]] = {}
    failure_categories: dict[str, int] = {}
    for task in tasks:
        bucket = task_counts.setdefault(task.task_type, {"completed": 0, "failed": 0, "other": 0})
        key = task.status if task.status in ("completed", "failed") else "other"
        bucket[key] += 1
        if task.status == "failed":
            output = _loads_json(task.output_json)
            category = str(output.get("error_category") or output.get("error_type") or "unknown")
            failure_categories[f"{task.task_type}:{category}"] = failure_categories.get(f"{task.task_type}:{category}", 0) + 1

    log_counts: dict[str, int] = {}
    for log in session.scalars(select(LLMRequestLog).where(LLMRequestLog.book_id == book_id)):
        key = f"{log.task_type}:{log.status}"
        log_counts[key] = log_counts.get(key, 0) + 1

    return {
        "task_counts": task_counts,
        "failure_categories": dict(sorted(failure_categories.items())),
        "llm_log_counts": dict(sorted(log_counts.items())),
    }


def _judge_stats(session: Session, *, book_id: int, all_reports: list[tuple[int, int, dict]]) -> dict:
    judge_tasks = list(
        session.scalars(
            select(GenerationTask).where(
                GenerationTask.book_id == book_id,
                GenerationTask.task_type == "prose_judgement",
            )
        )
    )
    completed = sum(1 for task in judge_tasks if task.status == "completed")
    failed = sum(1 for task in judge_tasks if task.status == "failed")
    gap_total = 0
    for _version_id, _report_id, data in all_reports:
        judgement = data.get("prose_judgement") if isinstance(data.get("prose_judgement"), dict) else {}
        try:
            gap_total += int(judgement.get("gap_count") or 0)
        except (TypeError, ValueError):
            continue
    return {
        "judge_runs": len(judge_tasks),
        "judge_completed": completed,
        "judge_failed": failed,
        "judge_success_rate": round(completed / len(judge_tasks), 3) if judge_tasks else None,
        "judge_gap_total": gap_total,
    }


def render_health_report_markdown(report: dict) -> str:
    lines: list[str] = []
    lines.append(f"# 系统体检报告 · book {report['book_id']}")
    lines.append("")
    lines.append(
        f"覆盖：{report['chapter_count']} 章 / {report['version_count']} 版本 / "
        f"质检报告 {report['quality_report_total']} 份"
        f"（趋势口径：每版本取最新一份去重，计 {report['quality_report_count']} 份；"
        f"失败/判卷统计用全量任务记录，不去重）"
    )
    cal = report["calibration"]
    lines.append(
        f"口径：慢性低分 = 维度分 < {cal['chronic_low_score']} 且命中率 >= "
        f"{int(cal['chronic_hit_rate'] * 100)}%（趋势口径，非门禁口径）"
    )
    lines.append("")

    lines.append("## 一、版本分数轨迹")
    for trajectory in report["trajectories"]:
        lines.append("")
        lines.append(f"### 第 {trajectory.chapter_number} 章（{trajectory.chapter_status}）")
        lines.append("")
        lines.append("| 版本 | 标题 | 字数 | 来源 | 状态 | 质检分 | verdict | 阅读层级 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for point in trajectory.points:
            score = point.quality_score if point.quality_score is not None else "—"
            lines.append(
                f"| v{point.version_id}（第{point.version_number}稿） | {point.title or '—'} | "
                f"{point.chars} | {point.source or '—'} | {point.status} | {score} | "
                f"{point.quality_verdict or '—'} | {point.reading_level or '—'} |"
            )
    lines.append("")

    lines.append("## 二、慢性低分维度（跨版本持续低分 = 尺子和稿子的长期矛盾）")
    lines.append("")
    chronic: list[ChronicDimension] = report["chronic_dimensions"]
    if chronic:
        lines.append("| 维度 | 报告数 | 低分命中 | 命中率 | 均值 | 最低 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in chronic:
            lines.append(
                f"| {item.name} | {item.reports_seen} | {item.low_hits} | "
                f"{item.hit_rate:.0%} | {item.mean_score} | {item.min_score} |"
            )
    else:
        lines.append("（无慢性低分维度）")
    lines.append("")

    lines.append("## 二-b、近期塌陷维度（早期及格、近期显著下滑 = 回归型警报）")
    lines.append("")
    regressions: list[DimensionRegression] = report["dimension_regressions"]
    if regressions:
        lines.append("| 维度 | 早期均值 | 近期均值 | 变化 | 早期报告数 | 近期报告数 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for item in regressions:
            lines.append(
                f"| {item.name} | {item.earlier_mean} | {item.recent_mean} | "
                f"{item.delta:+.1f} | {item.earlier_count} | {item.recent_count} |"
            )
    else:
        lines.append("（无近期塌陷维度）")
    lines.append("")

    lines.append("## 三、失败分类计数")
    lines.append("")
    failures = report["failures"]
    lines.append("### 生成任务")
    lines.append("")
    lines.append("| 任务类型 | 成功 | 失败 | 其他 |")
    lines.append("| --- | --- | --- | --- |")
    for task_type in sorted(failures["task_counts"]):
        bucket = failures["task_counts"][task_type]
        lines.append(f"| {task_type} | {bucket['completed']} | {bucket['failed']} | {bucket['other']} |")
    lines.append("")
    lines.append("### 失败类别")
    lines.append("")
    if failures["failure_categories"]:
        for category, count in failures["failure_categories"].items():
            lines.append(f"- {category} × {count}")
    else:
        lines.append("- （无失败记录）")
    lines.append("")
    lines.append("### LLM 请求日志")
    lines.append("")
    for key, count in failures["llm_log_counts"].items():
        lines.append(f"- {key} × {count}")
    lines.append("")

    judge = report["judge"]
    lines.append("## 四、判卷（prose judgement）")
    lines.append("")
    rate = judge["judge_success_rate"]
    lines.append(
        f"- 判卷运行 {judge['judge_runs']} 次：成功 {judge['judge_completed']} / "
        f"失败 {judge['judge_failed']}（成功率 {f'{rate:.0%}' if rate is not None else '—'}）"
    )
    lines.append(f"- 缺口总数（各报告 gap_count 累加）：{judge['judge_gap_total']}")
    lines.append("")
    return "\n".join(lines)
