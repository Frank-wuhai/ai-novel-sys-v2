from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.automation.openclaw_ops import OpenClawPublishingOperator
from app.models.entities import ChapterVersion, PublishExecution, PublishJob, PublishingTarget
from app.services.publish_preflight import build_publish_preflight
from app.workflows.state_machine import WorkflowError, move


def create_publish_job(session: Session, *, version_id: int, platform: str) -> PublishJob:
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    if version.status != "approved":
        raise ValueError("only approved chapter versions can create publish jobs")
    existing = session.scalar(
        select(PublishJob).where(
            PublishJob.chapter_version_id == version_id,
            PublishJob.platform == platform,
            PublishJob.status.in_(["pending", "dry_run_ready", "queued", "published"]),
        )
    )
    if existing:
        raise ValueError(f"active publish job already exists: {existing.id} ({existing.status})")
    target = get_publishing_target(session, platform=platform)
    payload = {}
    if target:
        payload = {
            "publishing_target_id": target.id,
            "account_label": target.account_label,
            "work_identifier": target.work_identifier,
            "automation_mode": target.automation_mode,
            "target_config": _loads_json(target.config_json),
        }
    job = PublishJob(
        chapter_version_id=version_id,
        platform=platform,
        status="pending",
        automation_payload=json.dumps(payload, ensure_ascii=False),
    )
    session.add(job)
    session.flush()
    return job


def auto_prepare_publish_job(
    session: Session,
    *,
    version_id: int,
    platform: str,
    confirm_real_platform: bool = False,
) -> dict:
    """Move an approved chapter as far through publishing as safety gates allow."""
    version = session.get(ChapterVersion, version_id)
    if not version:
        raise ValueError(f"chapter version not found: {version_id}")
    if version.status != "approved":
        raise ValueError("one-click publish requires an approved chapter version")

    steps: list[dict] = []
    job = _active_publish_job(session, version_id=version_id, platform=platform)
    if not job:
        preflight = build_publish_preflight(session, version_id=version_id)
        if not preflight["passed"]:
            return {
                "status": "blocked",
                "job": None,
                "steps": steps,
                "preflight": preflight,
                "message": "发布前检查未通过，未创建发布任务。",
            }
        job = create_publish_job(session, version_id=version_id, platform=platform)
        steps.append({"action": "create_publish_job", "status": job.status, "publish_job_id": job.id})

    if job.status == "pending":
        preflight = build_publish_preflight(session, version_id=version_id)
        if not preflight["passed"]:
            return {
                "status": "blocked",
                "job": _publish_job_summary(job),
                "steps": steps,
                "preflight": preflight,
                "message": "发布前检查未通过，未生成发布预览。",
            }
        job = publish_job_dry_run(session, job_id=job.id)
        steps.append({"action": "publish_job_dry_run", "status": job.status, "publish_job_id": job.id})

    if job.status == "dry_run_ready":
        preflight = build_publish_preflight(session, version_id=version_id)
        if not preflight["passed"]:
            return {
                "status": "blocked",
                "job": _publish_job_summary(job),
                "steps": steps,
                "preflight": preflight,
                "message": "发布前检查未通过，未进入待发布。",
            }
        job = queue_publish_job(session, job_id=job.id)
        steps.append({"action": "queue_publish_job", "status": job.status, "publish_job_id": job.id})

    preflight = build_publish_preflight(session, version_id=version_id)
    if job.status == "queued" and confirm_real_platform:
        job, execution = execute_publish_job(session, job_id=job.id, confirm=True)
        steps.append(
            {
                "action": "execute_publish_job_confirm",
                "status": execution.status,
                "publish_job_id": job.id,
                "publish_execution_id": execution.id,
            }
        )

    message = {
        "queued": "发布任务已准备好，等待最终平台发布确认。",
        "published": "发布任务已执行并标记为已发布。",
        "failed": "发布执行失败，请查看执行报告。",
    }.get(job.status, f"发布任务停在 {job.status}。")
    return {
        "status": job.status,
        "job": _publish_job_summary(job),
        "steps": steps,
        "preflight": preflight,
        "message": message,
    }


def upsert_publishing_target(
    session: Session,
    *,
    platform: str,
    account_label: str = "",
    work_identifier: str = "",
    automation_mode: str = "manual",
    status: str = "active",
    config_json: str = "{}",
) -> PublishingTarget:
    if not platform:
        raise ValueError("platform is required")
    _loads_json(config_json)
    target = session.scalar(
        select(PublishingTarget).where(
            PublishingTarget.platform == platform,
            PublishingTarget.account_label == account_label,
            PublishingTarget.work_identifier == work_identifier,
        )
    )
    if not target:
        target = PublishingTarget(platform=platform, account_label=account_label, work_identifier=work_identifier)
        session.add(target)
    target.automation_mode = automation_mode
    target.status = status
    target.config_json = config_json
    session.flush()
    return target


def get_publishing_target(
    session: Session,
    *,
    platform: str,
    account_label: str = "",
    work_identifier: str = "",
) -> PublishingTarget | None:
    stmt = select(PublishingTarget).where(PublishingTarget.platform == platform, PublishingTarget.status == "active")
    if account_label:
        stmt = stmt.where(PublishingTarget.account_label == account_label)
    if work_identifier:
        stmt = stmt.where(PublishingTarget.work_identifier == work_identifier)
    return session.scalar(stmt.order_by(PublishingTarget.id.desc()))


