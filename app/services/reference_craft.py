from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text as sql_text


ROOT = Path(__file__).resolve().parents[2]
CURATED_PARADIGM_PATH = ROOT / "reference_corpus" / "writing_paradigm_curated.json"
SENTENCE_RELATION_PARADIGM_PATH = ROOT / "reference_corpus" / "sentence_relation_paradigm.json"
SENTENCE_RELATION_PROMPT_PATH = ROOT / "reference_corpus" / "sentence_relation_prompt_block.md"
LITERARY_RELATION_OVERRIDES_PATH = ROOT / "reference_corpus" / "literary_relation_overrides.json"

SENSORY_TERMS = ("光", "风", "声", "味", "冷", "热", "雨", "血", "灰", "汗", "湿", "疼", "腥", "臭", "亮", "暗", "烫")
SPACE_TERMS = ("墙", "门", "窗", "街", "院", "山", "水", "桌", "床", "屋", "巷", "灯", "地", "角", "缝", "槛", "柜", "帘")
BODY_TERMS = (
    "心", "喉咙", "后背", "手心", "指节", "牙", "眼皮", "肩", "掌心", "脊背",
    "后颈", "胃", "牙关", "指甲", "膝", "腿", "脚", "额头", "汗毛", "伤口",
    "小腿", "胸口", "嗓子", "呼吸", "心跳", "冷汗", "发麻", "僵", "一紧",
)
THOUGHT_TERMS = (
    "想", "知道", "明白", "意识到", "怕", "不敢", "忽然觉得", "记起",
    "以为", "觉得", "怀疑", "判断", "误判", "认出", "看出", "猜", "意识",
    "反应过来", "不确定", "才懂", "才明白",
)
ACTION_TERMS = (
    "攥", "盯", "抬", "压", "撞", "拽", "扯", "砸", "滚", "抖", "缩", "咬", "退",
    "扣", "按", "摸", "捡", "塞", "踩", "扶", "挡", "掀", "抓起", "停住", "放下",
    "抠", "攥住", "推", "拎", "贴", "转", "撑", "躲",
)
RHETORIC_RE = re.compile(r"(像|仿佛|好似|如同|似的|一般)")
DIALOGUE_SIGNAL_RE = re.compile(r"(对白|对话|开口|说道|低声道|问道|反问|回答|试探|遮掩|套话|话音)")


@dataclass
class ReferenceCraftReport:
    score: int
    checks: dict[str, int]
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "warnings": self.warnings,
        }


@dataclass(frozen=True)
class ReferenceCraftCard:
    key: str
    title: str
    instruction: str
    anchors: tuple[str, ...] = ()

    def prompt_line(self, *, anchor_limit: int = 1) -> str:
        line = f"{self.title}: {self.instruction}"
        picked = [anchor for anchor in self.anchors if anchor][:anchor_limit]
        if picked:
            line += " 参照拆解: " + " / ".join(_compact(anchor, 76) for anchor in picked)
        return line

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "title": self.title,
            "instruction": self.instruction,
            "anchors": list(self.anchors),
        }


