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

    # Change A regression: modern/urban payoff sentences must lift
    # payoff_grounding above the hard-blocker threshold (>=50). Prior to
    # this change the anchor set was古风-only (换/抵/三日/账/凭据/…),
    # which meant urban prose scored a flat 40 and永远踩 blocker.
    urban_grounded = (
        "他把笔记本塞进抽屉，做了个交易：她保住那份合同，他会向她交代赵岩的秘密。"
        "如果他今天不承诺这件事，明天林姐就会失去她想抓住的把柄，代价太大他承担不起。"
    )
    urban_report = evaluate_narrative_logic(urban_grounded)
    if urban_report.checks.get("payoff_grounding", 0) < 50:
        failures.append(
            "urban_payoff_still_flat: "
            f"score={urban_report.checks.get('payoff_grounding')}"
        )

    empty_payoff = "他坐在工位上想事情。窗外阳光很好。他打字。"
    empty_report = evaluate_narrative_logic(empty_payoff)
    if empty_report.checks.get("payoff_grounding", 100) >= 50:
        failures.append(
            "no_payoff_scored_as_grounded: "
            f"score={empty_report.checks.get('payoff_grounding')}"
        )

    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "bad_report": report.to_dict(),
                "good_report": good_report.to_dict(),
                "urban_grounded_report": urban_report.to_dict(),
                "empty_payoff_report": empty_report.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
