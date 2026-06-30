from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ExpressionPrecisionReport:
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


@dataclass(frozen=True)
class PrecisionFinding:
    category: str
    message: str
    suggestion: str

    def text(self) -> str:
        return f"{self.message} 建议：{self.suggestion}"


@dataclass(frozen=True)
class CollocationRule:
    left: str
    right: str
    message: str
    suggestion: str
    penalty: int = 12


@dataclass(frozen=True)
class PhraseRule:
    marker: str
    message: str
    suggestion: str
    penalty: int = 10


COLLOCATION_RULES = (
    CollocationRule("别的是", "刀穗", "刀穗通常系在刀柄或刀鞘上，不宜写成腰上别着刀穗。", "改为“腰间那把短刀的柄尾垂着一截刀穗”。"),
    CollocationRule("别着", "刀穗", "“别着刀穗”物件动作不准。", "改为“佩着短刀，刀柄下垂着刀穗”或“刀鞘上系着刀穗”。"),
    CollocationRule("穿的是", "料子", "“穿的是料子”偏生硬。", "改为“衣裳是青阳镇布庄常见的细麻料”或“袖口露着青阳布庄的织纹”。"),
    CollocationRule("鞋底", "出处", "站立视角一眼判断鞋底出处不可信，需补动作或观察条件。", "改成鞋帮纹样、鞋沿草编、泥印，或让对方抬脚/踩凳露出鞋底。"),
    CollocationRule("一眼", "鞋底", "如果对方站着，观察鞋底需要弯腰、抬脚、泥印或磨痕等条件。", "补“鞋底边露出一圈防滑草结”或“地上留下船工草鞋的横纹泥印”。"),
    CollocationRule("站定", "鞋底", "站着时鞋底通常不可见。", "改为鞋帮、鞋面、鞋沿、鞋底边、泥印或磨痕。"),
    CollocationRule("系统随机给的", "衣服", "角色不应轻易把不合逻辑归因给系统随机，应先感到不对并检查。", "改成主角先检查衣料、刀穗、鞋面等可见细节，再意识到这身行头拼得不对。"),
    CollocationRule("装出来的，", "给个说法", "“装出来的”前面宜补条件，否则语气像缺了半截。", "改为“若是装出来的，今天就得说清谁教你的、替谁探路”。"),
    CollocationRule("按下", "手印", "手印不是按下一个按钮，通常是蘸/刺血后按在纸上。", "改为“蘸了血，按在纸角”或“捏着他的拇指往契纸上一摁”。", penalty=16),
    CollocationRule("签", "手印", "签字和按手印是两个动作，混用会显得不准。", "改为“签名画押”或“按手印”。", penalty=16),
    CollocationRule("佩着", "腰牌", "腰牌通常挂、系、悬在腰间，不是佩着。", "改为“腰间挂着一块木牌/铜牌”。"),
    CollocationRule("别着", "木牌", "木牌更常见是挂着、系着、坠着。", "改为“腰绳上系着一块木牌”。"),
    CollocationRule("看见", "气味", "气味不能被看见。", "改为“闻到一股……”。", penalty=20),
    CollocationRule("看见", "腥味", "气味不能被看见。", "改为“闻到一股腥味”。", penalty=20),
    CollocationRule("看见", "臭味", "气味不能被看见。", "改为“闻到一股臭味”。", penalty=20),
    CollocationRule("看见", "香味", "气味不能被看见。", "改为“闻到一股香味”。", penalty=20),
    CollocationRule("听见", "颜色", "颜色不能被听见。", "改为“看见/瞥见颜色”。", penalty=20),
    CollocationRule("听见", "红色", "颜色不能被听见。", "改为“看见那片红色逼近”。", penalty=20),
    CollocationRule("听见", "黑色", "颜色不能被听见。", "改为“看见那片黑色压近”。", penalty=20),
)

PHRASE_RULES = (
    PhraseRule("布上有刺鼻的靛蓝味", "“布上有刺鼻的靛蓝味”搭配生硬，像把颜色当气味。", "改为“粗布散着刺鼻的染料味，靛蓝水顺着布纹渗开”。", penalty=14),
    PhraseRule("靛蓝味", "“靛蓝味”不自然，靛蓝是颜色/染料名，气味应落到染料、潮布或药水。", "改为“染料味”“潮布味”或“靛蓝染水的酸涩气”。", penalty=12),
    PhraseRule("拿嘴买命", "“拿嘴买命”语义别扭，威胁关系不清。", "改为“喝了水，就少开口惹祸”或“想活命，就别乱问”。", penalty=16),
    PhraseRule("少拿嘴买命", "“少拿嘴买命”不是自然中文表达。", "改为“少拿嘴惹祸”或“别靠嘴把命送出去”。", penalty=16),
    PhraseRule("死在馆里，馆里给薄棺", "“死在馆里给薄棺”作为收人逻辑过于离谱，需要更可信的利益/威胁。", "改为“伤在馆里算馆里的账，逃在外头算你家的债”，让威胁和制度更合理。", penalty=12),
    PhraseRule("量不到你的骨头", "“量不到你的骨头”意象不清，威胁显得硬凑。", "改为符合当前势力/场景的具体威胁，或直接写“先量你的手指”。", penalty=10),
)

