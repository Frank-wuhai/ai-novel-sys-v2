from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "app" / "dashboard.html"


def main() -> int:
    html = DASHBOARD.read_text(encoding="utf-8")
    failures: list[str] = []
    if "function currentChapterQueueTasks" not in html:
        failures.append("missing_current_chapter_queue_filter")
    if "if (!currentChapterQueueTasks('pending').length) return;" not in html:
        failures.append("auto_start_queue_not_limited_to_current_chapter")
    forbidden = [
        "$('chapter').value = autoChapter.number;",
        "chapter = autoChapter;",
        "已切到第 ${autoChapter.number} 章继续写作",
    ]
    for marker in forbidden:
        if marker in html:
            failures.append(f"implicit_chapter_switch_still_present:{marker}")
    run_next_block = _function_block(html, "$('runNext').addEventListener")
    if "const item = selectedChapter(currentSnapshot);" not in run_next_block:
        failures.append("run_next_does_not_use_selected_chapter")
    if "AUTO_PRODUCTION_ACTIONS.includes(item.next_action)" not in run_next_block:
        failures.append("run_next_missing_current_chapter_safety_check")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("dashboard-current-chapter-guard-regression: PASS")
    return 0


def _function_block(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    return text[start : start + 900]


if __name__ == "__main__":
    raise SystemExit(main())