def list_publishing_targets(session: Session, *, platform: str = "", status: str = "") -> list[PublishingTarget]:
    stmt = select(PublishingTarget).order_by(PublishingTarget.id)
    if platform:
        stmt = stmt.where(PublishingTarget.platform == platform)
    if status:
        stmt = stmt.where(PublishingTarget.status == status)
    return list(session.scalars(stmt))


def list_publish_jobs(session: Session, *, status: str = "") -> list[PublishJob]:
    stmt = select(PublishJob).order_by(PublishJob.id)
    if status:
        stmt = stmt.where(PublishJob.status == status)
    return list(session.scalars(stmt))


def get_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    return job


def list_publish_executions(session: Session, *, job_id: int | None = None, limit: int = 20) -> list[PublishExecution]:
    stmt = select(PublishExecution).order_by(PublishExecution.id.desc()).limit(limit)
    if job_id is not None:
        stmt = stmt.where(PublishExecution.publish_job_id == job_id)
    return list(session.scalars(stmt))


def publish_job_dry_run(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError("publish job points to missing chapter version")
    if version.status != "approved":
        raise ValueError("publish dry-run requires approved chapter version")
    payload = _loads_json(job.automation_payload)
    operator = OpenClawPublishingOperator()
    result = operator.publish_dry_run(
        platform=job.platform,
        title=version.title,
        content=version.content,
        job_id=job.id,
        target_config=_automation_target_config(payload),
    )
    job.status = move("publish_job", job.status, result.status, "dry_run")
    job.result_report = _append_artifact_report(result.report, artifact_path=result.artifact_path)
    session.flush()
    return job


def queue_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    job.status = move("publish_job", job.status, "queued", "queue_for_platform")
    session.flush()
    return job


def mark_publish_job(session: Session, *, job_id: int, status: str, report: str = "") -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    if status == "published":
        action = "mark_published"
    elif status == "failed":
        action = "mark_failed"
    else:
        raise WorkflowError("publish job can only be marked published or failed")
    job.status = move("publish_job", job.status, status, action)
    if report:
        job.result_report = report
    session.flush()
    return job


def retry_publish_job(session: Session, *, job_id: int) -> PublishJob:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    job.status = move("publish_job", job.status, "queued", "retry")
    session.flush()
    return job


def execute_publish_job(session: Session, *, job_id: int, confirm: bool = False) -> tuple[PublishJob, PublishExecution]:
    job = session.get(PublishJob, job_id)
    if not job:
        raise ValueError(f"publish job not found: {job_id}")
    version = session.get(ChapterVersion, job.chapter_version_id)
    if not version:
        raise ValueError("publish job points to missing chapter version")
    if job.status != "queued":
        raise ValueError("publish execution requires queued publish job")
    payload = _loads_json(job.automation_payload)
    target_config = _automation_target_config(payload)
    operator = OpenClawPublishingOperator()
    if not confirm:
        result = operator.publish_dry_run(
            platform=job.platform,
            title=version.title,
            content=version.content,
            job_id=job.id,
            target_config=target_config,
        )
        execution = PublishExecution(
            publish_job_id=job.id,
            platform=job.platform,
            status="blocked",
            automation_mode="confirmation_required",
            report=_append_artifact_report(f"Final publish confirmation required. {result.report}", artifact_path=result.artifact_path),
            artifact_path=result.artifact_path,
        )
        session.add(execution)
        session.flush()
        return job, execution
    result = operator.publish_confirmed(
        platform=job.platform,
        title=version.title,
        content=version.content,
        job_id=job.id,
        target_config=target_config,
    )
    target_status = "published" if result.status == "published" else "failed"
    action = "mark_published" if target_status == "published" else "mark_failed"
    job.status = move("publish_job", job.status, target_status, action)
    job.result_report = _append_artifact_report(result.report, artifact_path=result.artifact_path)
    execution = PublishExecution(
        publish_job_id=job.id,
        platform=job.platform,
        status=target_status,
        automation_mode="confirmed",
        report=job.result_report,
        artifact_path=result.artifact_path,
    )
    session.add(execution)
    session.flush()
    return job, execution


def _append_artifact_report(report: str, *, artifact_path: str) -> str:
    if not artifact_path:
        return report
    return f"{report}\nartifact_path={artifact_path}"


def _active_publish_job(session: Session, *, version_id: int, platform: str) -> PublishJob | None:
    return session.scalar(
        select(PublishJob)
        .where(
            PublishJob.chapter_version_id == version_id,
            PublishJob.platform == platform,
            PublishJob.status.in_(["pending", "dry_run_ready", "queued", "published", "failed"]),
        )
        .order_by(PublishJob.id.desc())
    )


def _publish_job_summary(job: PublishJob) -> dict:
    return {
        "id": job.id,
        "version_id": job.chapter_version_id,
        "platform": job.platform,
        "status": job.status,
        "result_report": job.result_report,
    }


def _automation_target_config(payload: dict) -> dict:
    target_config = payload.get("target_config", {})
    if not isinstance(target_config, dict):
        target_config = {}
    merged = dict(target_config)
    for key in ("publishing_target_id", "account_label", "work_identifier", "automation_mode"):
        if key in payload and key not in merged:
            merged[key] = payload[key]
    return merged


def _loads_json(value: str) -> dict:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON object is required")
    return data
