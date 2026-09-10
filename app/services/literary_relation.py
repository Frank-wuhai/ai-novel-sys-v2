from __future__ import annotations

import re
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class LiteraryRelationFinding:
    category: str
    message: str
    suggestion: str
    penalty: int = 10

    def text(self) -> str:
        return f"{self.message} 建议：{self.suggestion}"


@dataclass(frozen=True)
class LiteraryRelationReport:
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


SENSORY_SOURCES = (
    "酸味", "馊味", "霉味", "腥味", "臭味", "香味", "药味", "血腥气", "潮气", "热气", "冷气",
    "苦味", "烟味", "汗味", "铁锈味", "土腥味", "塑料味", "泡面味",
)
ABSTRACT_TARGETS = ("欠账", "债", "命运", "人生", "压力", "焦虑", "孤独", "恐惧", "绝望", "规矩")
ABSTRACT_TARGET_RE = re.compile(r"欠.{0,4}账|债|命运|人生|压力|焦虑|孤独|恐惧|绝望|规矩")
SENSORY_PATH_VERBS = ("飘", "钻", "冲", "呛", "熏", "闷", "裹", "混", "贴", "泛", "涌", "漫", "散")
BODY_LANDING = ("鼻", "喉", "嗓", "脑门", "额头", "胃", "胸口", "眼", "牙", "皮肤", "后颈")
SCENE_FUNCTION_MARKERS = ("穷", "旧", "脏", "乱", "闷", "热", "累", "困", "窄", "压", "躲不开", "懒得", "皱")
ROOT = Path(__file__).resolve().parents[2]
OVERRIDE_PATH = ROOT / "reference_corpus" / "literary_relation_overrides.json"
SENSORY_DOMAIN_MARKERS = {
    "smell": ("味", "气味", "酸", "馊", "霉", "腥", "臭", "香", "呛", "熏", "鼻"),
    "touch": ("冷", "热", "烫", "凉", "疼", "痛", "麻", "硌", "刺", "潮", "湿", "汗", "黏"),
    "sound": ("声", "响", "咔", "砰", "哗", "听", "耳", "嗡", "吱"),
    "light": ("光", "亮", "暗", "黑", "白", "红", "黄", "影", "灯", "闪"),
    "abstract": ABSTRACT_TARGETS,
}
PHYSICAL_CONTAINERS = ("屋", "房间", "隔断间", "墙", "门", "窗", "床", "桌", "地", "山", "巷", "街")
UNNATURAL_PERSONIFICATION = (
    ("屋", "吐出", "一口气"),
    ("屋", "喘出", "一口气"),
    ("屋", "烂出", "一口气"),
    ("房间", "吐出", "一口气"),
    ("墙", "咽下", "声音"),
    ("门板", "哑的", ""),
)
COEXIST_BUT_DO_NOT_BLEND = {
    "馊潮气": "馊味和潮气可以共存，但不宜压成一个新词。",
    "酸潮气": "酸味和潮气可以混合，不宜压成“酸潮气”。",
    "臭潮气": "臭味和潮气可以并列，不宜压成“臭潮气”。",
    "焦虑气": "焦虑是心理状态，不宜直接造作气味名词。",
    "债味": "债务不是气味来源，不宜写成“债味”。",
    "穷酸风": "穷困和风不是稳定感官组合，容易变成概念拼贴。",
}
ATTRIBUTE_SPLICE_RE = re.compile(
    r"(隔夜泡面|泡面汤|霉味|酸味|馊味|潮气|热气|冷气|血腥气)[，,]\s*(酸|馊|臭|难闻|潮|冷|热|重|浓|厚|冲|刺|腻)(?:得很|得厉害|得发沉|。|$)"
)


BAD_PHRASES = (
    LiteraryRelationFinding(
        "lexical_blend",
        "“馊潮气”把可共存的馊味和潮气硬合成一个词，生造逻辑不稳。",
        "改成“酸味/馊味混着潮气”或“潮气里夹着一股馊味”。",
        18,
    ),
    LiteraryRelationFinding(
        "collocation",
        "“烂出一口气”搭配不自然：烂出通常接洞、味儿、汁水、霉斑，不宜接“一口气”。",
        "沿感官链收束，如“熏得人脑门发胀”或“屋里像被热气捂馊了”。",
        18,
    ),
    LiteraryRelationFinding(
        "abstract_analogy",
        "“酸味像欠账”只靠“散不掉”这种抽象共同点硬连，感官域和社会压力域跨度过大。",
        "酸味应优先关联馊、闷、钻鼻、反胃、廉价、狼狈等同域属性。",
        20,
    ),
)


