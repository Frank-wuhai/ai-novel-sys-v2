from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, Chapter, ChapterVersion, QualityReport
from app.services.quality import chinese_chars


OPENING_STRATEGIES = [
    ("abnormal_detail", "异常细节", "用一个不合理但可感知的细节牵引读者，例如声音、痕迹、规矩被打破或物件异样。"),
    ("desire", "人物欲望", "先让主角或关键配角想要某个具体结果，再让阻碍自然出现。"),
    ("relationship_tension", "关系张力", "从两个人的误会、试探、隐瞒、交易或旧账切入。"),
    ("exchange", "利益交换", "从资源、情报、人情、门派规矩或代价谈判切入。"),
    ("consequence", "行动后果", "承接上一章动作造成的伤势、误会、追查、收益或欠账。"),
    ("misdirection", "悬念误导", "先给出看似普通的判断，再用证据逐步推翻。"),
]


@dataclass(frozen=True)
class WritingIntelligenceContext:
    opening_strategy: dict[str, str]
    recent_openings: list[dict[str, str]]
    scene_plan: list[str]
    reaction_chain: list[str]
    good_examples: list[dict[str, str]]
    low_cost_variants: list[str]
    prompt_block: str

    def to_dict(self) -> dict:
        return {
            "opening_strategy": self.opening_strategy,
            "recent_openings": self.recent_openings,
            "scene_plan": self.scene_plan,
            "reaction_chain": self.reaction_chain,
            "good_examples": self.good_examples,
            "low_cost_variants": self.low_cost_variants,
        }


def build_writing_intelligence_context(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str = "",
    mode: str = "draft",
) -> WritingIntelligenceContext:
    book = session.get(Book, book_id)
    recent_openings = _recent_opening_memory(session, book_id=book_id, before_chapter=chapter_number, limit=5)
    strategy = _choose_opening_strategy(
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        previous_chapter_context=previous_chapter_context,
        recent_openings=recent_openings,
    )
    scene_plan = _scene_plan(
        chapter_number=chapter_number,
        opening_label=strategy["label"],
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        previous_chapter_context=previous_chapter_context,
        mode=mode,
    )
    reaction_chain = _reaction_chain(goal=goal, required_beats=required_beats, previous_chapter_context=previous_chapter_context)
    good_examples = _good_chapter_examples(session, book_id=book_id, limit=3)
    variants = _low_cost_variants(strategy_label=strategy["label"], recent_openings=recent_openings, goal=goal)
    prompt_block = _prompt_block(
        book_title=book.title if book else "",
        strategy=strategy,
        recent_openings=recent_openings,
        scene_plan=scene_plan,
        reaction_chain=reaction_chain,
        good_examples=good_examples,
        variants=variants,
    )
    return WritingIntelligenceContext(
        opening_strategy=strategy,
        recent_openings=recent_openings,
        scene_plan=scene_plan,
        reaction_chain=reaction_chain,
        good_examples=good_examples,
        low_cost_variants=variants,
        prompt_block=prompt_block,
    )


def _choose_opening_strategy(
    *,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str,
    recent_openings: list[dict[str, str]],
) -> dict[str, str]:
    text = "；".join([goal, required_beats, constraints, previous_chapter_context])
    used = {item.get("strategy") for item in recent_openings[-4:] if item.get("strategy")}
    candidates = list(OPENING_STRATEGIES)
    if chapter_number > 1 and "consequence" not in used:
        candidates.insert(0, next(item for item in OPENING_STRATEGIES if item[0] == "consequence"))
    if any(marker in text for marker in ("交易", "交换", "人情", "欠", "账", "资源", "情报")):
        candidates.insert(0, next(item for item in OPENING_STRATEGIES if item[0] == "exchange"))
    elif any(marker in text for marker in ("误会", "关系", "门派", "师", "旧", "隐瞒")):
        candidates.insert(0, next(item for item in OPENING_STRATEGIES if item[0] == "relationship_tension"))
    elif any(marker in text for marker in ("异常", "异样", "秘密", "发现", "线索", "规矩")):
        candidates.insert(0, next(item for item in OPENING_STRATEGIES if item[0] == "abnormal_detail"))

    deduped = []
    seen = set()
    for key, label, instruction in candidates:
        if key in seen:
            continue
        seen.add(key)
        deduped.append((key, label, instruction))
    for key, label, instruction in deduped:
        if key not in used:
            return {"key": key, "label": label, "instruction": instruction}
    key, label, instruction = deduped[chapter_number % len(deduped)]
    return {"key": key, "label": label, "instruction": instruction}


