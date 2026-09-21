from __future__ import annotations

"""B 管道：两阶段逐单元精写生成器。

设计目标（解决整章一次性生成的病根：注意力被稀释）：
  阶段1  LLM 把 brief 拆成 6-8 个连续场景单元（beat sheet）。
  阶段2  对每个单元独立调用 LLM 精写，每次只专注一个 300-450 字场景，
         全部算力压在"这个场景的比喻与情感落地"上。
  阶段3  拼接 → DraftOutput，交回 draft_chapter 复用后续字数补足/单元返修/落库。

相对原型 (reference_corpus/b_pipeline_proto.py) 的工程化改进：
  1. 字数预算：按 target_max 反推每单元字数窗口，避免整章膨胀到 4000+。
  2. 单元内强制分段：prompt 要求 + 后处理兜底，杜绝千字大墙。
  3. "不是A，是B" 句式清零：生成强约束 + 后处理检测（记录到 self_check）。
  4. 复用项目 parse_or_repair_json_object，避免脆弱的手写 JSON 解析。

开关：settings.b_pipeline_enabled（环境变量 B_PIPELINE_ENABLED，默认 False）。

v25.12(2026-08-07):
  - 标题优化: few-shot 注入真爆款标题 + 4 件套结构 + 题材词表 + 3 次重试
  - JSON 解析加重试 1 次
"""

import json
import re
import sqlite3 as sq
from app.core.config import settings
from app.llm.schemas import DraftOutput
from app.services.chapter_units import evaluate_chapter_units
from app.services.prompt_isolation import isolate_generation_inputs
from app.services.production_llm import parse_or_repair_json_object
from app.services.prompts import PARADIGM_EXEMPLARS_BLOCK, style_refs_block_for_genre
from app.services.reference_craft import build_reference_craft_block
from app.services.story_bible_logic_gate import evaluate_story_bible_logic
from app.services.unit_quality_gate import evaluate_unit_quality, unit_repair_contract

# 每单元字数窗口的默认下限/上限（会被 target_max 动态覆盖）
_UNIT_MIN_DEFAULT = 260
_UNIT_MAX_DEFAULT = 460

# "不是A，是B" 检测：只清空洞句（A、B 都是纯情绪/抽象状态词），
# 保留有信息量的（如"不是踩，是借力"——B 提供了 A 没有的具体动作/画面）。
# 情绪/抽象状态词表：这类词对立时是空洞充数，命中即算残留。
_EMO_WORDS = (
    "累|怕|恐惧|害怕|紧张|震惊|愤怒|生气|难过|伤心|开心|高兴|兴奋|平静|"
    "焦虑|不安|慌|慌张|镇定|冷静|绝望|希望|痛|疼|痛苦|快乐|悲伤|恐慌|"
    "惊|惊讶|惊恐|担心|忧虑|烦躁|烦|郁闷|委屈|愧疚|羞愧|尴尬|无奈|失落|"
    "麻木|空虚|孤独|寂寞|温暖|冰冷|恨|爱|喜欢|讨厌|厌恶|嫉妒|羡慕"
)
# 空洞 AB：不是<情绪词>[，。]是<情绪词>  —— 两端都是抽象情绪
_BUSHI_RE = re.compile(
    rf"不是({_EMO_WORDS})(?:那种|一种|的)?[，。]\s*(?:而)?是({_EMO_WORDS})"
)


# 开篇世界观落地：前 OPENING_CHAPTERS 章需向读者"交代"世界观，而非仅"不违反"。
# 断点根因：canon_context 只作为"铁律·禁止违反"传入，生成器从不知道
# "首章任务是把世界讲给读者听"，且 B 管道原本不感知 chapter_number。
OPENING_CHAPTERS = 3


def _surface_game_physical_entry_contract(*texts: str) -> bool:
    source = "\n".join(t or "" for t in texts)
    return (
        any(marker in source for marker in ("物理意义上坠入", "物理坠入", "物理意义上过去", "整个人坠入"))
        and any(marker in source for marker in ("神经接驳头盔", "游戏世界/玩家/面板作为长期设定但需克制", "内测", "代练"))
        and any(marker in source for marker in ("不是 VR 游玩", "禁止误写", "写成主角主动进游戏", "不是虚拟接入"))
    )


def _opening_beatsheet_directive(chapter_number: int | None, *authority_texts: str) -> str:
    """拆场景阶段：前三章追加"世界观落地"节拍要求。"""
    if not chapter_number or chapter_number > OPENING_CHAPTERS:
        return ""
    if chapter_number == 1:
        if _surface_game_physical_entry_contract(*authority_texts):
            return """

【★第1章开篇铁律·表层游戏动机 / 真实物理坠入（最高优先级）】

■ 本章允许并且应该交代"游戏相关表层动机"：沈渡为什么买旧盔、为什么半夜启动、为什么愿意冒险。可写二手游戏头盔、代练群、内测名额、登录流程、日结报酬、房租压力。

■ 但场景链必须把"主观误判"和"真实机制"分开：
  ① 表层：沈渡以为自己接的是一单游戏/内测/跑流程的底层活，买旧盔是为了拿到报酬。
  ② 异常：设备启动后出现数据异常/雪屏/无法理解的信息，异常感逐步压过普通游戏流程。
  ③ 真实：他不是进入虚拟游戏，也不是意识上传；是整个人被数据壁垒异常物理拽走，落到写实仙侠凡间。
  ④ 落地：落地后用疼痛、血、冷、饥饿、无信号、陌生山地和求伤药目标证明"这不是普通游戏体验"。

■ 拆单元建议顺序：现实底层处境 → 买旧盔与接单动机 → 启动设备和异常征兆 → 物理坠落 → 山地醒来确认现实 → 发现人烟/药味 → 求活交换 → 章末钩子。

■ 禁止误写：不要把后续仙侠世界写成游戏副本/NPC村庄/玩家任务线；不要写成主角兴奋游玩。游戏元素只能是入口前的误判外壳和长期设定伏线，不能替代人物行动与真实代价。
"""
        return """

【★第1章开篇铁律·详略分配与情绪落点（最高优先级，压倒一切）】

■【现实世界观底座·必须交代清楚，否则读者出戏降智】
  本书现实线设定在**近未来时代（约2040年代）**，脑机接口已是成熟的消费级技术。写第1章时，必须让读者在不知不觉中接收到这三条设定，绝不能让它们"凭空"：
  ① 时代锚点：用一两个近未来的生活细节点出这不是当下（例：脑机接口设备随处可见、全感沉浸游戏是主流娱乐、街头有相关广告/新闻/他人也在玩）。不要长篇科普，用环境细节自然带出即可。
  ② 头盔的来历：主角那台游戏头盔（脑机接口设备）**不能凭空出现在床头**。必须交代它怎么来的——是这个时代人人都有的普通消费电子（像今天的手机），还是内测资格附赠、二手淘来、朋友旧物。哪怕一句话，也要让读者知道"他为什么有这台设备"。
  ③ 沉浸原理：为什么戴上头盔就能身临其境？必须点出这是**脑机接口/神经直连**技术——设备直接向大脑输入神经信号，绕过肉眼肉耳，所以视觉、触觉、痛觉都跟真的一样。这在这个时代是成熟技术、不稀奇，一两句带过让读者信服即可。
  ★注意区分"平常"与"异常"：脑机接口沉浸游戏在这个时代是**平常的**（人人能玩、不神奇）。真正异常的内容必须服从 Story Bible / Canon；如果设定要求游戏与现实隔离，严禁写成游戏能力、内力、修为或伤势带回现实。第1章要让读者感到：进入世界很平常，但核心规则或异常体验值得追读。

■ 这本书的第一章要建立读者承诺：让读者代入主角，推开一扇门，走进一个**宏大、辽阔、高自由、高拟真、充满未知机遇的武侠世界**，跟着主角遇奇遇、习武学、看具体行动和世界规则逐步展开。现实线负责动机和边界，是否反馈现实必须严格遵守本书 Story Bible，不得使用其他书的旧设定。

■【详略铁律·拆场景时按此分配篇幅】
  ✓ 要详写（多给场景、给足笔墨）：进入游戏后那个武侠世界的**第一印象奇观**（辽阔天地、山河庙宇、江湖气象、扑面而来的高手/势力/传说，让读者"哇"一声）；主角遇到的机遇与奇遇；学到武学时那门武学的**来历、威力、施展画面**；打斗过程的**具体招式与酣畅感**；变强后的**实力反馈爽感**（快、狠、比之前强多少，看得见摸得着）。
  ✗ 要略写（一两笔带过，绝不铺陈）：现实里的苦难琐碎（送外卖、催款电话、父亲病情）——只用**最精炼的几笔**交代"他为什么非进游戏不可"这个动机即可，不要反复渲染惨、不要让苦难占据大量场景。现实是"动机燃料"，不是"主菜"。
  ✗ 金手指的"代价/副作用"在第1章**只露一丝苗头即可**，绝不能喧宾夺主盖过"变强的爽"。第1章的主基调是"打开新世界+尝到甜头"，不是"背上负担"。

■【进入游戏的仪式感·必须有】
  主角进入本书核心游戏/异世界入口的那一刻，是"推开新世界大门"的高光时刻，必须给一个**让读者眼前一亮的世界展开场景**：这个世界长什么样、有多大、有多真、藏着多少可能（可以是宏伟的登录画面、一览众山的开局视角、扑面而来的江湖信息、旁人口中的传说高手与秘境）。绝不允许用一句冷冰冰的系统告示就把读者扔进陌生场景——那是把最该详写的"新世界初体验"给略写了。系统提示可以有，但它是**点缀**，不是**开场介绍的全部**；世界的辽阔与自由要靠画面和主角的见闻来展现。

■【第1章仍需交代清楚（用场景，不用旁白）】
① 主角是谁、为何非进游戏不可（精炼几笔，交代动机即可）。
② 这个游戏/武侠世界的特殊性：沙盒高自由（没有新手引导、一切自谋，但这是"自由"不是"劝退"）、高拟真（游戏内感受真实到可怕）、可探索（世界辽阔，处处是机遇）。这一条要**详写、要让读者向往**。
③ 关键传承不得跳跃、且必须"闭环"：主角获得师父、功法、秘籍、信物时，"获得的过程本身"必须占一个独立场景（遇到谁、如何求得、对方为何给），且要写出这门武学/这件宝物**牛在哪、让主角尝到什么甜头**。
   ★闭环铁律：若本章出现拜师/传功/授宝的**开端**，就必须在本章内写完"**兑现**"动作——明确写出"收/不收徒""传/不传功""给/不给信物"的结果。绝不允许开了头（高人考察主角、发现他资质好）却不兑现，就跳到下一幕让主角凭空拥有师门身份/功法/信物。若这一章不打算完成拜师，就不要让主角在本章末尾已经拥有对应身份、功法或信物。
   ★功法名称一致性铁律：主角所练、所用的功法名称，必须与前文**明确传授给他的那一门**严格一致。绝不允许中途换名或给主角凭空冠上一门没传授过的功法名。练什么就叫什么，得到哪门才用哪门。
④ 章末追读问题：第1章结尾可以露出核心规则的异常、世界更深层入口、主角选择的后果或下一次探索机会；若 Story Bible 要求游戏与现实隔离，严禁把钩子写成游戏能力带回现实。"""
    return f"""

【★第{chapter_number}章开篇铁律·世界观延续落地】
本章仍属开篇区（前{OPENING_CHAPTERS}章）。读者对世界观尚不熟悉，拆场景时注意：
① 新登场的人物/势力/地点，首次出现要给一个身份锚点（他是谁、和主角什么关系、为何出现），禁止凭空冒出一个名字就展开互动。
② 金手指产物（资金/技能/道具/人脉）首次获得必须有来历交代，禁止凭空出现。
③ 若本章再次使用金手指，延续第1章已建立的规则与代价，不要出现与前文矛盾的新用法。"""