OBSERVATION_LEAPS = (
    ("一眼", "认出"),
    ("一看", "知道"),
    ("从头看到脚", "鞋底"),
    ("扫了一眼", "来路"),
    ("看了两秒", "出处"),
)

UNCERTAIN_INFERENCE_MARKERS = ("要么", "肯定", "必是", "一定", "显然", "只能说明")
CONDITIONAL_SOFTENERS = ("如果", "若是", "要是", "多半", "像是", "看着像", "八成", "怕是")


def evaluate_expression_precision(text: str) -> ExpressionPrecisionReport:
    body = str(text or "")
    findings: list[PrecisionFinding] = []
    issues: list[str] = []
    collocation_score = _collocation_score(body, findings)
    observation_score = _observation_logic_score(body, findings)
    inference_score = _inference_chain_score(body, findings)
    wording_score = _wording_specificity_score(body, findings)
    checks = {
        "object_verb_collocation": collocation_score,
        "observation_logic": observation_score,
        "inference_chain": inference_score,
        "wording_specificity": wording_score,
    }
    for name, value in checks.items():
        if value < 60:
            issues.append(f"{name}={value}")
    score = round(sum(checks.values()) / len(checks))
    recommendations = _recommendations(checks, findings)
    return ExpressionPrecisionReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        examples=[finding.text() for finding in findings[:10]],
        recommendations=recommendations,
    )


def precision_prompt_rules() -> list[str]:
    return [
        "物件和动作必须搭配准确：刀穗是系/垂在刀柄或刀鞘上，不要写成腰上别着刀穗；衣裳是料子做的，不要写穿的是某地料子。",
        "角色能观察到什么必须符合视线条件：站着不能一眼认出鞋底出处，除非写出抬脚、泥印、磨痕、鞋帮纹样等可见证据。",
        "推理链必须补中间证据：从衣料、刀饰、鞋履推断身份时，要说明哪个细节为什么指向某地或某行当。",
        "强判断前要有条件词或证据：没有十足把握时，用“如果/若是/多半/看着像”，不要直接下死结论。",
        "对白也要符合人说话的顺序：先指出看得见的异常，再给判断，再提出要求；不要把作者推理塞成一句全知台词。",
        "身体动作必须写完整：不要写“本能想挣”这种半截动作，应写成“想挣脱/挣开/挣扎着抽回手”。",
        "生成后逐句复盘：这句话里的物件能不能这样动、这个部位能不能被看见、这个结论有没有证据链。",
    ]


def _collocation_score(text: str, findings: list[PrecisionFinding]) -> int:
    score = 90
    sentences = _sentences(text)
    for rule in COLLOCATION_RULES:
        left, right = rule.left, rule.right
        if _rule_matches(sentences, left=left, right=right):
            score -= rule.penalty
            findings.append(PrecisionFinding("collocation", rule.message, rule.suggestion))
    for rule in PHRASE_RULES:
        if rule.marker in text:
            score -= rule.penalty
            findings.append(PrecisionFinding("phrase", rule.message, rule.suggestion))
    for sentence in sentences:
        if re.search(r"(?:本能)?(?:想|要|想要|试图|正要)挣(?:[，。！？；、,.!?;]|$)", sentence):
            score -= 14
            findings.append(
                PrecisionFinding(
                    "action_completion",
                    "“想挣”这类身体动作只写单字动词，读起来像话没说完。",
                    "改为“想挣脱/挣开/挣扎着抽回手”，把动作方向和结果补完整。",
                )
            )
    return max(0, min(100, score))


