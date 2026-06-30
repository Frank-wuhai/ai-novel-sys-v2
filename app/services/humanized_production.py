from __future__ import annotations


HUMANIZED_WRITING_PROCESS = [
    "先确认故事承诺：读者为什么持续追读，爽点、情绪、题材钩子和主角魅力是什么。",
    "逐项确认核心设定：主角欲望与缺陷、世界规则、能力收益与代价、长期压力、禁忌套路。",
    "把一卷拆成剧情段：明确阶段目标、高潮、转折和主角应抵达的新位置。",
    "写每章前做章内设计：开场场景、主角当下目标、阻碍、信息增量、代价和章末期待。",
    "把正文拆成连续小单元：每约500字推进一次小目标、阻碍、反应、信息增量和局面微变化。",
    "写完从读者体验回看：可读性、主动性、因果、设定自然度、代价和追读钩子是否成立。",
    "修改时先理解意见：把抽象不满转成场景、行动、因果和读者体验的可见改变。",
]


HUMANIZED_UNIT_METHOD = [
    "每个约500字小单元必须有小目标、阻碍、人物反应、信息增量和局面微变化。",
    "后一单元必须承接前一单元的动作后果，不得跳成剧情梗概。",
    "每2个单元至少出现一次人物对话或互动，让人物像活人在现场反应。",
    "设定只能由事件、对话、异常或后果触发，不用说明书式灌输。",
    "章末单元必须由本章行动自然引出新危险、新机会、新问题或关系变化。",
]


HUMANIZED_REVISION_METHOD = [
    "保留原始修订方向，禁止改写成空泛关键词。",
    "先判断意见背后的真实问题：节奏、动机、爽点、场景、文风、设定兑现或章末期待。",
    "把意见转成可验收的正文变化：新增/删除/替换哪些场景，主角做什么不同选择，读者获得什么不同感受。",
    "如果意见和旧稿冲突，优先服从最新修订方向和最新生产骨架。",
    "修订后 self_check 必须说明修订方向如何在正文里被看见。",
]


def humanized_process_text() -> str:
    return "\n".join(f"- {item}" for item in HUMANIZED_WRITING_PROCESS)


def humanized_unit_method_text() -> str:
    return "\n".join(f"- {item}" for item in HUMANIZED_UNIT_METHOD)


def humanized_revision_method_text() -> str:
    return "\n".join(f"- {item}" for item in HUMANIZED_REVISION_METHOD)
