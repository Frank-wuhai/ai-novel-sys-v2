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
    def is_preview(self) -> bool:
        return self == ExecutionMode.PREVIEW

    @property
    def uses_dry_llm(self) -> bool:
        return self in {ExecutionMode.PREVIEW, ExecutionMode.SANDBOX}

    @property
    def queues_heavy_generation(self) -> bool:
        return self == ExecutionMode.EXECUTE


def execution_mode_from_flags(
    *,
    dry_run: bool = True,
    preview_only: bool = False,
    sandbox: bool = False,
    mode: str | ExecutionMode | None = None,
) -> ExecutionMode:
    if isinstance(mode, ExecutionMode):
        return mode
    if mode:
        normalized = str(mode).strip().lower()
        try:
            return ExecutionMode(normalized)
        except ValueError as exc:
            allowed = ", ".join(item.value for item in ExecutionMode)
            raise ValueError(f"unsupported execution mode {mode!r}; expected one of: {allowed}") from exc
    if preview_only:
        return ExecutionMode.PREVIEW
    if sandbox:
        return ExecutionMode.SANDBOX
    return ExecutionMode.PREVIEW if dry_run else ExecutionMode.EXECUTE
