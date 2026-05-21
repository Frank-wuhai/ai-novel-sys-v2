from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import PromptTemplate


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

可用市场/读者证据：
{market_evidence}

Canon 长期设定：
{canon_context}

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


def seed_prompt_templates(session: Session) -> list[PromptTemplate]:
    templates: list[PromptTemplate] = []
    for version, body in (
        ("v1", DRAFT_CHAPTER_TEMPLATE),
        ("v2", DRAFT_CHAPTER_TEMPLATE_V2),
        ("v3", DRAFT_CHAPTER_TEMPLATE_V3),
    ):
        existing = session.scalar(
            select(PromptTemplate).where(
                PromptTemplate.name == "draft_chapter",
                PromptTemplate.version == version,
            )
        )
        if existing:
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
    existing_revision = session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.name == "revise_chapter",
            PromptTemplate.version == "v1",
        )
    )
    if existing_revision:
        templates.append(existing_revision)
    else:
        revision_template = PromptTemplate(
            name="revise_chapter",
            version="v1",
            template=REVISE_CHAPTER_TEMPLATE_V1,
            status="active",
        )
        session.add(revision_template)
        templates.append(revision_template)
    existing_review = session.scalar(
        select(PromptTemplate).where(
            PromptTemplate.name == "review_chapter",
            PromptTemplate.version == "v1",
        )
    )
    if existing_review:
        templates.append(existing_review)
    else:
        review_template = PromptTemplate(
            name="review_chapter",
            version="v1",
            template=REVIEW_CHAPTER_TEMPLATE_V1,
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
    return template.template.format(**safe_values)
