from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NarrativeLogicReport:
    score: int
    checks: dict[str, int]
    issues: list[str]
    examples: list[str]
    recommendations: list[str]

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "checks": self.checks,
            "issues": self.issues,
            "examples": self.examples,
            "recommendations": self.recommendations,
        }


FORCED_COST_PATTERNS = (
    ("喝了水，就少拿嘴买命", "喝水和买命之间缺少因果，像为了制造代价硬拼的狠话。"),
    ("死在馆里，馆里给薄棺", "收人去试拳的制度威胁过于离谱，需要更可信的利益/责任逻辑。"),
    ("量不到你的骨头", "威胁意象不清，像临时造句，不像人物自然说话。"),
    ("救我家三日，不是救一世", "人情债表述过硬，需补角色为什么只敢给三日、三日后会发生什么。"),
)
CAUSAL_ANCHORS = ("因为", "所以", "于是", "因此", "这才", "这让", "逼得", "换来", "导致", "后果", "代价", "欠", "账", "按印")
PAYOFF_ANCHORS = (
    # 古风/传统 payoff anchors — 都市/悬疑/科幻都可复用的通用抽象词
    "换", "抵", "保", "救", "拖", "三日", "账", "规矩", "凭据", "证据",
    "谁在乎", "会怎样",
    # Change A (2026-07-02): modern/urban prose anchors so contemporary
    # settings (职场/都市/悬疑现代向) can register payoff-grounding hits
    # instead of forever hitting the flat 40 baseline. These keywords mark
    # (a) explicit trade/交换, (b) cost/代价, (c) commitment/承诺, and
    # (d) supernatural/rule-based payoff markers (提问机会/忘掉/必须…)
    # that make chapter-end hooks feel earned rather than dropped-in.
    "交换", "交代", "交易", "承诺", "承担",
    "代价", "抓住", "把柄", "秘密", "认账",
    "会失去", "就没了", "换来", "换到",
    "机会", "忘掉", "必须", "不敢",
    "答应", "负责", "拿走", "算账",
)
ATMOSPHERE_SENSORY = ("雨", "风", "灯", "影", "味", "冷", "热", "潮", "血", "泥", "声", "疼", "汗")
ATMOSPHERE_PRESSURE = ("怕", "慌", "疼", "逼", "躲", "债", "伤", "死", "追", "压", "不敢", "危险")


def evaluate_narrative_logic(text: str) -> NarrativeLogicReport:
    body = str(text or "")
    examples: list[str] = []
    checks = {
        "causal_continuity": _causal_continuity_score(body),
        "cost_plausibility": _cost_plausibility_score(body, examples),
        "scene_atmosphere": _scene_atmosphere_score(body),
        "payoff_grounding": _payoff_grounding_score(body),
    }
    score = round(sum(checks.values()) / len(checks))
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    recommendations = _recommendations(checks)
    return NarrativeLogicReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        examples=examples[:8],
        recommendations=recommendations,
    )


def narrative_logic_prompt_rules() -> list[str]:
    return [
        "代价必须有社会/利益/身体/关系上的因果来源，不要为了满足“有代价”硬塞荒唐惩罚。",
        "人物狠话要符合身份和处境；如果一句话读起来像作者硬造金句，改成更朴素、更具体的威胁或交易。",
        "每个选择都要回答：主角为什么不能不选、选了立刻失去什么、暂时换来什么。",
        "场景氛围不是堆物件名；天气、气味、光线、声音必须影响人物判断或行动。",
        "章末钩子必须由本章行为自然导致，不能突然给一个旧物、门派或神秘人当奖励。",
    ]


def _causal_continuity_score(text: str) -> int:
    paragraphs = [item for item in text.splitlines() if item.strip()]
    if not paragraphs:
        return 0
    anchor_hits = sum(1 for marker in CAUSAL_ANCHORS if marker in text)
    paragraph_bonus = min(len(paragraphs), 10) * 3
    return _clamp(38 + min(anchor_hits, 8) * 6 + paragraph_bonus)


def _cost_plausibility_score(text: str, examples: list[str]) -> int:
    score = 86
    for marker, message in FORCED_COST_PATTERNS:
        if marker in text:
            score -= 18
            examples.append(message)
    if "代价" in text and not any(marker in text for marker in ("失去", "欠", "伤", "疼", "按印", "暴露", "三日", "抵", "换")):
        score -= 10
        examples.append("正文提到代价，但没有落到可见损失、关系债、身体伤害或资源交换。")
    return _clamp(score)


def _scene_atmosphere_score(text: str) -> int:
    paragraphs = [item for item in text.splitlines() if item.strip()]
    if not paragraphs:
        return 0
    atmospheric = 0
    for paragraph in paragraphs:
        if any(marker in paragraph for marker in ATMOSPHERE_SENSORY) and any(marker in paragraph for marker in ATMOSPHERE_PRESSURE):
            atmospheric += 1
    ratio = atmospheric / len(paragraphs)
    return _clamp(35 + round(ratio * 65))


def _payoff_grounding_score(text: str) -> int:
    payoff_hits = sum(1 for marker in PAYOFF_ANCHORS if marker in text)
    if "旧物" in text and not any(marker in text for marker in ("来源", "谁给", "谁认", "规矩", "旧债")):
        return 45
    return _clamp(40 + min(payoff_hits, 10) * 6)


def _recommendations(checks: dict[str, int]) -> list[str]:
    rows: list[str] = []
    if checks.get("causal_continuity", 100) < 60:
        rows.append("补清每一场的因果：上一动作造成什么麻烦，下一动作为什么非做不可。")
    if checks.get("cost_plausibility", 100) < 60:
        rows.append("重写离谱代价：让惩罚、交易和威胁来自制度、利益、身体伤害或关系债。")
    if checks.get("scene_atmosphere", 100) < 60:
        rows.append("氛围要服务人物压力：光线、气味、雨声、空间边界必须改变人物判断或行动。")
    if checks.get("payoff_grounding", 100) < 60:
        rows.append("章末收益/钩子必须有来源、规则和代价，不能像系统奖励或作者硬塞道具。")
    return rows


def _clamp(value: int) -> int:
    return max(0, min(100, int(value)))
