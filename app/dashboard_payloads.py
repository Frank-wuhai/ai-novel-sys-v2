from __future__ import annotations

import difflib
import json

from sqlalchemy import select

from app.models.entities import (
    Chapter,
    ChapterBrief,
    ChapterVersion,
    GenerationTask,
    PublishExecution,
    PublishJob,
    PublishingTarget,
    QualityReport,
)
from app.services.db_ops import check_database_health, check_schema_version, list_database_backups
from app.services.llm_audit import llm_failure_suggestion, list_llm_request_logs, summarize_llm_failures, summarize_llm_usage
from app.services.llm_costs import summarize_llm_cost
from app.services.llm_queue import QUEUE_TYPES
from app.services.publish_preflight import build_publish_preflight


def _llm_usage_payload(session, *, book_id: int) -> dict:
    usage = summarize_llm_usage(session, book_id=book_id)
    cost = summarize_llm_cost(session, book_id=book_id)
    recent = list_llm_request_logs(session, book_id=book_id, limit=8)
    return {
        "usage": {
            "book_id": usage.book_id,
            "request_count": usage.request_count,
            "completed_count": usage.completed_count,
            "failed_count": usage.failed_count,
            "estimated_total_tokens": usage.estimated_total_tokens,
            "actual_total_tokens": usage.actual_total_tokens,
            "billable_prompt_tokens": usage.billable_prompt_tokens,
            "billable_response_tokens": usage.billable_response_tokens,
            "billable_total_tokens": usage.billable_total_tokens,
            "elapsed_ms": usage.elapsed_ms,
        },
        "cost": {
            "book_id": cost.book_id,
            "model": cost.model,
            "request_count": cost.request_count,
            "billable_prompt_tokens": cost.billable_prompt_tokens,
            "billable_response_tokens": cost.billable_response_tokens,
            "billable_total_tokens": cost.billable_total_tokens,
            "input_price_per_1m_tokens": cost.input_price_per_1m_tokens,
            "output_price_per_1m_tokens": cost.output_price_per_1m_tokens,
            "estimated_cost": cost.estimated_cost,
            "currency": cost.currency,
        },
        "recent_requests": [
            {
                "id": item.id,
                "generation_task_id": item.generation_task_id,
                "task_type": item.task_type,
                "status": item.status,
                "provider": item.provider,
                "model": item.model,
                "prompt_template": item.prompt_template,
                "estimated_total_tokens": item.estimated_total_tokens,
                "actual_total_tokens": item.actual_total_tokens,
                "elapsed_ms": item.elapsed_ms,
                "error_category": item.error_category,
            }
            for item in recent
        ],
    }


def _failed_tasks_payload(session, *, book_id: int) -> dict:
    tasks = list(
        session.scalars(
            select(GenerationTask)
            .where(GenerationTask.book_id == book_id, GenerationTask.status == "failed")
            .order_by(GenerationTask.id.desc())
            .limit(20)
        )
    )
    counts: dict[str, int] = {}
    advice: dict[str, str] = {}
    rows = []
    for task in tasks:
        if _obsolete_failed_generation_task(session, task):
            continue
        input_data = _loads_json(task.input_json)
        output_data = _loads_json(task.output_json)
        error_category = str(output_data.get("error_category") or _infer_error_category(output_data))
        counts[error_category] = counts.get(error_category, 0) + 1
        advice[error_category] = llm_failure_suggestion(error_category)
        rows.append(
            {
                "id": task.id,
                "task_type": task.task_type,
                "status": task.status,
                "chapter_number": input_data.get("chapter_number"),
                "attempt": output_data.get("attempt") or input_data.get("attempt"),
                "max_attempts": output_data.get("max_attempts") or input_data.get("max_attempts"),
                "error_category": error_category,
                "error": str(output_data.get("error") or "")[:300],
                "is_queue_task": task.task_type in QUEUE_TYPES,
            }
        )
    return {
        "total": len(rows),
        "by_error_category": dict(sorted(counts.items())),
        "advice_by_error_category": {key: advice[key] for key in sorted(advice)},
        "llm_failures": [
            {
                "error_category": item.error_category,
                "count": item.count,
                "latest_request_id": item.latest_request_id,
                "latest_task_type": item.latest_task_type,
                "latest_provider": item.latest_provider,
                "latest_model": item.latest_model,
                "latest_elapsed_ms": item.latest_elapsed_ms,
                "suggestion": item.suggestion,
            }
            for item in summarize_llm_failures(session, book_id=book_id, limit=20)
        ],
        "items": rows,
    }


def _obsolete_failed_generation_task(session, task: GenerationTask) -> bool:
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