def evaluate_literary_relation(text: str) -> LiteraryRelationReport:
    body = str(text or "")
    sentences = _sentences(body)
    findings: list[LiteraryRelationFinding] = []

    lexical = _lexical_naturalness_score(body, findings)
    analogy = _analogy_legality_score(sentences, findings)
    sensory = _sensory_chain_score(sentences, findings)
    scene_fit = _scene_technique_fit_score(sentences, findings)
    beauty = _beauty_grounding_score(body, findings)

    checks = {
        "relation_legality": round((analogy + scene_fit) / 2),
        "lexical_naturalness": lexical,
        "sensory_chain": sensory,
        "scene_technique_fit": scene_fit,
        "beauty_grounding": beauty,
    }
    score = round(
        checks["relation_legality"] * 0.28
        + lexical * 0.22
        + sensory * 0.20
        + scene_fit * 0.15
        + beauty * 0.15
    )
    issues = [f"{name}={value}" for name, value in checks.items() if value < 60]
    return LiteraryRelationReport(
        score=max(0, min(100, score)),
        checks=checks,
        issues=issues,
        examples=[finding.text() for finding in findings[:12]],
        recommendations=_recommendations(checks, findings),
    )


def literary_relation_prompt_rules() -> list[str]:
    return [
        "先判关系再造句：两个素材不能只因“都散不掉/都压人”就硬连，必须有具体共同属性。",
        "先判断关系类型：属性归属、来源、路径、混合、伴随、引发、比喻、反应，不能把所有关系都写成“名词+形容词”。",
        "气味类素材优先走来源->路径->身体落点->人物反应：从哪来、怎么飘/钻/冲、落到鼻子/喉咙/脑门、人物怎么躲或忍。",
        "不要把可共存关系误压成生造词：馊味和潮气可写“混着/夹着/裹着”，不要硬造“馊潮气”。",
        "比喻必须同域：酸味可连馊、闷、钻鼻、反胃、廉价和狼狈；慎连欠账、命运、人生这类抽象压力。",
        "拟人化必须有语境支撑：屋子、墙、门不能随手吐气、喘气、咽声；如果只是空气运动，就写风、热气、味道怎么动。",
        "漂亮句必须先过搭配关：词和词能不能这么接，物件能不能这样动，感官能不能这样落点。",
        "好看的复合词要能半秒理解：冷腥气、土腥味、湿热气可用；馊潮气、债味、焦虑气这类只可拆开重写。",
        "静态环境段不能写成“名词+属性”拼接；至少补来源、空间滞留、感知路径或人物轻反应中的两项。",
        "紧张动作段少做跨域比喻；若用比喻，只能来自当场可见/可闻/可触的物象，并且不能打断动作因果。",
        "生成后删除伪文学句：凡是读者需要停下来解释“这是什么意思”的漂亮句，先改成准确句。",
    ]


def _lexical_naturalness_score(text: str, findings: list[LiteraryRelationFinding]) -> int:
    score = 92
    overrides = _load_relation_overrides()
    allowed_terms = {str(item.get("term") or "") for item in overrides.get("allowed", []) if isinstance(item, dict)}
    for item in overrides.get("banned", []):
        if not isinstance(item, dict):
            continue
        term = str(item.get("term") or "").strip()
        if term and term in text and term not in allowed_terms:
            score -= 14
            _append_unique(
                findings,
                LiteraryRelationFinding(
                    str(item.get("category") or "manual_banned_collocation"),
                    f"“{term}”已在人工搭配库标为慎用/禁用：{item.get('reason', '')}",
                    str(item.get("suggestion") or "拆成显性关系后重写。"),
                    14,
                ),
            )
    if "馊潮气" in text:
        score -= 18
        _append_unique(findings, BAD_PHRASES[0])
    if "烂出一口气" in text or "烂出了一口气" in text:
        score -= 18
        _append_unique(findings, BAD_PHRASES[1])
    if "酸味像欠账" in text:
        score -= 14
        _append_unique(findings, BAD_PHRASES[2])
    for marker, reason in COEXIST_BUT_DO_NOT_BLEND.items():
        if marker in text and marker != "馊潮气":
            score -= 12
            _append_unique(
                findings,
                LiteraryRelationFinding(
                    "lexical_blend",
                    f"“{marker}”像把关系直接焊成词，读者能猜但中文搭配不稳：{reason}",
                    "拆成“X味混着Y气/夹着Y味”，让关系显性化。",
                    12,
                )
            )
    for subject, verb, obj in UNNATURAL_PERSONIFICATION:
        if subject in text and verb in text and (not obj or obj in text):
            score -= 12
            _append_unique(
                findings,
                LiteraryRelationFinding(
                    "personification_collocation",
                    f"“{subject}{verb}{obj}”拟人搭配风险高，容易显得硬拗。",
                    "先判断是否真要拟人；多数情况下改为空气、热气、味道、声音的具体运动。",
                    12,
                ),
            )
    return max(0, min(100, score))