def build_reference_craft_block(
    session,
    *,
    book_id: int | None = None,
    chapter_number: int | None = None,
    limit: int = 6,
    scene_context: str | None = None,
) -> str:
    cards = build_reference_craft_cards(session, book_id=book_id, chapter_number=chapter_number, limit=limit)
    if cards:
        return _format_reference_craft_cards(cards, scene_context=scene_context)
    examples = _load_reference_examples(session, limit=limit)
    if not examples:
        return (
            "【范文技法约束】\n"
            "- 场景描绘：每个关键动作必须嵌入可感知环境，不只交代事件。\n"
            "- 心理链：用身体反应、误判、迟疑、再行动写心理，不直接贴标签。\n"
            "- 修辞用词：比喻来自当场物象；动词必须服务人物压力和局面变化。\n"
            "- 人物声音：对白要带身份、顾虑、试探、遮掩或利益算盘。\n"
            "- 节奏留白：关键转折可短收，但必须先有动作和情绪铺垫。\n"
            "- 因果钩子：章末钩子来自未完成选择、代价、异常证据或新问题。\n"
            "【范文技法约束结束】"
        )
    scene = _pick_examples(examples, _is_scene_para, limit=2)
    psychology = _pick_examples(examples, _is_psychology_para, limit=2)
    rhetoric = _pick_examples(examples, lambda p: bool(RHETORIC_RE.search(p)), limit=1)
    diction = _pick_examples(examples, lambda p: _count_any(p, ACTION_TERMS) >= 2, limit=1)
    parts = [
        "【范文技法约束】",
        "本章必须学习范文的技法结构，而不是照搬句子。",
        _format_group("场景描绘", scene, "关键动作前后补足光线、声音、温度、空间位置，让读者看见人物处境。"),
        _format_group("心理描写", psychology, "用身体反应->判断/误判->选择动作的链条表达心理。"),
        _format_group("修辞", rhetoric, "比喻必须来自当场物象，少用空泛形容词。"),
        _format_group("用词", diction, "优先使用能改变局面的具体动词，避免“感觉、震惊、复杂”等概括词堆叠。"),
        "执行硬约束：正文至少落地场景描绘、心理链、具体动词三类技法；章末不能留下系统截断或说明文字。",
        "【范文技法约束结束】",
    ]
    return "\n".join(part for part in parts if part)


def build_reference_craft_cards(
    session=None,
    *,
    book_id: int | None = None,
    chapter_number: int | None = None,
    limit: int = 6,
) -> list[ReferenceCraftCard]:
    paradigm = _load_curated_paradigm()
    cards = _cards_from_curated_paradigm(paradigm, chapter_number=chapter_number) if paradigm else []
    if cards:
        return cards[:limit]
    examples = _load_reference_examples(session, limit=limit) if session else []
    return _cards_from_examples(examples)[:limit]


def format_reference_craft_card_lines(cards: list[ReferenceCraftCard], *, limit: int = 6) -> list[str]:
    return [card.prompt_line(anchor_limit=1) for card in cards[:limit]]


def evaluate_reference_craft(text: str, *, session=None, book_id: int | None = None) -> ReferenceCraftReport:
    paragraphs = [p.strip() for p in re.split(r"\n+", text or "") if p.strip()]
    if not paragraphs:
        return ReferenceCraftReport(score=0, checks={}, issues=["reference_craft_underlearned: empty_text"])
    scene = _ratio_score(paragraphs, _is_scene_para)
    psychology = _ratio_score(paragraphs, _is_psychology_para)
    rhetoric = min(100, max(35, len(RHETORIC_RE.findall(text or "")) * 20))
    diction = min(100, 35 + _count_any(text or "", ACTION_TERMS) * 3)
    action_reaction = _ratio_score(paragraphs, _has_action_reaction)
    score = round(scene * 0.28 + psychology * 0.24 + rhetoric * 0.14 + diction * 0.16 + action_reaction * 0.18)
    checks = {
        "scene_craft": scene,
        "psychological_chain": psychology,
        "rhetoric_specificity": rhetoric,
        "diction_vividness": diction,
        "action_reaction_chain": action_reaction,
    }
    issues: list[str] = []
    warnings: list[str] = []
    if score < 45:
        issues.append(f"reference_craft_underlearned: {score}")
    elif score < 60:
        warnings.append(f"reference_craft_weak: {score}")
    for name, value in checks.items():
        if value < 45:
            warnings.append(f"{name}_weak:{value}")
    return ReferenceCraftReport(score=score, checks=checks, issues=issues, warnings=warnings)


