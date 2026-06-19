from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "app" / "dashboard.html"


def main() -> int:
    html = DASHBOARD.read_text(encoding="utf-8")
    failures: list[str] = []
    required_markers = [
        "function formatDuration",
        "function activeGenerationText",
        "已运行 ${elapsed}",
        "超时阈值 ${timeout}",
        "const activeText = activeGenerationText(health);",
        "const text = activeGenerationText(health) || command.detail || nextStepText(snapshot, health);",
    ]
    for marker in required_markers:
        if marker not in html:
            failures.append(f"missing_marker:{marker}")
    live_block = _function_block(html, "function renderLiveStatus")
    if "activeText || backgroundText" not in live_block:
        failures.append("live_status_does_not_prioritize_active_generation_text")
    cockpit_block = _function_block(html, "function renderMainlineCockpit")
    if "activeGenerationText(health)" not in cockpit_block:
        failures.append("mainline_cockpit_does_not_show_active_generation_text")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("dashboard-generation-status-regression: PASS")
    return 0


def _function_block(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start : start + 1800]


if __name__ == "__main__":
    raise SystemExit(main())
