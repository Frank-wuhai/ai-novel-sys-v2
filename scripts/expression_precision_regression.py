from __future__ import annotations

import json

from app.services.expression_precision import evaluate_expression_precision


def main() -> int:
    failures: list[str] = []
    bad = (
        "布上有刺鼻的靛蓝味，陈默刚喝了水，妇人便说：喝了水，就少拿嘴买命。"
        "铁尺馆的人冷笑，死在馆里，馆里给薄棺，别让铁尺馆量不到你的骨头。"
    )
    report = evaluate_expression_precision(bad)
    examples = "\n".join(report.examples)
    for marker in ("靛蓝味", "拿嘴买命", "薄棺", "量不到你的骨头"):
        if marker not in examples:
            failures.append(f"bad_phrase_not_detected:{marker}")
    incomplete_action = "陈默被他扣住的手腕还疼着，本能想挣。"
    action_report = evaluate_expression_precision(incomplete_action)
    action_examples = "\n".join(action_report.examples)
    if "想挣" not in action_examples:
        failures.append("incomplete_action_not_detected")
    if report.checks.get("object_verb_collocation", 100) >= 70:
        failures.append("bad_phrase_penalty_too_weak")
    good = "粗布散着刺鼻的染料味，靛蓝水顺着布纹渗开。妇人低声道：喝了水，就少开口惹祸。"
    good_report = evaluate_expression_precision(good)
    if good_report.checks.get("object_verb_collocation", 0) < 80:
        failures.append("natural_phrase_penalized")
    good_action = "陈默被他扣住的手腕还疼着，本能想挣脱，却被对方压回桌边。"
    good_action_report = evaluate_expression_precision(good_action)
    if good_action_report.checks.get("object_verb_collocation", 0) < 80:
        failures.append("complete_action_penalized")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "bad_report": report.to_dict(),
                "action_report": action_report.to_dict(),
                "good_report": good_report.to_dict(),
                "good_action_report": good_action_report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