def _observation_logic_score(text: str, findings: list[PrecisionFinding]) -> int:
    score = 86
    for left, right in OBSERVATION_LEAPS:
        if left in text and right in text:
            score -= 12
            findings.append(
                PrecisionFinding(
                    "observation",
                    f"观察跳跃：'{left}...{right}' 需要补可见证据或动作条件。",
                    "把“一眼知道”改成可见细节、经验来源和试探性判断。",
                )
            )
    sentences = _sentences(text)
    for index, sentence in enumerate(sentences):
        if "鞋底" not in sentence:
            continue
        window = _nearby_text(sentences, index)
        if not any(marker in window for marker in _shoe_visibility_markers()):
            score -= 16
            findings.append(
                PrecisionFinding(
                    "observation",
                    "写到鞋底判断，但近邻句缺少让鞋底可见的动作或痕迹。",
                    "改成鞋帮/鞋沿可见纹样，或补对方抬脚、鞋印、泥痕、磨痕。",
                )
            )
    for index, sentence in enumerate(sentences):
        if not any(marker in sentence for marker in ("认出", "看出", "知道", "断定")):
            continue
        if any(marker in sentence for marker in ("鞋底", "来路", "出处", "门派", "身份")):
            window = _nearby_text(sentences, index)
            if not any(marker in window for marker in ("因为", "纹", "泥", "磨", "旧", "压痕", "口音", "结法", "针脚", "刀茎", "刀鞘", "凭据")):
                score -= 10
                findings.append(
                    PrecisionFinding(
                        "observation",
                        "身份/来路判断缺少可见证据。",
                        "补一处可核对细节，如织纹、泥印、刀鞘结法、口音或凭据。",
                    )
                )
    return max(0, min(100, score))


def _inference_chain_score(text: str, findings: list[PrecisionFinding]) -> int:
    score = 86
    for sentence in _sentences(text):
        if any(marker in sentence for marker in UNCERTAIN_INFERENCE_MARKERS) and not any(
            marker in sentence for marker in CONDITIONAL_SOFTENERS
        ):
            if len(sentence) > 18 and any(marker in sentence for marker in ("来路", "身份", "装", "逃", "门派", "哪条道", "给个说法")):
                score -= 8
                findings.append(
                    PrecisionFinding(
                        "inference",
                        f"强推断缺少缓冲或证据：{sentence[:80]}",
                        "改成“若是/多半/看着像”，并补可见证据。",
                    )
                )
        if "要么" in sentence and sentence.count("要么") >= 2 and not any(marker in sentence for marker in ("若", "如果", "看着", "凭")):
            score -= 6
            findings.append(
                PrecisionFinding(
                    "inference",
                    f"二选一判断过硬：{sentence[:80]}",
                    "改成试探式判断：先指出异常，再说“两种可能”，最后逼问凭据。",
                )
            )
    return max(0, min(100, score))


def _wording_specificity_score(text: str, findings: list[PrecisionFinding]) -> int:
    vague = ("某种", "一些", "东西", "感觉", "似乎", "显得", "一类", "之类")
    score = 82 - min(20, sum(text.count(marker) for marker in vague) * 3)
    if "说法" in text and "给个说法" in text and not any(marker in text for marker in ("怎么来的", "谁给的", "哪家", "凭据")):
        score -= 6
        findings.append(
            PrecisionFinding(
                "wording",
                "“给个说法”可以更具体。",
                "改成“说清这身行头谁给的、替哪家探路、凭据在哪”。",
            )
        )
    return max(0, min(100, score))


def _recommendations(checks: dict[str, int], findings: list[PrecisionFinding]) -> list[str]:
    rows: list[str] = []
    if checks.get("object_verb_collocation", 100) < 70:
        rows.append("逐句检查名词和动词是否真能搭配，尤其是衣料、兵器、饰物、身体动作。")
    if checks.get("observation_logic", 100) < 70:
        rows.append("补足视线条件：角色凭什么看见、看清、认出这个细节。")
    if checks.get("inference_chain", 100) < 70:
        rows.append("把强推断改成证据链：可见细节 -> 可能解释 -> 试探性判断。")
    if checks.get("wording_specificity", 100) < 70:
        rows.append("把含混词换成具体物、具体动作、具体要求。")
    if findings and not rows:
        rows.append("存在少量表达精确性风险，编辑精修时优先核对物件、动作和观察条件。")
    return rows


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?]\s*", text or "") if item.strip()]


def _rule_matches(sentences: list[str], *, left: str, right: str) -> bool:
    return any(left in sentence and right in sentence for sentence in sentences)


def _nearby_text(sentences: list[str], index: int) -> str:
    return "".join(sentences[max(0, index - 1) : min(len(sentences), index + 2)])


def _shoe_visibility_markers() -> tuple[str, ...]:
    return ("抬脚", "鞋印", "泥印", "磨痕", "鞋帮", "鞋沿", "草屑", "低头", "鞋底边", "踩上", "跷脚", "翻起")
