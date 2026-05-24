from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.core.config import settings


@dataclass
class AutomationResult:
    status: str
    report: str
    artifact_path: str = ""


class OpenClawPublishingOperator:
    """Boundary object for platform operations.

    This class intentionally does not make story or quality decisions. It should
    receive an approved publish job and perform browser/platform actions only.
    """

    def publish_dry_run(
        self,
        *,
        platform: str,
        title: str,
        content: str,
        job_id: int | None = None,
        target_config: dict | None = None,
    ) -> AutomationResult:
        if not title or not content:
            return AutomationResult(status="blocked", report="title and content are required")
        artifact_path = _write_publish_artifact(
            mode="dry_run",
            platform=platform,
            title=title,
            content=content,
            job_id=job_id,
            target_config=target_config or {},
            status="dry_run_ready",
        )
        return AutomationResult(
            status="dry_run_ready",
            report=f"Would publish to {platform}: title={title!r}, chars={len(content)}",
            artifact_path=artifact_path,
        )

    def publish_confirmed(
        self,
        *,
        platform: str,
        title: str,
        content: str,
        job_id: int | None = None,
        target_config: dict | None = None,
    ) -> AutomationResult:
        if not title or not content:
            return AutomationResult(status="failed", report="title and content are required")
        config = target_config or {}
        if config.get("require_manual_platform_step", False):
            artifact_path = _write_publish_artifact(
                mode="confirmed_blocked",
                platform=platform,
                title=title,
                content=content,
                job_id=job_id,
                target_config=config,
                status="failed",
            )
            return AutomationResult(
                status="failed",
                report="platform target requires manual platform step; no browser automation command configured",
                artifact_path=artifact_path,
            )
        artifact_path = _write_publish_artifact(
            mode="confirmed",
            platform=platform,
            title=title,
            content=content,
            job_id=job_id,
            target_config=config,
            status="published",
        )
        return AutomationResult(
            status="published",
            report=f"Confirmed publish to {platform}: title={title!r}, chars={len(content)}",
            artifact_path=artifact_path,
        )


def _write_publish_artifact(
    *,
    mode: str,
    platform: str,
    title: str,
    content: str,
    job_id: int | None,
    target_config: dict,
    status: str,
) -> str:
    stamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
    safe_platform = _safe_name(platform or "unknown")
    safe_job = str(job_id or "manual")
    artifact_dir = settings.outputs_dir / "publish_executions" / f"job-{safe_job}-{safe_platform}-{stamp}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "mode": mode,
        "platform": platform,
        "job_id": job_id,
        "title": title,
        "content_chars": len(content),
        "content_excerpt": content[:1200],
        "target_config": _redact_config(target_config),
        "status": status,
        "created_at": datetime.utcnow().isoformat(timespec="seconds"),
    }
    (artifact_dir / "payload.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    (artifact_dir / "content.txt").write_text(content, encoding="utf-8")
    (artifact_dir / "report.txt").write_text(
        f"mode={mode}\nplatform={platform}\njob_id={job_id or ''}\nstatus={status}\ntitle={title}\ncontent_chars={len(content)}\n",
        encoding="utf-8",
    )
    return str(artifact_dir)


def _safe_name(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip())
    return safe or "unknown"


def _redact_config(config: dict) -> dict:
    redacted = {}
    for key, value in config.items():
        lowered = str(key).lower()
        if any(token in lowered for token in ("secret", "token", "password", "key")):
            redacted[key] = "***"
        else:
            redacted[key] = value
    return redacted
