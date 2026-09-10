from __future__ import annotations

import re

from app.services.humanized_production import HUMANIZED_UNIT_METHOD


STANDARD_MARKER = "通用章节生产标准"

# 字数口径单一真源（2026-07-28 D方案 Phase1）：
#   DISPLAY_MAX  = 2600 汉字 · APP 显示口径 · 超此记 too_long 观感 issue（软·可 soft_pass）
#   REBUILD_MAX  = 2800 汉字 · 硬结构重建门 · 超此才判 over_target_max_chars 强制重建
# 用户口径「字数不管=比旧版±200可接受」：2600-2800 容差带内不强制返工（返修扩写自然落点），
# 真超 2800（失控膨胀）才触发硬重建。二者同源避免多层门字数标准漂移。
DISPLAY_MAX_CHARS = 2600
REBUILD_TOLERANCE = 200
REBUILD_MAX_CHARS = DISPLAY_MAX_CHARS + REBUILD_TOLERANCE  # 2800


def build_chapter_production_standard(*, chapter_number: int, arc_phase: str = "", arc_goal: str = "", chapter_type: str = "") -> str:
    phase = arc_phase or "normal"
    phase_line = f"章节阶段:{phase}"
    if arc_goal:
        phase_line += f"；本章必须服务剧情段目标:{arc_goal}"
    # B 方案（2026-08-10）· 开篇章专属读感约束升级：3 条新增（背景/世界观/钩子前置）
    # 6 条原有 + 3 条新增 = 9 条 · 在 chapter_type=opening 时插入，原通用位置 42-50
    if chapter_type == "opening":
        opening_constraints = (
            "  - **【第1章设定型开篇标准 · opening_world_promise_v1】**:\n"
            "    - **第一章首要任务不是机械冲突，而是世界承诺成立**:读者看完前半章必须知道现实底座、主角处境、进入何种世界、核心设定新鲜点和本书可期待的成长路径。\n"
            "    - **现实底座要清楚但不卖惨**:用现场动作和生活细节交代主角身份、时代背景、设备/入口来历和现实动机；债、病、穷只作动机燃料，不展开苦难说明书。\n"
            "    - **世界观入口必须自然落地**:前 500 字内让读者知道主角接触的是游戏/异世界/系统/门派/超凡入口中的哪一种，以及它和普通现实的边界；不得只写盘问或受伤让读者猜设定。\n"
            "    - **核心卖点必须可感**:第一章至少出现一次能代表本书差异化的异常体验、规则展示、能力苗头或世界奇观；要让读者明白这本书为什么值得追。\n"
            "    - **世界吸引力优先于硬塞危机**:可以用异常细节、选择压力、世界奇观、规则反常、利益诱因或人物关系牵引开篇；不强制第一句冲突、不强制前300字爆发矛盾、不强制前700字盘问。\n"
            "    - **设定释放必须场景化**:介绍世界背景时必须绑定主角看到、听到、触碰、误判、选择或付出的小后果；禁止百科式大段说明，也禁止完全不介绍。\n"
            "    - **第一章结构建议**:现实底座/入口来历 -> 进入或接触核心世界 -> 世界第一印象/规则展示 -> 主角第一次小选择或试探 -> 得到甜头或异常反馈 -> 章末留下具体追读问题。\n"
            "    - **章末钩子必须服务设定承诺**:结尾的新问题应来自核心设定或主角选择的后果，而不是无关惊悚句、廉价追杀或泛泛危险。\n"
        )
    else:
        opening_constraints = (
            "  - **【第1章开篇读感约束】**:\n"
            "    - **不写苦难说明书**:可以写债、病、罚款和穷，但必须落到现场动作、人物选择和可见代价，不要停在卖惨介绍。\n"
            "    - **世界吸引力优先于硬塞危机**:可以用异常细节、选择压力、世界奇观、规则反常、利益诱因或人物关系牵引开篇；不强制第一句冲突、不强制前300字爆发矛盾、不强制前700字盘问。\n"
            "    - **感官服务局面**:开篇至少有2个可感细节，但必须影响人物判断或行动，不能只堆气味、光线和温度。\n"
            "    - **前5段内出第1钩子**:必须有一个悬念、反差、关系压力或异常后果让读者想知道『然后呢』。\n"
            "    - **主角登场要可见**:用动作、衣着、疲态、习惯动作和别人反应带出人物，不用抽象标签介绍。\n"
            "    - **核心卖点必须可感**:第一章至少出现一次能代表本书差异化的异常体验、规则展示、能力苗头或世界奇观；要让读者明白这本书为什么值得追。\n"
            "    - **章末钩子必须具体**:最后300字要有动作、异常后果、人物反应或新问题，且来自本章行动。\n"
            "    - **压力略写、行动详写**:处境压力只写必要事实，把篇幅给主角如何判断、试探、交换、付代价和拿到回报。\n"
        )
    return "\n".join(
        [
            f"{STANDARD_MARKER}:",
            "- 正文字数:1800-2500中文字符；低于1800视为未完成章节，超过2800视为膨胀（一律不得用短场景冒充完整章，也不得用堆砌描写凑字数）。",
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
            "- **分段硬约束（番茄阅读体验）**:每段正文≤80中文字符（约2-3句），超过必须拆段。整章至少30段。对话必须独占一段（一个人一段）。短段不等于电报句；不要连续堆 2-8 字孤句，每3-5段至少有一句18-45字的自然复合句承接动作、反应和后果。段落之间用单换行分隔(\\n)不留空行。",
            f"- 第{chapter_number}章交付物:读者看完应知道主角想要什么、遇到什么阻碍、做了什么选择、付出什么代价、下一章为什么要继续看。",
            opening_constraints,
        ]
    )