def _infer_error_category(output_data: dict) -> str:
    error = str(output_data.get("error") or "")
    if "LLM output is not valid JSON" in error or "StructuredOutputError" in error:
        return "structured_output"
    return ""


def _publishing_payload(session, *, book_id: int) -> dict:
    targets = list(session.scalars(select(PublishingTarget).order_by(PublishingTarget.id)))
    ready_versions = _ready_publish_versions(session, book_id=book_id)
    jobs = list(
        session.scalars(
            select(PublishJob)
            .join(ChapterVersion, ChapterVersion.id == PublishJob.chapter_version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id)
            .order_by(PublishJob.id.desc())
            .limit(20)
        )
    )
    executions = list(
        session.scalars(
            select(PublishExecution)
            .join(PublishJob, PublishJob.id == PublishExecution.publish_job_id)
            .join(ChapterVersion, ChapterVersion.id == PublishJob.chapter_version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id)
            .order_by(PublishExecution.id.desc())
            .limit(20)
        )
    )
    return {
        "targets": [
            {
                "id": target.id,
                "platform": target.platform,
                "account_label": target.account_label,
                "work_identifier": target.work_identifier,
                "automation_mode": target.automation_mode,
                "status": target.status,
                "config": _loads_json(target.config_json),
            }
            for target in targets
        ],
        "ready_versions": ready_versions,
        "jobs": [_publish_job_payload(session, job) for job in jobs],
        "executions": [
            {
                "id": execution.id,
                "publish_job_id": execution.publish_job_id,
                "platform": execution.platform,
                "status": execution.status,
                "automation_mode": execution.automation_mode,
                "report": execution.report,
                "artifact_path": execution.artifact_path,
            }
            for execution in executions
        ],
    }


def _ready_publish_versions(session, *, book_id: int) -> list[dict]:
    rows: list[dict] = []
    chapters = list(session.scalars(select(Chapter).where(Chapter.book_id == book_id).order_by(Chapter.chapter_number)))
    for chapter in chapters:
        version = session.scalar(
            select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc())
        )
        if not version or version.status != "approved":
            continue
        active_job = session.scalar(
            select(PublishJob)
            .where(
                PublishJob.chapter_version_id == version.id,
                PublishJob.status.in_(["pending", "dry_run_ready", "queued", "published", "failed"]),
            )
            .order_by(PublishJob.id.desc())
        )
        if active_job:
            continue
        preflight = build_publish_preflight(session, version_id=version.id)
        rows.append(
            {
                "version_id": version.id,
                "chapter_number": chapter.chapter_number,
                "title": version.title,
                "status": version.status,
                "preflight": preflight,
            }
        )
    return rows


def _database_payload(session) -> dict:
    health = check_database_health(session)
    schema = check_schema_version(session)
    backups = list_database_backups(session, limit=20)
    return {
        "health": {
            "database_url": health.database_url,
            "sqlite_path": health.sqlite_path,
            "table_count": health.table_count,
            "tables": health.tables,
            "migration_count": health.migration_count,
            "latest_migration": health.latest_migration,
            "backup_count": health.backup_count,
        },
        "schema_version": {
            "database_url": schema.database_url,
            "current_versions": schema.current_versions,
            "expected_head": schema.expected_head,
            "status": schema.status,
            "migration_count": schema.migration_count,
            "latest_migration": schema.latest_migration,
            "message": schema.message,
        },
        "backups": [
            {
                "id": backup.id,
                "database_url": backup.database_url,
                "backup_path": backup.backup_path,
                "status": backup.status,
                "size_bytes": backup.size_bytes,
                "report": backup.report,
            }
            for backup in backups
        ],
    }


def _publish_job_payload(session, job: PublishJob) -> dict:
    version = session.get(ChapterVersion, job.chapter_version_id)
    chapter = session.get(Chapter, version.chapter_id) if version else None
    content = version.content if version else ""
    return {
        "id": job.id,
        "version_id": job.chapter_version_id,
        "chapter_number": chapter.chapter_number if chapter else None,
        "platform": job.platform,
        "status": job.status,
        "automation_payload": _loads_json(job.automation_payload),
        "result_report": job.result_report,
        "preflight": build_publish_preflight(session, version_id=job.chapter_version_id) if version else None,
        "preview": {
            "title": version.title if version else "",
            "content_chars": len(content),
            "content_excerpt": content[:1200],
        },
    }