def _opening_write_directive(chapter_number: int | None, is_first: bool, *authority_texts: str) -> str:
    """精写阶段：前三章追加"世界观落地"写作要求。"""
    if not chapter_number or chapter_number > OPENING_CHAPTERS:
        return ""
    if chapter_number == 1 and _surface_game_physical_entry_contract(*authority_texts):
        lines = ["", "【★第1章精写契约·像作者样稿那样写】"]
        lines += [
            "- 游戏/内测/代练/头盔可以写，但写成沈渡接烂活的现实动机：缺钱、房租、旧设备、老板不退不换、他盘算报酬。",
            "- 不要把这些元素当禁词删掉；也不要写成普通网游游玩爽开局。核心读感是：他以为是游戏活，异常发生后才发现身体真的掉进陌生山地。",
            "- 买盔目的必须成立：任务是内测试玩/登录流程/跑新手流程/代练测试号，头盔只是自备工具，不写成'验自己的二手头盔'。",
            "- 入口事故要按感知链写：旧盔/接线/雪屏/文字看不清/失重/身体被拽走/摔醒；每一步让沈渡先误判，再用身体痛感纠正。",
            "- 物理坠入后必须用具体事实确认：头盔不见、手机无信号、时间停住、血、疼、冷、山、药味、人烟。",
            "- 比喻和判断必须从沈渡当下经验里长出来。像'血珠子晃了一下，他仿佛看见屏幕雪花；再眨眼又只剩血'这种写法，优先于硬比喻。",
            "- 对白按市井口吻写，保留'想要就五十拿去，到手不退不换啊''坏了可别拿回来找我'这类人话。",
            "- 禁止写成论坛/NPC/玩家任务刷屏、意识上传、VR游玩、主动进游戏升级；异常后的世界是写实仙侠凡间，有真实痛感和求活代价。",
        ]
        return "\n".join(lines)
    lines = ["", f"【★开篇区第{chapter_number}章·世界观落地与爽感要求（写这个场景时务必落实）】"]
    if is_first and chapter_number == 1:
        lines.append(
            "- 这是全书第一个场景。若本场景是主角进入本书核心游戏/异世界入口，务必把它写成\"推开新世界大门\"的高光——"
            "让读者透过主角的眼睛，看见一个辽阔、瑰丽、充满未知的武侠世界（天地山河、江湖气象、扑面而来的高手传说与秘境），"
            "写出\"哇，这世界真大真真实真自由\"的向往感。绝不能一句冷冰冰的系统告示就把人扔在雨里。"
        )
        lines.append(
            "- 【现实世界观底座·防降智】本书现实线是近未来（约2040年代），脑机接口是成熟消费技术。"
            "若本场景写到那台游戏头盔：绝不能让它凭空出现在床头——要交代它的来历（时代标配的普通设备/内测附赠/二手淘来等，一句话即可）；"
            "要点出它是脑机接口设备，靠神经直连让人身临其境（这个时代很平常，不神奇，一两句带过）。"
            "记住：戴头盔能沉浸是这个时代的平常事；真正异常的规则必须来自本书 Story Bible / Canon。若本书要求游戏与现实隔离，不得写游戏所练同步到现实身体。"
        )
    lines += [
        "- 【情绪落点】本场景若涉及主角变强、习武、遇奇遇，落点必须是**正向的爽**——"
        "写清这门武学/这件宝物多厉害、施展起来什么画面、主角尝到了什么甜头、比之前强了多少（看得见摸得着）。"
        "读者代入的是\"爽\"，不是\"惨\"。",
        "- 【详略】现实里的苦难（送外卖、催款、病情）只用最精炼的一两笔交代动机，绝不反复渲染惨；"
        "武侠世界的奇观、武学威力、打斗画面、变强反馈要**给足笔墨、写细写透**。",
        "- 金手指/核心规则的代价或边界只露苗头，绝不喧宾夺主；是否影响现实必须服从本书 Story Bible / Canon，不得沿用其他书的现实映射设定。",
        "- 关键传承（师父/功法/秘籍/信物）首次出现，必须交代它**怎么来的**且写出它**牛在哪、爽在哪**，禁止一句旁白带过。",
        "- 新人物/新势力首次登场，用一两句给出身份锚点（谁、和主角什么关系、为何在此），不要让读者对着陌生名字猜半天。",
    ]
    return "\n".join(lines)


def _reality_isolation_write_block(*texts: str, is_last: bool = False) -> str:
    source = "\n".join(t or "" for t in texts)
    markers = (
        "游戏与现实彻底隔离",
        "禁游戏修为外溢现实",
        "游戏修为不得外溢现实",
        "游戏能力不得外溢现实",
        "现实里他还是那个底层小人物",
        "禁现实灵气",
        "禁现实修真",
        "禁掌心发热",
        "禁经脉热流",
    )
    if not any(marker in source for marker in markers):
        return ""
    last_line = (
        "\n- 如果这是章末钩子，钩子只能来自游戏内未解之谜、下一次探索机会、现实压力或信息差；"
        "不得用现实身体出现热流/丹田/经脉/一股气/功法运转来制造悬念。"
        if is_last
        else ""
    )
    return f"""
【现实隔离硬禁】
- 本书 Story Bible 已禁止游戏修为/能力外溢现实。现实场景里，主角不能获得、保留或运转游戏中的热流、真气、内力、丹田、经脉、气感、功法、伤势或符咒。
- 可以写主角怀疑设备异常、担心神经损伤、想重新进入游戏验证；但必须保持“现实身体没有超凡收益”的边界。
- 禁止把游戏里的伤势、掌风、发力链条、肌肉记忆、握力提升、反应变快、神经反馈、淤青痛感写成进入现实或刻在现实身体上。
- 禁止写“游戏里练成的功法在现实丹田运转”“这不是游戏身体却多了一股气”“掌心热流带回出租屋”“发力链条刻进现实神经”“游戏伤势留在现实后背”等同类表达。{last_line}
"""


def _early_mortal_power_ban_block(*texts: str) -> str:
    source = "\n".join(t or "" for t in texts)
    markers = (
        "禁掌心发热",
        "禁经脉热流",
        "禁章末硬塞修真觉醒",
        "修真元素在凡人阶段:绝不出现",
        "修真元素在凡人阶段：绝不出现",
        "凡人阶段绝不修真",
    )
    if not any(marker in source for marker in markers):
        return ""
    return """
【凡人阶段能力表现硬禁】
- 本书当前阶段禁止修真体感和修真觉醒。无论在游戏内还是现实中，都不得写掌心发热、热流游走、丹田、经脉、真气、内力、气感、符咒显形。
- 凡人武学的爽感要写成外显动作结果：脚步更稳、发力更顺、出拳更准、反应更快、招式打中目标、旁观者改变态度。
- 如果要写“学会拳法/剑法”，只能通过动作、姿势、呼吸节奏、击中木桩/逼退对手等写实效果表现，不用热流、气、丹田或经脉解释。
"""


def _chapter_commitment_write_block(*texts: str, is_last: bool = False) -> str:
    source = "\n".join(t or "" for t in texts)
    markers = ("本章剧情承诺", "主动选择", "可见代价", "核心能力", "明确回报", "章末出现", "章末钩子")
    if not any(marker in source for marker in markers):
        return ""
    last_line = (
        "\n- 本章结尾必须把变化落成具体物象或局面：一件线索/信物、一个明确机会、一段关系变化、一个新风险；"
        "不要只写“他决定继续”“远处有动静”。"
        if is_last
        else ""
    )
    return f"""
【本章剧情承诺落地法】
- 每个场景都要让主角做一次可见选择：问、赌、拒绝、忍、交换、试探、追查、留下或转身；不要只让他被安排。
- 可见代价必须落成现场后果：磨破、受伤、欠钱、被记住脸、被拒、失去机会、关系变坏或时间被压缩。
- 能力/规则回报不能靠面板替代：写成拿到线索、学到一招、动作变稳、旁人改变态度、获得信物/机会或打开下一处入口。
- 这些承诺必须服从 Story Bible；若本书禁止现实外溢，回报只能发生在世界内、信息层或下一步机会中，不能写成现实身体变强。{last_line}
"""


def hanzi_count(text: str) -> int:
    return len(re.findall(r"[\u4e00-\u9fff]", text))


