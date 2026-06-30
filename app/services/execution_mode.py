from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    PREVIEW = "preview"
    SANDBOX = "sandbox"
    EXECUTE = "execute"

    @property
    def writes_live_db(self) -> bool:
        return self == ExecutionMode.EXECUTE

    @property
    def preview_only(self) -> bool:
        return self == ExecutionMode.PREVIEW


def execution_mode_from_flags(*, dry_run: bool = True, preview_only: bool = False, sandbox: bool = False) -> ExecutionMode:
    if preview_only:
        return ExecutionMode.PREVIEW
    if sandbox:
        return ExecutionMode.SANDBOX
    return ExecutionMode.PREVIEW if dry_run else ExecutionMode.EXECUTE
