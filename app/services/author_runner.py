from __future__ import annotations

from dataclasses import dataclass

from app.db.session import session_scope
from app.services.continuity import default_chapter_continuity_summary, record_chapter_continuity
from app.services.planning import plan_chapters, run_next_action


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
    max_actions = max_revision_cycles * 3 + 8
    for _ in range(max_actions):
        with session_scope() as session:
            item = plan_chapters(session, book_id=book_id, start=chapter_number, count=1)[0]
            if item.next_action == "approve_chapter":
                executed.append(
                    {
                        "action": "approve_chapter",
                        "status": "blocked",
                        "message": "可读稿已完成，等待人工通过。",
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
                summary = default_chapter_continuity_summary(
                    session,
                    book_id=book_id,
                    chapter_number=chapter_number,
                )
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
                    executed.append(
                        {
                            "action": "revise_chapter",
                            "status": "blocked",
                            "message": "已达到自动修订轮数上限，需要人工判断方向。",
                            "object_id": item.latest_version_id,
                        }
                    )
                    break
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
    return AuthorModeRun(
        executed=executed,
        terminal_status=terminal["status"],
        terminal_message=terminal["message"],
    )


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
        return {"status": "needs_author_direction", "message": "自动修订已达到上限，需要你给一句明确方向。"}
    if status == "blocked":
        return {"status": "ready_for_human_reading", "message": message or "正文阶段已完成，等待人工判断。"}
    return {"status": "needs_author_direction", "message": message or "主笔模式已停止，需要人工判断下一步。"}


def author_background_timeout_seconds(max_revision_cycles: int) -> int:
    cycles = max(1, min(5, int(max_revision_cycles or 3)))
    return max(1800, cycles * 900)
