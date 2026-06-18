from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import ChapterBrief, ChapterVersion
from app.services.continuity import default_chapter_continuity_summary, record_chapter_continuity
from app.services.planning import plan_chapters, run_next_action
from app.services.production_decision import decide_chapter_production
from app.services.revision_supervisor import apply_revision_budget_recovery


@dataclass(frozen=True)
class AuthorModeRun:
    executed: list[dict]
    terminal_status: str
    terminal_message: str

    @property
    def latest_result(self) -> dict:
        return self.executed[-1] if self.executed else {}


def run_author_mode(
    *,
    book_id: int,
    chapter_number: int,
    platform: str = "manual",
    max_revision_cycles: int = 3,
) -> AuthorModeRun:
    if not book_id or not chapter_number:
        raise ValueError("book_id and chapter_number are required")
    max_revision_cycles = max(1, min(5, int(max_revision_cycles or 3)))
    executed: list[dict] = []
    revision_count = 0
    recovery_revision_used = False
    budget_recovery_used = False
    max_actions = max_revision_cycles * 3 + 8
    for _ in range(max_actions):
        with session_scope() as session:
            item = plan_chapters(session, book_id=book_id, start=chapter_number, count=1)[0]
            decision = decide_chapter_production(item)
            if decision.needs_author:
                executed.append(
                    {
                        "action": item.next_action,
                        "status": "blocked",
                        "message": decision.next_step,
                        "object_id": item.latest_version_id,
                    }
                )
                break
            if item.next_action in {"done", "create_publish_job", "publish_job_dry_run", "queue_publish_job", "mark_publish_job"}:
                executed.append(
                    {
                        "action": item.next_action,
                        "status": "blocked",
                        "message": "章节正文阶段已完成，后续交给人工或发布流程。",
                        "object_id": item.latest_version_id or item.publish_job_id,
                    }
                )
                break
            if item.next_action == "record_chapter_continuity":
                summary = default_chapter_continuity_summary(session, book_id=book_id, chapter_number=chapter_number)
                result = record_chapter_continuity(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                    summary=summary,
                )
                executed.append(
                    {
                        "action": "record_chapter_continuity",
                        "status": "executed",
                        "message": "已自动回写连续性。",
                        "object_id": result.chapter_id,
                    }
                )
                continue
            if item.next_action == "revise_chapter":
                if revision_count >= max_revision_cycles:
                    if _is_recovery_revision_pending(session, item) and not recovery_revision_used:
                        recovery_revision_used = True
                        revision_count += 1
                    elif not budget_recovery_used:
                        recovery = apply_revision_budget_recovery(session, book_id=book_id, chapter_number=chapter_number)
                        recovered = recovery.status in {"recovered", "restored_readable", "restored_readable_needs_revision"}
                        executed.append(
                            {
                                "action": "revision_budget_recovery",
                                "status": "executed" if recovered else "blocked",
                                "message": recovery.message,
                                "object_id": recovery.recovery_brief_id or recovery.recovery_version_id,
                            }
                        )
                        if recovered:
                            budget_recovery_used = True
                            recovery_revision_used = True
                            revision_count += 1
                            continue
                        break
                    else:
                        executed.append(
                            {
                                "action": "revise_chapter",
                                "status": "blocked",
                                "message": "自动修订预算已用完，系统已回到当前最佳稿并换策略处理；若仍未通过，系统会保留最佳稿并暂停消耗。",
                                "object_id": item.latest_version_id,
                            }
                        )
                        break
                else:
                    revision_count += 1
            result = run_next_action(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                dry_run=False,
                queue_generation=False,
                platform=platform,
            )
            executed.append(
                {
                    "action": result.action,
                    "status": result.status,
                    "message": result.message,
                    "object_id": result.object_id,
                }
            )
            if result.status != "executed":
                break
    terminal = author_terminal_status(executed)
    return AuthorModeRun(executed=executed, terminal_status=terminal["status"], terminal_message=terminal["message"])


def _is_recovery_revision_pending(session, item) -> bool:
    if not item.latest_version_id or not item.chapter_id:
        return False
    version = session.get(ChapterVersion, item.latest_version_id)
    if version and str(version.source or "").startswith("revision_recovery:"):
        return True
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == item.chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]) if brief else ""
    return "system_revision_trend_recovery" in text


def author_terminal_status(executed: list[dict]) -> dict:
    if not executed:
        return {"status": "system_failed", "message": "主笔模式没有执行任何步骤。"}
    last = executed[-1]
    action = str(last.get("action") or "")
    status = str(last.get("status") or "")
    message = str(last.get("message") or "")
    if action == "approve_chapter":
        return {"status": "ready_for_human_reading", "message": "可读稿已完成，等待你阅读后通过或修改。"}
    if status == "failed":
        return {"status": "system_failed", "message": message or "模型或系统执行失败。"}
    if action == "revise_chapter" and status == "blocked":
        return {"status": "auto_paused", "message": message or "自动修订预算已用完，系统已暂停以避免继续消耗。"}
    if action == "revision_budget_recovery" and status == "executed":
        return {"status": "auto_paused", "message": message or "系统已自动回到最佳稿并换策略修订。"}
    if status == "blocked":
        return {"status": "ready_for_human_reading", "message": message or "正文阶段已完成，等待人工判断。"}
    return {"status": "auto_paused", "message": message or "主笔模式已暂停，系统已保留当前最佳状态。"}


def author_background_timeout_seconds(max_revision_cycles: int) -> int:
    cycles = max(1, min(5, int(max_revision_cycles or 3)))
    return max(1800, cycles * 900)
