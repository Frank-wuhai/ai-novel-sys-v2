from __future__ import annotations

import time
from threading import Lock
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import GenerationTask
from app.services.llm_queue import QUEUE_TYPES


VISIBLE_GENERATION_TASK_TYPES = set(QUEUE_TYPES) | {"rebuild_chapter_candidates"}


def background_runs_payload(runs: dict[str, dict[str, Any]], lock: Lock, *, now: float | None = None) -> list[dict]:
    current = time.time() if now is None else now
    with lock:
        for run in runs.values():
            if (
                run.get("status") == "running"
                and current - float(run.get("last_progress_at") or run.get("started_at") or current) > int(run.get("idle_timeout_seconds") or run.get("timeout_seconds") or 180)
            ):
                kind = str(run.get("kind") or "")
                timeout_message = "后台任务长时间没有新进展，请重试；若反复出现，查看模型连接或数据库锁。"
                if kind == "sample":
                    timeout_message = "章节小样生成长时间没有新进展，请重试；若反复出现，查看模型连接。"
                elif kind == "author":
                    timeout_message = "后台主笔长时间没有新进展，请重试；若反复出现，查看模型连接或数据库锁。"
                run.update(
                    {
                        "status": "failed",
                        "finished_at": current,
                        "error": f"后台任务超过 {int(run.get('idle_timeout_seconds') or run.get('timeout_seconds') or 180)} 秒没有新的进展，已自动标记为失败。",
                        "terminal_status": "system_failed",
                        "terminal_message": timeout_message,
                    }
                )
        recent_runs = list(runs.values())[-10:]
    payload = []
    for run in reversed(recent_runs):
        started_at = float(run.get("started_at") or current)
        finished_at = run.get("finished_at")
        payload.append(
            {
                "run_id": run.get("run_id", ""),
                "kind": run.get("kind", "queue"),
                "status": run.get("status", ""),
                "running_age_seconds": int((float(finished_at) if finished_at else current) - started_at),
                "executed_count": run.get("executed_count", 0),
                "error": run.get("error", ""),
                "result": run.get("result", {}),
                "terminal_status": run.get("terminal_status", ""),
                "terminal_message": run.get("terminal_message", ""),
                "timeout_seconds": int(run.get("timeout_seconds") or 180),
            }
        )
    return payload


def pending_generation_task_id(session: Session, *, book_id: int = 0, chapter_number: int = 0) -> int | None:
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.task_type.in_(VISIBLE_GENERATION_TASK_TYPES), GenerationTask.status == "pending")
        .order_by(GenerationTask.id)
    )
    if book_id:
        stmt = stmt.where(GenerationTask.book_id == book_id)
    tasks = list(session.scalars(stmt))
    for task in tasks:
        input_data = _loads_json(task.input_json)
        if chapter_number and int(input_data.get("chapter_number") or 0) != chapter_number:
            continue
        return task.id
    return None


def _loads_json(value: str) -> dict:
    import json

    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
