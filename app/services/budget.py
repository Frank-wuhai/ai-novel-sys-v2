from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import GenerationTask


@dataclass(frozen=True)
class BudgetReport:
    book_id: int
    token_budget: int
    used_tokens: int
    remaining_tokens: int
    task_count: int
    passed: bool


def check_token_budget(
    session: Session,
    *,
    book_id: int,
    token_budget: int,
) -> BudgetReport:
    if token_budget < 0:
        raise ValueError("token_budget must be >= 0")
    tasks = list(session.scalars(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id)))
    used = sum(_estimated_tokens(task) for task in tasks)
    remaining = token_budget - used
    return BudgetReport(
        book_id=book_id,
        token_budget=token_budget,
        used_tokens=used,
        remaining_tokens=remaining,
        task_count=len(tasks),
        passed=remaining >= 0,
    )


def _estimated_tokens(task: GenerationTask) -> int:
    try:
        output_data = json.loads(task.output_json or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(output_data, dict):
        return 0
    value = output_data.get("estimated_total_tokens")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0