def _recent_opening_memory(session: Session, *, book_id: int, before_chapter: int, limit: int) -> list[dict[str, str]]:
    rows = (
        session.execute(
            select(Chapter, ChapterVersion)
            .join(ChapterVersion, ChapterVersion.chapter_id == Chapter.id)
            .where(Chapter.book_id == book_id, Chapter.chapter_number < before_chapter)
            .order_by(Chapter.chapter_number.desc(), ChapterVersion.id.desc())
        )
        .unique()
        .all()
    )
    result: list[dict[str, str]] = []
    seen_chapters = set()
    for chapter, version in rows:
        if chapter.chapter_number in seen_chapters:
            continue
        seen_chapters.add(chapter.chapter_number)
        opening = _opening_excerpt(version.content)
        result.append(
            {
                "chapter": str(chapter.chapter_number),
                "strategy": _classify_opening(opening),
                "signature": _opening_signature(opening),
                "excerpt": _compact(opening, 90),
            }
        )
        if len(result) >= limit:
            break
    return list(reversed(result))


def _good_chapter_examples(session: Session, *, book_id: int, limit: int) -> list[dict[str, str]]:
    rows = (
        session.execute(
            select(QualityReport, ChapterVersion, Chapter)
            .join(ChapterVersion, ChapterVersion.id == QualityReport.chapter_version_id)
            .join(Chapter, Chapter.id == ChapterVersion.chapter_id)
            .where(Chapter.book_id == book_id, QualityReport.passed.is_(True))
            .order_by(QualityReport.score.desc(), QualityReport.id.desc())
            .limit(limit * 3)
        )
        .unique()
        .all()
    )
    examples: list[dict[str, str]] = []
    seen = set()
    for quality, version, chapter in rows:
        if chapter.chapter_number in seen:
            continue
        seen.add(chapter.chapter_number)
        examples.append(
            {
                "chapter": str(chapter.chapter_number),
                "score": str(quality.score),
                "opening_pattern": _opening_signature(_opening_excerpt(version.content)),
                "ending_pattern": _ending_signature(version.content),
                "style_takeaway": _style_takeaway(version.content),
            }
        )
        if len(examples) >= limit:
            break
    return examples


def _scene_plan(
    *,
    chapter_number: int,
    opening_label: str,
    goal: str,
    required_beats: str,
    constraints: str,
    previous_chapter_context: str,
    mode: str,
) -> list[str]:
    beats = _split_points(required_beats)[:5]
    while len(beats) < 5:
        beats.append(
            [
                "承接前章后果并建立现场目标",
                "主角主动试探规则或人物",
                "阻碍升级并迫使主角修正判断",
                "获得阶段性主动权，同时付出可见代价",
                "由本章行动引出章末新问题",
            ][len(beats)]
        )
    opening = f"开篇策略={opening_label}：前400-600字只做具体处境和人物反应，不讲设定史。"
    if chapter_number > 1:
        opening += " 必须接住上一章结尾的后果、情绪或未解决问题。"
    return [
        opening,
        f"单元1：{beats[0]}；人物先有普通解释，再被细节推翻。",
        f"单元2：{beats[1]}；通过动作或对话释放一个信息增量。",
        f"单元3：{beats[2]}；阻碍来自具体人物、利益、伤势、规矩或误判。",
        f"单元4：{beats[3]}；爽点要由判断或行动换来，代价必须落到身体、关系、资源、处境或信息上。",
        f"单元5：{beats[4]}；章末钩子必须由本章选择导致，不能凭空丢危险。",
    ]


def _reaction_chain(*, goal: str, required_beats: str, previous_chapter_context: str) -> list[str]:
    return [
        "感知：人物先看到、听到或触到一个具体异常，不直接下全知结论。",
        "普通解释：主角或配角先用经验给出合理误判。",
        "证据推翻：新证据让误判站不住，压力自然升级。",
        "试探：主角用观察、交涉、表演、修炼、交易或冒险做小步验证。",
        "修正行动：主角根据代价和后果调整策略，拿到阶段性主动权或更具体的问题。",
    ]


