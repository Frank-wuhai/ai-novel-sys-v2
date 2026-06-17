from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.feedback import submit_revision_suggestion


@dataclass(frozen=True)
class RevisionSupervisionReport:
    status: str
    latest_score: int | None
    previous_score: int | None
    failed_revision_count: int
    blockers: list[str]
    next_action: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "latest_score": self.latest_score,
            "previous_score": self.previous_score,
            "failed_revision_count": self.failed_revision_count,
            "blockers": self.blockers,
            "next_action": self.next_action,
        }


@dataclass(frozen=True)
class RevisionBudgetRecovery:
    status: str
    recovery_version_id: int | None
    recovery_brief_id: int | None
    source_version_id: int | None
    message: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "recovery_version_id": self.recovery_version_id,
            "recovery_brief_id": self.recovery_brief_id,
            "source_version_id": self.source_version_id,
            "message": self.message,
        }


def supervise_revision_trend(session: Session, *, book_id: int, chapter_number: int) -> RevisionSupervisionReport:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return RevisionSupervisionReport("missing_chapter", None, None, 0, ["chapter not found"], "先生成章节说明。")
    rows = _failed_quality_rows(session, chapter_id=chapter.id, limit=5)
    if not rows:
        return RevisionSupervisionReport("no_failed_revision", None, None, 0, [], "按正常生产流程推进。")
    latest_version, latest_quality, latest_report = rows[0]
    previous_score = int(rows[1][1].score or 0) if len(rows) > 1 else None
    latest_score = int(latest_quality.score or 0)
    blockers: list[str] = []
    if previous_score is not None and latest_score <= previous_score:
        blockers.append(f"latest_not_improved:v{latest_version.id}:{latest_score}<={previous_score}")
    if len(rows) >= 2:
        latest_dims = latest_report.get("dimensions") if isinstance(latest_report.get("dimensions"), dict) else {}
        previous_dims = rows[1][2].get("dimensions") if isinstance(rows[1][2].get("dimensions"), dict) else {}
        watched = ("brief_coverage", "reader_momentum", "hook_strength", "chapter_unit_flow", "scene_atmosphere", "writer_craft")
        degraded = [
            f"{name}:{int(previous_dims.get(name) or 0)}->{int(latest_dims.get(name) or 0)}"
            for name in watched
            if int(previous_dims.get(name) or 0) - int(latest_dims.get(name) or 0) >= 8
        ]
        if len(degraded) >= 2:
            blockers.append("dimension_degraded:" + ",".join(degraded[:4]))
    status = "degrading" if blockers else "stable"
    next_action = "自动回退到近期最佳稿并换策略修订。" if blockers else "继续当前修订策略，但不得扩大修订范围。"
    return RevisionSupervisionReport(status, latest_score, previous_score, len(rows), blockers, next_action)


