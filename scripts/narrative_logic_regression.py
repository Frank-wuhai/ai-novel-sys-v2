from __future__ import annotations

import json

from app.services.narrative_logic import evaluate_narrative_logic


def main() -> int:
    failures: list[str] = []
    bad = (
        "妇人把水递给陈默，说：喝了水，就少拿嘴买命。"
        "铁尺馆的人说，死在馆里，馆里给薄棺，别让铁尺馆量不到你的骨头。"
        "随后她又给他一截旧物，让他今晚去后门赌命。"
    )
    report = evaluate_narrative_logic(bad)
    if report.checks.get("cost_plausibility", 100) >= 60:
        failures.append("forced_cost_not_detected")
    if not report.examples:
        failures.append("forced_cost_examples_missing")

    good = (
        "妇人把水递给陈默，指了指门外的铁尺馆弟子。她低声说，喝了水就别乱问，"
        "那两个人正在找替账的人。若他开口露了外乡口音，铺子和孩子都会被记上一笔。"
        "陈默按下手印，换来三日缓账，也把铁尺馆的目光引到自己身上。"
    )
    good_report = evaluate_narrative_logic(good)
    if good_report.checks.get("cost_plausibility", 0) < 70:
        failures.append("plausible_cost_penalized")

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "bad_report": report.to_dict(),
                "good_report": good_report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
