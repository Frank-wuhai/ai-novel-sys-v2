from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AutomationResult:
    status: str
    report: str


class OpenClawPublishingOperator:
    """Boundary object for platform operations.

    This class intentionally does not make story or quality decisions. It should
    receive an approved publish job and perform browser/platform actions only.
    """

    def publish_dry_run(self, *, platform: str, title: str, content: str) -> AutomationResult:
        if not title or not content:
            return AutomationResult(status="blocked", report="title and content are required")
        return AutomationResult(
            status="dry_run_ready",
            report=f"Would publish to {platform}: title={title!r}, chars={len(content)}",
        )