def _format_reference_craft_cards(cards: list[ReferenceCraftCard], *, scene_context: str | None = None) -> str:
    lines = [
        "【范文技法卡｜学习结构，不照搬句子】",
        "执行：本章每个主要小单元至少落地 2 类技法卡；返修时优先补缺失卡，不整章重写。",
    ]
    for card in cards:
        lines.append(f"- {card.prompt_line(anchor_limit=1)}")
    lines.append("【范文技法卡结束】")
    relation_block = _build_sentence_relation_prompt_block(scene_context)
    if relation_block:
        lines.extend(["", relation_block])
    return "\n".join(lines)


def _build_sentence_relation_prompt_block(scene_context: str | None = None) -> str:
    paradigm = _load_json(SENTENCE_RELATION_PARADIGM_PATH)
    if not paradigm:
        return _load_sentence_relation_prompt_block()
    focus = _infer_relation_focus(scene_context or "")
    scene_rules = _select_scene_rules(paradigm, focus["functions"])
    domain_rules = _select_domain_rules(paradigm, focus["domains"])
    relation_templates = _select_relation_templates(paradigm, focus["templates"])
    manual_rows = _manual_relation_rows(_load_json(LITERARY_RELATION_OVERRIDES_PATH), limit=6)
    lines = [
        "【句子关系范式卡｜按当前场景调用】",
        "用途：先判断素材关系是否合法，再追求文采；不要把范文句式机械拼接。",
        f"当前场景判断：{_focus_label(focus)}",
        "",
        "硬规则：",
        "- 先判关系类型：属性归属、来源、路径、混合、伴随、引发、比喻、反应。",
        "- 感官句优先补完整链条：来源 -> 路径动词 -> 身体落点/人物反应。",
        "- 可共存不等于可合成；馊味和潮气可写混着/夹着，不要硬造复合词。",
        "- 比喻两端必须同域且共同属性具体，不能只靠抽象情绪相似。",
        "- 漂亮句先过搭配关：主语能不能这样动，词能不能这样接，读者能不能半秒理解。",
        "",
        "当前优先关系：",
    ]
    if scene_rules:
        for item in scene_rules[:4]:
            lines.append(f"- {item['title']}：{item['rule']}")
    else:
        lines.append("- 通用：先确认句子承担叙事功能，再选择感官、动作、比喻、心理或对白技法。")
    if domain_rules:
        lines.extend(["", "当前感官链："])
        for item in domain_rules[:3]:
            lines.append(f"- {item['title']}：{item['rule']}")
    if relation_templates:
        lines.extend(["", "可用关系模板："])
        for item in relation_templates[:4]:
            lines.append(f"- {item['key']}：{item['rule']}")
    if manual_rows:
        lines.extend(["", "已确认禁配/改写："])
        lines.extend(manual_rows)
    lines.append("【句子关系范式卡结束】")
    return "\n".join(lines[:38])


def _load_sentence_relation_prompt_block() -> str:
    try:
        text = SENTENCE_RELATION_PROMPT_PATH.read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not text:
        return ""
    # Keep this compact when injected into generation prompts.
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[:42])


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _load_curated_paradigm() -> dict:
    return _load_json(CURATED_PARADIGM_PATH)


