from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.entities import LLMRequestLog


@dataclass(frozen=True)
class LLMUsageSummary:
    book_id: int | None
    request_count: int
    completed_count: int
    failed_count: int
    estimated_total_tokens: int
    actual_total_tokens: int
    billable_prompt_tokens: int
    billable_response_tokens: int
    billable_total_tokens: int
    elapsed_ms: int


@dataclass(frozen=True)
class LLMFailureBucket:
    error_category: str
    count: int
    latest_request_id: int
    latest_task_type: str
    latest_provider: str
    latest_model: str
    latest_elapsed_ms: int
    suggestion: str


def record_llm_request(
    session: Session,
    *,
    book_id: int,
    task_type: str,
    generation_task_id: int | None,
    provider: str,
    model: str,
    request_id: str = "",
    prompt_template: str = "",
    prompt_chars: int = 0,
    response_chars: int = 0,
    estimated_prompt_tokens: int = 0,
    estimated_response_tokens: int = 0,
    actual_prompt_tokens: int = 0,
    actual_response_tokens: int = 0,
    actual_total_tokens: int = 0,
    elapsed_ms: int = 0,
    status: str = "completed",
    error_category: str = "",
) -> LLMRequestLog:
    log = LLMRequestLog(
        generation_task_id=generation_task_id,
        book_id=book_id,
        task_type=task_type,
        provider=provider,
        model=model,
        request_id=request_id,
        prompt_template=prompt_template,
        prompt_chars=prompt_chars,
        response_chars=response_chars,
        estimated_prompt_tokens=estimated_prompt_tokens,
        estimated_response_tokens=estimated_response_tokens,
        estimated_total_tokens=estimated_prompt_tokens + estimated_response_tokens,
        actual_prompt_tokens=actual_prompt_tokens,
        actual_response_tokens=actual_response_tokens,
        actual_total_tokens=actual_total_tokens,
        elapsed_ms=elapsed_ms,
        status=status,
        error_category=error_category,
    )
    session.add(log)
    session.flush()
    return log


def list_llm_request_logs(
    session: Session,
    *,
    book_id: int | None = None,
    status: str = "",
    limit: int = 20,
) -> list[LLMRequestLog]:
    stmt = select(LLMRequestLog).order_by(LLMRequestLog.id.desc()).limit(limit)
    if book_id is not None:
        stmt = stmt.where(LLMRequestLog.book_id == book_id)
    if status:
        stmt = stmt.where(LLMRequestLog.status == status)
    return list(session.scalars(stmt))


def summarize_llm_usage(session: Session, *, book_id: int | None = None) -> LLMUsageSummary:
    stmt = select(
        func.count(LLMRequestLog.id),
        func.sum(LLMRequestLog.estimated_total_tokens),
        func.sum(LLMRequestLog.actual_total_tokens),
        func.sum(LLMRequestLog.actual_prompt_tokens),
        func.sum(LLMRequestLog.actual_response_tokens),
        func.sum(LLMRequestLog.estimated_prompt_tokens),
        func.sum(LLMRequestLog.estimated_response_tokens),
        func.sum(
            case(
                (LLMRequestLog.actual_total_tokens > 0, LLMRequestLog.actual_prompt_tokens),
                else_=LLMRequestLog.estimated_prompt_tokens,
            )
        ),
        func.sum(
            case(
                (LLMRequestLog.actual_total_tokens > 0, LLMRequestLog.actual_response_tokens),
                else_=LLMRequestLog.estimated_response_tokens,
            )
        ),
        func.sum(LLMRequestLog.elapsed_ms),
        func.sum(case((LLMRequestLog.status == "completed", 1), else_=0)),
        func.sum(case((LLMRequestLog.status == "failed", 1), else_=0)),
    )
    if book_id is not None:
        stmt = stmt.where(LLMRequestLog.book_id == book_id)
    row = session.execute(stmt).one()
    actual_total = int(row[2] or 0)
    billable_prompt = int(row[7] or 0)
    billable_response = int(row[8] or 0)
    return LLMUsageSummary(
        book_id=book_id,
        request_count=int(row[0] or 0),
        estimated_total_tokens=int(row[1] or 0),
        actual_total_tokens=actual_total,
        billable_prompt_tokens=billable_prompt,
        billable_response_tokens=billable_response,
        billable_total_tokens=billable_prompt + billable_response,
        elapsed_ms=int(row[9] or 0),
        completed_count=int(row[10] or 0),
        failed_count=int(row[11] or 0),
    )


def summarize_llm_failures(session: Session, *, book_id: int | None = None, limit: int = 20) -> list[LLMFailureBucket]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    stmt = (
        select(LLMRequestLog)
        .where(LLMRequestLog.status == "failed")
        .order_by(LLMRequestLog.id.desc())
        .limit(limit)
    )
    if book_id is not None:
        stmt = stmt.where(LLMRequestLog.book_id == book_id)
    buckets: dict[str, list[LLMRequestLog]] = {}
    for log in session.scalars(stmt):
        category = log.error_category or "unknown"
        buckets.setdefault(category, []).append(log)
    rows: list[LLMFailureBucket] = []
    for category, logs in sorted(buckets.items(), key=lambda item: (-len(item[1]), item[0])):
        latest = logs[0]
        rows.append(
            LLMFailureBucket(
                error_category=category,
                count=len(logs),
                latest_request_id=latest.id,
                latest_task_type=latest.task_type,
                latest_provider=latest.provider,
                latest_model=latest.model,
                latest_elapsed_ms=latest.elapsed_ms,
                suggestion=llm_failure_suggestion(category),
            )
        )
    return rows


def llm_failure_suggestion(error_category: str) -> str:
    suggestions = {
        "auth": "检查 ARK_API_KEY、ARK_BASE_URL、账号权限和环境变量是否被当前进程加载。",
        "permission": "检查模型访问权限、平台账号权限和工作区授权。",
        "rate_limit": "降低 worker 并发/循环频率，稍后重试，必要时调整供应商限额。",
        "timeout": "提高任务超时阈值或降低 max_tokens；检查网络延迟和供应商响应时间。",
        "network": "检查网络、DNS、代理和供应商 endpoint 可达性。",
        "context_length": "缩短 prompt、减少上下文片段或降低章节输入长度。",
        "structured_output": "检查 prompt 的 JSON 约束，必要时降低温度或补修复解析逻辑。",
        "provider": "查看供应商状态、模型名、返回体和服务端错误；可稍后重试。",
        "validation": "检查任务前置条件，例如 Brief、Story Bible、Canon 或章节状态。",
        "execution": "查看任务详情和 traceback 类错误，优先复现单任务。",
        "unknown": "查看最近失败请求和对应 GenerationTask 详情，补充错误分类规则。",
    }
    return suggestions.get(error_category or "unknown", suggestions["unknown"])
