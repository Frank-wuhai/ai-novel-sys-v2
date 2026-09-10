"""paradigm_refine.py — 范式驱动精修关卡（生产回路服务）
在 draft_chapter 生成生稿后、review_chapter 质检前，自动跑一道精修。
精修后写新 ChapterVersion（source="paradigm_refine"），后续 review 对精修版质检。

通过 env PARADIGM_REFINE_ENABLED=true/false 控制开关。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.models.entities import Chapter, ChapterVersion
from app.services.production_state import next_version_number

# paradim 路径
PARADIGM_PATH = Path(__file__).resolve().parent.parent.parent / "reference_corpus" / "writing_paradigm_v2.json"


def _hanzi(t: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", t))


def _load_paradigm_brief() -> str:
    """把范式库压缩成可注入 prompt 的检查标准。"""
    try:
        data = json.loads(PARADIGM_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ""
    plot = data.get("plot_mechanics", {})
    micro = data.get("micro", {})
    lines = []
    lines.append("【剧情结构与推力·硬标准】")
    for key, cn in [("causal_rules", "因果咬合"), ("escalation_rules", "冲突升级"),
                    ("hook_rules", "章末钩子"), ("payoff_rules", "伏笔兑现")]:
        rules = plot.get(key, [])
        if rules:
            lines.append(f"◆{cn}:")
            for r in rules[:4]:
                lines.append(f"  - {r}")
    diag = plot.get("流水账诊断清单", [])
    if diag:
        lines.append("◆流水账信号(出现即判定写砸):")
        for d in diag:
            lines.append(f"  - {d}")
    lines.append("")
    lines.append("【微观笔法·AI通病清单(逐段对照)】")
    for key, cn in [("protagonist_inner_voice", "主角内心"), ("character_voice", "配角声线"),
                    ("setting_delivery", "设定植入"), ("emotion_grounding", "情绪落地"),
                    ("rhythm_and_restraint", "节奏留白"), ("opening_hook", "开篇钩子")]:
        traps = micro.get(key, {}).get("ai_traps", [])
        if traps:
            lines.append(f"◆{cn}: " + " / ".join(t[:60] for t in traps[:3]))
    return "\n".join(lines)


DIAGNOSE_PROMPT = """你是番茄网文金牌主编，正在用一套从79章真实爆款提炼的范式，逐段审一章库存稿。

下面是范式库标准（你的审稿标尺）：
{paradigm}

你的任务：
1. 通读全章，先判断剧情层面——事件之间是真因果推进，还是"然后…然后…"的流水账？章末有没有钩子？对照上面的流水账信号。
2. 再逐段扫描——揪出具体的AI味句子（炫技比喻/作者升华腔/排比工整/情绪贴标签/配角工具人/旁白解释设定）。必须引用原句，指明病在哪、犯了范式库里哪条。
3. 只挑真问题。如果某段没问题就不要硬挑。宁可少挑准挑，不要为凑数编病。

严格输出 JSON（不要markdown、不要代码块）：
{{
  "plot_diagnosis": {{
    "is_flowing_account": "是/否——这章整体是不是流水账",
    "causal_gaps": ["具体指出哪两个事件之间缺因果咬合，引原文；没有就空数组"],
    "hook_check": "章末钩子是什么/有没有，引结尾原句判断",
    "escalation_check": "本章冲突比前文升级了吗，靠什么"
  }},
  "line_issues": [
    {{"original": "有问题的原句", "problem": "什么AI味/什么病", "rule_violated": "违反范式库哪条", "suggestion": "改写后的句子"}}
  ],
  "overall": "一句话总评：这稿子的主要短板是什么"
}}

待审章节正文：
{content}
"""

REFINE_PROMPT = """你是番茄网文金牌主编。下面是一章库存稿，以及主编逐段诊断出的问题。请据诊断精修全文。

精修铁律：
1. 只改诊断点名的问题句，其余原样保留——不要重写整章、不要改情节走向。
2. 改写要落地范式库要求：用动作/物件/生理反应承载情绪(不贴标签)；配角台词带身份和潜台词(不当工具人)；设定藏进事件对话(不旁白解释)；删掉炫技比喻和作者升华腔。
3. 若诊断指出剧情因果断裂或章末无钩子，可补1-2句衔接或钩子，但不得注水、不得改变既定情节。
4. 保持番茄网文风格：短段、口语、对话独占段。
5. 【字数硬铁律】精修后汉字数必须 ≤ 原文汉字数，绝不允许超过。只做等量替换（把AI味句换成更好的句），不得整体扩写、不得逐句加细节。若原文已接近2600汉字上限，宁可精简也不可膨胀。补钩子/补衔接时用最省的字，单章总字数不得超过2600汉字。

