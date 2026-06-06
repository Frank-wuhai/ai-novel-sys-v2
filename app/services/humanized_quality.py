from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HumanizedDeliveryReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "recommendations": self.recommendations,
        }


OPENING_SCENE_MARKERS = (
    "门",
    "窗",
    "街",
    "雨",
    "风",
    "灯",
    "血",
    "脚步",
    "声音",
    "低声",
    "抬头",
    "回头",
    "看见",
    "听见",
    "掌心",
    "身后",
    "眼前",
)
ACTION_MARKERS = ("走", "推", "抓", "握", "退", "停", "看", "听", "问", "答", "接", "递", "藏", "试", "换", "冲")
OBSTACLE_MARKERS = ("阻", "拦", "挡", "逼", "追", "伤", "痛", "压", "危险", "麻烦", "误会", "代价")
CHOICE_MARKERS = ("选择", "决定", "还是", "只能", "宁可", "换", "交易", "接下", "拒绝", "转身")
COST_MARKERS = ("代价", "付出", "损耗", "疼", "伤", "欠", "失去", "暴露", "后果", "反噬")
REACTION_MARKERS = ("沉默", "皱眉", "笑", "愣", "怒", "怕", "惊", "疑", "退", "盯", "呼吸", "脸色")
CAUSAL_MARKERS = ("因此", "于是", "所以", "却", "但", "偏偏", "反而", "果然", "下一刻", "随即", "这让", "正因为")
SETTING_EMBED_MARKERS = ("规矩", "门派", "药", "灵", "阵", "城", "客栈", "江湖", "修炼", "师门", "交易", "人情")
EXPOSITION_MARKERS = ("世界观", "设定", "系统说明", "背景介绍", "规则如下", "首先", "其次")
HOOK_MARKERS = ("门外", "黑影", "脚步", "消息", "秘密", "发现", "转折", "陌生", "下一次", "笑声", "低声", "没有结束")


def evaluate_humanized_delivery(text: str) -> HumanizedDeliveryReport:
    body = str(text or "")
    opening = _slice_chinese(body, 420)
    ending = _slice_chinese(body, 420, tail=True)
    checks = {
        "opening_in_scene": _opening_score(opening),
        "protagonist_action_chain": _action_chain_score(body),
        "causal_unit_flow": _causal_flow_score(body),
        "interaction_reaction": _interaction_score(body),
        "embedded_setting": _embedded_setting_score(body),
        "earned_hook": _hook_score(ending),
    }
    score = round(sum(checks.values()) / len(checks)) if checks else 0
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    recommendations = _recommendations(checks)
    return HumanizedDeliveryReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        recommendations=recommendations,
    )


def _opening_score(opening: str) -> int:
    scene_hits = _hit_count(opening, OPENING_SCENE_MARKERS)
    action_hits = _hit_count(opening, ACTION_MARKERS)
    exposition_hits = _hit_count(opening, EXPOSITION_MARKERS)
    score = 35 + min(scene_hits, 4) * 12 + min(action_hits, 3) * 8
    if exposition_hits:
        score -= min(35, exposition_hits * 15)
    return _clamp(score)


def _action_chain_score(text: str) -> int:
    action = _hit_count(text, ACTION_MARKERS)
    obstacle = _hit_count(text, OBSTACLE_MARKERS)
    choice = _hit_count(text, CHOICE_MARKERS)
    cost = _hit_count(text, COST_MARKERS)
    score = 25 + min(action, 8) * 5 + min(obstacle, 4) * 8 + min(choice, 3) * 8 + min(cost, 3) * 7
    return _clamp(score)


def _causal_flow_score(text: str) -> int:
    paragraphs = [part for part in text.splitlines() if part.strip()]
    paragraph_score = 20 if len(paragraphs) >= 6 else len(paragraphs) * 3
    causal_hits = _hit_count(text, CAUSAL_MARKERS)
    consequence_hits = _hit_count(text, ("后果", "代价", "因此", "这让", "反而", "逼得", "不得不"))
    return _clamp(35 + paragraph_score + min(causal_hits, 7) * 5 + min(consequence_hits, 4) * 5)


def _interaction_score(text: str) -> int:
    dialogue_marks = text.count("“") + text.count("”") + text.count('"')
    reaction_hits = _hit_count(text, REACTION_MARKERS)
    question_hits = text.count("？") + text.count("?")
    return _clamp(30 + min(dialogue_marks, 10) * 4 + min(reaction_hits, 6) * 5 + min(question_hits, 4) * 4)


def _embedded_setting_score(text: str) -> int:
    setting_hits = _hit_count(text, SETTING_EMBED_MARKERS)
    action_hits = _hit_count(text, ACTION_MARKERS)
    exposition_hits = _hit_count(text, EXPOSITION_MARKERS)
    score = 40 + min(setting_hits, 6) * 7 + min(action_hits, 6) * 3
    if exposition_hits:
        score -= min(30, exposition_hits * 10)
    return _clamp(score)


def _hook_score(ending: str) -> int:
    hook_hits = _hit_count(ending, HOOK_MARKERS)
    pressure_hits = _hit_count(ending, OBSTACLE_MARKERS + COST_MARKERS)
    question_hits = ending.count("？") + ending.count("?")
    return _clamp(35 + min(hook_hits, 4) * 12 + min(pressure_hits, 3) * 7 + min(question_hits, 2) * 5)


def _recommendations(checks: dict[str, int]) -> list[str]:
    mapping = {
        "opening_in_scene": "开场先进入具体处境，用动作、声音、关系张力或异常细节牵引读者。",
        "protagonist_action_chain": "补足主角目标、阻碍、主动选择、收益和代价，避免只写事件摘要。",
        "causal_unit_flow": "让后一小单元承接前一小单元后果，减少跳跃式剧情梗概。",
        "interaction_reaction": "增加人物对话、误判、沉默、追问或身体反应，让人物像在现场。",
        "embedded_setting": "把设定放进交易、冲突、观察、代价和人物关系里，不要说明书式交代。",
        "earned_hook": "章末钩子要由本章行动导致，留下具体危险、发现、误会或机会。",
    }
    return [mapping[name] for name, value in checks.items() if value < 60]


def _hit_count(text: str, markers: tuple[str, ...]) -> int:
    return sum(1 for marker in markers if marker and marker in text)


def _slice_chinese(text: str, limit: int, *, tail: bool = False) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:] if tail else text[:limit]


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
