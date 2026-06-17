from __future__ import annotations

import json
import re

from app.core.config import settings
from app.llm.providers import ArkOpenAIProvider
from app.services.aesthetic_profile import build_aesthetic_profile_block
from app.services.story_dna import build_story_dna_from_development


FIELDS = [
    "title",
    "genre",
    "reader_promise",
    "premise",
    "prose_style",
    "atmosphere",
    "story_route",
    "style_must_not",
    "world_engine",
    "protagonist_engine",
    "conflict_engine",
    "volume_summary",
    "arc_goal",
    "arc_climax",
    "arc_turn",
    "chosen_creative_engine",
]


def develop_new_book_from_inspiration(
    *,
    idea_prompt: str,
    title: str = "",
    genre: str = "玄幻脑洞",
    platform: str = "番茄小说",
    feedback: str = "",
) -> dict:
    idea = idea_prompt.strip()
    if not idea and not title.strip():
        raise ValueError("请先写一段灵感，或者至少填一个书名")
    prompt = f"""
你是资深网文主编，任务是把作者一句灵感，按“作者开书的脑内顺序”逐步展开成逻辑丝滑、环环相扣、可直接生产的作品设定。

作者灵感：
{idea or "未填写"}

暂定书名：{title or "未定"}
类型：{genre or "玄幻脑洞"}
平台：{platform or "番茄小说"}
补充要求：{feedback or "无"}

请只输出 JSON 对象，不要解释。字段必须包含：
title, genre, reader_promise, premise,
prose_style, atmosphere, story_route, style_must_not,
protagonist_engine, world_engine, conflict_engine,
volume_summary, arc_goal, arc_climax, arc_turn,
creative_candidates, chosen_creative_engine, development_steps

creative_candidates 必须给 3 套彼此明显不同的创意发动机，每套包含：
name, goldfinger_form, mechanism_principle, activation_method, growth_method,
reality_sync_rule, cost_logic, cost_escalation, failure_trigger,
failure_consequence, recovery_path, cost, failure_case, signature_scene

chosen_creative_engine 必须从 3 套中选择 1 套，并说明为什么它最适合本书；随后所有生产字段都必须围绕这套方案重写。

development_steps 必须是 5 个对象，顺序固定：
1 开书核心：说明这本书卖什么、给读者什么期待、主味是什么。
2 主角发动机：说明主角身份、欲望、优势、缺陷、代价如何互相咬合。
3 世界与收益机制：说明世界规则、机缘/资源/成长回报如何产生，为什么不能白拿。
4 故事推进：说明长期压力、章节发动机、第一卷前5章如何滚动。
5 写作边界：说明文风、禁忌、不能滑向的题材惯性。

要求：
- 所有字段必须彼此因果相连，不能像独立填空。
- premise 一句话说清主角、核心钩子、主要冲突。
- reader_promise 必须是读者持续追读的明确期待。
- 金手指不能写成“系统/面板/天赋/同步变强”这种泛词，必须有独特形式、触发方式、成长方式、误判风险和可视化表现。
- 现实同步不能只是“实力同步到现实”，必须写清同步对象、阈值、延迟、副作用、被科技世界观测到的方式。
- 每套金手指必须先写 mechanism_principle：它为什么能工作；代价必须从这个原理自然推出，不能外贴惩罚。
- cost_logic 必须回答“为什么使用越多越会付出这个代价”；cost_escalation 必须写三段升级：轻微异常 -> 可见麻烦 -> 结构性危机。
- failure_trigger 必须是主角一个可理解但会出错的选择，不许写“随机反噬/被人发现/系统惩罚”。
- failure_consequence 必须具体到一个可写章节场景，且同时影响游戏与现实；recovery_path 必须给出主角能主动补救但会牺牲另一项利益的办法。
- 代价不能只写“有代价/有风险/被盯上”，必须是具体可写场景：身体感知错位、账号行为留痕、朋友误会、设备异常、课程/工作断裂、游戏内关系变形、现实动作失控、奖励污染、桥段错位中的至少两项。
- 对“全真虚拟现实武侠网游 + 现实同步”题材，优先考虑有逻辑咬合的代价来源：神经反馈、登录舱传感数据、现实肌肉记忆外溢、身体下意识摆出招式、服务器行为审计、玩家录屏剪辑、朋友/家人误解、游戏内好感错账、桥段复刻污染后续剧情、奖励同步延迟、现代医疗检测异常。
- 不要把“被门派追杀、被官方盯上、被现实机构关注、被大势力围剿、资本采样实验”写成主要代价、失败场景或第一卷核心冲突；这些最多只能是很后期的背景余波。前三卷的主要戏剧性必须来自机制误用、人物关系错位、收益污染、现实生活失衡和主角主动补救。
- 每套创意发动机必须提供一个“反俗套失败场景”：失败不是有人来抓他，而是主角因为错误复刻、过度贴合经典桥段、临场改词、替别人承担因果、把游戏动作带回现实，造成一件尴尬、可笑、疼痛或关系变形的具体事件。
- protagonist_engine 写“身份/欲望/优势/缺陷/代价”，并嵌入 chosen_creative_engine 的触发方式。
- world_engine 写“游戏规则/现实同步/收益/限制/失败条件”，并嵌入 chosen_creative_engine 的同步规则。
- conflict_engine 写长期压力和升级方式，不能只列敌人。
- style_must_not 写作者和题材最容易写偏的惯性。
- 第一卷信息要能支撑前 5 章不同场景，不要锁死在一个桥段。
- 禁止机械模板词：更高层势力、实力水涨船高、获得金手指、代价和限制、逐渐发现、巨大火花。若必须表达同义内容，必须换成具体机制和场景。
- 禁止廉价失败场景：昏迷、吐血、被追杀、账号封禁、官方盯上、机构关注、势力追捕、门派追杀、资本实验。即使作为背景，也不能替代本书的核心机制代价。
- 每个普通字段 80-160 个汉字；development_steps 每项 80-140 个汉字。
- 字段值里不要重复输出字段名、英文 key、JSON 符号、编号标签或 markdown 标记；只写给作者看的中文内容。
""".strip()
    provider = ArkOpenAIProvider()
    response = provider.generate(
        prompt,
        max_tokens=5600,
        temperature=max(0.82, settings.llm_planning_temperature),
        model=settings.llm_planning_model,
        response_format={"type": "json_object"},
    )
    data = _load_or_repair_json_object(provider, response.text)
    if not isinstance(data, dict):
        raise ValueError("AI 开书展开没有返回 JSON 对象")
    cliche_report = _creative_cliche_report(data)
    if cliche_report:
        rewrite_prompt = f"""
下面是一版“开书骨架重构”JSON，但它把代价/失败/冲突写得太俗，滑向了“被追杀、被官方/机构/势力盯上、门派围剿”。
请保留原书题材和核心灵感，只重写创意发动机、代价、失败场景、第一卷推进和相关字段，让代价从金手指机制本身自然长出来。

必须避免的问题：
{chr(10).join(f"- {item}" for item in cliche_report)}

重写要求：
- 不要把被追杀、被门派追杀、官方盯上、机构关注、势力围剿、资本实验写成主要戏剧压力。
- 失败场景必须来自主角可理解但错误的选择：错误复刻、过度贴合桥段、临场改词、替别人承因果、把游戏动作带回现实、奖励同步延迟、好感错账、现实生活失衡。
- 每个 creative_candidate 的 failure_consequence 必须是一场能直接写成章节的具体事件，优先写尴尬、可笑、疼痛、关系错位、收益污染或现实动作失控。
- conflict_engine 要写长期压力的“机制升级链”，不要列敌人。
- 只输出合法 JSON 对象，字段结构保持不变。

原 JSON：
{json.dumps(data, ensure_ascii=False, indent=2)[:18000]}
""".strip()
        rewritten = provider.generate(
            rewrite_prompt,
            max_tokens=5600,
            temperature=max(0.86, settings.llm_planning_temperature),
            model=settings.llm_planning_model,
            response_format={"type": "json_object"},
        )
        data = _load_or_repair_json_object(provider, rewritten.text)
    payload = _clean(data, fallback_title=title, fallback_genre=genre)
    payload["aesthetic_profile"] = build_aesthetic_profile_block(
        prose_style=payload["prose_style"],
        atmosphere=payload["atmosphere"],
        story_route=payload["story_route"],
        must_not=payload["style_must_not"],
    )
    payload["story_dna"] = build_story_dna_from_development(payload)
    payload["llm"] = {
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        "elapsed_ms": response.elapsed_ms,
    }
    return payload


