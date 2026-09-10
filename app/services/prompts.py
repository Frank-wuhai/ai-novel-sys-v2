from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PromptTemplate
from app.services.humanized_production import humanized_process_text, humanized_revision_method_text, humanized_unit_method_text


HUMANIZED_PROCESS_BLOCK = humanized_process_text()
HUMANIZED_UNIT_BLOCK = humanized_unit_method_text()
HUMANIZED_REVISION_BLOCK = humanized_revision_method_text()


# 2026-07-22 新增：筛选版范式库 few-shot 正例注入
#   数据源：writing_paradigm_curated.json（25章真爆款白名单·凡骨/十日终焉/我在精神病院学斩神/我不是戏神/诸神愚戏）
#   目的：与其堆禁令告诉模型"别写成AI"，不如直接甩真爆款原文让它模仿质感。
_CURATED_PARADIGM_PATH = Path(__file__).resolve().parents[2] / "reference_corpus" / "writing_paradigm_curated.json"


def _load_curated_exemplars() -> str:
    """读筛选版范式库·格式化成 few-shot 正例块。文件缺失时返回空串（优雅降级）。"""
    try:
        data = json.loads(_CURATED_PARADIGM_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return ""
    lines: list[str] = []
    books = data.get("meta", {}).get("whitelist_books", [])
    lines.append(
        "【真爆款范文·质感参照 · 必读】\n"
        f"以下是番茄/起点验证过的头部作品（{('、'.join(books))}）的真实原文片段。\n"
        "它们是你要模仿的\"质感基准\"——不是让你抄情节，而是让你写出这种：\n"
        "画面能被读者看见、情绪落在具体身体动作上、对白有声线、叙述不堆标签。\n"
        "对照这些正文，检查你自己的每一段是否达到同样的现场感。\n"
    )
    dim_labels = {
        "opening_hook": "开篇钩子（怎么第一句就把读者拽进现场）",
        "emotion_grounding": "情绪落地（情绪长在身体动作/细节上，不是'他很紧张'）",
        "protagonist_inner_voice": "主角内心声线（自然、有个性，不是分析腔）",
        "character_voice": "配角声线（带身份、欲望、旧怨）",
        "setting_delivery": "设定交付（设定嵌进事件，不是百科式铺陈）",
        "rhythm_and_restraint": "节奏与留白（张弛有度，不是每句一段的碎句流）",
    }
    micro = data.get("micro", {})
    for dim, label in dim_labels.items():
        anchors = micro.get(dim, {}).get("best_anchors", [])[:3]
        if not anchors:
            continue
        lines.append(f"\n▸ {label}：")
        for a in anchors:
            lines.append(f"   · {a}")
    # 剧情因果/钩子
    pm = data.get("plot_mechanics", {}).get("best_anchors", [])[:3]
    if pm:
        lines.append("\n▸ 剧情推进与章末钩子（事件环环相扣·结尾留具体悬念）：")
        for a in pm:
            lines.append(f"   · {a}")
    return "\n".join(lines)


PARADIGM_EXEMPLARS_BLOCK = _load_curated_exemplars()


# 2026-07-29 新增：题材匹配·整段原文范文注入（去 AI 味核心）
#   数据源：writing_style_refs.json（对标书前几章真实正文·整段血肉，非碎锚点句）
#   与 PARADIGM_EXEMPLARS_BLOCK 的区别：那个是全局通用碎句，这个是按题材匹配的【整段原文】，
#   让模型直接照着对标书的文风/桥段质感/叙事节奏写，而不是照转写后的规则条文写。
_STYLE_REFS_PATH = Path(__file__).resolve().parents[2] / "reference_corpus" / "writing_style_refs.json"


def _load_style_refs() -> tuple[list[str], list[dict]]:
    """读题材范文库·返回 (genre_tags, exemplars)。文件缺失时返回空（优雅降级）。"""
    try:
        data = json.loads(_STYLE_REFS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return [], []
    tags = data.get("meta", {}).get("genre_tags", []) or []
    exemplars = data.get("exemplars", []) or []
    return tags, exemplars


_STYLE_REF_TAGS, _STYLE_REF_EXEMPLARS = _load_style_refs()


def style_refs_block_for_genre(genre: str | None) -> str:
    """按题材返回整段原文范文块。题材不匹配或无范文时返回空串。

    只在题材命中 genre_tags 时注入，避免给不相关题材喂错味儿。"""
    if not _STYLE_REF_EXEMPLARS:
        return ""
    g = (genre or "").strip()
    if not g:
        return ""
    if not any(tag in g or g in tag for tag in _STYLE_REF_TAGS):
        return ""
    src = "、".join(
        json.loads(_STYLE_REFS_PATH.read_text(encoding="utf-8")).get("meta", {}).get("source_books", [])
    ) if _STYLE_REFS_PATH.exists() else ""
    lines = [
        "【同题材爆款·整段原文范文 · 照这个味儿写】",
        f"下面是与本作同题材的头部网文（{src}）前几章的**真实原文整段**。",
        "这是你要模仿的“味儿”基准——不是抄情节，是学它的：",
        "内心声线怎么像活人吐槽（而不是分析腔）、系统提示怎么带调侃、",
        "穷困开局怎么写得真实不悬浮（具体的钱/干粮/借贷，不是抽象的“艰难”）、",
        "配角怎么各有腔调、设定怎么嵌进事件自然交付。",
        "对照这些原文，让你写的每一段都有同样的现场感和网感，去掉书面 AI 腔。",
    ]
    for ex in _STYLE_REF_EXEMPLARS:
        lines.append(f"\n▸ {ex.get('dim', '')}")
        why = ex.get("why", "")
        if why:
            lines.append(f"  （看点：{why}）")
        lines.append("  ——原文——")
        for para in str(ex.get("text", "")).split("\n"):
            if para.strip():
                lines.append(f"  {para}")
    return "\n".join(lines)


# 2026-07-16 新增：章节标题吸睛规范
# 2026-07-19 v2 更新：基于番茄爆款 17000+ 章标题量化统计重写
#   - 数据源：凡骨(4024章·72分) / 网游武侠金色词条(952章·66分) / 校园洪荒(70分)
#   - 核心发现：8-12 字·双句式 99.7% · 老版 12-16 字太长
# 问题：过去 LLM 生成的标题多是"试探""代价""七天""铁指环"这类抽象/空洞的词，
# 番茄目录页里读者划过时完全没有点击欲望。
# 规范强制标题必须带戏眼+口语+反差，模仿网文头部作品的钩子风格。
TITLE_STYLE_BLOCK = """【章节标题硬要求 · 必须每章遵守 · 数据基于番茄 17000+ 章爆款统计】
标题决定读者在番茄目录页要不要点进来。垃圾标题 = 0 点击 = 0 收益。

★ 铁律 1【字数】：**8-12 字**（不含"第N章 "前缀），11 字最佳，禁止 >13 字。
   数据来源：凡骨均值 11.8 字·金色词条均值 9.7 字·校园洪荒均值 8.7 字。

★ 铁律 2【句式 · 二选一】：
   A. **双句式**（推荐 · 占爆款 99.7%）
      - 前 3-5 字：场景/物件/事件锚点
      - 中间用「，」隔开
      - 后 4-6 字：主角反应/悬念/反差
      - 例：「送趟镖，他让我别打开」「他考我，我瞄他鞋底红泥」「思过崖一夜，起手式全错」
   B. **单句+情绪符**（?！...）
      - 主角内心吐槽 / 悬念钩子
      - 例：「谁在查我」「这买卖有点亏」「走路都收不住」

★ 铁律 3【禁用】：
   ✗ 纯名词罗列：「规则惩罚」「纯阳导引术」「矿工装备」「代价」
   ✗ 抽象概念：「宿命」「因果」「机缘」「试探」「秘密」「决断」
   ✗ 系统腔/游戏腔：「任务奖励」「等级提升」「版本更新」
   ✗ 剧透关键钩子：不要在标题里把本章高潮点说破，保留悬念

★ 铁律 4【本书特调 · 武侠+网游+主角吐槽】：
   - 主角林北是大三计算机系学生·意外把游戏武功带进现实
   - 60% 双句式（武侠场景钩子）+ 40% 单句情绪型（口语吐槽）
   - 反差感优先：现实/游戏、稳/慌、装/怕 之间的对比

★ 铁律 5【同书查重】：不能和本书前面任何章节标题重复或高度相似（番茄 API 返回 -3011 拒收）。

✅ 好例子（Ch29-49 定稿·参照）：
- 第29章 念错一字，老乞丐差点杀我
- 第32章 这趟镖，送的是我自己
- 第39章 刚入内门，先挨一顿毒打
- 第41章 他考我，我瞄他鞋底红泥
- 第42章 挖矿时，师兄把我底裤套穿
- 第48章 数据流小陈，锁定我了
- 第49章 巷子里，他识破我像NPC

❌ 坏例子（不要这么写 · 都是被 revise 掉的老标题）：
- 第14章 规则惩罚          ← 纯名词 · 空洞
- 第21章 纯阳导引术        ← 功法名 · 无戏眼
- 第24章 饭钱三十两        ← 信息量不够
- 第30章 顾剑棠的代价      ← "代价"是禁用词
- 第38章 两个道士抱拳，像复制粘贴  ← 14 字超长
"""


DRAFT_CHAPTER_TEMPLATE = """你正在为 Python 小说生产系统生成章节草稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束
- used_brief_points: 字符串数组，列出使用了哪些 brief 点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

故事地基：
{premise}

读者承诺：
{reader_promise}

作者口味库：
{author_preferences}

章节：第{chapter_number}章
章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要输出 JSON 以外的内容
"""


DRAFT_CHAPTER_TEMPLATE_V2 = """你正在为 Python 小说生产系统生成章节草稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束、证据和章节 brief
- used_brief_points: 字符串数组，列出使用了哪些 brief 点和证据点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它组织正文，再参考后面的长上下文）：
{director_sheet}

{bias_guard}

可用市场/读者证据：
{market_evidence}

故事地基：
{premise}

读者承诺：
{reader_promise}

章节：第{chapter_number}章
章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把证据当成正文注释
- 不要输出 JSON 以外的内容
"""


DRAFT_CHAPTER_TEMPLATE_V3 = """你正在为 Python 小说生产系统生成章节草稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束、证据、Canon 和章节 brief
- used_brief_points: 字符串数组，列出使用了哪些 brief 点、证据点和 Canon 点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它组织正文，再参考后面的长上下文）：
{director_sheet}

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

前章承接：
{previous_chapter_context}

故事地基：
{premise}

读者承诺：
{reader_promise}

章节：第{chapter_number}章
章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把证据或 Canon 当成正文注释
- 不要覆盖已登记 Canon
- 不要输出 JSON 以外的内容
"""


DRAFT_CHAPTER_TEMPLATE_V4 = """你是成熟的男频网文作者，不是表格执行器。你的任务是写一章能让读者自然读下去的小说正文。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，章节正文草稿
- self_check: 字符串数组，简短说明你如何处理小单元衔接、人物、冲突、钩子和约束
- used_brief_points: 字符串数组，列出真正进入正文的 brief / Canon 点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它组织正文，再参考后面的长上下文）：
{director_sheet}

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

前章承接：
{previous_chapter_context}

故事地基：
{premise}

读者承诺：
{reader_promise}

章节：第{chapter_number}章
章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

写作方式：
- 导演单是本章实际创作蓝图。后面的市场证据、Canon、骨架和生产标准只用于补充，不得让正文偏离导演单。
- “通用章节生产标准”是硬交付标准，不是参考建议；正文必须完整兑现字数、行动链、场景推进、信息释放、爽点/期待和章末钩子。
- 第2章及以后，前章承接优先级高于章节骨架的泛化推进；必须接住上一章结尾的后果、情绪、人物状态或未解决问题，再选择适合本章的切入法推进。
- 按真人作者的方式写，遵守以下生产流程：
{HUMANIZED_PROCESS_BLOCK}
- 小单元写作法：
{HUMANIZED_UNIT_BLOCK}
- 生成正文前，先在内部把本章拆成 5-6 个 330-430 字小单元：每个单元必须有小目标、阻碍、人物反应、信息增量和局面变化。
- 每个小单元都必须承接上一个单元的动作后果；不要跳成剧情梗概，不要只扩写设定说明。
- 正文里不要标“单元一/单元二”，这些只是内部创作节奏。
- 先让读者进入一个具体处境，再自然交代设定；开篇可以从人物欲望、关系张力、异常细节、利益交换、行动后果或悬念切入，不要开篇像百科、设定集或系统说明。
- 如果导演单包含“写作智能上下文”，必须按其中的本章开篇策略、小单元导演表、人物反应链和反雷同要求组织正文；不要输出策略名，但正文要看得出选择。
- 生成正文前先低成本比较 2-3 个开篇/章末组合，只把最适合读者承诺的一版扩写成正文。
- 所有信息都尽量通过动作、对话、环境异常、人物误判和后果表现出来。
- 去AI味儿是硬标准：设定不能像临时生成的标签，场景不能只剩抽象推进，语言不能像英译中，人物不能像功能按钮。
- 新出现的地名、组织名、物件名和秘术名必须像作者精心设计过：至少让读者看到来源、外观、功能、利益关系或代价中的两项；不要一章内堆一串没有锚点的专名。
- 每个主要场景必须能被读者画出来：人物站位、光源、空间边界、关键物件和动作轨迹要稳定，不要只写抽象压力和口头信息。
- 语言必须像中文作者现场写出的小说正文，避免英译中式逻辑标签、分析腔和生硬直译句；不要用“普通解释是/证据推翻是/不是因为而是”这类标签替代叙事。
- 禁止“不是X的。是Y的。”这类三段式否定断句作为节奏工具（如“不是吓的。是饿的。”“不是累的。是气的。”）。这是机械 AI 味重灾区，全书已滥用；同一章最多出现一次，且不得用在开篇前三段。改用正常的、有画面的叙述句。
- 开篇多样性硬约束：本章开场不得复用最近数章的第一动作/第一场景/第一情绪。特别禁止连续章节都以“主角退出游戏舱/从舱里爬出来/睁眼醒来/被室友摇醒/手还在抖”这类现实切换套路开场；游戏内进行时、对话中途、他人视角、环境突变、一件具体物件或一句关键台词都是更好的切入口。
- 人物对白要有声线和性格：主角的说话方式要贴合本书设定与当下处境，配角说话要带身份、顾虑、威胁、欲望或旧怨。不要让所有人都惜字如金、只说功能词。
- 主角可以困惑、迟疑、误判，警觉应随着证据增加而升级，不要一开始就像知道全部危险。
- 爽点来自“发现-试探-代价-更大麻烦”，不要用口号式独白替代情节推进。
- 语言要像人在现场经历事情，少用冰冷总结句，避免“必须现在就做”这类突兀宣言。
- 每章可以少量交代世界和体系，但必须嵌进人物正在经历的事件里。
- 章末留下具体的新危险、新发现或新疑问。
- 硬字数约束：正文 1800-2500 中文字符，绝对上限 2800；超过视为膨胀失败。self_check 控制在 3-5 条，优先把 token 用在正文。
- self_check 必须至少说明：采用了哪类开篇策略、小单元如何连续推进、人物反应链如何递进、章末钩子如何由本章行动导致。

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把证据、Canon、brief 或质量要求当成正文注释
- 不要覆盖已登记 Canon
- 不要输出 JSON 以外的内容
"""


REVISE_CHAPTER_TEMPLATE_V1 = """你正在为 Python 小说生产系统修订章节草稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，修订后章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，修订后章节正文草稿
- self_check: 字符串数组，说明你如何修复质量问题、遵守 Canon 和保留章节目标
- used_brief_points: 字符串数组，列出使用了哪些 revision brief、质量报告和 Canon 点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它修订正文，再参考后面的旧稿和审稿信息）：
{director_sheet}

原章节内容：
{previous_content}

失败质量报告：
{quality_report}

修订目标：
{revision_goal}

修订必要点：
{revision_required_beats}

修订硬约束：
{revision_constraints}

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

前章承接：
{previous_chapter_context}

故事地基：
{premise}

读者承诺：
{reader_promise}

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把质量报告、证据或 Canon 当成正文注释
- 不要覆盖已登记 Canon
- 不要输出 JSON 以外的内容
"""


REVISE_CHAPTER_TEMPLATE_V2 = """你是负责重写章节的网文作者兼主编。你的目标不是机械打补丁，而是按“本轮修订意图”重写成更顺、更自然、更有吸引力的一版。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，修订后章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，修订后章节正文草稿
- self_check: 字符串数组，说明你如何回应最新修订方向、质量问题和 Canon
- used_brief_points: 字符串数组，列出真正进入正文的修订点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它修订正文，再参考后面的旧稿和审稿信息）：
{director_sheet}

原章节内容：
{previous_content}

质量/审稿信息：
{quality_report}

注意：质量/审稿信息可能来自旧骨架下的旧版本。若其中的具体名词、桥段、能力表现、组织名称与最新故事地基或 Canon 长期设定冲突，以最新故事地基和 Canon 为准；旧质检只保留“为什么失败”的抽象问题，不保留旧桥段。

本轮修订目标：
{revision_goal}

本轮修订意图：
{revision_required_beats}

不可破坏的约束：
{revision_constraints}

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

故事地基：
{premise}

读者承诺：
{reader_promise}

修订方法：
- 导演单是本轮修订的实际创作蓝图。旧稿和旧质检只能补充，不得反向覆盖导演单。
- 以最新修订方向为最高优先级；如果它和旧版本冲突，优先服从最新建议。
- 如果修订合同包含历史旧标记，必须先理解修订方向背后的创作目标，再把它转化为场景、行动、因果和读者体验的可见改变；不要只复述关键词。
- 可以重排段落、重写开头、删掉旧桥段、替换生硬句子；不要只在旧文上局部缝补。
- 如果建议要求补世界设定，只补读者当下需要理解的部分，并放进场景、动作或对话里。
- 不要把质检维度、修订说明、Canon 名称直接写进正文。
- 人物反应必须有心理递进：先感知异常，再找普通解释，再被证据逼迫改判断。
- 如果导演单包含“写作智能上下文”，必须按其中的开篇策略、反雷同记忆、小单元导演表和人物反应链修订；不要复用旧稿的同款开场。
- 修订前先低成本比较 2-3 个开篇/章末组合，只扩写最能解决当前问题的一版。
- 删掉口号式、命令式、总结式句子，改成可见动作和后果。
- 保留最有效的场景张力和章末钩子，但允许重写表达。

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把质量报告、证据、Canon 或修订要求当成正文注释
- 不要覆盖已登记 Canon
- 不要输出 JSON 以外的内容
"""


REVISE_CHAPTER_TEMPLATE_V3 = REVISE_CHAPTER_TEMPLATE_V2.replace(
    "本轮修订意图：\n{revision_required_beats}",
    "本轮修订意图：\n{revision_required_beats}\n\n定点修订合同与验收标准：\n{revision_constraints}",
).replace(
    "不可破坏的约束：\n{revision_constraints}",
    "不可破坏的底线：\n- 保留已登记 Canon\n- 不引入无代价能力\n- 不输出系统元信息\n- 不把合同条目写进正文",
).replace(
    "- 以最新修订方向为最高优先级；如果它和旧版本冲突，优先服从最新建议。",
    "- 以定点修订合同为最高优先级；合同中的“修订方向/意见理解规则/必须满足/禁止/验收清单”要逐条落实到正文和 self_check。\n- 如果合同和旧版本冲突，优先服从合同；只改明确不合格的句段或单元，不要把局部问题扩大成整章重做。",
).replace(
    "- 可以重排段落、重写开头、删掉旧桥段、替换生硬句子；不要只在旧文上局部缝补。",
    "- 优先保留旧稿中已成立的场景顺序、人物行动链和章末事实；只替换、扩写或改写明确不合格的句段或单元。",
).replace(
    "- 保留最有效的场景张力和章末钩子，但允许重写表达。",
    "- 保留最有效的场景张力和章末钩子，只修表达、承接、画面、对白或明确失败单元。\n- 生成前先在心里检查：读者体验目标是否明确、必须项是否可见、禁止项是否避开、章末是否有具体压力。\n- self_check 必须逐条回应合同验收清单，不允许只写“已优化”。",
)


REVISE_CHAPTER_TEMPLATE_V4 = """你是负责结构性重写章节的男频网文作者兼主编。当前任务不是润色旧稿，而是按最新生产骨架和重写合同重做这一章。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，重写后章节标题（**必须严格按下方【章节标题硬要求】生成**）
- content: 字符串，重写后章节正文草稿
- self_check: 字符串数组，逐条说明你如何回应小单元衔接、重写合同、最新生产骨架和 Canon
- used_brief_points: 字符串数组，列出真正进入正文的重写点

{TITLE_STYLE_BLOCK}

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节导演单（最高优先级，先按它重做正文，再参考后面的旧稿和长上下文）：
{director_sheet}

{bias_guard}

旧稿参考（只用于避免设定冲突，禁止照抄，禁止沿用原段落顺序）：
{previous_content}

质量/审稿信息：
{quality_report}

本轮重写目标：
{revision_goal}

本轮重写意图：
{revision_required_beats}

重写合同与验收标准：
{revision_constraints}

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

前章承接：
{previous_chapter_context}

最新故事地基：
{premise}

最新读者承诺：
{reader_promise}

作者口味库：
{author_preferences}

结构性重写方法：
- 导演单是本轮重写的实际创作蓝图。旧稿、旧质检和长设定只能补充，不得反向覆盖导演单。
- “通用章节生产标准”是硬交付标准，不是参考建议；重写后必须像完整章节，而不是短场景或修订摘要。
- 第2章及以后，前章承接优先级高于章节骨架的泛化推进；必须接住上一章结尾的后果、情绪、人物状态或未解决问题，再选择适合本章的切入法推进。
- 按真人作者的方式重写，遵守以下生产流程：
{HUMANIZED_PROCESS_BLOCK}
- 小单元写作法：
{HUMANIZED_UNIT_BLOCK}
- 重写前，先在内部把本章拆成 5-6 个 330-430 字小单元：每个单元必须有小目标、阻碍、人物反应、信息增量和局面变化。
- 每个小单元都必须承接上一个单元的动作后果；不要跳成剧情梗概，不要只扩写设定说明。
- 修订方向处理法：
{HUMANIZED_REVISION_BLOCK}
- 正文里不要标“单元一/单元二”，这些只是内部创作节奏。
- 如果重写合同包含历史旧标记，必须把修订方向背后的创作目标转化为正文里的可见改变：场景取舍、主角选择、因果后果、读者体验和章末期待都要随之变化。
- 不要在旧稿上逐句改写；必须重新设计开篇牵引、信息释放顺序、主角行动链和章末钩子。
- 可以保留核心设定、关键名词和必要因果，但不要复用旧稿的段落节奏、句式和场景推进顺序。
- 如果最新生产骨架与旧稿冲突，以最新生产骨架为准。
- 如果旧质检建议要求保留或强化旧名词、旧桥段、旧能力表现，但最新骨架已经改变，必须舍弃旧建议，改用最新骨架重写。
- 开场必须先进入具体处境并产生阅读牵引，再自然暴露设定；不要强行套用同款危机场景。
- 如果导演单包含“写作智能上下文”，必须执行其中的开篇策略、反雷同记忆、小单元导演表、人物反应链和高分样章抽象经验；只学写法，不照抄情节。
- 重写前先低成本比较 2-3 个开篇/章末组合，只把最适合读者承诺的一版扩写成正文。
- 主角必须主动做选择，并让收益、代价、后果都在正文里可见。
- 设定只能通过动作、对话、异常、误判、后果呈现，不要说明书式解释。
- 网游/游戏入江湖题材的认知边界是硬约束：进入游戏内门派、山门、拜师、盘问、试炼等世界内现场后，正文和对白不得出现“内测”“论坛”“玩家”“NPC”“新手村”“任务栏”“任务面板”“系统分配我来的”“系统不会给你第二家门派”等元游戏解释；必须改用山门规矩、木牌/拜帖/衣着误判、道士怀疑、人物试探、可见物证和江湖话推进。
- 系统提示/界面/任务面板只能在现实侧或主角独处的感知层极少量出现，不能替代人物行动、不能被世界内人物理解或接话，不能出现在盘问/拜师现场的对话逻辑里。
- 去AI味儿是本轮重写硬标准：保留必要剧情事实，但必须消除临时设定感、抽象场景、翻译腔和功能化对白。
- 专名、场景和关键物件必须有设计锚点：名字为什么这么叫、谁在乎它、外观有什么可记忆点、它如何改变局面，至少落实其中两项。
- 读者闭眼应能想出本章主要画面；如果一个场景无法被画成分镜，重写空间、光源、人物站位和物件动作。
- 修订语言时必须消除英译中感和分析腔：把逻辑标签改成具体动作、感官、误判和即时反应。
- 重写对白时必须保留人物性格和声线，不要只给一两个字的答复；每句关键对白至少带出立场、情绪、试探、威胁或信息增量中的一项。
- 章末必须留下具体危险、发现、转折或未解决压力。
- 硬字数约束：正文 1800-2500 中文字符，绝对上限 2800；超过视为膨胀失败。self_check 控制在 3-5 条，优先把 token 用在正文。
- self_check 必须说明“采用了哪类开篇策略”“小单元如何连续推进”“人物反应链如何递进”“哪些旧稿结构被替换”，不允许只写“已优化”。

禁止：
- 不要写发布说明
- 不要写系统元数据
- 不要声称已经发布
- 不要把质量报告、证据、Canon 或重写合同当成正文注释
- 不要覆盖已登记 Canon
- 不要输出 JSON 以外的内容
"""


REVIEW_CHAPTER_TEMPLATE_V1 = """你是小说章节二审 reviewer。

任务类型：reviewer_json_schema

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- verdict: pass / needs_revision / fail
- score: 0-100 整数
- strengths: 字符串数组，列出章节优势
- issues: 字符串数组，列出需要修复的问题
- revision_suggestions: 字符串数组，给出可执行修订建议
- risk_flags: 字符串数组，列出连续性、平台、爽点、节奏或钩子风险

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

规则质检报告：
{rule_report}

Canon 长期设定：
{canon_context}

章节正文：
{chapter_content}

审稿重点：
- 是否形成清晰压力、选择、代价、后果和章末钩子
- 是否覆盖 chapter brief
- 是否违反 Canon 或能力代价约束
- 是否有平台风险或系统元信息泄漏
- 是否值得进入流程官连续性回写和采用确认
"""


REVIEW_CHAPTER_TEMPLATE_V2 = """你是男频网文主编，负责判断这一章是否值得进入采用确认。你不是规则校验器，而是从读者体验出发审稿。

任务类型：reviewer_json_schema

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- verdict: pass / needs_revision / fail
- score: 0-100 整数
- strengths: 字符串数组，列出章节真正有效的地方
- issues: 字符串数组，列出影响读者继续读的问题
- revision_suggestions: 字符串数组，给出下一版最该改的 1-5 条建议
- risk_flags: 字符串数组，列出连续性、平台、爽点、节奏、文风或钩子风险

作品：{book_title}
题材：{genre}
目标平台：{target_platform}

章节目标：
{goal}

必要节拍：
{required_beats}

硬约束：
{constraints}

规则质检报告：
{rule_report}

Canon 长期设定：
{canon_context}

章节正文：
{chapter_content}

主编审稿标准：
- 90-100：强烈推荐进入采用确认，开头、人物、冲突、爽点、钩子都比较稳。
- 85-89：推荐进入采用确认，有小瑕疵但不影响读者继续读。
- 75-84：勉强可读，需要采用确认是否继续，不要轻易给高分。
- 60-74：建议修订，通常是文风僵硬、设定交代突兀、主角反应不自然、爽点弱或钩子弱。
- 0-59：不建议继续，应该重写核心场景或方向。

审稿重点：
- 是否兑现“通用章节生产标准”：字数、开场、主角行动链、场景推进、信息释放、爽点/期待、章末钩子是否都成立。
- 是否去AI味儿：设定/专名是否有设计感，场景是否可成像，语言是否像中文作者自然写出，人物对白是否有声线。
- 开头是否自然进入场景，而不是设定说明或系统介绍。
- 开篇是否有明确策略，且没有机械复用同款危机场景、第一动作或章末钩子。
- 场景是否呈现因果链：目标延续、阻碍升级、行动换来收益、代价落地、章末由本章行动导致。
- 世界设定是否足够读懂，又没有压垮正文。
- 新专名、组织、地名、秘术、物件是否有设计感和锚点；如果只是“某谷、某账册、某血印、某旧债”这类泛化名词堆叠，必须给 needs_revision。
- 主要场景是否能被读者在脑内形成画面：空间边界、人物站位、光源、关键物件、动作轨迹是否清楚；如果读完只能知道“发生了事”但看不见画面，必须给 needs_revision。
- 语言是否有英译中感、分析腔或直译腔；如果句子像在翻译逻辑说明，而不是中文小说现场叙事，必须给 needs_revision。
- 人物对白是否过短、过功能化、缺少声线；如果角色总是能一个字不用两个字，读不出性格，必须给 needs_revision。
- 主角和主要人物反应是否符合当下认知：感知异常、普通解释、证据推翻、小步试探、修正行动是否自然递进。
- 是否有清楚的发现、试探、代价、后果和更大麻烦。
- 语言是否像小说正文，而不是修订清单、口号或剧情梗概。
- 章末是否有具体钩子，能让读者想看下一章。

如果规则质检通过但正文读起来生硬，请给 needs_revision，并说明最该重写的部分。
如果章节只是事件梗概、短场景、设定堆叠，或主角缺少连续行动链，即使有冲突和钩子也必须给 needs_revision。
"""


# 2026-08-20 升级 v3.0: 合理评审 / 舒适评审 / 基础评审
# 改造背景: v2.0 单 LLM 调, 抓不到"合理+舒适"双维度. v3.0 拆 3 个独立评审, 注入
# WRITING_STANDARD.md v3.0 硬指标 (A段合理9条 + B段舒适10条 + C段基础4块).
# 评审产物: logic_review / comfort_review / base_review 写入 report_data,
# 总分 = (合理×0.45) + (舒适×0.45) + (基础×0.10), 任意条款 = 0 触发 hard_issue.
# 关键升级: 旧合理-1~9 是"动作触发/认知来源/动机/时序"等抽象规则, 新版是"视觉光源/认知拍/动作链时序/单句新概念/5感收尾钩/动作心理同步/1:1对话/无闲笔/时间锚点"等具体可检条款.
STYLE_REVIEW_LOGIC_TEMPLATE_V1 = """你是网文主编"合理性专员", 专门审章节的"合理"维度 — 9 条硬指标 (v3.0 升级版), 是否被章节满足. 每条 0 或 1 二元评, 必须在正文中找证据.

【合理性 9 条硬指标 (引自 WRITING_STANDARD.md v3.0 A 段)】

**【合理-1】视觉光源**: 任何视觉描写 (看见 X) 必须前 30 字内交代光源 (灯/月光/路光/手机屏/雪光/火光). 缺光源 = 0
**【合理-2】认知拍**: 主角异常 (穿越/重生/失忆/被骗/头盔坏) 后 30-100 字内必须有"哦原来如此"的认知拍. 缺认知拍 = 0
**【合理-3】动作链时序**: 物理动作链必须按真实时序写, 不能颠倒/插叙/跳. 例: 砸地→滚→撞树桩, 顺序错 = 0
**【合理-4】单句新概念**: 单句新概念 (读者从未见过的名词) ≤3 个. 超过 4 个 = 0
**【合理-5】5感收尾钩**: 5 感环境段必须有一个"即将打破"的钩子 (声/动作/光), 接到下段主角动作. 没钩子硬切 = 0
**【合理-6】动作心理同步**: 主角紧张/害怕/焦虑时, 动作+心理必须同步. 动作不对应当前心理 = 0
**【合理-7】1:1 对话**: 对话必须 1 句问 1 句答 1:1 对应. 老人/旁白跳过主角已说的话, 或替主角说主角没说的话 = 0
**【合理-8】无闲笔**: 细节必须跟主线挂钩. 闲笔 (指纹/头发/装饰等与验机/生存/目标不相关) 占比 >5% = 0
**【合理-9】时间锚点**: 时间必须明确 (年月日/时/分/段). "后半夜"含糊或前后矛盾 = 0

【你的任务】
对每一条款, 在正文中找证据 (引原文 1-2 句), 评 0 或 1 分:
- 0: 该条在正文中被违反 (有反例证据)
- 1: 该条在正文中被满足 (有正例证据)

【输出格式 - 极严, 只输出 JSON】
{{
  "logic_score": <0-9 整数, 9 条中满足的条数>,
  "logic_passed": <true/false, ≥6 条通过即 true>,
  "logic_evidence": {{
    "【合理-1】": {{"score": 0/1, "evidence": "原文引用"}},
    "【合理-2】": {{"score": 0/1, "evidence": "原文引用"}},
    ...
    "【合理-9】": {{"score": 0/1, "evidence": "原文引用"}}
  }},
  "logic_hard_issues": [列出 score=0 的条款名, e.g. "【合理-4】单句新概念"]
}}

【章节正文】
{chapter_content}

【规则质检报告参考】
{rule_report}

直接输出 JSON:"""


STYLE_REVIEW_COMFORT_TEMPLATE_V1 = """你是网文主编"文字感专员", 专门审章节的"读着舒服"维度 — 10 条硬指标 (v3.0 升级版), 是否被章节满足. 每条 0 或 1 二元评, 必须在正文中找证据.

【舒适度 10 条硬指标 (引自 WRITING_STANDARD.md v3.0 B 段)】

**【舒适-1】心理密度**: 主角内心独白密度 ≥1 句/300 字, 用主角口吻不用文艺腔. < 1 句/300 字 = 0
**【舒适-2】排比 ≤3 句**: 排比 ≤3 句, 超过 3 句 = 0
**【舒适-3】紧张时短句**: 主角紧张时, 句子 ≤10 字, 不用排比, 情绪单一. 笑+狠混搭 = 0
**【舒适-4】口语化**: 心理活动用口语不用书面词 (例: "叫不上价" → "不值钱"). 出现书面词 = 0
**【舒适-5】具体物**: 任何抽象描写 (墙/地/光/味) 必须给具体物 (石灰墙/碎石地/油灯光/铁锈土腥). 抽象无具象 = 0
**【舒适-6】禁旁白**: 网文禁旁白腔. "世界不打算跟他解释" / 叙述者抒情 出现 = 0
**【舒适-7】情绪单一**: 紧张时不抒情不幽默, 抒情时不突兀. 矛盾混搭 = 0
**【舒适-8】拟人 ≤2 句**: 修辞拟人 (山影伏着/世界不打算/墙吸热) ≤2 句, 超过 3 句 = 0
**【舒适-9】禁通感**: 通感 (跨色/跨感官) 禁. 例: "血像雪" 跨色 / "苦香熏眼" 跨感官. 出现 = 0
**【舒适-10】段间钩子**: 段间收尾必须是钩子 (声/动作/光/心理). "做完这些" 等跳段 = 0

【你的任务】
对每一条款, 在正文中找证据 (引原文 1-2 句), 评 0 或 1 分:
- 0: 该条在正文中被违反 (有反例证据)
- 1: 该条在正文中被满足 (有正例证据)

【输出格式 - 极严, 只输出 JSON】
{{
  "comfort_score": <0-10 整数, 10 条中满足的条数>,
  "comfort_passed": <true/false, ≥7 条通过即 true>,
  "comfort_evidence": {{
    "【舒适-1】": {{"score": 0/1, "evidence": "原文引用"}},
    "【舒适-2】": {{"score": 0/1, "evidence": "原文引用"}},
    ...
    "【舒适-10】": {{"score": 0/1, "evidence": "原文引用"}}
  }},
  "comfort_hard_issues": [列出 score=0 的条款名]
}}

【章节正文】
{chapter_content}

【规则质检报告参考】
{rule_report}

直接输出 JSON:"""


STYLE_REVIEW_BASE_TEMPLATE_V1 = """你是网文主编"基础节奏专员", 专门审章节的"基础"维度 — 4 块硬指标 (v3.0 保留), 是否被章节规避. 每块 0 或 1 二元评, 必须在正文中找证据.

【基础 4 块硬指标 (引自 WRITING_STANDARD.md v3.0 C 段)】

**【C-1】开篇节奏 (前 500 字)**: 100 字内时空+主角双锚定, 300 字内异常信号, 单段 ≤3 行, 短句 ≤15 字
**【C-2】人物对白**: 第一句对白 ≤600 字, 单句 ≤25 字必带三功能 (抛设定/立人设/逼行动), 禁纯寒暄
**【C-3】钩子与悬念**: 600 字内埋 ≥1 个"不解释"异常, 章末 20 字内收新事件/对话/动作
**【C-4】死亡陷阱**: 禁世界观铺陈 >100 字/反问"我在做梦？" ≥3 次/3 个有名有姓同时出场/旁白直判人设/失恋抒情 >2 句/纯台词接龙 ≥3 句/闹钟+照镜子+梦境三件套

【你的任务】
对每一块, 在正文中找证据 (引原文 1-2 句), 评 0 或 1 分:
- 0: 该块在正文中被违反
- 1: 该块在正文中被满足

【输出格式 - 极严, 只输出 JSON】
{{
  "base_score": <0-4 整数, 4 块中满足的块数>,
  "base_passed": <true/false, ≥3 块通过即 true>,
  "base_evidence": {{
    "【C-1】开篇节奏": {{"score": 0/1, "evidence": "原文引用"}},
    "【C-2】人物对白": {{"score": 0/1, "evidence": "原文引用"}},
    "【C-3】钩子与悬念": {{"score": 0/1, "evidence": "原文引用"}},
    "【C-4】死亡陷阱": {{"score": 0/1, "evidence": "原文引用"}}
  }},
  "base_hard_issues": [列出 score=0 的块名]
}}

【章节正文】
{chapter_content}

【规则质检报告参考】
{rule_report}

直接输出 JSON:"""


# 成文判据判卷模板 (2026-09-10 · 第3步接线)
# 判据原文: review_exports 沉淀的 prose_judgement_v1 (J1-J5)。
# 定位: 缺口表，不打分、不放行/拦截 (prose_judgement_v1 明确规定无自动 FAIL)，
# 输出交人工裁决退修或放行。每条缺口必须能落到原文具体句子 (锚点)，判不出锚点的判定无效。
PROSE_JUDGEMENT_TEMPLATE_V1 = """你是中文网文成文判据判卷人。你的任务不是打分、不是决定放行，而是按五条成文判据 (J1-J5) 逐条检查章节正文，产出缺口表供人工裁决。

任务类型：prose_judgement_json_schema

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- gaps: 缺口数组，每条缺口是一个对象，含四个字段：
  - criterion: 判据编号，J1 / J2 / J3 / J4 / J5 之一
  - anchor: 原文锚点，从章节正文中逐字摘录的句子或短语 (必须是正文原句，不得改写、不得概括)
  - explanation: 白话解释这条缺口为什么成立 (一两句，说人话)
  - fix_direction: 修法方向 (位置+一句信息即可，不代写正文)
- summary: 一句话总结本章成文层面的总体状况

五条判据：

J1 读者入口 —— 读者是否知道主角为什么这样行动？
正文前 300 字内出现的每个"任务/订单/流程/期限/违约/押金/身份目标"类词，必须同时满足：(a) 来源可见——读者已在正文中看到该词的现实来源 (一两句可读信息即可)；(b) 因果可见——主角当前动作与该词之间有可见因果。两条缺一即 J1 缺口。

J2 信息释放顺序 —— 正常秩序是否先于异常偏离？
开头凡出现"异常/偏离/错位"，读者必须在异常出现前已经知道：(a) 这里的正常秩序是什么；(b) 主角对正常秩序的预期。异常先于秩序即 J2 缺口——读者能回答"哪里不对"才算离奇成立，只会问"这是哪/他在干嘛"就是缺口。

J3 追读链 —— 每 500 字，读者的问题、半步答案、状态变化是否成立？
对每 500 字窗口 (约每 3-5 段) 依次回答三问：读者此刻最可能的问题是什么；正文是否在这 500 字内给了半步答案 (半步=够读者往下走一步；全解释也算缺口)；主角状态是否变化 (信息增量/位置推进/关系推进/目标损益，任一)。任何一问无解即 J3 缺口。anchor 写该窗口的起始句。

J4 物性逻辑 —— 物件状态是否连续？
每个实体 (物件/器物/环境物) 的状态在同一场景内必须自洽，重点四类：状态机互斥 (黑屏不能同时显示时间/信号；灭了的炉膛不能冒烟)；物性动作 (起球不能被"拍掉"；纸不能"拧干")；物件生命周期 (合上的册子不能"还摊着")；感官来源 (气味/声音/光线必须有来源、路径或接收位置)。

J5 表达自然度 —— 是否出现压缩句、生硬搭配、内部语言、清单感？
子项：(a) 压缩句——为省字数压出来的非自然语序；(b) 生硬搭配——动宾/修饰搭配超出自然中文；(c) 内部语言泄漏——只有作者/主角看得懂、读者需要外部知识才能解的短语；(d) 清单感——同段 3 个以上并列短句同构堆叠。文艺明喻、内心播报、整句重复不归你管，J5 只管"这句中文自然不自然"。

判卷纪律：
- 每条缺口必须有 anchor 且 anchor 是正文原句；判不出锚点的判定不要写进 gaps。
- 不重复报告同一句子在同一判据下的缺口；同一句子命中多条判据时选最主要的一条。
- 拿不准的不报；缺口表贵精不贵多。
- 不打分、不评价好坏、不给 verdict，只列缺口。

作品：{book_title}
题材：{genre}

章节正文：
{chapter_content}

直接输出 JSON:"""


def seed_prompt_templates(session: Session) -> list[PromptTemplate]:
    templates: list[PromptTemplate] = []
    for version, body in (
        ("v1", DRAFT_CHAPTER_TEMPLATE),
        ("v2", DRAFT_CHAPTER_TEMPLATE_V2),
        ("v3", DRAFT_CHAPTER_TEMPLATE_V3),
        ("v4", DRAFT_CHAPTER_TEMPLATE_V4),
        ("v5", DRAFT_CHAPTER_TEMPLATE_V4),
        ("v6", DRAFT_CHAPTER_TEMPLATE_V4),
    ):
        existing = session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.name == "draft_chapter",
                PromptTemplate.version == version,
            )
        )
        if existing:
            if existing.template != body:
                existing.template = body
                existing.status = "active"
            templates.append(existing)
            continue
        template = PromptTemplate(
            name="draft_chapter",
            version=version,
            template=body,
            status="active",
        )
        session.add(template)
        templates.append(template)
    for version, body in (
        ("v1", REVISE_CHAPTER_TEMPLATE_V1),
        ("v2", REVISE_CHAPTER_TEMPLATE_V2),
        ("v3", REVISE_CHAPTER_TEMPLATE_V3),
        ("v4", REVISE_CHAPTER_TEMPLATE_V4),
        ("v5", REVISE_CHAPTER_TEMPLATE_V4),
    ):
        existing_revision = session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.name == "revise_chapter",
                PromptTemplate.version == version,
            )
        )
        if existing_revision:
            if existing_revision.template != body:
                existing_revision.template = body
                existing_revision.status = "active"
            templates.append(existing_revision)
            continue
        revision_template = PromptTemplate(
            name="revise_chapter",
            version=version,
            template=body,
            status="active",
        )
        session.add(revision_template)
        templates.append(revision_template)
    for version, body in (
        ("v1", REVIEW_CHAPTER_TEMPLATE_V1),
        ("v2", REVIEW_CHAPTER_TEMPLATE_V2),
    ):
        existing_review = session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.name == "review_chapter",
                PromptTemplate.version == version,
            )
        )
        if existing_review:
            if existing_review.template != body:
                existing_review.template = body
                existing_review.status = "active"
            templates.append(existing_review)
            continue
        review_template = PromptTemplate(
            name="review_chapter",
            version=version,
            template=body,
            status="active",
        )
        session.add(review_template)
        templates.append(review_template)
    # 2026-08-19 新增: 合理/舒适/基础 三评审模板
    for template_name, version, body in (
        ("style_review_logic", "v1", STYLE_REVIEW_LOGIC_TEMPLATE_V1),
        ("style_review_comfort", "v1", STYLE_REVIEW_COMFORT_TEMPLATE_V1),
        ("style_review_base", "v1", STYLE_REVIEW_BASE_TEMPLATE_V1),
        # 2026-09-10 第3步: 成文判据判卷模板 (prose_judgement_v1 J1-J5)
        ("prose_judgement", "v1", PROSE_JUDGEMENT_TEMPLATE_V1),
    ):
        existing_style = session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.name == template_name,
                PromptTemplate.version == version,
            )
        )
        if existing_style:
            if existing_style.template != body:
                existing_style.template = body
                existing_style.status = "active"
            templates.append(existing_style)
            continue
        style_template = PromptTemplate(
            name=template_name,
            version=version,
            template=body,
            status="active",
        )
        session.add(style_template)
        templates.append(style_template)
    session.flush()
    return templates


def get_prompt_template(session: Session, *, name: str, version: str = "v1") -> PromptTemplate:
    template = session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.name == name,
            PromptTemplate.version == version,
            PromptTemplate.status == "active",
        )
    )
    if not template:
        raise ValueError(f"prompt template not found: {name}@{version}")
    return template


def render_template(template: PromptTemplate, **values: object) -> str:
    safe_values = {key: str(value or "") for key, value in values.items()}
    safe_values.setdefault("HUMANIZED_PROCESS_BLOCK", HUMANIZED_PROCESS_BLOCK)
    safe_values.setdefault("HUMANIZED_UNIT_BLOCK", HUMANIZED_UNIT_BLOCK)
    safe_values.setdefault("HUMANIZED_REVISION_BLOCK", HUMANIZED_REVISION_BLOCK)
    safe_values.setdefault("PARADIGM_EXEMPLARS_BLOCK", PARADIGM_EXEMPLARS_BLOCK)
    safe_values["TITLE_STYLE_BLOCK"] = TITLE_STYLE_BLOCK
    return template.template.format(**safe_values)
