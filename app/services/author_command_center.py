from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, GenerationTask
from app.services.llm_queue import QUEUE_TYPES
from app.services.planning import plan_chapters
from app.services.production_decision import decide_chapter_production
from app.services.readiness import check_production_readiness
from app.services.status_language import author_next_action_text, author_status_text


def build_author_command_center(
    session: Session,
    *,
    book_id: int,
    chapter_number: int = 1,
    start: int = 1,
    count: int = 20,
) -> dict[str, Any]:
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    readiness = check_production_readiness(session, book_id=book_id, start=start, count=count, live_llm=False)
    plan_items = plan_chapters(session, book_id=book_id, start=start, count=count)
    current = next((item for item in plan_items if item.chapter_number == chapter_number), None)
    auto_item = next((item for item in plan_items if decide_chapter_production(item).can_continue), None)
    tasks = list(session.scalars(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()).limit(80)))
    counts = Counter(task.status for task in tasks)
    running = counts.get("running", 0)
    pending = counts.get("pending", 0)
    failed_tasks = _active_failed_tasks(session, book_id=book_id, limit=5)
    failed = len(failed_tasks)

    if not readiness.passed:
        blockers = [f"{item.name}: {item.detail}" for item in readiness.blockers]
        detail = author_status_text(blockers[0] if blockers else "生产门禁未通过。")
        return _center(
            status="blocked",
            stage="setup",
            headline="生产前有打断项",
            detail=detail,
            primary_label="自动处理打断项",
            primary_intent="auto_resolve_blocker",
            blockers=blockers,
            next_actions=[author_next_action_text(detail)],
        )
    if running:
        return _center(
            status="running",
            stage="generate",
            headline="后台正在生成",
            detail="模型任务正在运行，等待完成后页面会刷新到下一步。",
            primary_label="等待自动刷新",
            primary_intent="wait",
            next_actions=["不要重复启动生产；等待后台任务完成。"],
        )
    if pending:
        return _center(
            status="queued",
            stage="generate",
            headline="有生成任务待启动",
            detail="队列里已有待启动任务，点击主按钮启动后台生成。",
            primary_label="启动后台生成",
            primary_intent="continue",
            next_actions=["启动后台生成。"],
        )
    if failed:
        first = failed_tasks[0] if failed_tasks else {}
        chapter_text = f"第 {first.get('chapter_number')} 章" if first.get("chapter_number") else "未知章节"
        type_text = _task_type_label(str(first.get("task_type") or ""))
        error_text = author_status_text(str(first.get("error") or first.get("error_category") or "未记录具体错误"))
        return _center(
            status="blocked",
            stage="diagnose",
            headline="有生成任务需要处理",
            detail=f"{chapter_text}的{type_text}需要处理：{error_text}",
            primary_label="自动处理打断项",
            primary_intent="auto_resolve_blocker",
            blockers=["generation_queue_failed"],
            next_actions=[author_next_action_text(error_text)],
            failed_tasks=failed_tasks,
        )
    if not current:
        return _center(
            status="idle",
            stage="select",
            headline="请选择有效章节",
            detail="当前章不在加载范围内。",
            primary_label="刷新章节地图",
            primary_intent="refresh",
            next_actions=["调整当前章或扩大章节范围后刷新。"],
        )

    decision = decide_chapter_production(current)
    if decision.needs_author:
        return _center(
            status=decision.status,
            stage=decision.stage,
            headline=decision.headline,
            detail=decision.next_step,
            primary_label=decision.primary_label,
            primary_intent=decision.primary_intent,
            next_actions=[decision.next_step],
        )
    if decision.can_continue:
        return _center(
            status="can_produce",
            stage=decision.stage,
            headline=decision.headline,
            detail=decision.next_step,
            primary_label=decision.primary_label,
            primary_intent=decision.primary_intent,
            next_actions=[decision.next_step],
        )
    if auto_item:
        auto_decision = decide_chapter_production(auto_item)
        return _center(
            status="can_produce",
            stage=auto_decision.stage,
            headline=f"当前章需人工处理，可先推进第 {auto_item.chapter_number} 章",
            detail=auto_decision.next_step,
            primary_label=f"切到第 {auto_item.chapter_number} 章继续",
            primary_intent="continue_auto_chapter",
            target_chapter_number=auto_item.chapter_number,
            next_actions=[auto_decision.next_step],
        )
    return _center(
        status=decision.status,
        stage=decision.stage,
        headline=decision.headline,
        detail=decision.next_step,
        primary_label=decision.primary_label,
        primary_intent=decision.primary_intent,
        next_actions=[decision.next_step],
    )


