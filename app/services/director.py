from __future__ import annotations

from app.services.brief_sanitizer import sanitize_prompt_contract_text


def build_chapter_director_sheet(
    *,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str,
    canon_context: str,
    author_preferences: str = "",
    revision_goal: str = "",
    revision_required_beats: str = "",
    revision_constraints: str = "",
    mode: str = "draft",
) -> str:
    """Build a compact writing plan that keeps generation focused."""
    beats = _compact_points(revision_required_beats or required_beats, limit=8)
    constraints_points = _compact_points(revision_constraints or constraints, limit=6)
    canon_points = _compact_lines(canon_context, limit=5)
    previous_focus = _previous_focus(previous_chapter_context)
    target = revision_goal or goal
    units = _unit_plan(
        beats,
        previous_focus=previous_focus,
        target=target,
        constraints=constraints_points,
        mode=mode,
    )
    emotion_line = _emotion_line(target, previous_focus)
    opening_line = _opening_line(chapter_number=chapter_number, previous_focus=previous_focus, target=target)
    hook_line = _hook_line(beats, constraints_points)
    lines = [
        f"章节导演单：第{chapter_number}章",
        f"生产模式：{'整章重写' if mode == 'fresh' else ('修订' if mode == 'revision' else '草稿')}",
        f"本章核心目标：{_one_line(target)}",
        "",
        "前章承接：",
        previous_focus,
        "",
        "开场要求：",
        f"- {opening_line}",
        "",
        "情绪线：",
        f"- {emotion_line}",
        "",
        "正文小单元推进：",
        *units,
        "",
        "章末钩子：",
        f"- {hook_line}",
        "",
        "必须兑现：",
        *_prefix_points(beats[:6], "- "),
        "",
        "不可破坏：",
        *_prefix_points([*constraints_points[:4], *canon_points[:3]], "- "),
    ]
    if author_preferences and "暂无" not in author_preferences:
        lines.extend(["", "作者口味：", *_prefix_points(_compact_lines(author_preferences, limit=3), "- ")])
    lines.extend(
        [
            "",
            "写作底线：",
            "- 如果本书设定为真实游戏世界，必须把游戏世界写成有血有肉的异世界；人物有欲望、恐惧、利益、门派关系和生活逻辑，不是任务 NPC。",
            "- 成长来自修炼、拜师、交易、冒险、观察规则和承担后果，不要写成打怪升级、刷副本、刷经验或机械任务链。",
            "- 正文优先，不输出导演单标题或小单元编号。",
            "- 所有设定必须通过场景、动作、对话、误判和后果呈现。",
            "- 新出现的地名、组织名、秘术名和关键物件名必须有设计锚点：来源、外观、功能、利益关系或代价至少兑现两项，不要堆随意专名。",
            "- 本章主要场景必须能被读者画出来：空间边界、人物站位、光源、关键物件和动作轨迹要清楚。",
            "- 每个单元都要承接上一个单元的动作后果，不要另起炉灶。",
            "- 每个主要人物出场时，都要带着自己的欲望、顾虑或误判，不要只服务主角。",
            "- 章末必须留下由本章行动引发的新危险、新发现或未解决压力。",
        ]
    )
    return "\n".join(line for line in lines if line is not None)


def _unit_plan(beats: list[str], *, previous_focus: str, target: str, constraints: list[str], mode: str) -> list[str]:
    fallback = [
        "开场承接前章后果，用人物欲望、关系张力、异常细节或行动余波建立牵引。",
        "主角做出第一步主动选择，并暴露短期阻碍。",
        "用动作或对话释放关键设定，不做说明书。",
        "让能力/资源产生收益，同时付出可见代价。",
        "引入反转或更大麻烦，迫使主角调整策略。",
        "用本章行动导向章末钩子。",
    ]
    source = beats or fallback
    units = []
    for index, beat in enumerate(source[:8], start=1):
        prefix = _unit_prefix(index)
        detail = _unit_detail(index=index, beat=beat, target=target, constraints=constraints, mode=mode)
        units.append(f"{index}. {prefix}{_one_line(beat)}；{detail}")
    return units


def _unit_prefix(index: int) -> str:
    prefixes = {
        1: "承接：",
        2: "试探：",
        3: "受阻：",
        4: "转圜：",
        5: "反压：",
        6: "变局：",
        7: "代价：",
        8: "钩子：",
    }
    return prefixes.get(index, "推进：")


def _unit_detail(*, index: int, beat: str, target: str, constraints: list[str], mode: str) -> str:
    if index == 1:
        return "必须接住上一章最后动作的后果，前300字用具体处境牵引读者，不强制爆发冲突，不讲设定史。"
    if index == 2:
        return "让主角用可见行动试探规则或人物，不靠旁白下结论。"
    if index == 3:
        return "阻碍来自具体人物、环境、伤势、利益或误判。"
    if index == 4:
        return "用对话或动作带出信息增量，同时让局面微变。"
    if index == 5:
        return "主角获得阶段性主动权，但必须留下代价或反噬。"
    if index == 6:
        return "把本章选择推向新危险、新机会或新关系变化。"
    if index == 7:
        return "收束本章行动链，不草草总结。"
    return "章末必须让读者知道下一章为什么非看不可。"


def _opening_line(*, chapter_number: int, previous_focus: str, target: str) -> str:
    if chapter_number <= 1:
        return "直接进入具体处境，可用主角欲望、关系张力、异常细节、利益交换或悬念建立阅读钩子。"
    return "先承接上一章结尾的后果、情绪或未解决问题，再用适合本章的切入法推进，不要机械制造同款危机场景。"


def _emotion_line(target: str, previous_focus: str) -> str:
    text = f"{target}\n{previous_focus}"
    if any(marker in text for marker in ("追", "危", "死", "毒", "伤", "堵", "逃")):
        return "紧张中带临场判断：角色害怕、误判、强撑和嘴硬都要可见。"
    if any(marker in text for marker in ("秘密", "发现", "试探", "规则")):
        return "好奇和风险并行：每个发现都要带来新的代价或误判。"
    return "保持读者期待：让人物目标、阻碍和反应连续升级。"


def _hook_line(beats: list[str], constraints: list[str]) -> str:
    text = "；".join([*beats, *constraints])
    if "套路触发" in text or "桥段" in text:
        return "由本章行动自然引出一个可被主角主动设计、但有真实风险的经典桥段诱因。"
    if "追" in text or "危险" in text:
        return "章末出现更具体的追逼、误会、证据或人物选择，不能只写泛泛危险。"
    return "章末留下新问题、新危险、新机会或关系变化，并和本章选择有因果关系。"


def _previous_focus(value: str) -> str:
    text = " ".join(line.strip() for line in str(value or "").splitlines() if line.strip())
    if not text:
        return "本章需要自行建立具体场景，但不能违反已登记 Canon。"
    return _one_line(text[-420:])


def _compact_points(value: str, *, limit: int) -> list[str]:
    normalized = sanitize_prompt_contract_text(value).replace("\n", "；").replace("，", "；").replace(",", "；")
    items = [
        _one_line(item)
        for item in normalized.split("；")
        if _one_line(item) and not _one_line(item).startswith(("修订模式:", "验收方式：", "验收方式:"))
    ]
    return items[:limit]


def _compact_lines(value: str, *, limit: int) -> list[str]:
    items = [_one_line(line) for line in str(value or "").splitlines() if _one_line(line)]
    return items[:limit]


def _prefix_points(items: list[str], prefix: str) -> list[str]:
    return [f"{prefix}{item}" for item in items if item]


def _one_line(value: str, *, max_chars: int = 180) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"