def _analogy_legality_score(sentences: list[str], findings: list[LiteraryRelationFinding]) -> int:
    score = 88
    for sentence in sentences:
        if not re.search(r"(像|仿佛|好似|如同|似的)", sentence):
            continue
        if any(source in sentence for source in SENSORY_SOURCES) and ABSTRACT_TARGET_RE.search(sentence):
            score -= 32
            findings.append(
                LiteraryRelationFinding(
                    "abstract_analogy",
                    f"跨域比喻风险：{_clip(sentence)}",
                    "感官物先连同域感官、空间或身体反应；抽象压力另用动作和处境呈现。",
                    16,
                )
            )
        domains = _domains(sentence)
        if "abstract" in domains and len(domains - {"abstract"}) >= 1 and not any(source in sentence for source in SENSORY_SOURCES):
            score -= 8
            _append_unique(
                findings,
                LiteraryRelationFinding(
                    "abstract_analogy",
                    f"抽象域和感官/物理域混连，需要确认共同属性是否具体：{_clip(sentence)}",
                    "若共同点说不成一个具体感受或动作，就拆开写处境，不要硬比喻。",
                    8,
                ),
            )
        if "屋" in sentence and "一口气" in sentence and any(v in sentence for v in ("烂出", "吐出", "喘出")):
            score -= 16
            findings.append(
                LiteraryRelationFinding(
                    "collocation",
                    f"拟人搭配过硬：{_clip(sentence)}",
                    "若无强拟人语境，屋子不要“吐/喘一口气”，改写为空气、热气、味道的运动。",
                    14,
                )
            )
    return max(0, min(100, score))


def _sensory_chain_score(sentences: list[str], findings: list[LiteraryRelationFinding]) -> int:
    sensory_sentences = [s for s in sentences if any(term in s for term in SENSORY_SOURCES)]
    if not sensory_sentences:
        return 72
    score = 82
    for sentence in sensory_sentences:
        has_path = any(verb in sentence for verb in SENSORY_PATH_VERBS)
        has_body = any(part in sentence for part in BODY_LANDING)
        has_source = any(marker in sentence for marker in ("从", "床底", "桌角", "门缝", "锅", "碗", "桶", "衣", "墙", "地"))
        if not has_path and not has_body:
            score -= 10
            findings.append(
                LiteraryRelationFinding(
                    "sensory_chain",
                    f"感官素材像标签拼接，缺少路径或身体落点：{_clip(sentence)}",
                    "补“从哪里来/怎么钻进鼻子/熏到哪里/人物如何反应”。",
                    10,
                )
            )
        if not has_source and len(sentence) <= 18:
            score -= 6
    return max(0, min(100, score))


def _scene_technique_fit_score(sentences: list[str], findings: list[LiteraryRelationFinding]) -> int:
    score = 84
    overrides = _load_relation_overrides()
    for sentence in sentences:
        for item in overrides.get("rewrite", []):
            if not isinstance(item, dict):
                continue
            bad = str(item.get("bad") or "").strip()
            if bad and bad in sentence:
                score -= 14
                _append_unique(
                    findings,
                    LiteraryRelationFinding(
                        str(item.get("category") or "manual_rewrite"),
                        f"“{bad}”已在人工改写库标为问题表达：{item.get('reason', '')}",
                        str(item.get("suggestion") or "按人工建议重写。"),
                        14,
                    ),
                )
        if ATTRIBUTE_SPLICE_RE.search(sentence):
            score -= 16
            findings.append(
                LiteraryRelationFinding(
                    "attribute_splice",
                    f"素材和属性像标签拼接，缺少正常写作里的来源、路径和反应：{_clip(sentence)}",
                    "改成“泡面汤没倒/从床底飘来/热气一搅/往鼻子里钻/人物皱眉或懒得理”。",
                    14,
                )
            )
        if any(source in sentence for source in SENSORY_SOURCES) and ABSTRACT_TARGET_RE.search(sentence):
            score -= 12
            findings.append(
                LiteraryRelationFinding(
                    "scene_fit",
                    f"感官细节被硬拔到抽象压力，场景功能跑偏：{_clip(sentence)}",
                    "把压力另写成账单、催租、动作迟疑或选择代价，不要让酸味替欠账说话。",
                    10,
                )
            )
        if _looks_like_static_label_stack(sentence):
            score -= 8
            _append_unique(
                findings,
                LiteraryRelationFinding(
                    "attribute_splice",
                    f"静态细节连续堆标签，缺少关系动词或人物接收：{_clip(sentence)}",
                    "把至少一个细节改成来源、路径、动作或人物反应。",
                    8,
                ),
            )
        if any(source in sentence for source in SENSORY_SOURCES) and "像" in sentence:
            if not any(marker in sentence for marker in SCENE_FUNCTION_MARKERS + BODY_LANDING):
                score -= 8
                findings.append(
                    LiteraryRelationFinding(
                        "scene_fit",
                        f"修辞没有落回场景功能或人物感受：{_clip(sentence)}",
                        "让比喻服务穷困、闷热、脏乱、紧张或角色反应中的一个具体功能。",
                        8,
                    )
                )
    return max(0, min(100, score))