def _infer_relation_focus(scene_context: str) -> dict[str, list[str]]:
    text = scene_context or ""
    functions: list[str] = []
    domains: list[str] = []
    templates: list[str] = []

    def add(target: list[str], *values: str) -> None:
        for value in values:
            if value and value not in target:
                target.append(value)

    if any(term in text for term in ("气味", "味", "臭", "腥", "酸", "馊", "泡面", "霉", "烟味", "酒味")):
        add(functions, "environment_sensory")
        add(domains, "smell")
        add(templates, "source", "path", "reaction")
    if any(term in text for term in ("声音", "声", "响", "听", "听见", "耳", "喊", "叫", "爆鸣")):
        add(functions, "environment_sensory")
        add(domains, "sound")
        add(templates, "source", "path", "reaction")
    if any(term in text for term in ("光", "灯", "亮", "暗", "影", "看见", "视线")):
        add(functions, "environment_sensory")
        add(domains, "light")
        add(templates, "source", "attribute")
    if any(term in text for term in ("冷", "热", "疼", "痛", "湿", "汗", "皮肤", "手心", "胸口", "喉咙")):
        add(functions, "environment_sensory", "action_reaction")
        add(domains, "touch")
        add(templates, "path", "reaction")
    if any(term in text for term in ("动作", "打", "追", "逃", "躲", "撞", "推", "抓", "试一拳", "阻碍", "危险", "紧张")):
        add(functions, "action_reaction")
        add(templates, "cause_effect", "reaction")
    if any(term in text for term in ("心理", "情绪", "怕", "谨慎", "犹豫", "误判", "判断", "意识到", "不服")):
        add(functions, "inner_voice", "action_reaction")
        add(templates, "cause_effect", "reaction")
    if DIALOGUE_SIGNAL_RE.search(text):
        add(functions, "dialogue_voice")
    if any(term in text for term in ("比喻", "像", "仿佛", "好似", "如同")):
        add(functions, "rhetoric")
        add(templates, "simile")
    if not functions:
        add(functions, "action_reaction", "environment_sensory", "dialogue_voice", "inner_voice")
    if not templates:
        add(templates, "source", "path", "reaction", "cause_effect")
    return {"functions": functions[:5], "domains": domains[:4], "templates": templates[:5]}


def _focus_label(focus: dict[str, list[str]]) -> str:
    function_names = {
        "environment_sensory": "环境/感官",
        "action_reaction": "动作反应",
        "inner_voice": "心理链",
        "dialogue_voice": "对白声线",
        "rhetoric": "修辞",
    }
    domain_names = {"smell": "气味", "sound": "声音", "light": "光影", "touch": "触感"}
    functions = "、".join(function_names.get(item, item) for item in focus.get("functions", [])[:4])
    domains = "、".join(domain_names.get(item, item) for item in focus.get("domains", [])[:3])
    return functions + (f"；感官重点={domains}" if domains else "")


def _select_scene_rules(paradigm: dict, functions: list[str]) -> list[dict[str, str]]:
    rows = paradigm.get("scene_rules") if isinstance(paradigm.get("scene_rules"), list) else []
    by_key = {str(item.get("function") or ""): item for item in rows if isinstance(item, dict)}
    selected = []
    titles = {
        "environment_sensory": "环境/感官",
        "action_reaction": "动作反应",
        "inner_voice": "心理链",
        "dialogue_voice": "对白声线",
        "rhetoric": "修辞",
        "plain": "普通叙述",
    }
    for key in functions:
        item = by_key.get(key)
        if item:
            selected.append({"title": titles.get(key, key), "rule": str(item.get("rule") or "")})
    return selected


def _select_domain_rules(paradigm: dict, domains: list[str]) -> list[dict[str, str]]:
    rows = paradigm.get("domain_rules") if isinstance(paradigm.get("domain_rules"), list) else []
    by_key = {str(item.get("key") or ""): item for item in rows if isinstance(item, dict)}
    selected = []
    for key in domains:
        item = by_key.get(key)
        if item:
            selected.append({"title": str(item.get("domain") or key), "rule": str(item.get("rule") or "")})
    return selected


def _select_relation_templates(paradigm: dict, template_focus: list[str]) -> list[dict[str, str]]:
    relation_templates = paradigm.get("relation_templates") if isinstance(paradigm.get("relation_templates"), dict) else {}
    rows = relation_templates.get("positive_templates") if isinstance(relation_templates.get("positive_templates"), list) else []
    selected = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        key = str(item.get("template_key") or "")
        if key == "plain":
            continue
        if any(part in key for part in template_focus):
            selected.append({"key": key, "rule": str(item.get("rule") or "")})
        if len(selected) >= 4:
            break
    if selected:
        return selected
    return [
        {"key": "source->path->reaction", "rule": "先交代来源，再写进入人物身体或感知的路径，最后接人物轻反应。"},
        {"key": "cause_effect->reaction", "rule": "动作或外物必须触发人物反应，避免无根情绪标签。"},
    ]


