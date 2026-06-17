from __future__ import annotations

import json

from app.services.quality import _coverage_score, evaluate_chapter, split_points


def main() -> int:
    text = _chapter_text()
    goal = "第2章：承接第1章结尾压力，写陈默进一步确认《大江湖》不是机械游戏，而是活的江湖。"
    required = "；".join(
        [
            "开场先承接第1章后果、伤势、追兵或梅引压力",
            "沈青梧或托孤者必须有具体恐惧、选择和门派恩怨，不是发任务工具人",
            "陈默要判断这段论坛热梗为何在真实江湖里成立，并为贪这份奇遇付出代价",
            "让奖励、线索或遗物引出新的江湖关系",
            "章末自然推向第3章的追兵、守洞人或主动试探桥段",
            "拟人化小单元修复：当前单元流评分 63，共 13 个单元；修订必须按 300-700 字小单元重建目标、阻碍、动作后果和承接点",
            "第1单元需局部重修：目标不清、动作链弱、阻碍不足、后果没落地；保留本单元有效信息，补清目标、阻碍、动作后果和下一单元承接点。",
            "局部修订闭环：优先修复 imageable_paragraphs=56，不要整章换方向。",
        ]
    )
    constraints = "通用章节生产标准: 正文字数:3000-4500中文字符；主角行动链:目标->阻碍->主动选择->可见代价->结果变化。"
    coverage = _coverage_score(text, [goal, *split_points(required), *split_points(constraints)])
    quality = evaluate_chapter(
        text,
        goal=goal,
        required_beats=required,
        constraints=constraints,
        min_chars=3000,
        max_chars=8000,
    )
    report = json.loads(quality.report)
    failures: list[str] = []
    if coverage < 50:
        failures.append(f"coverage_still_blocked:{coverage}")
    if quality.dimensions.get("brief_coverage", 0) < 50:
        failures.append(f"quality_brief_coverage_low:{quality.dimensions.get('brief_coverage')}")
    if any(str(issue).startswith("brief_coverage_underfulfilled") for issue in quality.issues):
        failures.append("brief_coverage_issue_not_filtered")
    payload = {
        "status": "fail" if failures else "pass",
        "failures": failures,
        "coverage": coverage,
        "brief_coverage": quality.dimensions.get("brief_coverage"),
        "issues": quality.issues,
        "warnings": report.get("warnings", [])[:6],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if failures else 0


def _chapter_text() -> str:
    unit = (
        "陈默把铜片压进腰带，掌心被边缘割出一道热辣的血线。院门被雨水撞开，灰袖追兵举着灯笼进来，"
        "光从破瓦缝里扫过草席，照见老人嘴角还没干的黑血。瘦高个问他是哪一支亲眷，陈默先顺着话认错，"
        "又借肺痨遮住老人腮侧的硬块。他知道这不是游戏任务，梅引两个字压在铜片背面，意味着一笔真实旧债。"
        "瘦高个临走前报出柳条巷，逼他在交出铜片保命和赌一次线索之间做选择。陈默选了后者，代价是脚踝扭伤，"
        "还被回风馆记住脸。章末他听见巷口脚步回转，铜片上的梅引二字在雨水里发冷。"
    )
    return "\n".join([unit] * 14)


if __name__ == "__main__":
    raise SystemExit(main())
