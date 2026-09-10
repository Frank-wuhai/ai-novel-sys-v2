# -*- coding: utf-8 -*-
"""semantic_gate.py — 生成管道内的语义返修门(app 内可调用)。

把原 reference_corpus/semantic_probe.py 的 S1-S7 语义诊断能力抽到 app 层,
供生成链在 lineage_repair 之后调用做定向返修。

★铁律遵守:本模块只做"缺陷诊断+定向修复指令",不打分、不做入库判定。
  用能判语义的工具(LLM 探针)做门,而不是用判不了语义的关键词硬凑。
  这是对"确定性关键词守卫无法判定语义充分性(来源/活人/落点)"这一架构边界的正解。

★模型:必须用通用中文对话模型(deepseek-v4-pro),不能用代码模型(ark-code-latest)——
  代码模型不遵守 json_object 约束、返回英文散文推理→no_json。
"""
from __future__ import annotations

import json
import os
import re

# S1-S7 语义检查项(与 reference_corpus/semantic_probe.py 对齐,单一事实源在此)
SEMANTIC_CHECK_PROMPT = """你是资深网文责编,只做缺陷诊断,不写作、不夸奖、不打分。
下面是一章网文正文。请按正文自身已经建立的 Story Bible / Canon 逻辑检查，不要把其他书的“现实同步”“游戏收益反馈现实”“清虚观拜师”等旧设定当成默认前提。

请逐项检查以下7类问题,每项只回答:该章是否存在(yes/no)+ 若yes则摘录触发的原文句子(原样复制,不改写)+ 一句话说明。
禁止编造原文里没有的句子。只报确实存在的问题,没有就写no。

S1 剧情突兀/漏洞:功法、道具、秘籍、人物是否"突然出现"而前文无任何交代来源?(例:主角忽然掏出一张功法图谱,但没人给过他;或一个新人物突然出场且立刻展现超常能力,无任何铺垫)
S2 世界活人感:若本章明确是多人游戏场景，检查是否只有主角和功能化 NPC 互动、缺少其他玩家或活人纹理；若正文/设定呈现为真实异世界、单人沉浸、无玩家层或禁止 NPC/玩家术语，则不得要求补玩家/NPC，只检查人物是否像活人、场景是否有真实互动。若本章无对应场景则no。
S3 钩子/悬念调性:章末或章中的悬念钩子是否偏离玄幻武侠调性(硬塞现代惊悚、悬疑推理、系统冷腔)?
S4 人物语言同质:同一章里不同人物说话是否一个腔调、无个性区分?(只有旁白无对话则no)
S5 用词搭配失当:是否有别扭的形容词/比喻搭配(如用"锈透"形容人身体、"生锈"形容本该发黑的东西)?
S6 详略失衡/情绪落点跑偏:本章是否详略不当或情绪落点偏负向?具体判据(命中任一即yes)——
   (a)现实苦难(送外卖/催款/父亲病情)占用过多篇幅、反复渲染惨,喧宾夺主盖过武侠世界内容;
   (b)该详写的武侠内容(武侠世界的辽阔奇观、武学的威力与施展画面、奇遇、打斗招式、变强的爽感)被一笔带过、抽象概括、没写透;
   (c)进入游戏世界时缺少"新世界初体验"的展开(世界多大/多真/多自由/藏着什么机遇),只用一句系统告示草草带过;
   (d)全章情绪落点偏向"惨/恐惧/被吸命/背负担"等负向感受,而非"爽/希望/变强/翻身"等正向反馈。
   这是爽文,读者要代入主角在核心世界里探索、行动、逐步获得主动权。若本章爽感充足、详略得当且不违背本书设定边界则no。
S7 世界观逻辑硬伤/降智:本章现实侧的科幻设定是否存在"读着降智、经不起推敲"的逻辑漏洞?具体判据(命中任一即yes)——
   (a)现实是什么时代不明:游戏头盔/脑机接口是尖端科技还是人人都有的平常物,读者读不出这是近未来还是当下,导致设定悬空;
   (b)游戏头盔/设备凭空出现:那台头盔/脑机设备突然出现在主角手里或床头,却从没交代它是怎么来的(哪来的、谁给的、为何他有);
   (c)沉浸原理不交代:主角戴上头盔就"身临其境",却完全没有解释为什么一个头盔能让人有真实的视觉触觉痛觉(缺脑机接口/神经直连之类的原理点破),让读者觉得"就这么进去了?"很假。
   注意:只查现实侧科技设定的合理性,不查游戏内武侠世界(武侠世界是游戏世界观,不适用现实逻辑)。若本章无现实场景/无进游戏动作则no。

严格输出JSON:
{{"S1":{{"hit":true/false,"quote":"原文句","note":"说明"}},"S2":{{...}},"S3":{{...}},"S4":{{...}},"S5":{{...}},"S6":{{"hit":true/false,"quote":"原文句","note":"说明"}},"S7":{{"hit":true/false,"quote":"原文句","note":"说明"}}}}

正文:
{content}
"""