def _manual_relation_rows(overrides: dict, *, limit: int) -> list[str]:
    rows: list[str] = []
    banned = overrides.get("banned") if isinstance(overrides.get("banned"), list) else []
    rewrite = overrides.get("rewrite") if isinstance(overrides.get("rewrite"), list) else []
    for item in banned:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if term:
            rows.append(f"- 禁用/慎用“{term}”：{suggestion or '拆成来源、路径、落点或人物反应。'}")
        if len(rows) >= limit:
            return rows
    for item in rewrite:
        if not isinstance(item, dict):
            continue
        bad = str(item.get("bad") or "").strip()
        suggestion = str(item.get("suggestion") or "").strip()
        if bad:
            rows.append(f"- 遇到“{bad}”：{suggestion or '按关系链重写。'}")
        if len(rows) >= limit:
            return rows
    return rows


def _cards_from_curated_paradigm(paradigm: dict, *, chapter_number: int | None) -> list[ReferenceCraftCard]:
    micro = paradigm.get("micro") if isinstance(paradigm.get("micro"), dict) else {}
    plot = paradigm.get("plot_mechanics") if isinstance(paradigm.get("plot_mechanics"), dict) else {}

    setting = _anchors_from_section(micro.get("setting_delivery"), limit=2)
    emotion = [
        *_anchors_from_section(micro.get("emotion_grounding"), limit=1),
        *_anchors_from_section(micro.get("protagonist_inner_voice"), limit=1),
    ]
    voice = _anchors_from_section(micro.get("character_voice"), limit=2)
    rhythm = [
        *_anchors_from_section(micro.get("rhythm_and_restraint"), limit=1),
        *_anchors_from_section(micro.get("opening_hook"), limit=1),
    ]
    causal = _anchors_from_section(plot.get("best_anchors"), limit=2)
    opening_instruction = "第一章先建立主角处境、世界背景入口和异常压力；钩子服务设定承诺，不为了刺激另起无关冲突。"
    if chapter_number and chapter_number > 1:
        opening_instruction = "开局承接前章状态，用新证据或新代价推进，不另起炉灶。"
    return [
        ReferenceCraftCard(
            key="scene_description",
            title="场景描绘",
            instruction="先给空间边界、光源/气味/声音、人物站位和可互动物件，再让动作发生。",
            anchors=tuple(setting),
        ),
        ReferenceCraftCard(
            key="psychological_chain",
            title="心理链",
            instruction="心理不贴标签，按身体反应->误判/判断->迟疑->选择动作写出来。",
            anchors=tuple(emotion),
        ),
        ReferenceCraftCard(
            key="rhetoric_diction",
            title="修辞用词",
            instruction="比喻从当场物象生长；动词要改变局面，少用震惊、复杂、压迫感这类空泛判断。",
            anchors=tuple(setting[:1] + emotion[:1]),
        ),
        ReferenceCraftCard(
            key="character_voice",
            title="人物声音",
            instruction="对白必须带身份、顾虑、试探、遮掩、急躁或利益算盘，删掉后不能不影响人物。",
            anchors=tuple(voice),
        ),
        ReferenceCraftCard(
            key="rhythm_restraint",
            title="节奏留白",
            instruction="关键转折可以短收，但短句前要有可见动作和情绪铺垫；留白不等于跳过因果。",
            anchors=tuple(rhythm),
        ),
        ReferenceCraftCard(
            key="causal_hook",
            title="因果钩子",
            instruction=opening_instruction + " 章末钩子必须来自未完成选择、代价、异常证据或新问题。",
            anchors=tuple(causal),
        ),
    ]