原章节正文：
{content}

主编诊断：
{diagnosis}

直接输出精修后的完整正文（不要任何说明、不要markdown标记）：
"""


def _llm_json(provider, prompt, max_tokens=6000):
    resp = provider.generate(prompt, max_tokens=max_tokens, temperature=0.3,
                             model=settings.llm_draft_model,
                             response_format={"type": "json_object"})
    text = resp.text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        fixed = text.rsplit("\n", 1)[0].rstrip().rstrip(",")
        fixed += "}" * max(0, fixed.count("{") - fixed.count("}"))
        return json.loads(fixed)


def _llm_text(provider, prompt, max_tokens=6000):
    resp = provider.generate(prompt, max_tokens=max_tokens, temperature=0.5,
                             model=settings.llm_draft_model)
    return resp.text.strip()


def _refine_content(content: str, hard_max: int = 2600) -> tuple[dict, str]:
    """精修单章正文。
    返回 (diagnosis dict, refined text)。
    若精修后超字数，自动重试一次；重试仍超则退回原文，只交付诊断。
    """
    paradigm = _load_paradigm_brief()
    if not paradigm:
        return {"warning": "范式库未找到、跳过精修"}, content
    provider = get_provider(False)
    src_hz = _hanzi(content)
    # 诊断
    diagnosis = _llm_json(provider, DIAGNOSE_PROMPT.format(paradigm=paradigm, content=content))
    n_issues = len(diagnosis.get("line_issues", []))
    # 精修
    refined = _llm_text(provider, REFINE_PROMPT.format(
        content=content, diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=1)))
    if refined.startswith("```"):
        refined = re.sub(r"^```[a-z]*\n?", "", refined)
        refined = re.sub(r"\n?```$", "", refined)
    ceiling = min(hard_max, max(src_hz, 1800))
    out_hz = _hanzi(refined)
    if out_hz > ceiling:
        # 重试压字
        retry_prompt = REFINE_PROMPT.format(
            content=content, diagnosis=json.dumps(diagnosis, ensure_ascii=False, indent=1)
        ) + f"\n\n【上一版超字了：{out_hz}汉字。必须压到 ≤{ceiling} 汉字。删冗余、并短句，绝不新增内容。】"
        refined2 = _llm_text(provider, retry_prompt)
        if refined2.startswith("```"):
            refined2 = re.sub(r"^```[a-z]*\n?", "", refined2)
            refined2 = re.sub(r"\n?```$", "", refined2)
        if _hanzi(refined2) <= ceiling:
            refined, out_hz = refined2, _hanzi(refined2)
        else:
            # 兜底：退回原文，只交付诊断
            refined, out_hz = content, src_hz
    return diagnosis, refined


def should_refine(session: Session, *, book_id: int, chapter_number: int) -> bool:
    """判断是否需要对此章节执行精修。
    条件：最新版是 draft 且来源不是 paradigm_refine（尚未精修过）。
    """
    if not settings.paradigm_refine_enabled:
        return False
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        return False
    version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.id.desc())
    )
    if not version:
        return False
    if version.status != "draft":
        return False
    if version.source == "paradigm_refine":
        return False
    return True


def refine_and_create_version(session: Session, *, book_id: int, chapter_number: int) -> tuple[dict | None, ChapterVersion | None]:
    """对章节最新draft版执行精修，写新版本。
    返回 (diagnosis, new_version)。
    若诊断0处或精修失败，返回 (diagnosis, None) 不创建新版本。
    """
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    old_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(ChapterVersion.id.desc())
    )
    if not old_version:
        raise ValueError("no version found")
    diagnosis, refined = _refine_content(old_version.content)
    n_issues = len(diagnosis.get("line_issues", []))
    if n_issues == 0:
        # 无问题不精修
        return diagnosis, None
    if refined == old_version.content:
        # 精修没产生变化
        return diagnosis, None
    # 写新版本
    new_version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=old_version.title,
        content=refined,
        status="draft",
        source="paradigm_refine",
    )
    session.add(new_version)
    session.flush()
    return diagnosis, new_version