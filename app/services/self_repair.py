from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import ArkOpenAIProvider
from app.services.dashboard import build_project_snapshot
from app.services.llm_queue import build_generation_queue_health


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "data" / "self_repair"
SAFE_REGRESSION_COMMANDS = {
    "production": ["venv/bin/python", "scripts/production_decision_regression.py"],
    "dashboard": ["venv/bin/python", "scripts/dashboard_real_click_path_regression.py"],
    "strategy": ["venv/bin/python", "scripts/production_strategy_regression.py"],
    "quick": ["venv/bin/python", "scripts/run_regressions.py", "--skip-smoke"],
}


@dataclass(frozen=True)
class SelfRepairReport:
    status: str
    report_path: str
    headline: str
    summary: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "report_path": self.report_path,
            "headline": self.headline,
            "summary": self.summary,
            "payload": self.payload,
        }


def generate_self_repair_plan(
    session: Session,
    *,
    issue: str,
    book_id: int = 0,
    chapter_number: int = 1,
    live_model: bool = False,
) -> SelfRepairReport:
    issue = str(issue or "").strip()
    if not issue:
        raise ValueError("请先描述要修复的问题。")
    context = _build_context(session, issue=issue, book_id=book_id, chapter_number=chapter_number)
    if live_model:
        model_payload = _ask_model_for_repair_plan(context)
    else:
        model_payload = _dry_repair_plan(context)
    payload = {
        "schema": "self_repair_plan_v1",
        "created_at": _now_label(),
        "live_model": live_model,
        "issue": issue,
        "context": context,
        "repair_plan": model_payload,
        "safety": {
            "direct_file_write": False,
            "arbitrary_shell": False,
            "secret_files_included": False,
            "apply_policy": "先生成方案和补丁草案；应用代码必须走受控补丁和回归验证。",
        },
    }
    path = _write_report("plan", payload)
    headline = str(model_payload.get("headline") or "自修复方案已生成")
    summary = str(model_payload.get("summary") or "已生成诊断、修复步骤和建议回归。")
    return SelfRepairReport("completed", _relative(path), headline, summary, payload)


def run_self_repair_regressions(*, suite: str = "production") -> SelfRepairReport:
    suite = suite if suite in SAFE_REGRESSION_COMMANDS else "production"
    command = SAFE_REGRESSION_COMMANDS[suite]
    started = datetime.utcnow()
    result = subprocess.run(
        command,
        cwd=str(ROOT),
        text=True,
        capture_output=True,
        timeout=900,
    )
    payload = {
        "schema": "self_repair_regression_v1",
        "created_at": _now_label(),
        "suite": suite,
        "command": command,
        "returncode": result.returncode,
        "elapsed_seconds": round((datetime.utcnow() - started).total_seconds(), 1),
        "stdout": _tail_text(result.stdout, 16000),
        "stderr": _tail_text(result.stderr, 12000),
    }
    path = _write_report("regression", payload)
    passed = result.returncode == 0
    return SelfRepairReport(
        "completed" if passed else "failed",
        _relative(path),
        "安全回归通过" if passed else "安全回归失败",
        f"{suite} suite returncode={result.returncode}",
        payload,
    )