def _generation_tasks_for_chapter(session, *, book_id: int, chapter_number: int, limit: int) -> list[dict]:
    rows: list[dict] = []
    tasks = session.scalars(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if input_data.get("chapter_number") != chapter_number:
            continue
        output_data = _loads_json(task.output_json)
        rows.append(
            {
                "id": task.id,
                "type": task.task_type,
                "status": task.status,
                "attempt": input_data.get("attempt") or output_data.get("attempt"),
                "error_category": output_data.get("error_category", ""),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def _brief_payload(brief: ChapterBrief | None) -> dict | None:
    if not brief:
        return None
    return {
        "id": brief.id,
        "goal": brief.goal,
        "required_beats": brief.required_beats,
        "constraints": brief.constraints,
        "status": brief.status,
    }


def _quality_payload(quality: QualityReport | None) -> dict | None:
    if not quality:
        return None
    data = _loads_json(quality.report)
    created_at = quality.created_at.isoformat() if quality.created_at else None
    data = {
        **data,
        "quality_report_id": quality.id,
        "chapter_version_id": quality.chapter_version_id,
        "created_at": created_at,
        "score": quality.score,
        "passed": quality.passed,
    }
    return {
        "id": quality.id,
        "chapter_version_id": quality.chapter_version_id,
        "score": quality.score,
        "passed": quality.passed,
        "created_at": created_at,
        "report": quality.report,
        "data": data,
    }


def _version_diff_payload(versions: list[ChapterVersion]) -> dict | None:
    if len(versions) < 2:
        return None
    right = versions[0]
    left = versions[1]
    diff = difflib.unified_diff(
        left.content.splitlines(),
        right.content.splitlines(),
        fromfile=f"version#{left.id}",
        tofile=f"version#{right.id}",
        lineterm="",
    )
    text = "\n".join(diff)
    return {
        "left_version_id": left.id,
        "right_version_id": right.id,
        "text": text,
    }


def _parse_feedback_ids(value) -> list[int]:
    if isinstance(value, list):
        return [int(item) for item in value]
    return [int(item.strip()) for item in str(value or "").split(",") if item.strip()]


def _approval_revision_mode_from_level(level: str) -> str:
    legacy = {
        "polish": "polish",
        "local_patch": "local_patch",
        "targeted": "targeted",
        "rewrite": "rewrite",
        "rebuild": "rewrite",
    }
    return legacy.get(level, "")


def _approval_revision_label(mode: str) -> str:
    labels = {
        "polish": "小修",
        "local_patch": "局部补丁",
        "targeted": "定点修订",
        "rewrite": "结构重写",
        "fresh": "按最新骨架重启",
    }
    return labels.get(mode, "结构重写")


def _approval_revision_text(*, mode: str, note: str = "") -> str:
    extras = note.strip()
    if mode == "fresh":
        base = (
            "主笔修订策略：按最新生产骨架重启本章。旧稿已废弃，不要在旧稿上局部润色，也不要参考旧稿段落顺序、旧场景推进或旧句式。"
            "必须以最新生产骨架、Story Bible、已同步 Canon 和修订方向为准，重新设计开篇牵引、主角行动链、信息释放顺序、主要场景推进和章末钩子。"
            "只保留数据库 Canon 中仍有效的必要事实；旧质检中的具体旧桥段、旧名词和旧能力表现不得反向污染新稿。"
        )
    elif mode == "rewrite":
        base = (
            "主笔修订策略：结构重写。按最新生产骨架重做整章，不要在旧稿上局部润色。"
            "旧稿只用于避免 Canon 断裂；必须重新设计开篇牵引、主角行动链、信息释放顺序、主要场景推进和章末钩子。"
            "允许替换旧场景、删除旧桥段、改变段落顺序和表达方式；只保留必要核心设定、关键名词和已登记 Canon。"
        )
    elif mode == "targeted":
        base = (
            "主笔修订策略：定点修订。当前版本有可用部分，不要彻底重写整章。"
            "必须保留已经有效的场景、人物行动链、爽点和章末钩子，只替换或扩写建议命中的问题段落。"
            "旧稿可作为主要结构参考，但最新生产骨架、Canon 和修订方向仍然优先。"
        )
    elif mode == "local_patch":
        base = (
            "主笔修订策略：局部补丁。当前版本主体可用，只修改建议命中的句子、词语或短段落。"
            "不得重排场景、不得改变章末事实、不得把可用段落扩写成整章重写。"
            "如果问题是模型套路词或世界观偏差，只把偏差表达改成符合当前世界规则的说法。"
        )
    else:
        base = "主笔修订策略：小修。保留当前剧情结构，只润色语言表达、删掉生硬句、增强现场感和人物反应自然度。"
    if extras:
        return f"{base} 补充意见：{extras}"
    return base


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {"raw": value}
    return data if isinstance(data, dict) else {"value": data}