def _creative_cliche_report(data: dict) -> list[str]:
    if not isinstance(data, dict):
        return []
    cliches = (
        "门派追杀",
        "被门派追杀",
        "官方盯上",
        "被官方盯上",
        "机构关注",
        "现实机构",
        "被机构",
        "势力关注",
        "势力追杀",
        "势力围剿",
        "高层势力",
        "更高层势力",
        "资本实验",
        "采样实验",
        "被追杀",
        "账号封禁",
    )
    watched_fields = [
        "conflict_engine",
        "volume_summary",
        "arc_goal",
        "arc_climax",
        "arc_turn",
        "chosen_creative_engine",
    ]
    hits: list[str] = []
    for field in watched_fields:
        text = str(data.get(field) or "")
        found = [item for item in cliches if item in text]
        if found:
            hits.append(f"{field} 使用廉价外部压力：{','.join(found[:4])}")
    candidates = data.get("creative_candidates") if isinstance(data.get("creative_candidates"), list) else []
    for index, item in enumerate(candidates[:3], start=1):
        if not isinstance(item, dict):
            continue
        for field in ("cost_logic", "cost_escalation", "failure_trigger", "failure_consequence", "failure_case", "signature_scene"):
            text = str(item.get(field) or "")
            found = [marker for marker in cliches if marker in text]
            if found:
                hits.append(f"方案{index}.{field} 使用俗套代价：{','.join(found[:4])}")
    return hits[:10]


