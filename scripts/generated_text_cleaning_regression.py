from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.book_development import clean_generated_text


def main() -> int:
    cases = [
        ('"goldfinger_form": "剧情演绎系统",', "剧情演绎系统"),
        ("cost_logic: 复刻越深，牵连的人情债越重。", "复刻越深，牵连的人情债越重。"),
        ("1. failure_trigger：主角为了救场临时替换桥段。", "主角为了救场临时替换桥段。"),
        ({"mechanism_principle": '"桥段只在人物因果吻合时成立",'}, "桥段只在人物因果吻合时成立"),
        (["{", '"signature_scene": "擂台上以错招复刻经典桥段。",', "}"], "擂台上以错招复刻经典桥段。"),
    ]
    failures = []
    for raw, expected in cases:
        cleaned = clean_generated_text(raw)
        if cleaned != expected:
            failures.append((raw, expected, cleaned))
        if any(marker in cleaned for marker in ("goldfinger_form", "cost_logic", "failure_trigger", "signature_scene", "{", "}", '":')):
            failures.append((raw, "no field debris", cleaned))
    if failures:
        for raw, expected, cleaned in failures:
            print("cleaning failed")
            print(f"raw={raw!r}")
            print(f"expected={expected!r}")
            print(f"cleaned={cleaned!r}")
        return 1
    print("generated-text-cleaning-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
