"""chapter_continuity R1 修复回归（2026-09-23 尺子产品化第一刀）。

R1 实案：ch3 开场逐字引用 ch2 尾句「灶上备火把，备热水——快！」并即时应声，
属引用回声式合法承接，却被判 opening_ignores_previous_tail_hook（用户已裁决为误报，
三份同文报告一致复现）。修复：开场逐字引用上一章末尾 200 字内语句（≥8 字）即算承接。

用法: PYTHONPATH=. python scripts/chapter_continuity_echo_regression.py
"""
from __future__ import annotations

import sys

from app.services.chapter_continuity_gate import evaluate_opening_continuity

# ch2 v7 (reviewed_pass) 真实尾部 200 字
CH2_TAIL = (
    "再平常不过的话。\n\n沈渡捏着那半张纸，纸边毛糙，扎着指腹。这不像日结零工的章程，倒像签了什么。"
    "他抬眼，会首一脸理所应当，便把这点异样咽了回去。\n\n入夜躺下，席子底下窸窣一响，是那半张纸。\n\n"
    "山里又是一声闷响，像隔着厚墙砸了一锤，比前两夜都近，震得柴堆上的枯枝簌簌往下掉。\n\n"
    "守夜的梆子紧跟着急成一串。\n\n棚外人影跑动，有人扯着嗓子喊：“沈渡！灶上备火把，备热水——快！”\n\n"
    "这一回，山里的事找上他了。"
)

# ch3 v13 真实开场
CH3_OPENING = (
    "“灶上备火把，备热水——快！”\n\n喊声隔着棚布砸进来，沈渡正揉着面，满手是白。\n\n"
    "他应了一声，手比脑子先动：灶膛添柴，火舌轰地舔起来。大锅舀满，盖上锅盖。\n\n"
    "浸了松脂的火把一根根斜插进灶口，烤得哔剥响。"
)

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    if not cond:
        failures.append(name)


# 1. R1 实案：引用回声式承接不再误报
r = evaluate_opening_continuity(CH2_TAIL, CH3_OPENING)
check("r1_echo_exempted", "opening_ignores_previous_tail_hook" not in r.issues)
check("r1_no_other_issues", r.issues == [])

# 2. 不过度豁免：同一悬念尾 + 无引用无锚词开场 → 仍报
r = evaluate_opening_continuity(CH2_TAIL, "天亮了。沈渡挑着担子出了镇口，一路无话。")
check("no_over_exemption", "opening_ignores_previous_tail_hook" in r.issues)

# 3. 短引用不豁免：与尾部仅共享 <8 字碎片 → 仍报
r = evaluate_opening_continuity(CH2_TAIL, "快！\n\n沈渡放下手里的活。")
check("short_echo_not_exempted", "opening_ignores_previous_tail_hook" in r.issues)

# 4. 无悬念尾 → 不报（基线不变）
r = evaluate_opening_continuity("沈渡吹了灯，一夜无话。", "次日天蒙蒙亮，沈渡起了个大早。")
check("resolved_tail_quiet", "opening_ignores_previous_tail_hook" not in r.issues)

# 5. 旧文重复仍报：引用 200 字以外的旧内容不属于承接豁免范围
old_scene = "祠堂里的长明灯灭了三次，族老们谁也不敢再点。"
prev = old_scene + "。" + ("过渡段落。" * 60) + CH2_TAIL
r = evaluate_opening_continuity(prev, old_scene + "\n\n众人面面相觑。")
check("old_repeat_still_flagged", any(i.startswith("repeated_previous_scene") for i in r.issues))

# 6. 地点跳变检查不受影响
reality_tail = "他躺在出租屋的硬板床上，盯着手机忽然黑屏。"
jianghu_opening = "山道口的客栈里，镖行的人正围着火盆说话。"
r = evaluate_opening_continuity(reality_tail, jianghu_opening)
check("location_jump_still_flagged", "opening_location_jump_after_reality_tail" in r.issues)

if failures:
    print("chapter_continuity_echo_regression=FAIL")
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)
print("chapter_continuity_echo_regression=PASS")
print("cases_evaluated=6")