def _load_or_repair_json_object(provider: ArkOpenAIProvider, text: str) -> dict:
    try:
        data = json.loads(_json_object_text(text))
    except json.JSONDecodeError as first_exc:
        repair_prompt = f"""
下面是一段“开书骨架重构”的模型输出，但它不是合法 JSON。
请只修复 JSON 语法，不要改写内容，不要增删字段含义，不要解释。

必须返回一个 JSON 对象，字段包含：
title, genre, reader_promise, premise, prose_style, atmosphere, story_route, style_must_not,
protagonist_engine, world_engine, conflict_engine, volume_summary, arc_goal, arc_climax, arc_turn,
creative_candidates, chosen_creative_engine, development_steps

creative_candidates 是数组；development_steps 是数组。

JSON 解析错误：
{type(first_exc).__name__}: {first_exc}

待修复文本：
{text[:18000]}
""".strip()
        repaired = provider.generate(
            repair_prompt,
            max_tokens=5600,
            temperature=0,
            model=settings.llm_planning_model,
            response_format={"type": "json_object"},
        )
        data = json.loads(_json_object_text(repaired.text))
    if not isinstance(data, dict):
        raise ValueError("AI 开书展开没有返回 JSON 对象")
    return data


def _clean(data: dict, *, fallback_title: str, fallback_genre: str) -> dict:
    cleaned = {field: clean_generated_text(data.get(field) or "") for field in FIELDS}
    cleaned["title"] = cleaned["title"] or clean_generated_text(fallback_title) or "新书"
    cleaned["genre"] = cleaned["genre"] or clean_generated_text(fallback_genre) or "玄幻脑洞"
    if not cleaned["premise"]:
        raise ValueError("AI 开书展开缺少一句话核心设定")
    candidates = data.get("creative_candidates") if isinstance(data.get("creative_candidates"), list) else []
    cleaned["creative_candidates"] = [_clean_candidate(item, index) for index, item in enumerate(candidates[:3], start=1)]
    steps = data.get("development_steps") if isinstance(data.get("development_steps"), list) else []
    cleaned["development_steps"] = [_clean_step(item, index) for index, item in enumerate(steps[:5], start=1)]
    return cleaned