def _cards_from_examples(examples: list[str]) -> list[ReferenceCraftCard]:
    return [
        ReferenceCraftCard(
            key="scene_description",
            title="场景描绘",
            instruction="关键动作前后补足光线、声音、温度、空间位置和可互动物件。",
            anchors=tuple(_pick_examples(examples, _is_scene_para, limit=2)),
        ),
        ReferenceCraftCard(
            key="psychological_chain",
            title="心理链",
            instruction="用身体反应、误判、迟疑、再行动表达心理，不直接贴标签。",
            anchors=tuple(_pick_examples(examples, _is_psychology_para, limit=2)),
        ),
        ReferenceCraftCard(
            key="rhetoric_diction",
            title="修辞用词",
            instruction="每个关键场景至少一次当场物象修辞；动词必须服务人物压力。",
            anchors=tuple(_pick_examples(examples, lambda p: bool(RHETORIC_RE.search(p)), limit=2)),
        ),
        ReferenceCraftCard(
            key="action_reaction",
            title="动作反应链",
            instruction="动作之后要有人物反应、环境后果或关系变化，禁止流水账推进。",
            anchors=tuple(_pick_examples(examples, _has_action_reaction, limit=2)),
        ),
    ]


def _anchors_from_section(value, *, limit: int) -> list[str]:
    if isinstance(value, dict):
        raw = value.get("best_anchors") or value.get("anchors") or []
    else:
        raw = value
    if not isinstance(raw, list):
        return []
    anchors = []
    for item in raw:
        text = _clean_anchor(str(item or ""))
        if text:
            anchors.append(text)
        if len(anchors) >= limit:
            break
    return anchors


def _clean_anchor(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip()
    text = re.sub(r"^【[^】]+】", "", text).strip()
    return text


def _load_reference_examples(session, *, limit: int) -> list[str]:
    if not session:
        return []
    examples: list[str] = []
    for table, column in (("knowledge_anchors", "opening_text"), ("web_corpus", "chapter_content")):
        try:
            exists = session.execute(sql_text("SELECT name FROM sqlite_master WHERE type='table' AND name=:name"), {"name": table}).first()
            if not exists:
                continue
            rows = session.execute(
                sql_text(f"SELECT {column} FROM {table} WHERE LENGTH({column}) > 120 LIMIT :limit"),
                {"limit": max(limit * 2, 8)},
            ).fetchall()
        except Exception:
            continue
        for (value,) in rows:
            for para in re.split(r"\n+", str(value or "")):
                para = para.strip()
                if 40 <= len(para) <= 220:
                    examples.append(para)
                    if len(examples) >= limit * 4:
                        return examples
    return examples


def _pick_examples(examples: list[str], predicate, *, limit: int) -> list[str]:
    picked = [ex for ex in examples if predicate(ex)]
    return picked[:limit]


def _format_group(name: str, examples: list[str], instruction: str) -> str:
    lines = [f"- {name}：{instruction}"]
    for example in examples:
        lines.append(f"  参照：{_compact(example, 90)}")
    return "\n".join(lines)


def _is_scene_para(paragraph: str) -> bool:
    return _count_any(paragraph, SENSORY_TERMS) >= 1 and _count_any(paragraph, SPACE_TERMS) >= 1


def _is_psychology_para(paragraph: str) -> bool:
    return _count_any(paragraph, BODY_TERMS) >= 1 and _count_any(paragraph, THOUGHT_TERMS) >= 1


def _has_action_reaction(paragraph: str) -> bool:
    return _count_any(paragraph, ACTION_TERMS) >= 1 and any(
        term in paragraph
        for term in (
            "却", "才", "便", "于是", "下一刻", "反倒", "只好", "可", "但", "又", "还",
            "先", "再", "这才", "没等", "刚", "立刻", "随即", "只能", "不得不",
            "哐", "响", "停", "抬头", "后退", "松开", "落", "撞上",
        )
    )


def _ratio_score(paragraphs: list[str], predicate) -> int:
    if not paragraphs:
        return 0
    hits = sum(1 for para in paragraphs if predicate(para))
    return min(100, 30 + round(hits / max(1, len(paragraphs)) * 100))


def _count_any(text: str, terms: tuple[str, ...]) -> int:
    return sum((text or "").count(term) for term in terms)


def _compact(text: str, limit: int) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value[:limit]
