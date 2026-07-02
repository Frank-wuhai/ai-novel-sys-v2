from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.feedback import format_chapter_sample_adoption_context, submit_revision_suggestion


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


@dataclass(frozen=True)
class PersistentRevisionBudget:
    exceeded: bool
    full_revision_count: int
    max_full_revisions: int
    total_elapsed_ms: int
    latest_task_id: int | None
    reason: str

    def to_dict(self) -> dict:
        return {
            "exceeded": self.exceeded,
            "full_revision_count": self.full_revision_count,
            "max_full_revisions": self.max_full_revisions,
            "total_elapsed_ms": self.total_elapsed_ms,
            "latest_task_id": self.latest_task_id,
            "reason": self.reason,
        }


def persistent_revision_budget(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    max_full_revisions: int,
) -> PersistentRevisionBudget:
    if max_full_revisions < 1:
        max_full_revisions = 1
    # Sprint 2 P0-1: cutoff based on the latest chapter version.
    # When a recovery-brief has been generated and is waiting for the worker
    # to produce a fresh revise task (i.e. no new ChapterVersion exists past
    # the current latest), we must not keep counting historical revise tasks
    # forever — otherwise the planner deadlocks on `revision_budget_recovery`
    # and never enqueues the follow-up revise task, freezing the chapter.
    from app.models.entities import Chapter, ChapterBrief, ChapterVersion
    chapter = session.scalar(
        select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number)
    )
    cutoff_dt = None
    if chapter:
        recovery_brief = session.scalar(
            select(ChapterBrief)
            .where(
                ChapterBrief.chapter_id == chapter.id,
                ChapterBrief.status == "revision_ready",
                ChapterBrief.required_beats.like("%system_revision_budget_recovery%"),
            )
            .order_by(ChapterBrief.id.desc())
        )
        if recovery_brief:
            latest_version = session.scalar(
                select(ChapterVersion)
                .where(ChapterVersion.chapter_id == chapter.id)
                .order_by(ChapterVersion.id.desc())
            )
            if latest_version is not None:
                cutoff_dt = latest_version.created_at
    rows = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.task_type == "revise_chapter", GenerationTask.status == "completed")
            .order_by(GenerationTask.id.desc())
            .limit(80)
        )
    )
    full_count = 0
    total_elapsed = 0
    latest_task_id: int | None = None
    for task in rows:
        try:
            input_data = json.loads(task.input_json or "{}")
            output_data = json.loads(task.output_json or "{}")
        except json.JSONDecodeError:
            continue
        if int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        if input_data.get("dry_run") is True or output_data.get("dry_run") is True:
            continue
        # Sprint 2 P0-1: skip tasks older than the recovery-brief cutoff.
        if cutoff_dt is not None and task.created_at is not None and task.created_at < cutoff_dt:
            continue
        strategy = str(output_data.get("strategy") or "")
        revision_mode = str(input_data.get("revision_mode") or "")
        source = str(output_data.get("source") or "")
        if strategy == "deterministic_local_patch" or revision_mode == "local_patch" or "revision_budget" in source:
            continue
        full_count += 1
        latest_task_id = latest_task_id or task.id
        total_elapsed += int(output_data.get("elapsed_ms") or 0)
    exceeded = full_count >= max_full_revisions
    reason = (
        f"persistent_revision_budget:{full_count}>={max_full_revisions}"
        if exceeded
        else f"persistent_revision_budget:{full_count}<{max_full_revisions}"
    )
    return PersistentRevisionBudget(exceeded, full_count, max_full_revisions, total_elapsed, latest_task_id, reason)


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
    force_rebuild_reason: str = "",
) -> RevisionBudgetRecovery:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return RevisionBudgetRecovery("missing_chapter", None, None, None, "找不到章节，无法自动恢复。")
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not latest or latest.status != "needs_revision":
        return RevisionBudgetRecovery("not_needed", None, None, latest.id if latest else None, "当前章节不处于待修订状态。")
    active_recovery = _active_budget_recovery(session, chapter_id=chapter.id, latest=latest)
    if active_recovery:
        brief, source_version_id = active_recovery
        return RevisionBudgetRecovery(
            "recovered",
            latest.id,
            brief.id,
            source_version_id,
            f"当前已有预算恢复稿 v{latest.id} 和恢复 brief #{brief.id}，继续执行该修订，不重复复制底稿。",
        )
    passed = _best_passed_quality_row(session, chapter_id=chapter.id, limit=24)
    if passed:
        passed_version, passed_quality, passed_report = passed
        protected_brief = _latest_protected_revision_brief(session, chapter_id=chapter.id)
        restore_status = "needs_revision" if protected_brief else "reviewed_pass"
        for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")):
            if protected_brief and brief.id == protected_brief.id:
                continue
            brief.status = "superseded"
        if protected_brief:
            protected_brief.status = "revision_ready"
        if latest.id == passed_version.id:
            recovery_version = passed_version
            recovery_version.status = restore_status
        else:
            recovery_version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=_next_version_number(session, chapter.id),
                title=passed_version.title,
                content=passed_version.content,
                status=restore_status,
                source=f"revision_budget_readable_restore:v{passed_version.id}",
            )
            session.add(recovery_version)
            session.flush()
        if protected_brief:
            from app.services.reading_assessment import create_clean_rebuild_brief, rebind_revision_brief_source

            if _recent_restore_count(session, chapter_id=chapter.id, limit=8) >= 3:
                protected_brief = create_clean_rebuild_brief(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                    version=recovery_version,
                    quality=passed_quality,
                    reason="同一章节连续修订失败并回退，系统已切断旧合同循环。",
                )
            else:
                rebind_revision_brief_source(protected_brief, version_id=recovery_version.id)
        restored_report = dict(passed_report)
        restored_report["revision_budget_recovery"] = {
            "status": "restored_readable_needs_revision" if protected_brief else "restored_readable",
            "source_version_id": passed_version.id,
            "latest_failed_version_id": latest.id,
            "protected_brief_id": protected_brief.id if protected_brief else None,
            "reason": (
                "自动修订预算耗尽时已有历史通过稿，但存在未解决的阅读评估/修订合同，恢复为待修订底稿而非待采用确认稿。"
                if protected_brief
                else "自动修订预算耗尽时已有历史通过稿，恢复可读稿并停止继续消耗。"
            ),
        }
        session.add(
            QualityReport(
                chapter_version_id=recovery_version.id,
                score=int(passed_quality.score or restored_report.get("score") or 75),
                passed=True,
                report=json.dumps(restored_report, ensure_ascii=False),
            )
        )
        session.flush()
        return RevisionBudgetRecovery(
            "restored_readable_needs_revision" if protected_brief else "restored_readable",
            recovery_version.id,
            protected_brief.id if protected_brief else None,
            passed_version.id,
            (
                f"已恢复历史通过稿 v{passed_version.id}，但保留阅读评估/修订合同 #{protected_brief.id}，不能进入采用确认。"
                if protected_brief
                else f"已恢复历史通过稿 v{passed_version.id}，停止继续自动修订。"
            ),
        )
    rows = _failed_quality_rows(session, chapter_id=chapter.id, limit=8)
    if not rows:
        return RevisionBudgetRecovery("missing_quality", None, None, latest.id, "缺少失败质检报告，无法自动判断最佳底稿。")
    stalled = _stalled_revision_dimensions(rows)
    if force_rebuild_reason and not stalled:
        stalled = [force_rebuild_reason]
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
        stalled_dimensions=stalled,
    )
    revision_mode = "rewrite" if stalled else "targeted"
    _feedback, _adjustment, brief, _version = submit_revision_suggestion(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        platform="system_revision_budget_recovery",
        suggestion_text=suggestion,
        revision_mode=revision_mode,
    )
    brief.goal = (
        f"自动重建第{chapter_number}章修订目标：旧 brief 覆盖停滞，按当前作品设定重做章节承诺。"
        if stalled
        else f"自动恢复修订第{chapter_number}章：以当前最佳稿 v{best_version.id} 为底稿，换策略完成可读稿。"
    )
    brief.required_beats = "\n".join(
        [
            "system_revision_budget_recovery: detected",
            f"自动修订预算触顶后，系统选择最佳底稿 v{best_version.id} score={int(best_quality.score or 0)}。",
            f"当前最新待修稿：v{latest.id}；不得继续沿无效方向堆修。",
            (
                "修订模式:rewrite；允许重排场景顺序和章节承诺；旧稿只保留可用素材，不保留失败结构。"
                if stalled
                else "修订模式:targeted；禁止 fresh；禁止整章重写；禁止要求作者给方向。"
            ),
            (
                "本轮先重建章节目标：进入游戏的具体处境、桥段复刻任务、一次可见回报、一次可见代价、章末同步钩子。"
                if stalled
                else "本轮只修最低分的 2-4 个明确问题：承接、场景展开、对白、人物反应、奖励代价或章末压力。"
            ),
            (
                "正文必须覆盖上述五个承诺点；不得继续追逐旧 brief 里的通用标准、禁区清单或系统恢复文本。"
                if stalled
                else "保留最佳稿的主事件、场景顺序、人物行动链和章末事实；合格段落不动。"
            ),
            "如果无法提升，保留最佳稿并停止继续消耗，不生成更差版本。",
        ]
    )
    brief.constraints = "\n".join(
        [
            brief.constraints or "",
            "system_revision_budget_recovery: 系统自行换策略，不向作者索要抽象方向。",
            format_chapter_sample_adoption_context(session, book_id=book_id, chapter_number=chapter_number),
            "coverage_rebuild: " + ("；".join(stalled) if stalled else "none"),
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
        (
            f"连续低分项停滞（{','.join(stalled)}），系统已自动重建 brief 并改用结构修订。"
            if stalled
            else f"自动选择最佳稿 v{best_version.id} 并换策略修订，不再要求额外给方向。"
        ),
    )


def _stalled_revision_dimensions(rows: list[tuple[ChapterVersion, QualityReport, dict]]) -> list[str]:
    if len(rows) < 3:
        return []
    watched = ("brief_coverage", "canon_consistency", "arc_alignment", "chapter_necessity")
    stalled: list[str] = []
    recent = rows[:5]
    for name in watched:
        scores = []
        for _version, _quality, report in recent:
            dimensions = report.get("dimensions") if isinstance(report.get("dimensions"), dict) else {}
            if name in dimensions:
                scores.append(int(dimensions.get(name) or 0))
        if len(scores) >= 3 and max(scores) < 60 and max(scores) - min(scores) <= 5:
            stalled.append(name)
    return stalled


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


def _best_passed_quality_row(session: Session, *, chapter_id: int, limit: int) -> tuple[ChapterVersion, QualityReport, dict] | None:
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
        if not quality or not quality.passed:
            continue
        rows.append((version, quality, _loads_json(quality.report)))
    if not rows:
        return None
    return max(rows, key=lambda row: (int(row[1].score or 0), int(row[0].id or 0)))


def _latest_protected_revision_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    briefs = list(
        session.scalars(
            select(ChapterBrief)
            .where(ChapterBrief.chapter_id == chapter_id)
            .order_by(ChapterBrief.id.desc())
            .limit(16)
        )
    )
    for brief in briefs:
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if any(
            marker in text
            for marker in (
                "reading_assessment_contract",
                "阅读评估结论",
                "当前稿不是正式批准稿",
                "修订方向:",
                "clean_rebuild_contract@v1",
            )
        ):
            return brief
    return None


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
    stalled_dimensions: list[str] | None = None,
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
            (
                "策略：覆盖类指标连续停滞，自动重建章节 brief，允许结构修订；不向作者索要方向。"
                if stalled_dimensions
                else "策略：回到最佳稿，定点修最低分问题；不 fresh，不重开，不扩大俗套冲突。"
            ),
            "停滞维度：" + ("；".join(stalled_dimensions) if stalled_dimensions else "无"),
            "低分维度：" + ("；".join(weak) if weak else "按质检报告中的最低分维度处理。"),
            "失败问题：" + ("；".join(issues) if issues else "按质检报告修复明确阻断。"),
        ]
    )


def _next_version_number(session: Session, chapter_id: int) -> int:
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.version_number.desc()))
    return (latest.version_number if latest else 0) + 1


def _active_budget_recovery(
    session: Session,
    *,
    chapter_id: int,
    latest: ChapterVersion,
) -> tuple[ChapterBrief, int | None] | None:
    source = str(latest.source or "")
    if not source.startswith(("revision_budget_recovery:", "revision_budget_readable_restore:")):
        return None
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not brief:
        return None
    return brief, _source_version_id(source)


def _source_version_id(source: str) -> int | None:
    _prefix, _sep, raw = source.partition(":v")
    if not raw:
        return None
    try:
        return int(raw.split(":", 1)[0])
    except ValueError:
        return None


def _recent_restore_count(session: Session, *, chapter_id: int, limit: int) -> int:
    versions = session.scalars(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter_id)
        .order_by(ChapterVersion.id.desc())
        .limit(limit)
    )
    prefixes = ("revision_compare_restore:", "revision_budget_readable_restore:", "revision_budget_recovery:")
    return sum(1 for version in versions if str(version.source or "").startswith(prefixes))
