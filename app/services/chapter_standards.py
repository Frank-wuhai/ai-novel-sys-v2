from __future__ import annotations

import re

from app.services.humanized_production import HUMANIZED_UNIT_METHOD


STANDARD_MARKER = "通用章节生产标准"


def build_chapter_production_standard(*, chapter_number: int, arc_phase: str = "", arc_goal: str = "") -> str:
    phase = arc_phase or "normal"
    phase_line = f"章节阶段:{phase}"
    if arc_goal:
        phase_line += f"；本章必须服务剧情段目标:{arc_goal}"
    return "\n".join(
        [
            f"{STANDARD_MARKER}:",
            "- 正文字数:3000-4500中文字符；低于3000视为未完成章节，不得用短场景冒充完整章。",
            f"- {phase_line}",
            "- 开篇牵引:第1章开场要快速进入具体处境；第2章及以后必须优先承接上一章结尾的后果、情绪或未解决问题。开篇可以从人物欲望、关系张力、异常细节、利益交换、行动后果或悬念切入，不强制前300字爆发冲突，不要机械硬塞危机/选择/代价。",
            "- 开篇反雷同:连续章节不得复用同一类开场地点、第一动作、第一矛盾和章末钩子；优先从异常细节、人物欲望、关系张力、利益交换、行动后果、悬念误导中轮换切入。",
            "- 主角行动链:目标->阻碍->主动选择->可见代价->结果变化，五项必须在正文中可见。",
            *(f"- 拟人化小单元:{item}" for item in HUMANIZED_UNIT_METHOD),
            "- 人物反应链:感知异常->普通解释->证据推翻->小步试探->修正行动，主要人物不能像工具人直接说结论。",
            "- 场景推进:至少完成2个连续场景或1个完整长场景+1个章末转折；每个场景都要有冲突、动作和信息增量。",
            "- 信息释放:本章只能释放1-3个新设定点，每个设定点必须由事件、对话、异常或后果触发。",
            "- 爽点/期待:主角必须凭判断、胆量、信息差或能力机制获得一次小胜或阶段性主动权，同时付出代价。",
            "- 章末钩子:最后300字必须出现由本章行动引发的新危险、新机会、新问题或关系变化。",
            "- 可读性:少用总结和抽象说明，多用动作、对话、感官、环境变化和人物误判；不要写剧情梗概。",
            f"- 第{chapter_number}章交付物:读者看完应知道主角想要什么、遇到什么阻碍、做了什么选择、付出什么代价、下一章为什么要继续看。",
        ]
    )


def ensure_chapter_production_standard(
    text: str,
    *,
    chapter_number: int,
    arc_phase: str = "",
    arc_goal: str = "",
) -> str:
    if STANDARD_MARKER in (text or ""):
        return _upgrade_standard_text(text)
    standard = build_chapter_production_standard(chapter_number=chapter_number, arc_phase=arc_phase, arc_goal=arc_goal)
    return _join_text(text, standard)


def extract_min_chars(*values: str, default: int = 1200) -> int:
    text = "\n".join(value or "" for value in values)
    match = re.search(r"正文字数[:：]\s*(\d+)\s*[-~－—到至]\s*(\d+)\s*中文字符", text)
    if not match:
        return default
    return max(default, int(match.group(1)))


def extract_max_chars(*values: str, default: int = 8000) -> int:
    text = "\n".join(value or "" for value in values)
    match = re.search(r"正文字数[:：]\s*(\d+)\s*[-~－—到至]\s*(\d+)\s*中文字符", text)
    if not match:
        return default
    lower = int(match.group(1))
    upper = int(match.group(2))
    if upper < lower:
        return default
    return max(lower, upper)


def _upgrade_standard_text(text: str) -> str:
    upgraded = re.sub(
        r"正文字数[:：]\s*2200\s*[-~－—到至]\s*3500\s*中文字符；低于2200",
        "正文字数:3000-4500中文字符；低于3000",
        text,
    )
    upgraded = re.sub(
        r"- 开场300字:必须进入具体场景，给出主角当下欲望/麻烦/外部压力，不得先讲设定百科。",
        "- 开篇牵引:第1章开场要快速进入具体处境；第2章及以后必须优先承接上一章结尾的后果、情绪或未解决问题。开篇可以从人物欲望、关系张力、异常细节、利益交换、行动后果或悬念切入，不强制前300字爆发冲突，不要机械硬塞危机/选择/代价。",
        upgraded,
    )
    upgraded = re.sub(
        r"- 开场承接:第1章开场要快速进入具体场景；第2章及以后必须优先承接上一章结尾的后果、情绪和未解决压力，再自然进入本章新冲突，不要机械硬塞危机/选择/代价。",
        "- 开篇牵引:第1章开场要快速进入具体处境；第2章及以后必须优先承接上一章结尾的后果、情绪或未解决问题。开篇可以从人物欲望、关系张力、异常细节、利益交换、行动后果或悬念切入，不强制前300字爆发冲突，不要机械硬塞危机/选择/代价。",
        upgraded,
    )
    if "拟人化小单元" in upgraded:
        return _ensure_new_standard_lines(upgraded)
    anchor = "- 主角行动链:目标->阻碍->主动选择->可见代价->结果变化，五项必须在正文中可见。"
    addition = (
        anchor
        + "\n"
        + "\n".join(f"- 拟人化小单元:{item}" for item in HUMANIZED_UNIT_METHOD)
    )
    return _ensure_new_standard_lines(upgraded.replace(anchor, addition))


def _ensure_new_standard_lines(text: str) -> str:
    lines = text
    opening_line = "- 开篇牵引:第1章开场要快速进入具体处境；第2章及以后必须优先承接上一章结尾的后果、情绪或未解决问题。开篇可以从人物欲望、关系张力、异常细节、利益交换、行动后果或悬念切入，不强制前300字爆发冲突，不要机械硬塞危机/选择/代价。"
    anti_repeat = "- 开篇反雷同:连续章节不得复用同一类开场地点、第一动作、第一矛盾和章末钩子；优先从异常细节、人物欲望、关系张力、利益交换、行动后果、悬念误导中轮换切入。"
    reaction = "- 人物反应链:感知异常->普通解释->证据推翻->小步试探->修正行动，主要人物不能像工具人直接说结论。"
    if anti_repeat not in lines:
        lines = lines.replace(opening_line, f"{opening_line}\n{anti_repeat}")
    anchor = "- 主角行动链:目标->阻碍->主动选择->可见代价->结果变化，五项必须在正文中可见。"
    if reaction not in lines:
        lines = lines.replace(anchor, f"{anchor}\n{reaction}")
    return lines


def _join_text(left: str, right: str) -> str:
    left = (left or "").strip()
    if not left:
        return right
    separator = "\n" if "\n" in left else "；"
    return f"{left}{separator}{right}"
