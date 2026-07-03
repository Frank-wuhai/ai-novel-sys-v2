from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.chapter_samples import TASK_TYPE_CHAPTER_SAMPLE, latest_chapter_samples
from app.services.feedback import format_chapter_sample_adoption_context
from app.services.production_optimization import apply_skeleton_preflight_to_brief


@dataclass(frozen=True)
class PreDraftInputPermit:
    passed: bool
    action: str
    reason: str
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    recommended_sample_index: int | None = None
    sample_task_id: int | None = None


def evaluate_pre_draft_inputs(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief | None,
    first_chapters_require_sample: int = 5,
) -> PreDraftInputPermit:
    if not brief:
        return PreDraftInputPermit(False, "create_chapter_brief", "当前章缺少章节 brief。", ("missing_brief",))

    changed = normalize_pre_draft_brief(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    warnings: list[str] = ["brief 已自动去重/补骨架映射"] if changed else []
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if _brief_has_dirty_contract(text):
        return PreDraftInputPermit(
            False,
            "repair_chapter_brief",
            "当前章 brief 仍含旧质检/旧修订合同残留，先自动清理生产输入。",
            ("dirty_brief_contract",),
            tuple(warnings),
        )

    backlog_blocker = _deferred_backlog_segment_blocker(session, book_id=book_id, chapter_number=chapter_number)
    if backlog_blocker:
        return PreDraftInputPermit(
            False,
            "resolve_deferred_backlog",
            backlog_blocker,
            ("deferred_backlog_must_be_resolved",),
            tuple(warnings),
        )

    previous_blocker = _previous_chapter_stability_blocker(session, book_id=book_id, chapter_number=chapter_number)
    if previous_blocker:
        return PreDraftInputPermit(
            False,
            "wait_previous_chapter_readable",
            previous_blocker,
            ("previous_chapter_not_readable",),
            tuple(warnings),
        )

    if chapter_number <= first_chapters_require_sample and not _sample_gate_skipped(text):
        adopted = format_chapter_sample_adoption_context(session, book_id=book_id, chapter_number=chapter_number)
        if not adopted:
            sample_state = latest_chapter_samples(session, book_id=book_id, chapter_number=chapter_number, limit=5)
            status = str(sample_state.get("status") or "")
            task_id = int(sample_state.get("task_id") or 0) or None
            if status in {"running", "pending"}:
                return PreDraftInputPermit(
                    False,
                    "wait_generation_task",
                    f"章节小样任务 {task_id} 正在运行，等待完成后再进入正文。",
                    ("sample_task_running",),
                    tuple(warnings),
                    sample_task_id=task_id,
                )
            recommended = _recommended_sample_index(sample_state)
            if recommended:
                return PreDraftInputPermit(
                    False,
                    "adopt_recommended_chapter_sample",
                    f"前五章需要先采用小样方向；已找到推荐小样 #{recommended}。",
                    ("sample_not_adopted",),
                    tuple(warnings),
                    recommended_sample_index=recommended,
                    sample_task_id=task_id,
                )
            return PreDraftInputPermit(
                False,
                "generate_chapter_samples",
                "前五章需要先生成章节小样并择优，避免直接按泛化 brief 写正文。",
                ("sample_missing",),
                tuple(warnings),
                sample_task_id=task_id,
            )

    return PreDraftInputPermit(True, "draft_chapter", "当前章生产输入已通过：brief 干净、骨架映射完成、必要小样方向已处理。", warnings=tuple(warnings))


def normalize_pre_draft_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> bool:
    original = (brief.goal or "", brief.required_beats or "", brief.constraints or "")
    brief.goal = _dedupe_lines(brief.goal or "")
    brief.required_beats = _dedupe_lines(brief.required_beats or "")
    brief.constraints = _dedupe_lines(brief.constraints or "")
    apply_skeleton_preflight_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=brief)
    changed = original != (brief.goal or "", brief.required_beats or "", brief.constraints or "")
    if changed:
        session.flush()
    return changed


def latest_sample_task_id(session: Session, *, book_id: int, chapter_number: int) -> int | None:
    for task in session.scalars(
        select(GenerationTask)
        .where(GenerationTask.book_id == book_id, GenerationTask.task_type == TASK_TYPE_CHAPTER_SAMPLE)
        .order_by(GenerationTask.id.desc())
        .limit(12)
    ):
        data = _loads_input(task.input_json)
        if int(data.get("chapter_number") or 0) == chapter_number:
            return task.id
    return None