def _low_cost_variants(*, strategy_label: str, recent_openings: list[dict[str, str]], goal: str) -> list[str]:
    avoid = "、".join(item["signature"] for item in recent_openings[-3:] if item.get("signature")) or "最近三章的开篇气质"
    return [
        f"方案A：沿用“{strategy_label}”，但避开{avoid}，用更具体的人物动作开场。",
        "方案B：同一目标换成关系/利益切入，让配角带着私心推动第一场。",
        "方案C：同一目标换成误导式悬念，先给普通解释，再用证据推翻。",
    ]


def _prompt_block(
    *,
    book_title: str,
    strategy: dict[str, str],
    recent_openings: list[dict[str, str]],
    scene_plan: list[str],
    reaction_chain: list[str],
    good_examples: list[dict[str, str]],
    variants: list[str],
) -> str:
    lines = [
        "写作智能上下文：",
        f"- 本章开篇策略：{strategy['label']}。{strategy['instruction']}",
        "- 反雷同要求：不得复用最近章节的开场地点、第一动作、第一矛盾和章末钩子形态。",
    ]
    if recent_openings:
        lines.append("- 最近开篇记忆：")
        lines.extend(f"  - 第{item['chapter']}章：{item['strategy']}｜{item['signature']}｜{item['excerpt']}" for item in recent_openings)
    lines.append("- 低成本开篇/章末备选：生成正文前只在内部比较，不输出方案名。")
    lines.extend(f"  - {item}" for item in variants)
    lines.append("- 章节小单元导演表：")
    lines.extend(f"  - {item}" for item in scene_plan)
    lines.append("- 人物反应链：")
    lines.extend(f"  - {item}" for item in reaction_chain)
    if good_examples:
        lines.append("- 本书高分样章抽象经验：只学写法，不照抄情节。")
        lines.extend(
            f"  - 第{item['chapter']}章/{item['score']}分：{item['opening_pattern']}；{item['ending_pattern']}；{item['style_takeaway']}"
            for item in good_examples
        )
    return "\n".join(lines)


def _classify_opening(opening: str) -> str:
    if any(x in opening for x in ("欠", "账", "交易", "价", "银", "换", "人情")):
        return "exchange"
    if any(x in opening for x in ("看着", "眼神", "师", "掌柜", "门派", "误会", "旧")):
        return "relationship_tension"
    if any(x in opening for x in ("异", "怪", "声音", "痕", "血", "门", "灯", "规矩")):
        return "abnormal_detail"
    if any(x in opening for x in ("后果", "伤", "追", "醒", "疼", "昨夜")):
        return "consequence"
    if any(x in opening for x in ("以为", "原来", "不是", "却")):
        return "misdirection"
    return "desire"


def _opening_signature(opening: str) -> str:
    if not opening:
        return "暂无开篇样本"
    first_sentence = re.split(r"[。！？\n]", opening.strip(), maxsplit=1)[0]
    return _compact(first_sentence, 42)


def _ending_signature(content: str) -> str:
    ending = (content or "")[-260:]
    if any(x in ending for x in ("门外", "脚步", "来了", "黑影")):
        return "章末以外部逼近收束"
    if any(x in ending for x in ("发现", "秘密", "原来", "竟")):
        return "章末以信息反转收束"
    if any(x in ending for x in ("机会", "交易", "答应", "欠")):
        return "章末以机会/交换收束"
    return "章末以未解决问题收束"


def _style_takeaway(content: str) -> str:
    quote_count = content.count("“")
    chars = chinese_chars(content)
    if quote_count >= 10:
        return "人物互动密度较高，适合用对话推进信息。"
    if any(x in content for x in ("痛", "血", "冷", "风", "脚步", "眼神")):
        return "现场感较强，适合用感官细节压住设定说明。"
    if chars >= 3000:
        return "章节体量完整，行动链和章末期待较稳定。"
    return "保留具体行动和因果推进，不学具体桥段。"


def _opening_excerpt(content: str) -> str:
    return (content or "").strip()[:360]


def _split_points(value: str) -> list[str]:
    normalized = str(value or "").replace("\n", "；").replace(",", "；").replace("，", "；").replace("、", "；")
    return [_compact(item.strip(" -\t"), 80) for item in normalized.split("；") if item.strip(" -\t")]


def _compact(value: str, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