def latest_self_repair_report() -> dict[str, Any]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = sorted(REPORT_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not rows:
        return {"status": "empty", "message": "暂无自修复报告。"}
    path = rows[0]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        data = {"status": "invalid_json"}
    return {"status": "found", "report_path": _relative(path), "payload": data}


def perform_self_repair_action(session: Session, payload: dict[str, Any]) -> dict[str, Any] | None:
    action = str(payload.get("action") or "")
    if action == "generate_self_repair_plan":
        return generate_self_repair_plan(
            session,
            issue=str(payload.get("issue") or ""),
            book_id=int(payload.get("book_id") or 0),
            chapter_number=int(payload.get("chapter_number") or 1),
            live_model=bool(payload.get("live_model")),
        ).to_dict()
    if action == "run_self_repair_regressions":
        return run_self_repair_regressions(suite=str(payload.get("suite") or "production")).to_dict()
    if action == "latest_self_repair_report":
        return latest_self_repair_report()
    return None


def _build_context(session: Session, *, issue: str, book_id: int, chapter_number: int) -> dict[str, Any]:
    queue = build_generation_queue_health(session)
    snapshot: dict[str, Any] = {}
    if book_id:
        try:
            snapshot = build_project_snapshot(session, book_id=book_id, start=chapter_number, count=1, chapter_number=chapter_number)
        except Exception as exc:  # noqa: BLE001 - report should preserve diagnostics.
            snapshot = {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "issue": issue[:2000],
        "book_id": book_id,
        "chapter_number": chapter_number,
        "queue": {
            "total": queue.total,
            "counts": queue.counts,
            "running_count": queue.running_count,
            "stale_running_count": queue.stale_running_count,
            "oldest_pending_id": queue.oldest_pending_id,
        },
        "snapshot": _compact_snapshot(snapshot),
        "git": {
            "status": _safe_command(["git", "status", "--short"], limit=12000),
            "diff_stat": _safe_command(["git", "diff", "--stat"], limit=12000),
        },
        "recent_dashboard_log": _read_tail(ROOT / "data" / "logs" / "dashboard-8765.log", 12000),
        "model_config": {
            "llm_plan": settings.llm_plan,
            "planning_model": settings.llm_planning_model,
            "revision_model": settings.llm_revision_model,
        },
    }


def _ask_model_for_repair_plan(context: dict[str, Any]) -> dict[str, Any]:
    prompt = (
        "你是 AI Novel System v2 的受控维护规划器。根据 JSON 上下文输出严格 JSON，"
        "只允许诊断、修复步骤、建议修改文件、建议回归测试和风险。不要要求任意 shell 权限，"
        "不要读取 .env，不要输出密钥，不要声称已经修改文件。\n\n"
        "输出 schema: {headline, summary, suspected_root_causes, repair_steps, files_to_inspect, "
        "patch_outline, regression_tests, risks}。\n\n"
        + json.dumps(context, ensure_ascii=False, indent=2)[:28000]
    )
    response = ArkOpenAIProvider(timeout=settings.llm_request_timeout_seconds).generate(
        prompt,
        max_tokens=2600,
        temperature=0.25,
        response_format={"type": "json_object"},
        model=settings.llm_planning_model,
    )
    try:
        data = json.loads(response.text or "{}")
    except json.JSONDecodeError:
        data = {"headline": "模型返回非 JSON", "summary": response.text[:3000], "raw": response.text}
    data["llm"] = {
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        "elapsed_ms": response.elapsed_ms,
        "usage": response.usage,
    }
    return data


def _dry_repair_plan(context: dict[str, Any]) -> dict[str, Any]:
    issue = context.get("issue") or ""
    status = str((context.get("git") or {}).get("status") or "").strip()
    return {
        "headline": "已生成自修复诊断草案",
        "summary": "演示模式未调用真实模型；已整理上下文，可切换为真实模型生成修复方案。",
        "suspected_root_causes": [
            "前端按钮 intent 与后端 next_action 可能未映射。",
            "后台队列、生产策略或状态修复可能存在旧状态残留。",
            "最近代码变更需要用回归套件确认没有连锁问题。",
        ],
        "repair_steps": [
            f"复现问题：{issue[:120]}",
            "检查 dashboard snapshot 中 command_center.primary_action 与 chapter.next_action 是否一致。",
            "检查对应 action 是否在 production_decision 与 AUTO_ACTIONS 中有明确映射。",
            "修复后运行 production_decision、dashboard_real_click_path、production_strategy 回归。",
        ],
        "files_to_inspect": [
            "app/services/production_decision.py",
            "app/services/production_orchestrator.py",
            "app/services/planning.py",
            "app/dashboard.html",
            "scripts/run_local_dashboard.py",
        ],
        "patch_outline": "为漏映射动作补 ProductionDecision 分支，并为 dashboard 点击路径补回归。",
        "regression_tests": list(SAFE_REGRESSION_COMMANDS),
        "risks": ["当前工作区已有改动：" + ("是" if status else "否")],
    }


def _compact_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    if not snapshot:
        return {}
    chapters = snapshot.get("chapters") or []
    current = chapters[0] if chapters else {}
    return {
        "book": snapshot.get("book"),
        "command_center": snapshot.get("command_center"),
        "production_control": snapshot.get("production_control"),
        "current_chapter": current,
        "readiness": snapshot.get("readiness"),
        "queue_counts": (snapshot.get("generation_queue") or {}).get("counts"),
    }


def _safe_command(command: list[str], *, limit: int) -> str:
    try:
        result = subprocess.run(command, cwd=str(ROOT), text=True, capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - diagnostic only.
        return f"{type(exc).__name__}: {exc}"
    return _tail_text((result.stdout + result.stderr).strip(), limit)


def _read_tail(path: Path, limit: int) -> str:
    if not path.exists():
        return ""
    return _tail_text(path.read_text(encoding="utf-8", errors="replace"), limit)


def _tail_text(text: str, limit: int) -> str:
    value = str(text or "")
    return value[-limit:] if len(value) > limit else value


def _write_report(prefix: str, payload: dict[str, Any]) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"{prefix}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _relative(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _now_label() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