def _deferred_backlog_segment_blocker(session: Session, *, book_id: int, chapter_number: int, segment_size: int = 5) -> str:
    if chapter_number <= segment_size:
        return ""
    previous_closed_segment_end = ((chapter_number - 1) // segment_size) * segment_size
    if previous_closed_segment_end <= 0:
        return ""
    deferred = None
    for chapter in session.scalars(
        select(Chapter)
        .where(
            Chapter.book_id == book_id,
            Chapter.chapter_number <= previous_closed_segment_end,
            Chapter.status == "continuity_deferred",
        )
        .order_by(Chapter.chapter_number)
    ):
        latest = session.scalar(
            select(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter.id)
            .order_by(ChapterVersion.version_number.desc(), ChapterVersion.id.desc())
            .limit(1)
        )
        if latest and latest.status == "needs_revision":
            deferred = chapter
            break
    if not deferred:
        return ""
    return (
        f"第{chapter_number}章暂停生产：第{deferred.chapter_number}章仍在暂存回炉队列。"
        f"每 {segment_size} 章必须清账一次；进入第{previous_closed_segment_end + 1}章及后续章节前，"
        f"先把第{deferred.chapter_number}章回炉修到正式通过。"
    )


def _previous_chapter_stability_blocker(session: Session, *, book_id: int, chapter_number: int) -> str:
    if chapter_number <= 1:
        return ""
    previous_number = chapter_number - 1
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == previous_number))
    if not chapter:
        return f"第{chapter_number}章不能先生产：第{previous_number}章还不存在，无法建立读者连续性。"
    latest = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not latest:
        return f"第{chapter_number}章不能先生产：第{previous_number}章还没有可承接正文。"
    quality = session.scalar(select(QualityReport).where(QualityReport.chapter_version_id == latest.id).order_by(QualityReport.id.desc()))
    if _is_stable_previous_version(latest, quality):
        return ""
    # Sprint 2 P1-3 stage-6: align with planning._pre_chapter_creation_blocker.
    # accept_early_stop can leave the best version at reviewed_pass while its
    # QualityReport.passed stays False (we accepted the score plateau, not a
    # gate-pass). In that case the chapter has already been handed to human
    # confirmation (needs_confirmation / approved / continuity_recorded) and
    # downstream production should proceed.
    if chapter.status in {"needs_confirmation", "approved", "continuity_recorded"}:
        has_reviewed_pass = session.scalar(
            select(ChapterVersion.id)
            .where(
                ChapterVersion.chapter_id == chapter.id,
                ChapterVersion.status.in_(("reviewed_pass", "approved")),
            )
            .limit(1)
        )
        if has_reviewed_pass:
            return ""
    score = f"，评分 {quality.score}" if quality and quality.score is not None else ""
    verdict = "未通过" if quality and quality.passed is False else "未定稿"
    return (
        f"第{chapter_number}章暂停生产：第{previous_number}章最新版本 v{latest.version_number} "
        f"状态为 {latest.status}{score}，{verdict}。先让上一章进入 reviewed_pass/approved，"
        "再生成本章小样或正文，避免后文承接不稳定草稿。"
    )


def _is_stable_previous_version(version: ChapterVersion, quality: QualityReport | None) -> bool:
    if version.status not in {"reviewed_pass", "approved"}:
        return False
    return not (quality and quality.passed is False)


def _recommended_sample_index(sample_state: dict) -> int | None:
    report = sample_state.get("diversity_report") if isinstance(sample_state.get("diversity_report"), dict) else {}
    fallback_report = sample_state.get("fallback_diversity_report") if isinstance(sample_state.get("fallback_diversity_report"), dict) else {}
    for data in (report, fallback_report):
        value = int(data.get("recommended_sample_index") or 0)
        if value:
            return value
        usable = data.get("usable_sample_indices") if isinstance(data.get("usable_sample_indices"), list) else []
        for item in usable:
            index = int(item or 0)
            if index:
                return index
    samples = sample_state.get("samples") or sample_state.get("fallback_samples") or []
    if isinstance(samples, list):
        for item in samples:
            if isinstance(item, dict) and int(item.get("index") or 0):
                return int(item.get("index") or 0)
    return None


def _sample_gate_skipped(text: str) -> bool:
    return "skip_chapter_sample_gate" in text or "跳过章节小样" in text


def _brief_has_dirty_contract(text: str) -> bool:
    dirty = (
        "质检报告 #",
        "原始人工意见",
        "原始机器修订建议",
        "验收清单:",
        "system_revision_loop_guard",
        "system_revision_trend_recovery",
    )
    return any(marker in text for marker in dirty)


def _dedupe_lines(text: str) -> str:
    lines = []
    seen: set[str] = set()
    for raw in str(text or "").replace("\r", "\n").splitlines():
        line = raw.strip()
        if not line:
            continue
        normalized = re.sub(r"\s+", " ", line)
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(line)
    return "\n".join(lines)


def _loads_input(value: str | None) -> dict:
    import json

    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