def _center(
    *,
    status: str,
    stage: str,
    headline: str,
    detail: str,
    primary_label: str,
    primary_intent: str,
    blockers: list[str] | None = None,
    next_actions: list[str] | None = None,
    target_chapter_number: int | None = None,
    failed_tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "stage": stage,
        "headline": headline,
        "detail": detail,
        "primary_action": {
            "label": primary_label,
            "intent": primary_intent,
            "target_chapter_number": target_chapter_number,
        },
        "secondary_actions": ["作品设定", "写修改意见", "后台排错"],
        "blockers": blockers or [],
        "next_actions": next_actions or [],
        "failed_tasks": failed_tasks or [],
    }


def _author_blocker_detail(value: str) -> str:
    text = str(value or "")
    if "skeleton" in text or "story_bible" in text or "StoryBible" in text or "骨架" in text:
        return "作品设定需要整理。系统会先生成修复草案；需要你确认时，只需点“保存并启用”。"
    if "semantic_memory" in text or "语义" in text:
        return "语义记忆需要重建。系统可以自动处理。"
    if "evidence" in text or "市场" in text:
        return "生产证据不足。系统可以自动补齐本地证据并继续。"
    if "canon" in text or "Canon" in text:
        return "长期设定需要整理。系统会先尝试自动补齐。"
    return "系统检测到生产前阻断，会先尝试自动处理可修复项。"


def _active_failed_tasks(session: Session, *, book_id: int, limit: int = 5) -> list[dict[str, Any]]:
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.status == "failed")
            .order_by(GenerationTask.id.desc())
            .limit(40)
        )
    )
    rows: list[dict[str, Any]] = []
    for task in tasks:
        if _obsolete_failed_generation_task(session, task):
            continue
        input_data = _loads_json(task.input_json)
        output_data = _loads_json(task.output_json)
        rows.append(
            {
                "id": task.id,
                "task_type": task.task_type,
                "task_label": _task_type_label(task.task_type),
                "chapter_number": input_data.get("chapter_number"),
                "error_category": str(output_data.get("error_category") or ""),
                "error": str(output_data.get("error") or "")[:220],
                "is_queue_task": task.task_type in QUEUE_TYPES,
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _obsolete_failed_generation_task(session: Session, task: GenerationTask) -> bool:
    if task.task_type in QUEUE_TYPES:
        return False
    input_data = _loads_json(task.input_json)
    chapter_number = input_data.get("chapter_number")
    source_version_id = input_data.get("source_version_id")
    revision_brief_id = input_data.get("revision_brief_id")
    quality_report_id = input_data.get("quality_report_id")
    stmt = (
        select(GenerationTask)
        .where(
            GenerationTask.book_id == task.book_id,
            GenerationTask.task_type == task.task_type,
            GenerationTask.status == "completed",
            GenerationTask.created_at > task.created_at,
        )
        .order_by(GenerationTask.id.desc())
    )
    for candidate in session.scalars(stmt):
        candidate_input = _loads_json(candidate.input_json)
        if chapter_number and candidate_input.get("chapter_number") != chapter_number:
            continue
        if source_version_id and candidate_input.get("source_version_id") != source_version_id:
            continue
        if revision_brief_id and candidate_input.get("revision_brief_id") != revision_brief_id:
            continue
        if quality_report_id and candidate_input.get("quality_report_id") != quality_report_id:
            continue
        return True
    return False


def _loads_json(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _task_type_label(value: str) -> str:
    labels = {
        "draft_chapter": "章节草稿",
        "queue_draft_chapter": "草稿生成",
        "revise_chapter": "章节修订",
        "queue_revise_chapter": "修订生成",
        "review_chapter": "章节质检",
        "llm_review_chapter": "模型审稿",
        "chapter_sample_lab": "章节小样",
    }
    return labels.get(value, value or "生成任务")
