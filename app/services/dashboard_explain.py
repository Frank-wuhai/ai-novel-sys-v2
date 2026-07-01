from __future__ import annotations


def explain_chapter_state(snapshot: dict) -> dict:
    action = str(snapshot.get("next_action") or "")
    team_status = str(snapshot.get("team_status") or snapshot.get("author_status") or "")
    reason = str(snapshot.get("reason") or "")
    version_status = str(snapshot.get("version_status") or "")
    quality_passed = snapshot.get("quality_passed")
    publish_status = str(snapshot.get("publish_status") or "")
    queue = snapshot.get("queue") if isinstance(snapshot.get("queue"), dict) else {}

    severity = "info"
    if action in {"manual_inspection", "wait_generation_task"} or team_status in {"blocked", "queued"}:
        severity = "warning"
    if action in {"create_revision_brief", "revision_budget_recovery", "generate_rebuild_candidates"}:
        severity = "attention"
    if action in {"approve_chapter", "mark_publish_job"}:
        severity = "decision"
    if action == "done" or version_status == "published" or publish_status == "published":
        severity = "success"

    return {
        "severity": severity,
        "summary": _summary(action=action, version_status=version_status, quality_passed=quality_passed, publish_status=publish_status, team_status=team_status),
        "why": _why(action=action, reason=reason, queue=queue),
        "next": _next(action=action, snapshot=snapshot, queue=queue),
        "operator_hint": _operator_hint(action=action, snapshot=snapshot, queue=queue),
    }


def explain_queue_task(snapshot: dict) -> dict:
    status = str(snapshot.get("status") or "")
    stale = bool(snapshot.get("stale"))
    task_type = str(snapshot.get("type") or "")
    chapter = snapshot.get("chapter") or ""
    severity = "success" if status == "completed" else "info"
    if status in {"pending", "running"}:
        severity = "warning" if stale else "info"
    if status == "failed":
        severity = "attention"
    return {
        "severity": severity,
        "summary": f"{task_type} chapter={chapter} status={status}",
        "why": "任务运行超时或 heartbeat 过期，可执行 stale recovery。" if stale else "任务状态正常可解释。",
        "next": "recover-stale-generation-tasks" if stale else _queue_next(status),
        "operator_hint": f"lease_owner={snapshot.get('lease_owner', '')} heartbeat_at={snapshot.get('heartbeat_at', '')} lease_expires_at={snapshot.get('lease_expires_at', '')}",
    }


def _summary(*, action: str, version_status: str, quality_passed, publish_status: str, team_status: str) -> str:
    if action == "done" or version_status == "published" or publish_status == "published":
        return "章节已完成，生产链路处于终态。"
    if action == "wait_generation_task":
        return "章节正在等待后台模型队列完成。"
    if action == "generate_rebuild_candidates":
        return "线性修订收益不足，系统将改走多候选重建。"
    if action in {"draft_chapter", "queue_draft_chapter"}:
        return "章节已具备草稿生产条件。"
    if action in {"revise_chapter", "queue_revise_chapter"}:
        return "章节需要按当前修订合同继续修订。"
    if action == "approve_chapter":
        return "质量门禁已通过，等待主编确认采用。"
    if action == "mark_publish_job":
        return "发布任务已准备好，等待人工确认发布结果。"
    if quality_passed is False:
        return "章节未通过质量门禁，需要先完成修订或重建。"
    return f"当前状态：{team_status or action or 'unknown'}。"


def _why(*, action: str, reason: str, queue: dict) -> str:
    if queue:
        status = queue.get("status") or ""
        return f"存在关联队列任务 status={status}；{reason}".strip("；")
    return reason or "由 production planner 根据章节版本、质检、发布任务与队列状态计算。"


def _next(*, action: str, snapshot: dict, queue: dict) -> str:
    chapter = snapshot.get("number") or snapshot.get("chapter") or ""
    if action == "wait_generation_task":
        return "运行 run-generation-queue 或等待后台 worker。"
    if action == "generate_rebuild_candidates":
        return f"运行 production-run-next --chapter-number {chapter} --queue-generation。"
    if action in {"draft_chapter", "revise_chapter"}:
        return f"运行 production-run-next --chapter-number {chapter}；生产模式下建议 queue_generation。"
    if action == "approve_chapter":
        return "打开 human-decision-package 后确认采用或退回。"
    if action == "mark_publish_job":
        return "确认平台发布结果并 mark-publish-job。"
    if action == "done":
        return "切换到下一章。"
    return "按 recommendation 或 production_control.next_actions 执行。"


def _operator_hint(*, action: str, snapshot: dict, queue: dict) -> str:
    if queue:
        return f"queue_task_id={queue.get('id', '')} heartbeat_at={queue.get('heartbeat_at', '')} stale={queue.get('stale', False)}"
    return f"chapter={snapshot.get('number', '')} version={snapshot.get('version_id', '')} action={action}"


def _queue_next(status: str) -> str:
    if status == "pending":
        return "run-generation-queue"
    if status == "running":
        return "等待 heartbeat 或检查 worker。"
    if status == "failed":
        return "show-generation-task 后按错误类型 retry 或人工处理。"
    return "无需处理。"