# 每类缺陷对应的定向修复指令(喂回 repair LLM 用)
_REPAIR_HINTS = {
    "S1": "有实体(功法/道具/人物)凭空出现无来源。在其首次出现处补一句交代来历/铺垫,让因果闭合。若是新人物突然展现超常能力,补一两句身份或出场铺垫,不要让他毫无征兆地出现并立刻碾压。",
    "S2": "场景读起来像单机或工具人舞台。若本书明确是多人游戏,可补一两处其他玩家或活人互动纹理；若本书设定是真实异世界/单人沉浸/禁止玩家层术语,严禁补玩家、NPC、频道黑话,改为补真实人物反应、场景细节和关系互动。",
    "S3": "章末/章中钩子偏离武侠调性(现代惊悚/悬疑/系统冷腔)。把钩子改写成符合玄幻武侠世界的悬念——留一个武侠向的谜题/机遇/威胁,古风克制语气,不要现代刑侦跟踪、不要冰冷系统播报。",
    "S4": "不同人物一个腔调。让老道/主角/其他角色说话各有个性——老道苍劲古雅,主角带点市井韧劲,按身份区分用词与语气。",
    "S5": "有别扭的形容词/比喻搭配。把不自然的比喻替换成贴切、符合语境的说法,保持文笔通顺。",
    "S6": "详略失衡或情绪落点偏负向。★重点:凡涉及打斗/闯关/夺宝/奇遇的关键情节,绝不能一笔带过或直接跳到结果——必须把过程写透:招式怎么出、险在哪、主角怎么应对、如何逆转、拿到什么。把现实苦难的反复渲染压缩,笔墨给到核心世界的奇观、武学威力、行动收益和主角主动权;进入新世界时展开'新世界初体验';让全章情绪落点落在'爽/希望/变强/翻身'的正向反馈上,而非'惨/恐惧/被吸命'。是否影响现实必须服从本书 Story Bible / Canon。",
    "S7": "现实侧科幻设定降智。在主角第一次拿起/戴上设备处补交代:这是近未来时代(点出时代感)、设备的来历(二手/内测附赠/攒钱买/标配等,须紧贴设备本身)、以及沉浸原理(脑机接口/神经直连让人有真实触觉)。一两句话即可,绝不能让设备凭空出现、就这么进去了。",
}


def _get_probe_provider():
    from app.llm.providers import get_provider
    return get_provider(False)


def probe_semantic(content: str, provider=None, model: str | None = None) -> dict:
    """对一章正文做 S1-S7 语义诊断。返回 {"S1":{hit,quote,note},...} 或 {"error":...}。"""
    if provider is None:
        provider = _get_probe_provider()
    probe_model = model or os.environ.get("SEMANTIC_PROBE_MODEL", "deepseek-v4-pro")
    prompt = SEMANTIC_CHECK_PROMPT.format(content=content[:3500])
    last_raw = ""
    for _ in range(3):
        resp = provider.generate(
            prompt, max_tokens=3000, temperature=0.2,
            response_format={"type": "json_object"}, model=probe_model,
        )
        txt = (resp.text or "").strip()
        last_raw = txt
        if not txt:
            continue
        txt = re.sub(r"^```(?:json)?\s*|\s*```$", "", txt, flags=re.S).strip()
        m = re.search(r"\{.*\}", txt, re.S)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                continue
    return {"error": "no_json", "raw": last_raw[:300]}