def _clean_candidate(item, index: int) -> dict:
    if not isinstance(item, dict):
        return {"index": index, "name": clean_generated_text(item)}
    fields = [
        "name",
        "goldfinger_form",
        "mechanism_principle",
        "activation_method",
        "growth_method",
        "reality_sync_rule",
        "cost_logic",
        "cost_escalation",
        "failure_trigger",
        "failure_consequence",
        "recovery_path",
        "cost",
        "failure_case",
        "signature_scene",
    ]
    cleaned = {field: clean_generated_text(item.get(field) or "") for field in fields}
    cleaned["index"] = index
    return cleaned


def _clean_step(item, index: int) -> dict:
    if not isinstance(item, dict):
        return {"step": index, "title": "", "content": clean_generated_text(item)}
    raw_step = item.get("step")
    try:
        step_number = int(raw_step or index)
    except (TypeError, ValueError):
        step_number = index
    title = clean_generated_text(item.get("title") or "")
    if not title and raw_step and not str(raw_step).isdigit():
        title = clean_generated_text(raw_step)
    return {
        "step": step_number,
        "title": title,
        "content": clean_generated_text(item.get("content") or item.get("summary") or ""),
    }


def clean_generated_text(value) -> str:
    if isinstance(value, dict):
        return "\n".join(
            item
            for item in (clean_generated_text(item) for item in value.values())
            if item
        )
    if isinstance(value, list):
        return "\n".join(
            item
            for item in (clean_generated_text(item) for item in value)
            if item
        )
    text = str(value or "")
    text = text.replace("\ufeff", "").replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
    text = text.replace("\\n", "\n").replace("\\t", "\t")
    text = text.strip().strip("`").strip()
    if text.startswith("json"):
        text = text[4:].strip()
    text = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", text.strip())
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*[-*•·]+\s*", "", line)
        line = re.sub(r"^\s*\d+[.)、]\s*", "", line)
        line = _strip_generated_field_prefix(line)
        line = line.strip().strip(",，;；")
        line = re.sub(r"^[\"'“”‘’]+|[\"'“”‘’]+$", "", line.strip())
        line = line.strip().strip("{}[]").strip().strip(",，;；")
        if line and line not in {"}", "{", "]", "["}:
            lines.append(line)
    text = "\n".join(line for line in lines if line)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_generated_field_prefix(line: str) -> str:
    field_names = (
        "字段",
        "内容",
        "建议稿",
        "输出",
        "value",
        "text",
        "title",
        "genre",
        "reader_promise",
        "premise",
        "prose_style",
        "atmosphere",
        "story_route",
        "style_must_not",
        "world_engine",
        "protagonist_engine",
        "conflict_engine",
        "volume_summary",
        "arc_goal",
        "arc_climax",
        "arc_turn",
        "chosen_creative_engine",
        "name",
        "goldfinger_form",
        "mechanism_principle",
        "activation_method",
        "growth_method",
        "reality_sync_rule",
        "cost_logic",
        "cost_escalation",
        "failure_trigger",
        "failure_consequence",
        "recovery_path",
        "cost",
        "failure_case",
        "signature_scene",
        "step",
        "summary",
        "content",
    )
    pattern = r"^\s*[\"'“”‘’]?(?:" + "|".join(re.escape(name) for name in field_names) + r")[\"'“”‘’]?\s*[:：]\s*"
    return re.sub(pattern, "", line.strip(), flags=re.I)


def _json_object_text(value: str) -> str:
    text = value.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        return text[start : end + 1]
    return text
