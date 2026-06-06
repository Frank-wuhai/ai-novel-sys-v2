from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PromptTemplate
from app.services.humanized_production import humanized_process_text, humanized_revision_method_text, humanized_unit_method_text


HUMANIZED_PROCESS_BLOCK = humanized_process_text()
HUMANIZED_UNIT_BLOCK = humanized_unit_method_text()
HUMANIZED_REVISION_BLOCK = humanized_revision_method_text()


DRAFT_CHAPTER_TEMPLATE = """你正在为 Python 小说生产系统生成章节草稿。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，章节标题
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束
- used_brief_points: 字符串数组，列出使用了哪些 brief 点

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
- title: 字符串，章节标题
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束、证据和章节 brief
- used_brief_points: 字符串数组，列出使用了哪些 brief 点和证据点

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
- title: 字符串，章节标题
- content: 字符串，章节正文草稿
- self_check: 字符串数组，说明你如何遵守约束、证据、Canon 和章节 brief
- used_brief_points: 字符串数组，列出使用了哪些 brief 点、证据点和 Canon 点

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
- title: 字符串，章节标题
- content: 字符串，章节正文草稿
- self_check: 字符串数组，简短说明你如何处理小单元衔接、人物、冲突、钩子和约束
- used_brief_points: 字符串数组，列出真正进入正文的 brief / Canon 点

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
- 生成正文前，先在内部把本章拆成 6-9 个 300-500 字小单元：每个单元必须有小目标、阻碍、人物反应、信息增量和局面变化。
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
- 人物对白要有声线和性格：陈默可以嘴硬、带梗、临场找补；江湖人物说话要带身份、顾虑、威胁或旧怨。不要让所有人都惜字如金、只说功能词。
- 主角可以困惑、迟疑、误判，警觉应随着证据增加而升级，不要一开始就像知道全部危险。
- 爽点来自“发现-试探-代价-更大麻烦”，不要用口号式独白替代情节推进。
- 语言要像人在现场经历事情，少用冰冷总结句，避免“必须现在就做”这类突兀宣言。
- 每章可以少量交代世界和体系，但必须嵌进人物正在经历的事件里。
- 章末留下具体的新危险、新发现或新疑问。
- 如果 brief 要求 3000-4500 中文字符，正文不要低于 3000 中文字符；不要用自检内容凑正文长度。self_check 控制在 3-5 条，优先把 token 用在正文。
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
- title: 字符串，修订后章节标题
- content: 字符串，修订后章节正文草稿
- self_check: 字符串数组，说明你如何修复质量问题、遵守 Canon 和保留章节目标
- used_brief_points: 字符串数组，列出使用了哪些 revision brief、质量报告和 Canon 点

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
- title: 字符串，修订后章节标题
- content: 字符串，修订后章节正文草稿
- self_check: 字符串数组，说明你如何回应最新人工建议、质量问题和 Canon
- used_brief_points: 字符串数组，列出真正进入正文的修订点

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
- 以最新人工建议为最高优先级；如果它和旧版本冲突，优先服从最新建议。
- 如果修订合同包含“原始人工意见”，必须先理解用户真实意图，再把它转化为场景、行动、因果和读者体验的可见改变；不要只复述关键词。
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
    "本轮修订意图：\n{revision_required_beats}\n\n重写合同与验收标准：\n{revision_constraints}",
).replace(
    "不可破坏的约束：\n{revision_constraints}",
    "不可破坏的底线：\n- 保留已登记 Canon\n- 不引入无代价能力\n- 不输出系统元信息\n- 不把合同条目写进正文",
).replace(
    "- 以最新人工建议为最高优先级；如果它和旧版本冲突，优先服从最新建议。",
    "- 以重写合同为最高优先级；合同中的“原始人工意见/意见理解规则/必须满足/禁止/验收清单”要逐条落实到正文和 self_check。\n- 如果合同和旧版本冲突，优先服从合同；必要时重写开头、重排场景或删除旧桥段。",
).replace(
    "- 保留最有效的场景张力和章末钩子，但允许重写表达。",
    "- 保留最有效的场景张力和章末钩子，但允许重写表达。\n- 生成前先在心里检查：读者体验目标是否明确、必须项是否可见、禁止项是否避开、章末是否有具体压力。\n- self_check 必须逐条回应合同验收清单，不允许只写“已优化”。",
)


REVISE_CHAPTER_TEMPLATE_V4 = """你是负责结构性重写章节的男频网文作者兼主编。当前任务不是润色旧稿，而是按最新生产骨架和重写合同重做这一章。