def hits_from_probe(probe: dict) -> list[dict]:
    """从探针结果里抽出命中的缺陷列表,每项带 code/quote/note/hint。"""
    if not probe or "error" in probe:
        return []
    hits = []
    for code in ("S1", "S2", "S3", "S4", "S5", "S6", "S7"):
        item = probe.get(code) or {}
        if isinstance(item, dict) and item.get("hit"):
            hits.append({
                "code": code,
                "quote": (item.get("quote") or "").strip(),
                "note": (item.get("note") or "").strip(),
                "hint": _REPAIR_HINTS.get(code, ""),
            })
    return hits


def build_repair_instruction(hits: list[dict]) -> str:
    """把命中缺陷组织成给 repair LLM 的定向修复指令块。"""
    lines = []
    for h in hits:
        lines.append(
            f"【{h['code']} 缺陷】锚点原文:「{h['quote']}」\n"
            f"  问题:{h['note']}\n"
            f"  修复要求:{h['hint']}"
        )
    return "\n".join(lines)


# ── 确定性沉浸原理注入 ────────────────────────────────────────────────
# S7(c) 沉浸原理不交代:LLM 补写常不愿意加这种设定句(嫌打断叙事),导致语义门反复
# 遗留。凡代码能确定性拦截的绝不交给 prompt 祈祷——检测到"戴设备+进游戏"动作但全章
# 无沉浸原理词时,在动作句后确定性插入一句标准原理交代。这是把 ch1 手工做的事自动化。

_PRINCIPLE_WORDS = ["脑机", "神经直连", "神经接口", "神经元", "接口直连", "沉浸原理", "神经信号"]
# 进游戏的动作锚点(在其后插入原理句)
_ENTER_ANCHORS = [
    r"意识[一]?(?:沉|坠|抽|被抽|沉下去)[^\n。]*",
    r"眼前一黑[^\n。]*",
    r"合上眼[^\n。]*",
]
# 标准原理交代句(古今结合,点破脑机直连,一句话不啰嗦)
_PRINCIPLE_SENTENCE = (
    "这年头的全感头盔早不是稀罕物，神经接口贴着太阳穴，把游戏里的一切直接送进脑子——"
    "痛觉、触觉、冷热，都和真的一样。"
)


def inject_immersion_principle(content: str) -> tuple[str, bool]:
    """确定性注入沉浸原理:有进游戏动作但全章无原理词时,在动作锚点前插入一句原理交代。

    返回 (新正文, 是否注入)。已有原理词则原样返回。
    """
    import re as _re
    # 已交代原理 → 不动
    if any(w in content for w in _PRINCIPLE_WORDS):
        return content, False
    # 必须确有"戴设备进游戏"语境,否则不注入(避免误伤纯现实/纯游戏内章节)
    has_device = any(w in content for w in ["头盔", "游戏舱", "脑机", "全感设备"])
    has_enter = bool(_re.search(r"意识(?:沉|坠|抽|被抽)|眼前一黑|进(?:了|入).{0,4}游戏|登(?:入|录).{0,4}游戏", content))
    if not (has_device and has_enter):
        return content, False
    # 在第一个进游戏动作锚点前插入原理句
    for pat in _ENTER_ANCHORS:
        m = _re.search(pat, content)
        if m:
            idx = m.start()
            # 找锚点所在段落的起始,插在该段之前作为独立一段
            para_start = content.rfind("\n\n", 0, idx)
            insert_at = para_start + 2 if para_start != -1 else idx
            new = content[:insert_at] + _PRINCIPLE_SENTENCE + "\n\n" + content[insert_at:]
            return new, True
    return content, False
