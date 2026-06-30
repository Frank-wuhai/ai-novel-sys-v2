from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.db.session import session_scope
from app.services.production_kernel import ProductionKernel, kernel_terminal_status


def default_revision_cycles() -> int:
    from app.core.config import settings

    return 8 if settings.production_profile == "deep" else 6


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
    max_revision_cycles: int | None = None,
    on_progress: Callable[[list[dict]], None] | None = None,
) -> AuthorModeRun:
    if not book_id or not chapter_number:
        raise ValueError("book_id and chapter_number are required")
    with session_scope() as session:
        run = ProductionKernel(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            platform=platform,
        ).run_until_terminal(dry_run=False, max_steps=_max_author_steps(max_revision_cycles), on_progress=on_progress)
    return AuthorModeRun(executed=run.executed, terminal_status=run.terminal_status, terminal_message=run.terminal_message)


def _max_author_steps(max_revision_cycles: int | None) -> int:
    cycles = max(1, min(12, int(max_revision_cycles or default_revision_cycles())))
    return cycles * 4 + 6


def author_terminal_status(executed: list[dict]) -> dict:
    return kernel_terminal_status(executed)


def author_background_timeout_seconds(max_revision_cycles: int) -> int:
    cycles = max(1, min(12, int(max_revision_cycles or default_revision_cycles())))
    return max(3600, (max(8, cycles + 4)) * 900)