def evaluate_beatsheet_quality(
    units: list[dict],
    *,
    min_units: int,
    story_bible_text: str = "",
    canon_context: str = "",
    constraints: str = "",
    chapter_number: int | None = None,
) -> tuple[bool, list[str]]:
    issues: list[str] = []
    if len(units) < min_units:
        issues.append(f"unit_count_low:{len(units)}<{min_units}")
    if not units:
        return False, issues or ["empty_beatsheet"]

    full = "\n".join(_unit_blob(unit) for unit in units)
    logic = evaluate_story_bible_logic(
        full,
        story_bible_text=story_bible_text,
        canon_context=canon_context,
        constraints=constraints,
    )
    issues.extend(f"story_bible_logic:{issue}" for issue in logic.issues)

    if chapter_number == 1 and _early_mortal_power_ban_block(canon_context, constraints, story_bible_text):
        forbidden = ("传功", "灌体", "散修", "丹田", "经脉", "真气", "内力", "热流", "气感", "符咒", "修真觉醒")
        hits = [term for term in forbidden if term in full]
        if hits:
            issues.append(f"stage_lock_forbidden_terms:{'/'.join(hits[:6])}")

    generic_handoffs = ("章末悬念", "留下悬念", "引出后续", "新的危机", "未知危险", "继续探索")
    weak_obstacle_count = 0
    for idx, unit in enumerate(units, start=1):
        scene = str(unit.get("scene") or "").strip()
        action = str(unit.get("action") or "").strip()
        emotion = str(unit.get("emotion") or "").strip()
        handoff = str(unit.get("handoff") or "").strip()
        missing = [name for name, value in (("scene", scene), ("action", action), ("emotion", emotion), ("handoff", handoff)) if len(value) < 4]
        if missing:
            issues.append(f"unit{idx}:missing_{'/'.join(missing)}")
        if _is_abstract_unit(scene, action):
            issues.append(f"unit{idx}:abstract_scene")
        if not _has_obstacle(action):
            weak_obstacle_count += 1
            issues.append(f"unit{idx}:weak_obstacle")
        if idx < len(units) and (not handoff or any(term in handoff for term in generic_handoffs)):
            issues.append(f"unit{idx}:generic_handoff")
        if idx > 1 and not _has_progression_marker(scene, action, handoff):
            issues.append(f"unit{idx}:weak_progression")

    hard_issue_count = sum(
        1
        for issue in issues
        if not issue.endswith(":weak_progression") and not issue.endswith(":weak_obstacle")
    )
    if weak_obstacle_count >= max(3, len(units) // 2 + 1):
        hard_issue_count += 1
    return hard_issue_count == 0, issues[:20]


def _unit_blob(unit: dict) -> str:
    return "\n".join(str(unit.get(key) or "") for key in ("scene", "action", "emotion", "handoff"))


def _is_abstract_unit(scene: str, action: str) -> bool:
    text = scene + action
    abstract_terms = ("铺垫", "介绍", "交代", "展现", "体现", "推进主线", "建立世界观", "制造悬念", "强化代入")
    concrete_terms = ("出租屋", "头盔", "山", "庙", "村", "镇", "院", "路", "坡", "崖", "木桩", "药", "柴刀", "门", "床")
    return any(term in text for term in abstract_terms) and not any(term in text for term in concrete_terms)


def _has_obstacle(action: str) -> bool:
    obstacle_terms = (
        "但", "却", "被", "不能", "不敢", "没钱", "欠", "疼", "伤", "野狼", "压", "摔", "滑",
        "阻", "拦", "追", "逼", "试", "抽", "打", "拒", "警惕", "代价", "风险",
    )
    return any(term in action for term in obstacle_terms)


def _has_progression_marker(*texts: str) -> bool:
    text = "".join(texts)
    markers = ("于是", "因此", "只好", "转而", "拿到", "失去", "发现", "决定", "答应", "拒绝", "跟上", "离开", "前往", "回来", "明天", "下一")
    return any(marker in text for marker in markers)


def _has_hard_story_bible_issue(issues: list[str]) -> bool:
    return any(str(issue).startswith("story_bible_logic:") for issue in issues or [])


def _unit_structure_repair_contract(text: str, *, unit_min: int, unit_max: int) -> tuple[str, dict]:
    report = evaluate_chapter_units(text, target_min=max(180, unit_min - 80), target_max=max(unit_max, unit_min + 120))
    weak: list[str] = []
    for row in report.units[:2]:
        for issue in row.get("issues", []) or []:
            if issue not in weak:
                weak.append(issue)
    if report.score >= 72 and not weak:
        return "", report.to_dict()
    labels = {
        "goal": "补清本场景里主角当下想要什么，必须落在一句动作或选择上",
        "action": "补强动作链，写出至少两步连续动作，不用概括句带过",
        "obstacle": "补出现场阻碍或代价，让主角不能轻松完成",
        "consequence": "补出动作造成的可见结果，例如拿到/失去/被拒/受伤/关系变化",
        "info_gain": "补出新的信息增量，让读者知道世界规则、人物关系或下一步机会",
        "reaction": "补出人物反应，不只写主角想法，也写对方/旁观者/环境的反馈",
        "handoff": "补出交接点，让本单元末尾自然推向下一单元",
        "precision": "修正表达和观察逻辑，让画面、动作、因果更具体",
        "causal_chain": "重排本单元因果链，写成目标之后有动作、阻碍迫使调整、动作带来后果、后果引出反应",
        "length": "补足或压缩到本单元预算内",
    }
    lines = [
        "【单元结构弱项定向补写】",
        "- 本轮不是润色：必须把本单元重写成一条完整小因果链。",
        "- 正文内必须可见：主角目标 → 现场阻碍 → 主角连续动作 → 可见后果/代价 → 人物反应 → 下一单元交接。",
        f"- 字数控制在 {unit_min}-{unit_max} 中文字符；不得为了补结构扩成多场景或跳到下一场景。",
    ]
    for issue in weak[:6]:
        lines.append(f"- {labels.get(issue, issue)}")
    if report.repair_contract:
        lines.append(f"- 结构评估摘要：{report.repair_contract[0]}")
    return "\n".join(lines), report.to_dict()


def _structure_issue_count(report: dict) -> int:
    return len(report.get("issues") or [])


def _beatsheet_retry_beats(required_beats: str, issues: list[str], *, unit_hint: int, attempt: int) -> str:
    issue_lines = "\n".join(f"- {issue}" for issue in issues[:12])
    return (
        f"{required_beats}\n\n"
        f"【拆单元重试硬约束·第{attempt}次】\n"
        f"上次 beat sheet 未通过门禁：\n{issue_lines}\n"
        f"- 必须输出 {unit_hint} 个连续场景单元，不多不少。\n"
        "- 不要合并成单场景，不要用摘要、主题、功能描述代替具体场景。\n"
        "- 每个单元都要有独立地点/动作/阻碍/情绪/交接钩子。\n"
        "- 每个单元必须写清：主角具体目标、现场阻碍、他采取的动作、动作造成的局面变化。\n"
        "- 严格遵守 Story Bible 和阶段锁；被列为禁用的能力/设定不得出现在场景规划里。\n"
        "- 禁用 NPC/玩家/论坛/内测 等元概念字样；需要人物时写成具体身份，如村民、店伙计、武馆弟子、药铺掌柜、路人。\n"
    )


def _budget_per_unit(target_min: int, target_max: int, unit_count: int) -> tuple[int, int]:
    """按整章目标字数反推每单元字数窗口。

    每单元下限 = 整章下限/单元数（保证累计达标），上限 = 整章上限*0.92/单元数（留边际）。
    再收敛到 [200, 520] 合理区间，避免大墙或碎句。
    """
    if unit_count <= 0:
        return _UNIT_MIN_DEFAULT, _UNIT_MAX_DEFAULT
    safe_max = int(target_max * 0.92)
    per_max = max(320, safe_max // unit_count)
    # 下限按整章下限均摊，并上浮到 per_max-60 附近，保证累计能到 target_min
    per_min = max(240, -(-target_min // unit_count))  # ceil division
    per_max = min(per_max, 520)
    per_min = min(per_min, per_max - 40)
    return per_min, per_max


def _call_json(provider, prompt, *, max_tokens, temperature, model, tag, schema, _retries: int = 1):
    """调 LLM 拿 JSON。失败重试 1 次,仍失败 raise StructuredOutputError。

    v25.11(2026-08-07): 加重试 1 次, 治"LLM 突然返空"假死。
    """
    last_exc: Exception | None = None
    for _attempt in range(_retries + 1):
        try:
            resp = provider.generate(
                prompt,
                max_tokens=max_tokens,
                temperature=temperature,
                model=model,
                response_format={"type": "json_object"} if provider.name != "dry_run" else None,
            )
            data = parse_or_repair_json_object(
                provider,
                response_text=resp.text,
                original_prompt=prompt,
                expected_schema=schema,
                max_tokens=max_tokens,
                temperature=temperature,
                model=model,
                task_label=tag,
            )
            return data, resp
        except Exception as _e:
            last_exc = _e
            if '"content"' in schema:
                raw_text = (locals().get("resp").text if "resp" in locals() and locals().get("resp") is not None else "")
                fallback = _fallback_content_from_non_json(raw_text)
                if fallback:
                    print(f"[B-pipe][{tag}] JSON失败，已从非JSON输出抽取正文兜底", flush=True)
                    return {"content": fallback, "note": "non_json_content_fallback"}, locals().get("resp")
            print(f"[B-pipe][{tag}] 尝试{_attempt+1}/{_retries+1} 失败: {_e}", flush=True)
            if _attempt < _retries:
                continue
    assert last_exc is not None
    raise last_exc


def _fallback_content_from_non_json(text: str) -> str:
    value = str(text or "").strip()
    if not value:
        return ""
    value = re.sub(r"^```(?:json|text|markdown)?\s*", "", value)
    value = re.sub(r"\s*```$", "", value).strip()
    value = re.sub(r"^(?:正文|替换后的小说正文|content)\s*[:：]\s*", "", value).strip()
    if not value:
        return ""
    if "{" in value or "}" in value:
        return ""
    if any(marker in value for marker in ("无法", "不能", "抱歉", "JSON", "json", "质检报告", "修订合同")):
        return ""
    count = hanzi_count(value)
    if count < 80:
        return ""
    # 2026-09-17 真机实测: 失控输出(模型抛开 JSON 契约直接长篇跑题)可达 3600+ 字
    # 且混入废弃设定词; 单元预算上限 520, 超过 1500 字一律视为失控, 拒收走重试。
    if count > 1500:
        return ""
    return value


def build_beatsheet(
    provider,
    *,
    book_title: str,
    genre: str,
    premise: str,
    goal: str,
    required_beats: str,
    constraints: str,
    canon_context: str,
    previous_chapter_context: str,
    unit_count_hint: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    chapter_number: int | None = None,
) -> list[dict]:
    """阶段1：拆场景单元。"""
    canon_block = f"""
【世界观设定·铁律】（拆场景必须在这套设定内，禁止发明违反设定的桥段）：
{canon_context}
""" if canon_context and "未登记" not in canon_context else ""
    opening_directive = _opening_beatsheet_directive(chapter_number, canon_context, constraints, goal, required_beats, premise, genre)
    mortal_power_ban_block = _early_mortal_power_ban_block(canon_context, goal, constraints, required_beats)
    prompt = f"""你是资深网文编剧。把下面这一章拆成 {unit_count_hint} 个连续的**场景单元**，做成拍摄脚本式的 beat sheet。

作品：{book_title}（{genre}）
故事地基：{premise}
{canon_block}
{mortal_power_ban_block}
本章目标：{goal}
必要节拍：{required_beats}
硬约束：{constraints}
前章结尾：{previous_chapter_context[:400]}
{opening_directive}

要求：
- 每个单元是一个**具体的连续场景**（一个地点、一段连续时间里发生的事），不是抽象概括。
- 单元之间是动作因果链：后一个单元承接前一个的后果。
- 每个单元标注：场景地点、这个场景里主角要做什么/遇到什么阻碍/情绪状态、这个场景结束时留给下个场景的钩子。
- **元概念边界**：若本章现实端需要交代接单动机，可出现"内测/代练/登录流程/头盔"；但进入异世界后的场景不得把人物写成 NPC/玩家/论坛/任务面板，必须写成具体身份（村民、店伙计、武馆弟子、药铺掌柜、路人等）。
- **场景设计不得违反世界观设定**：涉及武学/能力时，必须符合上面登记的力量体系规则、代价与限制。
- **传承闭环铁律**：如果这一章要让主角获得师父/功法/秘籍/信物，那么"考察→兑现"必须在本章的场景链里闭合——不能出现"某个场景高人考察主角/惊讶他资质"，却在后面场景里主角已经凭空拥有师门身份、在练那门功法、揣着信物下山。要么就安排一个明确的"收徒/传功/授信物"场景把它兑现，要么这一章根本不触发获得（主角本章末尾就不该拥有这些）。功法名称在所有场景里必须一致：主角练/用的功法，只能是前面场景里明确传给他的那一门，不许中途改名或凭空冒出新功法名。
- **场景衔接不许跳跃**：相邻两个场景之间若发生了重要状态变化（拜了师、得了功法、换了地点身份），这个变化本身必须是某个场景的内容，不能靠场景之间的空白让读者自己脑补。
- 第一个单元承接前章后果并把主角拽进具体处境；最后一个单元留章末悬念。
- 输出 {unit_count_hint} 个单元，不要多也不要少。

严格输出 JSON：{{"units":[{{"index":1,"scene":"场景地点与情境","action":"主角做什么/遇到什么","emotion":"主角此刻真实情绪(具体，不要'紧张')","handoff":"本场景结束时的钩子"}}]}}"""
    data, _ = _call_json(
        provider, prompt,
        max_tokens=max(max_tokens, 6000), temperature=temperature, model=model,
        tag="B管道拆场景",
        schema='{"units":[{"index":1,"scene":"..","action":"..","emotion":"..","handoff":".."}]}',
    )
    units = data.get("units") or []
    return [u for u in units if isinstance(u, dict) and u.get("scene")]


def rewrite_failed_unit(
    provider,
    *,
    book_title: str,
    genre: str,
    unit: dict,
    failed_text: str,
    failure_contract: str,
    prev_tail: str,
    next_scene: str,
    canon_context: str,
    constraints: str = "",
    unit_min: int,
    unit_max: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
) -> str:
    constraint_block = f"""
【本章硬约束】
{constraints[:1600]}
""" if constraints else ""
    isolation_block = _reality_isolation_write_block(canon_context, constraints)
    mortal_power_ban_block = _early_mortal_power_ban_block(canon_context, constraints)
    scene_context = "\n".join(
        str(item or "")
        for item in [
            unit.get("scene"),
            unit.get("action"),
            unit.get("emotion"),
            unit.get("handoff"),
            next_scene,
            failure_contract,
            constraints,
        ]
        if item
    )
    reference_craft_block = build_reference_craft_block(None, scene_context=scene_context)
    prompt = f"""你是成熟的男频网文作者。下面这个场景单元未通过单元审核，请只重写这一个单元。

作品：{book_title}（{genre}）
{reference_craft_block}

【前文结尾】
{prev_tail if prev_tail else '（本章开头，无前文）'}

【当前单元任务】
- 场景：{unit.get('scene')}
- 动作/阻碍：{unit.get('action')}
- 情绪：{unit.get('emotion')}
- 结尾交接：{unit.get('handoff')}
- 下一个场景：{next_scene}

【世界观铁律】
{canon_context[:1200]}
{constraint_block}
{isolation_block}
{mortal_power_ban_block}

【未通过原因】
{failure_contract}

【原失败单元】
{failed_text}

要求：
1. 只输出替换后的小说正文，{unit_min}-{unit_max} 中文字符。
2. 第一段直接承接前文最后动作或后果，不重述前文。
3. 修掉未通过原因，但不要堆指标词；用具体动作、环境、心理链完成场景。
4. 不写 JSON、标题、说明、质检话术。

严格输出 JSON：{{"content":"替换后的小说正文","note":"修复点"}}"""
    data, _ = _call_json(
        provider, prompt,
        # 2026-09-17 真机实测: kimi-k3 思考Token ~2000-4000+, 6000 上限下返修仍
        # 出现空响应/截断; 下限 8000 + 3 次尝试(_retries=2)吸收抖动尾部。
        max_tokens=max(max_tokens // 2, 8000), temperature=temperature, model=model,
        tag=f"B管道重写失败单元{unit.get('index')}",
        schema='{"content":"..","note":".."}',
        _retries=2,
    )
    return str(data.get("content") or "").strip()


def write_unit(
    provider,
    *,
    book_title: str,
    genre: str,
    premise: str,
    chapter_goal: str,
    unit: dict,
    prev_tail: str,
    next_scene: str,
    canon_context: str,
    constraints: str = "",
    is_first: bool,
    is_last: bool,
    unit_min: int,
    unit_max: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    chapter_number: int | None = None,
) -> str:
    """阶段2：精写单个场景。"""
    role = (
        "本章开篇（承接前章后果，把主角拽进具体处境）" if is_first
        else "本章结尾（由本章行动引出新危险/新发现/未解压力）" if is_last
        else "本章中段"
    )
    canon_block = f"""
【世界观设定·铁律】（本场景涉及的武学/能力/世界规则，必须严格符合下面登记的设定；不得发明违反设定的表现）：
{canon_context}
""" if canon_context and "未登记" not in canon_context else ""
    constraint_block = f"""
【本章硬约束】（与 Story Bible 同级；本场景必须执行，不能被爽点、钩子或范文风格覆盖）：
{constraints[:1800]}
""" if constraints else ""
    opening_block = _opening_write_directive(chapter_number, is_first, canon_context, constraints, chapter_goal, premise, genre)
    isolation_block = _reality_isolation_write_block(canon_context, constraints, chapter_goal, is_last=is_last)
    mortal_power_ban_block = _early_mortal_power_ban_block(canon_context, constraints, chapter_goal)
    commitment_block = _chapter_commitment_write_block(chapter_goal, constraints, is_last=is_last)
    style_refs_block = style_refs_block_for_genre(genre)
    scene_context = "\n".join(
        str(item or "")
        for item in [
            role,
            genre,
            chapter_goal,
            unit.get("scene"),
            unit.get("action"),
            unit.get("emotion"),
            unit.get("handoff"),
            next_scene,
            constraints,
        ]
        if item
    )
    reference_craft_block = build_reference_craft_block(None, chapter_number=chapter_number, scene_context=scene_context)
    prompt = f"""你是成熟的男频网文作者。现在只写**一个场景**，把它写透，不要写完整章，不要跳到别的场景。

{style_refs_block}

{PARADIGM_EXEMPLARS_BLOCK}

{reference_craft_block}

---
作品：{book_title}（{genre}）· 目标读者在番茄手机端阅读
故事地基：{premise[:300]}
{canon_block}
{constraint_block}
{isolation_block}
{mortal_power_ban_block}
{commitment_block}
本章大目标（仅供你把握方向，不要在这一个场景里写完）：{chapter_goal}
{opening_block}

【你要写的这个场景】（{role}）
- 场景：{unit.get('scene')}
- 主角要做什么/遇到什么：{unit.get('action')}
- 主角此刻的真实情绪：{unit.get('emotion')}
- 这个场景结束时要留的钩子：{unit.get('handoff')}
- 下一个场景预告（为了你的结尾能自然承接，但**不要写到下一场景里去**）：{next_scene}

【前文结尾】（你的文字要无缝接在这后面；若为空则是本章开头）：
{prev_tail if prev_tail else '（本章开头，无前文）'}

【这个场景怎么写 · 铁律】
1. 只写这一个场景，{unit_min}-{unit_max} 中文字符。写透一个场景，胜过草草带过三个。
2. **必须分段**：按对话、动作、心理转折自然分段，每段不超过 80 字；对话独占一段。绝对不要写成一大坨。
3. **情绪必须落在读者能共情的具体身体感受或动作上**——比喻要用读者体验过的东西（如"像踩在棉花上""胃里像塞了块冰"），绝不用读者没体会过的私人感觉（如"像被敲足三里"）。
4. **禁止"不是A，是B"句式**（如"不是累，是怕"）。要强调就直接写那个东西，不要用否定对比来强调。
5. 禁止"他很紧张/震惊/愤怒"这类情绪标签；禁止"愣了三秒/咽了口唾沫/后背发凉/心里咯噔"这类AI套话模板。
6. **涉及武学/超凡能力时，必须符合世界观设定的规则、代价、限制**——不得让能力做设定里做不到的事（例：轻功是借力身法，讲究轻巧，不会靠蛮力把地面踩裂）。
7. 对白有声线，人物说话带身份和处境。场景可画：有光源、空间、动作轨迹。
8. **对白必须口语化**——像真人当下脱口而出，不是书面腔：
   - 用短句、日常词，允许语气词（啊、吧、呗、咋、行了）、不完整句、被打断、话没说完。
   - 拒绝文绉绉的整句（如"此事恐怕另有隐情"→"这事儿不对劲吧"）；拒绝谁都一个腔调，粗人痞、书生绕、少女跳。
   - 越紧张越短促，越熟越随便。别让角色像在念稿。
9. **场景描述也要生活化**——用主角眼睛看到的大白话，别堆书面形容词。
   状态/动作用日常说法（如"天擦黑了""他脚一崴"），少用"暮色四合""踉跄"这种书面词。
10. 承接前文最后一个动作，不要重述前文，直接往下写。
11. **系统提示语气铁律**：若本场景出现系统提示/系统音/面板播报，必须古朴克制、公事公办，像古籍判词或冷硬公告；严禁现代网络吐槽腔、玩梗、调侃、卖萌（禁"杂鱼退散""多吃饭少打架""你终于不用被影子打死了""建议你现在后悔还来得及"这类）。数值播报（同步率/精气值百分比）点到即止，一场景最多一处，不堆砌。
12. **钩子调性铁律**：本场景若是结尾钩子，必须落在玄幻武侠或现实困境的具体物象上（一柄剑、一道伤、一封催款单、一个身影、一声脚步）；严禁现代惊悚/悬疑推理腔（禁"三天内暴毙""门牌号都对得上"式冷汗惊悚旁白）。现实催债/恐吓要写成人物具体动作与处境，不写成惊悚片解说。
13. **道具/功法来源铁律**：本场景若主角使出某功法、取出某道具/丹药/秘籍，该物必须是前文已获得的（拜师所授、闯荡所得、他人所赠），禁止让功法/道具凭空出现在主角手里而无来源交代；若确需首次出现，必须在本场景内交代它从哪来。
14. **用词搭配铁律**：禁止生造叠词/生造搭配（如"冷冷寒的""酸酸涩的"这类语法不通的堆叠）；比喻要贴合本体，金属类词（锈、生锈、锈蚀）只能用于金属器物，不得用于形容人的身体（禁"身子骨锈透"）。形容词要用规范、读者熟悉的搭配。
15. **题材爽感与详略铁律（服从本书 Canon，最重要）**：读者要的是代入主角，看他在本书核心世界里遇到具体机会、承受具体代价、做出具体选择并逐步翻身。若本书是网游/沙盒爽文，可写游戏世界的辽阔自由；若本书裁决为"表层游戏动机 + 真实物理坠入"，游戏元素只能服务入口误判，真正的爽感与压力必须落在写实仙侠现场。所以本场景——
   - 若写到武学/宝物/奇遇/变强：要**详写、写透、写出爽**——这门武学多厉害、施展时什么画面、威力如何、主角尝到什么甜头、比之前强多少（具体、可感）。这是读者花钱看的主菜，不许一笔带过。
   - 若写到武侠世界的风物景观：要写出**辽阔、瑰丽、高拟真、可探索**的向往感，让读者想钻进去。
   - 若写到打斗：要写**具体招式、攻防节奏、画面感**，不要抽象概括"打了一架"。
   - 若写到现实苦难（送外卖/催款/病情）：**只用最精炼一两笔交代动机**，绝不反复渲染惨、不铺陈。现实是动机燃料，不是主菜。
   - 情绪落点朝"希望、爽、变强、翻身"走，不朝"惨、恐惧、被吸命、背负担"走。金手指的代价只作暗线点到，绝不盖过变强的爽。

严格输出 JSON：{{"content":"这个场景的正文（可直接接在前文后面，正文内含分段换行）","note":"本场景完成的小变化"}}"""
    data, _ = _call_json(
        provider, prompt,
        # 2026-09-17 真机实测: kimi-k3 单元写作在 4000 上限下约半数单元 reasoning
        # 烧满额度、content 为空(0字单元); 8000 时 finish_reason=stop 正常出稿;
        # 偶发空响应/截断由 3 次尝试(_retries=2)吸收。
        max_tokens=max(max_tokens // 2, 8000), temperature=temperature, model=model,
        tag=f"B管道精写单元{unit.get('index')}",
        schema='{"content":"..","note":".."}',
        _retries=2,
    )
    return str(data.get("content") or "").strip()


def _force_paragraphs(text: str) -> str:
    """后处理兜底：把没分段的大坨拆成短段（按句末标点，超长无标点时硬切）。"""
    text = text.strip()
    paras = [p.strip() for p in text.split("\n") if p.strip()]
    out: list[str] = []
    for para in paras:
        if hanzi_count(para) <= 55:
            out.append(para)
            continue
        # 大段：按句末标点切成小句，再按 ~55 字打包成段，稳定满足番茄手机端段落密度。
        sentences = re.split(r"(?<=[。！？”])", para)
        buf = ""
        for sent in sentences:
            if not sent.strip():
                continue
            # 单句本身超长（无句末标点）：按 58 字硬切
            if hanzi_count(sent) > 80:
                if buf.strip():
                    out.append(buf.strip())
                    buf = ""
                out.extend(_hard_split(sent, 58))
                continue
            if hanzi_count(buf) + hanzi_count(sent) > 55 and buf:
                out.append(buf.strip())
                buf = sent
            else:
                buf += sent
        if buf.strip():
            out.append(buf.strip())
    return "\n\n".join(out)


def _extract_long_quotes(para: str, min_len: int = 12) -> list[str]:
    """提取段落内 ≥min_len 字的引号对白（中文弯引号/直引号），用于台词指纹去重。"""
    quotes = re.findall(r"[“\"]([^”\"]+)[”\"]", para)
    return [q for q in quotes if hanzi_count(q) >= min_len]


def _dedupe_adjacent_paragraphs(text: str, threshold: float = 0.82) -> str:
    """删除相邻的重复/高度相似段落（LLM 逐单元生成常在相邻段撞整句）。

    三层去重：
    1. 相邻窗口（前 2 段）：处理短句撞车（重复比喻）与紧邻整句复制。
    2. 全局长段扫描：整段 ≥30 字且相似度 ≥0.85，视为隔段整段复现。
    3. 长对白指纹：两段各含一句 ≥12 字引号台词且高度重合（≥0.9），
       即使外壳（动作前缀）不同也判为"换个动作又演一遍"的隔段重影
       （如报告厅提问台词换成"他站起来"/"苏晨转向台上"复现两次）。
    """
    from difflib import SequenceMatcher

    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) < 2:
        return text
    kept: list[str] = []
    kept_quotes: list[str] = []  # 已保留段落的长对白指纹池
    for para in paras:
        dup = False
        for prev in kept[-2:]:  # 只看前两段，防跨度过大误删
            # 短段(<6字对话/拟声)不参与去重，避免误删"两队。""接受。"这类
            if hanzi_count(para) < 6 or hanzi_count(prev) < 6:
                continue
            ratio = SequenceMatcher(None, prev, para).ratio()
            # 分级阈值：短句撞车(如重复比喻)用低阈值0.5；长句用高阈值0.82防误删呼应
            limit = 0.5 if max(hanzi_count(para), hanzi_count(prev)) < 35 else 0.82
            if ratio >= limit:
                dup = True
                break
        # 全局长段去重：仅对 ≥30 字长段，与已保留的全部长段比对，命中即视为隔段重影
        if not dup and hanzi_count(para) >= 30:
            for prev in kept:
                if hanzi_count(prev) < 30:
                    continue
                if SequenceMatcher(None, prev, para).ratio() >= 0.85:
                    dup = True
                    break
        # 长对白指纹去重：本段长台词若与此前任一长台词高度重合，判隔段重影
        para_quotes = _extract_long_quotes(para)
        if not dup and para_quotes:
            for q in para_quotes:
                for pq in kept_quotes:
                    if SequenceMatcher(None, pq, q).ratio() >= 0.9:
                        dup = True
                        break
                if dup:
                    break
        if not dup:
            kept.append(para)
            kept_quotes.extend(para_quotes)
    return "\n\n".join(kept)


def _hard_split(sent: str, size: int) -> list[str]:
    """无标点超长句按字数硬切（兜底，尽量在逗号/顿号处断）。"""
    pieces: list[str] = []
    buf = ""
    for ch in sent:
        buf += ch
        if hanzi_count(buf) >= size and ch in "，、；":
            pieces.append(buf.strip())
            buf = ""
        elif hanzi_count(buf) >= size + 25:  # 硬上限，防某段无任何标点
            pieces.append(buf.strip())
            buf = ""
    if buf.strip():
        pieces.append(buf.strip())
    return pieces


def _truncate_to_budget(text: str, budget: int) -> str:
    """按段落边界把超长单元截回预算内（保留完整段落，至少留 1 段）。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paras:
        return text
    kept: list[str] = []
    total = 0
    for para in paras:
        c = hanzi_count(para)
        if kept and total + c > budget:
            break
        kept.append(para)
        total += c
    return "\n\n".join(kept) if kept else paras[0]


def _trim_whole_to_budget(text: str, budget: int) -> str:
    """全文超预算时保头保尾：优先保留开头段落建立场景 + 最后一段(章末钩子)，
    从倒数第二段往前删中后段，直到总量落进预算。"""
    paras = [p.strip() for p in text.split("\n\n") if p.strip()]
    if len(paras) <= 2:
        return text
    last = paras[-1]  # 章末钩子必留
    body = paras[:-1]
    # 从后往前删 body 段，直到 body+last 总字数 <= budget
    while len(body) > 1 and sum(hanzi_count(p) for p in body) + hanzi_count(last) > budget:
        body.pop()  # 删 body 最后一段（最靠近结尾的中段）
    return "\n\n".join(body + [last])


_TITLE_ABSTRACT_BLOCKLIST = (
    "宿命", "因果", "机缘", "试探", "秘密", "决断", "抉择", "轮回", "命运",
    "regulation", "任务奖励", "等级提升", "版本更新", "系统提示",
)
# 番茄标题硬门（与 prompts.TITLE_STYLE_BLOCK 同源·D2 程序化守卫）：
#   长度 2-13 字（不含"第N章"前缀）· 禁纯抽象名词 · 禁系统腔 · 禁空标题
_TITLE_MIN_CHARS = 2
_TITLE_MAX_CHARS = 13


def _visible_title_len(title: str) -> int:
    """标题可见长度：去掉"第N章"前缀与首尾空白后的字符数（含标点/字母，符合番茄目录页显示口径）。"""
    t = re.sub(r"^第?\s*[0-9零一二三四五六七八九十百千]+\s*章\s*", "", (title or "").strip()).strip()
    return len(t)


def _title_quality_issues(title: str) -> list[str]:
    """确定性标题质检（零 LLM·零随机）：返回违规原因列表，空列表=合格。
    与 D1/D4 同一原则——纯规则判定，同一 title 永远同一结论。"""
    issues: list[str] = []
    raw = (title or "").strip()
    if not raw or raw in {"无题", "未命名", "标题"}:
        return ["empty_or_placeholder"]
    core = re.sub(r"^第?\s*[0-9零一二三四五六七八九十百千]+\s*章\s*", "", raw).strip()
    n = len(core)
    if n < _TITLE_MIN_CHARS:
        issues.append(f"too_short: {n} < {_TITLE_MIN_CHARS}")
    if n > _TITLE_MAX_CHARS:
        issues.append(f"too_long: {n} > {_TITLE_MAX_CHARS}")
    # 纯抽象/系统腔：整个标题就是一个 blocklist 词，或全是抽象名词无戏眼
    if core in _TITLE_ABSTRACT_BLOCKLIST:
        issues.append(f"abstract_only: {core}")
    if any(bad in core for bad in ("任务奖励", "等级提升", "版本更新", "系统提示")):
        issues.append("system_tone")
    return issues


# v25.12 升级版:在 v25 基础上加"戏眼度"+"反差度"硬校验
_DRAMATIC_VERBS = ("把", "丢", "砍", "杀", "夺", "抢", "破", "砸", "撞", "撕", "拽", "咳", "呕", "咽",
                   "吼", "怒", "哭", "笑", "笑", "砸", "掀", "扑", "倒", "伏", "跳", "扑", "砍", "骂",
                   "放", "挡", "拦", "震", "碎", "裂", "炸", "喷", "漏", "塞", "藏", "烧", "冻", "扇",
                   "摔", "掐", "捏", "舔", "咬", "磕", "锤", "堵", "闷", "戳", "切", "抽", "刮", "射")
_DRAMATIC_NOUNS = ("血", "骨", "尸", "首", "命", "魂", "心", "剑", "刀", "枪", "掌", "门", "山", "观",
                   "塔", "楼", "店", "会", "堂", "话", "梦", "信", "书", "纸", "丝", "发", "手", "眼",
                   "口", "牙", "眼", "针", "口", "袍", "灰", "玉", "金", "银", "铜", "铁", "霜", "雪",
                   "火", "水", "雷", "风", "雨", "雷", "鬼", "神", "佛", "魔", "尸", "兽", "兽", "头")
_TITLE_PARTICLES = ("的", "是", "在", "了", "和", "与", "但", "却", "仍", "还", "却", "又", "再")


def _title_quality_issues_v25(title: str) -> list[str]:
    """v25.12 标题质检升级:基础门 + 戏眼度 + 反差度。

    戏眼度:含具象物件(剑/掌/血/发) 或 强动作(把/丢/砍/抢) 或 对话(带引号)
    反差度:含转折词(却/但/却/又) 或 对比(前后 2 段不同方向)
    """
    issues = _title_quality_issues(title)
    if issues:
        return issues
    raw = (title or "").strip()
    core = re.sub(r"^第?\s*[0-9零一二三四五六七八九十百千]+\s*章\s*", "", raw).strip()
    # 戏眼度:必须含具象名词 OR 强动作 OR 对话
    has_noun = any(n in core for n in _DRAMATIC_NOUNS)
    has_verb = any(v in core for v in _DRAMATIC_VERBS)
    has_dialogue = '"' in core or '"' in core or '"' in core or '"' in core or '「' in core
    has_xiehou = any(p in core for p in _TITLE_PARTICLES)
    if not (has_noun or has_verb or has_dialogue or has_xiehou):
        issues.append("no_dramatic_anchor:缺具象物件/强动作/对话/转折词")
    # 反差度:含转折词
    if not any(p in core for p in ("却", "但", "却", "又", "再", "反", "偏", "就", "不")):
        # 不强制,但提示
        pass
    return issues


# v25.13 升级版:故事线概括 = 短(2-6 字) + 名词/属性词为主 + 无强动作
_STORY_ARC_KEYWORDS = (
    # 主题/状态/属性
    "卑微", "求援", "画皮", "节哀", "捡漏", "护花", "求援", "初入", "觉醒", "蜕变",
    "反噬", "暴露", "双线", "危机", "代价", "公开", "入门", "拜山", "赌局", "黑手",
    "副本", "通关", "陷阱", "反杀", "团灭", "逃生", "突破", "渡劫", "夺舍", "师门",
    "因果", "机缘", "暗战", "人质", "天劫", "入世", "传承", "秘籍", "山门", "道观",
    "求援", "求救", "反噬", "暗战", "觉醒", "赌局", "黑手",
    # 主题
    "代价", "选择", "暴露", "蜕变", "觉醒", "双线", "危机", "反噬", "公开", "拜山",
    "护花", "画皮", "卑微", "求援", "节哀", "捡漏", "实战", "爆裂", "拜山",
)
# 2026-09-21 第4.5步验收腿: 提示词/指令脚手架回声标题黑名单。
# ch3 首稿标题「用户要求」漏过全部门(4字、无动作词、退化分支恰好不拦)——
# 这类词是指令文本碎片,永远不可能是故事标题,优先于一切其他检查硬拒。
_SCAFFOLD_ECHO_TITLE_TERMS = (
    "用户要求", "用户指令", "用户需求", "用户提示", "系统提示", "系统要求",
    "修订要求", "修订指令", "章节要求", "写作要求", "创作要求", "质检要求",
    "任务要求", "输出要求", "根据要求", "按要求", "以上要求", "上述要求",
    "提示词", "指令如下", "要求如下", "注意事项", "输出格式",
)
# v25.13 禁用"动作/场景"特征词
_ACTION_VERBS_TITLE = (
    "把", "丢", "砍", "撕", "拽", "咳", "呕", "咽", "吼", "砸", "撞", "扑", "倒",
    "摔", "掐", "捏", "咬", "磕", "锤", "堵", "戳", "切", "抽", "刮", "射", "骂",
    "掀", "跳", "骂", "扇", "蹦", "跳",
)


def _title_quality_issues_v25_13(title: str) -> list[str]:
    """v25.13 标题质检:基础门 + 故事线概括(2-6 字 + 主题/状态词 + 无强动作)。

    治"标题是动作场景"问题。
    """
    issues = _title_quality_issues(title)
    if issues:
        return issues
    raw = (title or "").strip()
    core = re.sub(r"^第?\s*[0-9零一二三四五六七八九十百千]+\s*章\s*", "", raw).strip()

    # 脚手架回声硬拒(优先于长度/词性检查)
    if any(term in core for term in _SCAFFOLD_ECHO_TITLE_TERMS):
        issues.append(f"scaffold_echo:{core} 是指令/提示词碎片,不是故事标题")
        return issues

    # 长度:故事线概括必须短 2-6 字
    if len(core) > 6:
        issues.append(f"too_long_for_story_arc: {len(core)} > 6 字(故事线概括必须短)")
        return issues
    if len(core) < 2:
        issues.append(f"too_short: {len(core)} < 2")
        return issues

    # 禁强动作:有动作动词 → 必是动作场景
    if any(v in core for v in _ACTION_VERBS_TITLE):
        # 但允许"被 X"结构
        if not (core.startswith("被") or "被" in core[:2]):
            issues.append(f"action_verb_detected:含强动作 {core},标题应是故事线概括")

    # 必含故事线关键词(主题/状态/属性/事件) - 至少 1 个
    if not any(kw in core for kw in _STORY_ARC_KEYWORDS):
        # 退化到:含 1 个名词 + 1 个状态/事件修饰
        has_status = any(c in core for c in ("的", "却", "是", "在", "与", "和", "了", "又"))
        if not has_status and len(core) < 4:
            issues.append(f"not_story_arc:{core},缺故事线核心词(主题/事件/状态/属性)")

    return issues


def _fallback_title_from_content(full_text: str, chapter_number: int | None = None) -> str:
    """标题兜底（确定性·无 LLM）：LLM 起标题反复不合格时，从正文首段抽取一个
    动作/对话短句，裁到 8-12 字。保证永远产出一个合规长度、非占位的标题，
    杜绝『无题』或超长标题流入番茄目录页。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n+", full_text or "") if p.strip()]
    candidate = ""
    for para in paras[:3]:
        # 优先取带对话/动作的短句
        for sent in re.split(r"[。！？!?\n]", para):
            s = sent.strip().strip("　 ")
            hz = hanzi_count(s)
            if 6 <= len(s) <= 12 and hz >= 4:
                candidate = s
                break
        if candidate:
            break
    if not candidate and paras:
        head = re.sub(r"\s+", "", paras[0])
        candidate = head[:11]
    candidate = candidate[:_TITLE_MAX_CHARS].strip("，,。.、；;：: ")
    if len(candidate) < _TITLE_MIN_CHARS:
        candidate = f"第{chapter_number}章风波" if chapter_number else "变局骤起"
    return candidate


def _load_anchor_titles_by_genre(genre: str | None, *, k: int = 8) -> list[str]:
    """v25.13 标题优化:从 knowledge_anchors 按题材对位抽真爆款标题作 few-shot。

    v25.13 关键变化:抽"故事线概括"类标题(2-6 字),不抽"场景动作"类。
    题材对位策略:
    - 网游/无限流/系统 → 都市系统 + 无限流 + 玄幻
    - 玄幻/修真/仙侠 → 修真 + 玄幻
    - 都市/言情/悬疑 → 都市悬疑 + 都市系统
    - 其他 → 全题材抽
    """
    try:
        con = sq.connect("/home/frank/ai-novel-system-v2/data/novel.db")
        cur = con.cursor()
        g = (genre or "").lower()
        if any(kw in g for kw in ("网游", "无限流", "系统", "游戏", "惊悚", "末日")):
            tag_filter = "theme_tag IN ('都市系统', '无限流', '玄幻')"
        elif any(kw in g for kw in ("玄幻", "修真", "仙侠", "武侠")):
            tag_filter = "theme_tag IN ('修真', '玄幻')"
        elif any(kw in g for kw in ("都市", "言情", "悬疑", "现实")):
            tag_filter = "theme_tag IN ('都市悬疑', '都市系统', '穿越古言')"
        else:
            tag_filter = "1=1"
        # v25.13 修复:锚点真爆款都带"第N章",要剥掉"第N章"抽 2-6 字核心词
        cur.execute(f"""
            SELECT chapter_title FROM knowledge_anchors
            WHERE {tag_filter}
              AND chapter_title != ''
              AND chapter_title != '开始阅读'
            ORDER BY RANDOM() LIMIT ?
        """, (k * 4,))  # 多抽些,剥前缀后够 k
        titles = []
        for (t,) in cur.fetchall():
            t = re.sub(r"^第?\s*[0-9零一二三四五六七八九十百千]+\s*[\.、章\s]+\s*", "", (t or "")).strip()
            # 只要 2-6 字核心词
            if 2 <= len(t) <= 6 and not t.startswith("开始阅读") and not t.isdigit():
                titles.append(t)
                if len(titles) >= k:
                    break
        con.close()
        return titles[:k]
    except Exception as _e:
        print(f"[B-pipe][load_anchor_titles] 失败: {_e}", flush=True)
        return []


def _load_chapter_summary(full_text: str) -> str:
    """v25.13:让 LLM 用 1 句话概括本章故事线,作为起标题依据。

    用 LLM 1 次拿 story_arc(避免"看前 600 字就拍脑袋起标题")。
    """
    try:
        from app.llm.providers import get_provider
        from app.core.config import settings
        provider = get_provider(False)
        # 强约束 JSON 输出
        resp = provider.generate(
            f"用 15-25 个字概括这段小说正文的核心故事线(谁遇到了什么变化,代价/结果是什么):\n\n{full_text[:1500]}\n\n严格输出 JSON: {{\"summary\": \"故事线概括\"}}",
            max_tokens=80, temperature=0.3, model=settings.llm_draft_model,
        )
        import json as _json
        import re as _re
        text = resp.text or ""
        # 先剥 ```json 包装
        text = _re.sub(r"```json\s*", "", text)
        text = _re.sub(r"```\s*", "", text)
        try:
            data = _json.loads(text)
            return str(data.get("summary") or data.get("story_arc") or "").strip()
        except Exception:
            # 提取 JSON 部分
            m = _re.search(r'\{[^}]*"(?:summary|story_arc)"\s*:\s*"([^"]+)"', text)
            if m:
                return m.group(1).strip()
            # 兜底:取前 50 字
            return text.strip()[:50] if text else ""
    except Exception as _e:
        print(f"[B-pipe][load_chapter_summary] 失败: {_e}", flush=True)
        # 确定性兜底:取前 50 字
        import re as _re
        clean = _re.sub(r"\s+", "", full_text or "")
        return clean[:50] if clean else ""


def generate_title(provider, full_text, *, max_tokens, temperature, model, chapter_number: int | None = None, genre: str | None = None) -> str:
    """D2 切页命名守卫 v25.13(2026-08-07):

    核心认知(v25.13 修复):标题 = 故事线概括(主题/事件/状态),不是主角动作/场景。
    例: 卑微/求援/画皮/节哀/捡漏/实战考试/护花使者/大道蝗虫/爆裂末班车

    A. 故事线概括先于标题生成(LLM 1 次提取 story_arc)
    B. few-shot 注入真爆款"故事线类"标题(2-6 字)
    C. 结构性引导:故事线核心词 = 主语+状态/事件/属性(2-6 字)
    D. 评分升级:基础门 + 概括性(短+抽象但具体) + 主题对位
    E. 3 次重试 + 兜底

    治"标题难听"+ "标题应是故事线概括而非动作场景"。
    """
    # 1. 先抽故事线概括(LLM 1 次)
    story_arc = _load_chapter_summary(full_text)

    # 2. few-shot 真爆款标题(题材对位)
    _few_shot = _load_anchor_titles_by_genre(genre, k=8)
    _few_shot_block = ""
    if _few_shot:
        _few_shot_block = "\n\n真爆款标题参考(故事线概括类, 严禁照抄要换词):\n" + "\n".join(f"- {t}" for t in _few_shot)

    def _ask(extra: str = "") -> str:
        _prompt = f"""你是番茄网文起标题专家。只输出 1 个标题,严格 JSON。

【核心认知】标题 = 故事线概括(主题/事件/状态/属性),不是主角动作或场景。
- 故事线:主角这章遇到什么变化/代价/选择/暴露/觉醒/危机
- 不是"谁做了什么动作"而是"这章在讲什么"

【好标题范例(故事线概括)】
- 卑微 (主题:低姿态) | 求援 (事件:求助) | 画皮 (状态:伪装)
- 节哀 (主题:告别) | 捡漏 (事件:占便宜) | 实战考试 (事件:考验)
- 护花使者 (属性:守护) | 大道蝗虫 (主题:贪婪群像) | 爆裂末班车 (事件:末日逃亡)
- 初入 (状态:入门) | 求救 (事件:求助) | 觉醒 (状态:蜕变)
- 反噬 (主题:代价) | 暴露 (主题:秘密外泄) | 双线 (状态:并线)

【反例(动作/场景类,严禁)】
- 张口要绵掌,反手丢木剑 (动作)
- 白霜爬舱,跪碎青石 (场景画面)
- 师父,你藏得我真疼 (对话)
- 他还在刨破烂 (动作延续)

【硬门】
- 长度 2-6 字(故事线概括必须短!)
- 不要"第N章"前缀
- 必含故事线核心词(主题/事件/状态/属性)
- 禁纯抽象(宿命/命运/轮回)
- 禁动作场景(谁做了什么具体动作)
- 禁系统腔

【本章故事线概括】{story_arc or '(无)'}
【本章正文前 800 字】{full_text[:800]}{_few_shot_block}
【任务】{extra}只输出: {{"title": "标题"}}"""
        data, _ = _call_json(
            provider,
            _prompt,
            max_tokens=min(max_tokens, 200), temperature=temperature, model=model,
            tag="B管道起标题v25.13", schema='{"title":".."}',
        )
        return str(data.get("title") or "").strip()

    title = _ask("")
    if not _title_quality_issues_v25_13(title):
        return title
    # 重试 1:指明问题
    issues = _title_quality_issues_v25_13(title)
    retry = _ask(f"上次的标题「{title}」不合格({';'.join(issues)})。要的是故事线概括(2-6 字,如'卑微/求援/暴露/反噬'),不是动作场景。重起。")
    if not _title_quality_issues_v25_13(retry):
        return retry
    # 重试 2:更强约束
    retry2 = _ask(f"上两次「{title}」「{retry}」都太动作化/场景化。这次必须 2-6 字,故事线概括词,如 '代价/暴露/反噬/觉醒/蜕变/双线/危机' 之类。")
    if not _title_quality_issues_v25_13(retry2):
        return retry2
    # 兜底:故事线概括词表
    return _fallback_title_story_arc(story_arc, chapter_number)


# v25.13 故事线词表
_STORY_ARC_FALLBACKS = {
    "网游": ["代价", "暴露", "觉醒", "双线", "反噬", "公开", "入门", "拜山", "赌局", "黑手"],
    "无限流": ["副本", "通关", "陷阱", "暴露", "觉醒", "危机", "反杀", "团灭", "逃生"],
    "修真": ["突破", "渡劫", "夺舍", "反噬", "暴露", "觉醒", "拜山", "师门", "因果", "机缘"],
    "都市": ["暴露", "觉醒", "代价", "反噬", "赌局", "黑手", "暗战", "人质", "反杀", "双线"],
    "玄幻": ["突破", "夺舍", "渡劫", "反噬", "觉醒", "师门", "山门", "因果", "天劫", "入世"],
}


def _fallback_title_story_arc(story_arc: str, chapter_number: int | None) -> str:
    """v25.13 故事线兜底:从 story_arc 抽核心词,没就随机取词表。"""
    if story_arc:
        import re as _re
        # 抽 2-6 字核心词
        for length in [4, 3, 2, 5, 6]:
            for i in range(len(story_arc) - length + 1):
                candidate = story_arc[i:i+length]
                if _re.search(r"[一-鿿]", candidate) and not any(c in "的了是在和与" for c in candidate):
                    if 2 <= len(candidate) <= 6:
                        return candidate
    # 实在抽不出,取词表
    return "代价"


def generate_draft_via_b_pipeline(
    provider,
    *,
    book_title: str,
    genre: str,
    premise: str,
    goal: str,
    required_beats: str,
    constraints: str,
    canon_context: str,
    previous_chapter_context: str,
    target_min_chars: int,
    target_max_chars: int,
    target_unit_count: int,
    max_tokens: int,
    temperature: float | None,
    model: str | None,
    chapter_number: int | None = None,
) -> tuple[DraftOutput, dict]:
    """B 管道主入口：返回 (DraftOutput, meta)。

    meta 记录单元数、每单元字数、检测到的"不是A是B"处数等，供落库审计。

    v25.11(2026-08-07): 加进度回报 + 总 LLM 调超时 70 分钟自动暂停。
    """
    import time as _time
    _t_total = _time.time()
    _HARD_TIMEOUT_S = 70 * 60  # 70 分钟硬超时
    _PHASE_TIMEOUT_S = 12 * 60  # 单阶段 12 分钟(防单 LLM 调 hang)
    print(f"[B-pipe] ▶ 开始 {book_title} ch{chapter_number} 单元数={target_unit_count or 6} model={model}", flush=True)
    isolated_inputs = isolate_generation_inputs(
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
        previous_chapter_context=previous_chapter_context,
    )
    goal = isolated_inputs.goal
    required_beats = isolated_inputs.required_beats
    constraints = isolated_inputs.constraints
    canon_context = isolated_inputs.canon_context
    previous_chapter_context = isolated_inputs.previous_chapter_context
    if isolated_inputs.warnings:
        print(f"[B-pipe] prompt isolation cleaned {len(isolated_inputs.warnings)} stale lines", flush=True)

    unit_hint = max(6, min(7, target_unit_count or 6))
    print(f"[B-pipe] [1/3] build_beatsheet 启动 ...", flush=True)
    _t_phase = _time.time()
    units = build_beatsheet(
        provider,
        book_title=book_title, genre=genre, premise=premise, goal=goal,
        required_beats=required_beats, constraints=constraints,
        canon_context=canon_context,
        previous_chapter_context=previous_chapter_context,
        unit_count_hint=unit_hint,
        max_tokens=max_tokens, temperature=temperature, model=model,
        chapter_number=chapter_number,
    )
    print(f"[B-pipe] [1/3] build_beatsheet 完成 {len(units)} 单元 耗时={_time.time()-_t_phase:.0f}s", flush=True)
    if not units:
        raise ValueError("B pipeline: beat sheet 为空，无法逐单元生成")
    min_units = max(3, min(unit_hint, 5))
    beatsheet_passed, beatsheet_issues = evaluate_beatsheet_quality(
        units,
        min_units=min_units,
        story_bible_text=constraints,
        canon_context=canon_context,
        constraints=constraints,
        chapter_number=chapter_number,
    )
    for retry_index in range(1, 3):
        if beatsheet_passed:
            break
        print(
            f"[B-pipe] [1/3] beat sheet 门禁失败 {beatsheet_issues[:8]}，强化约束后重试 {retry_index}/2",
            flush=True,
        )
        retry_required_beats = _beatsheet_retry_beats(
            required_beats,
            beatsheet_issues,
            unit_hint=unit_hint,
            attempt=retry_index,
        )
        _t_phase = _time.time()
        units = build_beatsheet(
            provider,
            book_title=book_title, genre=genre, premise=premise, goal=goal,
            required_beats=retry_required_beats, constraints=constraints,
            canon_context=canon_context,
            previous_chapter_context=previous_chapter_context,
            unit_count_hint=unit_hint,
            max_tokens=max_tokens, temperature=temperature, model=model,
            chapter_number=chapter_number,
        )
        print(f"[B-pipe] [1/3] build_beatsheet 重试{retry_index}/2完成 {len(units)} 单元 耗时={_time.time()-_t_phase:.0f}s", flush=True)
        beatsheet_passed, beatsheet_issues = evaluate_beatsheet_quality(
            units,
            min_units=min_units,
            story_bible_text=constraints,
            canon_context=canon_context,
            constraints=constraints,
            chapter_number=chapter_number,
        )
    if not beatsheet_passed:
        raise ValueError(f"B pipeline: beat sheet 门禁失败，拒绝进入精写：{beatsheet_issues[:10]}")
    print(f"[B-pipe] [1/3] beat sheet 门禁通过 issues={beatsheet_issues[:4]}", flush=True)

    unit_min, unit_max = _budget_per_unit(target_min_chars, target_max_chars, len(units))

    parts: list[str] = []
    unit_meta: list[dict] = []
    full = ""
    print(f"[B-pipe] [2/3] 写 {len(units)} 单元 单元预算 {unit_min}-{unit_max} 字 ...", flush=True)
    for i, unit in enumerate(units):
        # 总超时 / 单阶段超时检测
        if _time.time() - _t_total > _HARD_TIMEOUT_S:
            raise TimeoutError(
                f"B 管道总耗时超 {_HARD_TIMEOUT_S//60} 分钟, "
                f"已写到 {i}/{len(units)} 单元, 自动暂停"
            )
        _t_unit = _time.time()
        prev_tail = full[-300:] if full else ""
        next_scene = str(units[i + 1].get("scene") or "") if i + 1 < len(units) else "（本章结束）"
        text = write_unit(
            provider,
            book_title=book_title, genre=genre, premise=premise, chapter_goal=goal,
            unit=unit, prev_tail=prev_tail, next_scene=next_scene,
            canon_context=canon_context,
            constraints=constraints,
            is_first=(i == 0), is_last=(i == len(units) - 1),
            unit_min=unit_min, unit_max=unit_max,
            max_tokens=max_tokens, temperature=temperature, model=model,
            chapter_number=chapter_number,
        )
        text = _force_paragraphs(text)
        unit_gate = evaluate_unit_quality(
            text,
            unit=unit,
            prev_tail=prev_tail,
            unit_min=unit_min,
            unit_max=unit_max,
            story_bible_text=constraints,
            canon_context=canon_context,
            constraints=constraints,
            is_first=(i == 0),
            is_last=(i == len(units) - 1),
        )
        structure_contract, structure_report = _unit_structure_repair_contract(
            text,
            unit_min=unit_min,
            unit_max=unit_max,
        )
        unit_rewrite_attempted = False
        structure_rewrite_attempted = False
        unit_rewrite_rounds = 0
        for repair_round in range(1, 3):
            if unit_gate.passed and not structure_contract:
                break
            unit_rewrite_attempted = True
            unit_rewrite_rounds += 1
            structure_rewrite_attempted = structure_rewrite_attempted or bool(structure_contract)
            round_contract = (
                f"【第{repair_round}轮单元局部返修】\n"
                "本轮必须修到单元硬门禁通过，并清除单元结构弱项；不要只润色句子。\n"
            )
            failure_contract = "\n".join(
                part for part in (round_contract, unit_repair_contract(unit_gate), structure_contract) if part
            )
            try:
                repair_text = rewrite_failed_unit(
                    provider,
                    book_title=book_title, genre=genre, unit=unit, failed_text=text,
                    failure_contract=failure_contract,
                    prev_tail=prev_tail, next_scene=next_scene, canon_context=canon_context,
                    constraints=constraints,
                    unit_min=unit_min, unit_max=unit_max,
                    max_tokens=max_tokens, temperature=temperature, model=model,
                )
            except Exception as _repair_exc:
                # 2026-09-17 真机实测: 返修 LLM 调用本身可能多次失败(空响应/截断)
                # 而抛 StructuredOutputError; 不得让单个单元的返修异常杀掉整章,
                # 丢弃本轮、交给下一轮返修; 原文若硬违规仍由循环后终检兜底。
                print(
                    f"[B-pipe] 单元{i+1} 第{repair_round}轮返修调用失败, 跳过本轮: {_repair_exc}",
                    flush=True,
                )
                continue
            repair_text = _force_paragraphs(repair_text)
            repair_gate = evaluate_unit_quality(
                repair_text,
                unit=unit,
                prev_tail=prev_tail,
                unit_min=unit_min,
                unit_max=unit_max,
                story_bible_text=constraints,
                canon_context=canon_context,
                constraints=constraints,
                is_first=(i == 0),
                is_last=(i == len(units) - 1),
            )
            if _has_hard_story_bible_issue(repair_gate.issues):
                # 2026-09-17 真机实测: 返修稿硬违规(失控输出混入废弃设定词)不得接受,
                # 但立即整章放弃会让 40 分钟 Draft 被单个失控单元杀掉;
                # 改为丢弃该返修稿、留给下一轮返修(温度采样)机会,
                # 全部轮次仍违规由循环后的终检(下文 raise)兜底, 安全性不变。
                print(
                    f"[B-pipe] 单元{i+1} 第{repair_round}轮返修稿硬违规已拒收: "
                    f"{repair_gate.issues[:4]}",
                    flush=True,
                )
                continue
            repair_structure_contract, repair_structure_report = _unit_structure_repair_contract(
                repair_text,
                unit_min=unit_min,
                unit_max=unit_max,
            )
            hard_improved = len(repair_gate.issues) < len(unit_gate.issues)
            structure_improved = _structure_issue_count(repair_structure_report) < _structure_issue_count(structure_report)
            accept_clean_repair = repair_gate.passed and not repair_structure_contract
            accept_better_passed_repair = repair_gate.passed and (hard_improved or structure_improved or not unit_gate.passed)
            accept_less_bad_hard_repair = (not unit_gate.passed) and hard_improved and (
                _structure_issue_count(repair_structure_report) <= _structure_issue_count(structure_report)
            )
            if accept_clean_repair or accept_better_passed_repair or accept_less_bad_hard_repair:
                text = repair_text
                unit_gate = repair_gate
                structure_contract = repair_structure_contract
                structure_report = repair_structure_report
                continue
            break
        if _has_hard_story_bible_issue(unit_gate.issues):
            raise ValueError(f"B pipeline: 单元{i+1} 违反 Story Bible，拒绝继续：{unit_gate.issues[:6]}")
        # 字数硬控：非结尾单元超预算 1.05 倍即按段落边界截回（结尾单元豁免以保钩子）
        if not (i == len(units) - 1) and hanzi_count(text) > int(unit_max * 1.05):
            text = _truncate_to_budget(text, unit_max)
        parts.append(text)
        _unit_elapsed = _time.time() - _t_unit
        print(
            f"[B-pipe]   [{i+1}/{len(units)}] 写完 {hanzi_count(text)} 字 "
            f"耗时={_unit_elapsed:.0f}s 总={_time.time()-_t_total:.0f}s",
            flush=True,
        )
        full = "\n\n".join(parts)
        unit_meta.append({
            "index": unit.get("index", i + 1),
            "scene": str(unit.get("scene"))[:60],
            "chars": hanzi_count(text),
            "gate_passed": unit_gate.passed,
            "gate_issues": unit_gate.issues[:6],
            "gate_warnings": unit_gate.warnings[:6],
            "rewrite_attempted": unit_rewrite_attempted,
            "rewrite_rounds": unit_rewrite_rounds,
            "structure_rewrite_attempted": structure_rewrite_attempted,
            "structure_score": structure_report.get("score"),
            "structure_issues": (structure_report.get("issues") or [])[:8],
        })

    content = _force_paragraphs("\n\n".join(parts))
    content = _dedupe_adjacent_paragraphs(content)
    # 全文总量兜底：超 target_max 时保头保尾（最后一段=章末钩子必留），从中后段删起
    if hanzi_count(content) > target_max_chars:
        content = _trim_whole_to_budget(content, target_max_chars)
    # ★确定性后处理器(B-3/B-4): C1游戏黑话/C2不是X是Y/C3拐杖意象,改了永不反弹
    from app.services.chapter_lint_fixer import fix_chapter_text
    content, lint_stats = fix_chapter_text(content)
    content = _force_paragraphs(content)  # 修复后重新规整分段
    bushi_hits = len(_BUSHI_RE.findall(content))
    title = generate_title(provider, content, max_tokens=max_tokens, temperature=temperature, model=model, chapter_number=chapter_number)
    print(
        f"[B-pipe] [3/3] 标题生成完成 title={title!r} 耗时={_time.time()-_t_total:.0f}s",
        flush=True,
    )

    draft = DraftOutput(
        title=title,
        content=content,
        self_check=[
            f"B管道逐单元精写：{len(units)}单元，共{hanzi_count(content)}中文字符。",
            f"单元即时审核：失败即局部重写最多2轮，最终未过单元 {sum(1 for item in unit_meta if not item.get('gate_passed'))} 个。",
            f"单元结构弱项定向返修 {sum(1 for item in unit_meta if item.get('structure_rewrite_attempted'))} 次。",
            f"每单元字数窗口{unit_min}-{unit_max}，目标区间{target_min_chars}-{target_max_chars}。",
            f"确定性后处理: C1游戏黑话{lint_stats['c1_jargon']}处/C2不是X是Y{lint_stats['c2_bushi']}处/C3拐杖意象{lint_stats['c3_crutch']}处 已清除。",
        ],
        used_brief_points=[goal[:80]] if goal else [],
    )
    meta = {
        "pipeline": "b_pipeline",
        "unit_count": len(units),
        "unit_budget": {"min": unit_min, "max": unit_max},
        "units": unit_meta,
        "unit_gate_failed_count": sum(1 for item in unit_meta if not item.get("gate_passed")),
        "unit_gate_rewrite_count": sum(1 for item in unit_meta if item.get("rewrite_attempted")),
        "unit_structure_rewrite_count": sum(
            1 for item in unit_meta if item.get("structure_rewrite_attempted")
        ),
        "total_chars": hanzi_count(content),
        "bushi_ab_hits": bushi_hits,
        "lint_fixed": lint_stats,
        "prompt_isolation": {
            "cleaned": bool(isolated_inputs.warnings),
            "warnings": isolated_inputs.warnings[:12],
        },
        "model": model,
        "anchor_hits": [],
        "anchor_fallback": False,
        "elapsed_seconds": round(_time.time() - _t_total, 1),
    }
    print(
        f"[B-pipe] ✅ 全部完成 单元={len(units)} 字数={hanzi_count(content)} "
        f"总耗时={_time.time()-_t_total:.0f}s",
        flush=True,
    )
    return draft, meta
