from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.book_development import _creative_cliche_report


def main() -> int:
    bad = {
        "conflict_engine": "主角使用金手指后被门派追杀，又被现实机构关注。",
        "creative_candidates": [
            {
                "name": "俗套方案",
                "cost_logic": "使用越多越容易被官方盯上。",
                "failure_consequence": "失败后被高层势力围剿。",
            }
        ],
    }
    good = {
        "conflict_engine": "长期压力来自桥段复刻污染后续剧情、好感错账、现实动作失控和奖励同步延迟。",
        "creative_candidates": [
            {
                "name": "桥段错账",
                "cost_logic": "复刻越像，系统越会把参演者的情绪债记到主角身上。",
                "failure_consequence": "主角临场改词救人，导致游戏里师姐把他当成负心旧识，现实中他下意识对同学行了一个江湖赔罪礼。",
            }
        ],
    }
    bad_report = _creative_cliche_report(bad)
    good_report = _creative_cliche_report(good)
    failures = []
    if len(bad_report) < 2:
        failures.append("bad_cliche_not_detected")
    if good_report:
        failures.append("good_mechanism_penalized:" + ";".join(good_report))
    if failures:
        print({"status": "fail", "failures": failures, "bad_report": bad_report, "good_report": good_report})
        return 1
    print("creative-cliche-guard-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