请严格输出 JSON 对象，不要 Markdown，不要代码块，不要额外解释。

JSON 字段：
- title: 字符串，重写后章节标题
- content: 字符串，重写后章节正文草稿
- self_check: 字符串数组，逐条说明你如何回应小单元衔接、重写合同、最新生产骨架和 Canon
- used_brief_points: 字符串数组，列出真正进入正文的重写点

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
- 重写前，先在内部把本章拆成 6-9 个 300-500 字小单元：每个单元必须有小目标、阻碍、人物反应、信息增量和局面变化。
- 每个小单元都必须承接上一个单元的动作后果；不要跳成剧情梗概，不要只扩写设定说明。
- 人工意见处理法：
{HUMANIZED_REVISION_BLOCK}
- 正文里不要标“单元一/单元二”，这些只是内部创作节奏。
- 如果重写合同包含“原始人工意见”，必须把用户真实意图转化为正文里的可见改变：场景取舍、主角选择、因果后果、读者体验和章末期待都要随之变化。
- 不要在旧稿上逐句改写；必须重新设计开篇牵引、信息释放顺序、主角行动链和章末钩子。
- 可以保留核心设定、关键名词和必要因果，但不要复用旧稿的段落节奏、句式和场景推进顺序。
- 如果最新生产骨架与旧稿冲突，以最新生产骨架为准。
- 如果旧质检建议要求保留或强化旧名词、旧桥段、旧能力表现，但最新骨架已经改变，必须舍弃旧建议，改用最新骨架重写。
- 开场必须先进入具体处境并产生阅读牵引，再自然暴露设定；不要强行套用同款危机场景。
- 如果导演单包含“写作智能上下文”，必须执行其中的开篇策略、反雷同记忆、小单元导演表、人物反应链和高分样章抽象经验；只学写法，不照抄情节。
- 重写前先低成本比较 2-3 个开篇/章末组合，只把最适合读者承诺的一版扩写成正文。
- 主角必须主动做选择，并让收益、代价、后果都在正文里可见。
- 设定只能通过动作、对话、异常、误判、后果呈现，不要说明书式解释。
- 去AI味儿是本轮重写硬标准：保留必要剧情事实，但必须消除临时设定感、抽象场景、翻译腔和功能化对白。
- 专名、场景和关键物件必须有设计锚点：名字为什么这么叫、谁在乎它、外观有什么可记忆点、它如何改变局面，至少落实其中两项。
- 读者闭眼应能想出本章主要画面；如果一个场景无法被画成分镜，重写空间、光源、人物站位和物件动作。
- 修订语言时必须消除英译中感和分析腔：把逻辑标签改成具体动作、感官、误判和即时反应。
- 重写对白时必须保留人物性格和声线，不要只给一两个字的答复；每句关键对白至少带出立场、情绪、试探、威胁或信息增量中的一项。
- 章末必须留下具体危险、发现、转折或未解决压力。
- 如果 brief 要求 3000-4500 中文字符，正文不要低于 3000 中文字符；不要用自检内容凑正文长度。self_check 控制在 3-5 条，优先把 token 用在正文。
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
- 是否值得进入人工连续性回写和审批
"""


REVIEW_CHAPTER_TEMPLATE_V2 = """你是男频网文主编，负责判断这一章是否值得进入人工审批。你不是规则校验器，而是从读者体验出发审稿。

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
- 90-100：强烈推荐进入审批，开头、人物、冲突、爽点、钩子都比较稳。
- 85-89：推荐进入审批，有小瑕疵但不影响读者继续读。
- 75-84：勉强可读，需要人工确认是否继续，不要轻易给高分。
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


def seed_prompt_templates(session: Session) -> list[PromptTemplate]:
    templates: list[PromptTemplate] = []
    for version, body in (
        ("v1", DRAFT_CHAPTER_TEMPLATE),
        ("v2", DRAFT_CHAPTER_TEMPLATE_V2),
        ("v3", DRAFT_CHAPTER_TEMPLATE_V3),
        ("v4", DRAFT_CHAPTER_TEMPLATE_V4),
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
    return template.template.format(**safe_values)