def _beauty_grounding_score(text: str, findings: list[LiteraryRelationFinding]) -> int:
    rhetoric_count = len(re.findall(r"(像|仿佛|好似|如同|似的)", text or ""))
    concrete_count = sum(text.count(marker) for marker in SENSORY_SOURCES + BODY_LANDING + SENSORY_PATH_VERBS)
    score = 76 + min(16, concrete_count * 2)
    if rhetoric_count >= 2 and concrete_count < rhetoric_count * 2:
        score -= 18
        findings.append(
            LiteraryRelationFinding(
                "beauty_grounding",
                "修辞数量高于具体感官和身体落点，容易变成伪文学感。",
                "每个漂亮句前后至少有一个可见/可闻/可触细节和一个人物反应。",
                12,
            )
        )
    return max(0, min(100, score))


def _recommendations(checks: dict[str, int], findings: list[LiteraryRelationFinding]) -> list[str]:
    rows: list[str] = []
    if checks.get("relation_legality", 100) < 70:
        rows.append("重审比喻两端的共同属性：必须同域且具体，不能只靠抽象相似点硬连。")
    if checks.get("lexical_naturalness", 100) < 70:
        rows.append("把生造复合词拆成显性关系：混着、夹着、裹着、从……里飘出来。")
    if checks.get("sensory_chain", 100) < 70:
        rows.append("补感知链：来源、路径、身体落点、人物反应至少两项同时出现。")
    if checks.get("scene_technique_fit", 100) < 70:
        rows.append("按场景功能选技法：环境铺垫重滞留和人物轻反应，紧张动作重即时身体反应。")
    if checks.get("beauty_grounding", 100) < 70:
        rows.append("漂亮句先改准确，再加质感；无法半秒理解的修辞删掉。")
    if findings and not rows:
        rows.append("存在少量语言关系风险，精修时核对搭配、同域关联和感知路径。")
    return rows


def _sentences(text: str) -> list[str]:
    return [item.strip() for item in re.split(r"[。！？!?]\s*", text or "") if item.strip()]


def _clip(text: str, limit: int = 90) -> str:
    value = re.sub(r"\s+", " ", text or "").strip()
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _domains(sentence: str) -> set[str]:
    domains: set[str] = set()
    for domain, markers in SENSORY_DOMAIN_MARKERS.items():
        if any(marker in sentence for marker in markers):
            domains.add(domain)
    return domains


def _looks_like_static_label_stack(sentence: str) -> bool:
    if len(sentence) > 42:
        return False
    comma_count = sentence.count("，") + sentence.count(",")
    if comma_count < 2:
        return False
    has_scene = any(marker in sentence for marker in PHYSICAL_CONTAINERS + SENSORY_SOURCES)
    has_motion = any(marker in sentence for marker in SENSORY_PATH_VERBS + ("走", "伸", "抬", "低", "翻", "钻", "飘"))
    has_reaction = any(marker in sentence for marker in BODY_LANDING + ("皱", "咳", "躲", "忍", "懒得"))
    return has_scene and not (has_motion or has_reaction)


def _append_unique(findings: list[LiteraryRelationFinding], finding: LiteraryRelationFinding) -> None:
    if not any(item.category == finding.category and item.message == finding.message for item in findings):
        findings.append(finding)


@lru_cache(maxsize=1)
def _load_relation_overrides() -> dict:
    try:
        data = json.loads(OVERRIDE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"allowed": [], "banned": [], "rewrite": []}
    if not isinstance(data, dict):
        return {"allowed": [], "banned": [], "rewrite": []}
    return {
        "allowed": data.get("allowed") if isinstance(data.get("allowed"), list) else [],
        "banned": data.get("banned") if isinstance(data.get("banned"), list) else [],
        "rewrite": data.get("rewrite") if isinstance(data.get("rewrite"), list) else [],
    }