def apply_revision_budget_recovery(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
) -> RevisionBudgetRecovery:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return RevisionBudgetRecovery("missing_chapter", None, None, None, "找不到章节，无法自动恢复。")
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not latest or latest.status != "needs_revision":
        return RevisionBudgetRecovery("not_needed", None, None, latest.id if latest else None, "当前章节不处于待修订状态。")
    rows = _failed_quality_rows(session, chapter_id=chapter.id, limit=8)
    if not rows:
        return RevisionBudgetRecovery("missing_quality", None, None, latest.id, "缺少失败质检报告，无法自动判断最佳底稿。")
    best_version, best_quality, best_report = max(rows, key=lambda row: (int(row[1].score or 0), -int(row[0].id or 0)))
    recovery_version = best_version
    if latest.id != best_version.id:
        recovery_version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=_next_version_number(session, chapter.id),
            title=best_version.title,
            content=best_version.content,
            status="needs_revision",
            source=f"revision_budget_recovery:v{best_version.id}",
        )
        session.add(recovery_version)
        session.flush()
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"
    suggestion = _budget_recovery_suggestion(
        chapter_number=chapter_number,
        best_version=best_version,
        best_quality=best_quality,
        latest_version=latest,
        report=best_report,
    )
    _feedback, _adjustment, brief, _version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="system_revision_budget_recovery",
        suggestion_text=suggestion,
        revision_mode="targeted",
    )
    brief.goal = f"自动恢复修订第{chapter_number}章：以当前最佳稿 v{best_version.id} 为底稿，换策略完成可读稿。"
    brief.required_beats = "\n".join(
        [
            "system_revision_budget_recovery: detected",
            f"自动修订预算触顶后，系统选择最佳底稿 v{best_version.id} score={int(best_quality.score or 0)}。",
            f"当前最新待修稿：v{latest.id}；不得继续沿无效方向堆修。",
            "修订模式:targeted；禁止 fresh；禁止整章重写；禁止要求作者给方向。",
            "本轮只修最低分的 2-4 个明确问题：承接、场景展开、对白、人物反应、奖励代价或章末压力。",
            "保留最佳稿的主事件、场景顺序、人物行动链和章末事实；合格段落不动。",
            "如果无法提升，保留最佳稿并停止继续消耗，不生成更差版本。",
        ]
    )
    brief.constraints = "\n".join(
        [
            brief.constraints or "",
            "system_revision_budget_recovery: 系统自行换策略，不向作者索要抽象方向。",
            "禁止：追杀模板、现实机构关注、门派通缉、系统面板直接解题、冷硬装酷式精炼。",
            "禁止只换形容词或压缩句子；必须把抽象判断写成可见动作、空间、对白和后果。",
            "验收：self_check 必须说明保留了哪一版、修了哪些最低分问题、为什么没有扩大重写。",
        ]
    )
    sanitize_existing_chapter_brief(session, book_id=book_id, brief=brief)
    session.flush()
    return RevisionBudgetRecovery(
        "recovered",
        recovery_version.id,
        brief.id,
        best_version.id,
        f"自动选择最佳稿 v{best_version.id} 并换策略修订，不再要求人工给方向。",
    )


def _failed_quality_rows(session: Session, *, chapter_id: int, limit: int) -> list[tuple[ChapterVersion, QualityReport, dict]]:
    versions = list(
        session.scalars(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter_id)
            .order_by(ChapterVersion.id.desc())
            .limit(limit)
        )
    )
    rows: list[tuple[ChapterVersion, QualityReport, dict]] = []
    for version in versions:
        quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc()))
        if quality and not quality.passed:
            rows.append((version, quality, _loads_json(quality.report)))
    return rows


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _budget_recovery_suggestion(
    *,
    chapter_number: int,
    best_version: ChapterVersion,
    best_quality: QualityReport,
    latest_version: ChapterVersion,
    report: dict,
) -> str:
    dimensions = report.get("dimensions") if isinstance(report.get("dimensions"), dict) else {}
    weak = [
        f"{name}={int(score or 0)}"
        for name, score in sorted(dimensions.items(), key=lambda item: int(item[1] or 0))
        if int(score or 0) < 70
    ][:6]
    issues = [str(item) for item in (report.get("issues") or [])[:6]]
    return "\n".join(
        [
            "system_revision_budget_recovery: detected",
            f"第{chapter_number}章自动修订预算触顶，系统不得继续向作者索要抽象方向。",
            f"选择最佳底稿 v{best_version.id} score={int(best_quality.score or 0)}；最新待修稿 v{latest_version.id}。",
            "策略：回到最佳稿，定点修最低分问题；不 fresh，不重开，不扩大俗套冲突。",
            "低分维度：" + ("；".join(weak) if weak else "按质检报告中的最低分维度处理。"),
            "失败问题：" + ("；".join(issues) if issues else "按质检报告修复明确阻断。"),
        ]
    )


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.version_number.desc()))
    return (latest.version_number if latest else 0) + 1
