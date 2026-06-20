from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "app" / "dashboard.html"


def main() -> int:
    html = DASHBOARD.read_text(encoding="utf-8")
    failures: list[str] = []
    required = [
        '<span>综合评估</span>',
        "综合评估通过",
        "综合评估需修订",
        "reviewed_pass: '综合评估通过'",
        "['FAIL', 'NEEDS_REVISION'].includes(value) ? '需修订'",
    ]
    forbidden = [
        '<span>质检结果</span>',
        "quality.passed ? '质检通过' : '质检未通过'",
        "reviewed_pass: '质检通过'",
    ]
    for marker in required:
        if marker not in html:
            failures.append(f"missing_unified_quality_label:{marker}")
    for marker in forbidden:
        if marker in html:
            failures.append(f"conflicting_quality_label_present:{marker}")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("dashboard-quality-verdict-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