def ensure_chapter_production_standard(
    text: str,
    *,
    chapter_number: int,
    arc_phase: str = "",
    arc_goal: str = "",
    chapter_type: str = "",
) -> str:
    if STANDARD_MARKER in (text or ""):
        return _upgrade_standard_text(text, chapter_type=chapter_type)
    standard = build_chapter_production_standard(
        chapter_number=chapter_number,
        arc_phase=arc_phase,
        arc_goal=arc_goal,
        chapter_type=chapter_type,
    )
    return _join_text(text, standard)


def extract_min_chars(*values: str, default: int = 1200) -> int:
    text = "\n".join(value or "" for value in values)
    # 抓最后一个"正文字数:X-Y"·brief 会 append 多次·最新覆盖旧值
    matches = list(re.finditer(r"正文字数[:：]\s*(\d+)\s*[-~－—到至]\s*(\d+)\s*中文字符", text))
    if not matches:
        return default
    return max(default, int(matches[-1].group(1)))


def extract_max_chars(*values: str, default: int = 8000) -> int:
    text = "\n".join(value or "" for value in values)
    matches = list(re.finditer(r"正文字数[:：]\s*(\d+)\s*[-~－—到至]\s*(\d+)\s*中文字符", text))
    if not matches:
        return default
    lower = int(matches[-1].group(1))
    upper = int(matches[-1].group(2))
    if upper < lower:
        return default
    return max(lower, upper)


def _resolve_chapter_type(chapter_number: int) -> str:
    """B 方案（2026-08-10）· 从 chapter_number 推断 chapter_type
    opening  = 第 1 章
    early_serial = 第 2-5 章
    serial_progress = 第 6 章及以后
    与 production_optimization.chapter_type_profile 完全对齐
    """
    if chapter_number == 1:
        return "opening"
    if chapter_number <= 5:
        return "early_serial"
    return "serial_progress"


def _upgrade_standard_text(text: str, *, chapter_type: str = "") -> str:
    # 老 brief (2200-3500) → 中版 (3000-4500) → 新版 (1800-2500 · 番茄阅读体验)
    upgraded = re.sub(
        r"正文字数[:：]\s*2200\s*[-~－—到至]\s*3500\s*中文字符；低于2200",
        "正文字数:1800-2500中文字符；低于1800",
        text,
    )
    upgraded = re.sub(
        r"正文字数[:：]\s*3000\s*[-~－—到至]\s*4500\s*中文字符；低于3000",
        "正文字数:1800-2500中文字符；低于1800",
        upgraded,
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
    upgraded = _upgrade_opening_world_promise(upgraded, chapter_type=chapter_type)
    if "拟人化小单元" in upgraded:
        return _ensure_new_standard_lines(upgraded)
    anchor = "- 主角行动链:目标->阻碍->主动选择->可见代价->结果变化，五项必须在正文中可见。"
    addition = (
        anchor
        + "\n"
        + "\n".join(f"- 拟人化小单元:{item}" for item in HUMANIZED_UNIT_METHOD)
    )
    return _ensure_new_standard_lines(upgraded.replace(anchor, addition))


def _upgrade_opening_world_promise(text: str, *, chapter_type: str = "") -> str:
    if chapter_type != "opening":
        return text
    upgraded = text
    replacements = {
        "必须有现场牵引:前3段内出现外部压力、异常细节、交易催促、人物动作或关系盘问，让读者立刻知道主角被什么推着走。":
            "世界吸引力优先于硬塞危机:可以用异常细节、选择压力、世界奇观、规则反常、利益诱因或人物关系牵引开篇；不强制第一句冲突、不强制前300字爆发矛盾、不强制前700字盘问。",
        "尽早给出矛盾:主角当下目标和现实阻碍要在前300字内形成拉扯。":
            "核心卖点必须可感:第一章至少出现一次能代表本书差异化的异常体验、规则展示、能力苗头或世界奇观；要让读者明白这本书为什么值得追。",
        "背景伏笔:前 700 字盘问段内必须埋一个背景伏笔":
            "世界承诺伏笔:第一章必须埋一个和核心设定相关的认知点或异常反馈",
    }
    for old, new in replacements.items():
        upgraded = upgraded.replace(old, new)
    regex_replacements = (
        (r"^\s*- \*\*必须有现场牵引\*\*:[^\n]*$", "    - **世界吸引力优先于硬塞危机**:可以用异常细节、选择压力、世界奇观、规则反常、利益诱因或人物关系牵引开篇；不强制第一句冲突、不强制前300字爆发矛盾、不强制前700字盘问。"),
        (r"^\s*- \*\*尽早给出矛盾\*\*:[^\n]*$", "    - **核心卖点必须可感**:第一章至少出现一次能代表本书差异化的异常体验、规则展示、能力苗头或世界奇观；要让读者明白这本书为什么值得追。"),
        (r"^\s*- \*\*背景伏笔\*\*:前\s*700\s*字盘问段[^\n]*$", "      - **世界承诺伏笔**:第一章必须埋一个和核心设定相关的认知点或异常反馈，让读者带走“这个世界还有规则没揭开”的追读问题。"),
    )
    for pattern, replacement in regex_replacements:
        upgraded = re.sub(pattern, replacement, upgraded, flags=re.M)
    if "opening_world_promise_v1" not in upgraded:
        upgraded += "\n  - **【第1章设定型开篇标准 · opening_world_promise_v1】**:第一章首要任务是让现实底座、世界入口、核心卖点和章末追读问题成立；冲突/钩子是手段，不是机械格式。"
    return upgraded


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
