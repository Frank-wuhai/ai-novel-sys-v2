from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / "reference_corpus" / "reality_logic"
DOMAINS_DIR = LIB_DIR / "domains"


@dataclass(frozen=True)
class RealityLogicReport:
    score: int
    matches: list[dict]
    severity_counts: dict[str, int]
    issues: list[str]
    warnings: list[str]
    prompt_block: str

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "matches": self.matches,
            "severity_counts": self.severity_counts,
            "issues": self.issues,
            "warnings": self.warnings,
            "prompt_block": self.prompt_block,
        }


def evaluate_reality_logic(text: str, *, prompt_limit: int = 6) -> RealityLogicReport:
    cards = load_reality_logic_cards()
    matches = [match for card in cards if (match := match_reality_logic_card(text, card))]
    counts = Counter(match["severity"] for match in matches)
    issues = [f"reality_logic_blocker:{item['id']}" for item in matches if item["severity"] == "blocker"]
    warnings = [
        f"reality_logic:{item['severity']}:{item['id']}:{item['scenario']}"
        for item in matches
        if item["severity"] != "blocker"
    ]
    score = max(0, min(100, 100 - counts.get("blocker", 0) * 25 - counts.get("high", 0) * 5 - counts.get("medium", 0) * 2))
    return RealityLogicReport(
        score=score,
        matches=matches,
        severity_counts=dict(counts),
        issues=issues,
        warnings=warnings,
        prompt_block=build_reality_logic_prompt_block(matches, limit=prompt_limit),
    )


def build_reality_logic_prompt_for_context(context: str, *, limit: int = 6) -> str:
    report = evaluate_reality_logic(context, prompt_limit=limit)
    if report.matches:
        return report.prompt_block
    static = LIB_DIR / "reality_logic_prompt_block.md"
    if static.exists():
        return static.read_text(encoding="utf-8").strip()
    return ""


def load_reality_logic_cards() -> list[dict]:
    cards: list[dict] = []
    if not DOMAINS_DIR.exists():
        return cards
    for path in sorted(DOMAINS_DIR.glob("*.jsonl")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            card = json.loads(line)
            card["_file"] = str(path.relative_to(ROOT))
            card["_line"] = lineno
            cards.append(card)
    return cards


def match_reality_logic_card(text: str, card: dict) -> dict | None:
    terms = [str(term) for term in card.get("trigger_terms", []) if str(term)]
    hits = [term for term in terms if term in (text or "")]
    hits = _refine_hits(text or "", card, hits)
    if not hits:
        return None
    severity = _refined_severity(text or "", card, hits, str(card.get("severity") or "medium"))
    threshold = 1 if severity in {"blocker", "high"} else 2
    if len(hits) < threshold:
        return None
    return {
        "id": card.get("id"),
        "domain": card.get("domain"),
        "scenario": card.get("scenario"),
        "severity": severity,
        "trigger_hits": hits[:12],
        "reader_default": card.get("reader_default"),
        "common_constraints": card.get("common_constraints", [])[:5],
        "fake_logic_patterns": card.get("fake_logic_patterns", [])[:5],
        "question_checklist": card.get("question_checklist", [])[:5],
        "usable_plot_patterns": card.get("usable_plot_patterns", [])[:4],
        "excerpt": _relevant_excerpt(text or "", hits),
        "source_card": {"file": card.get("_file"), "line": card.get("_line")},
    }


def build_reality_logic_prompt_block(matches: list[dict], *, limit: int = 6) -> str:
    picked = sorted(matches, key=lambda m: {"blocker": 0, "high": 1, "medium": 2, "low": 3}.get(m["severity"], 2))[:limit]
    if not picked:
        return ""
    lines = [
        "【现实认知校准｜先过常识关】",
        "剧情设计/评审前，先确认行业、价格、流程、人物选择是否符合普通读者默认认知。",
    ]
    for item in picked:
        lines.append(f"- {item['domain']}｜{item['scenario']}（{item['severity']}）")
        lines.append(f"  读者默认：{item['reader_default']}")
        if item.get("common_constraints"):
            lines.append("  约束：" + "；".join(item["common_constraints"][:3]))
        if item.get("fake_logic_patterns"):
            lines.append("  避免：" + "；".join(item["fake_logic_patterns"][:3]))
        if item.get("question_checklist"):
            lines.append("  必答：" + "；".join(item["question_checklist"][:3]))
    lines.append("【现实认知校准结束】")
    return "\n".join(lines)


def _relevant_excerpt(text: str, terms: list[str], limit: int = 110) -> str:
    compact = re.sub(r"\s+", " ", text or "")
    positions = [compact.find(term) for term in terms if term and compact.find(term) >= 0]
    if not positions:
        return ""
    pos = min(positions)
    start = max(0, pos - 35)
    end = min(len(compact), pos + limit)
    value = compact[start:end].strip()
    return value if len(value) < limit + 20 else value[: limit + 20] + "..."


def _refine_hits(text: str, card: dict, hits: list[str]) -> list[str]:
    card_id = str(card.get("id") or "")
    if card_id == "tech_hardware_repair_001":
        repair_terms = ("拆开后盖", "保险丝", "接口氧化", "砂纸", "胶布", "修头盔", "清灰")
        if hits == ["接线"] and not any(term in text for term in repair_terms):
            return []
    if card_id == "narrative_social_role_scope_001":
        role_terms = ("老丈", "老人", "村民", "药农", "猎户", "门房", "管事")
        internal_terms = ("清虚观", "丢了", "库房", "打过招呼", "听说", "送柴", "送药", "送米", "供货", "传话")
        if not any(term in text for term in role_terms) or not any(term in text for term in internal_terms):
            return []
    return hits


def _refined_severity(text: str, card: dict, hits: list[str], severity: str) -> str:
    card_id = str(card.get("id") or "")
    if card_id == "transaction_secondhand_electronics_001" and _plausible_rented_old_device(text):
        return "medium"
    if card_id == "tech_vr_neural_device_001" and _plausible_vr_login_flow(text):
        return "high"
    if card_id == "narrative_social_role_scope_001" and _plausible_limited_social_knowledge(text):
        return "high"
    if card_id == "choice_short_term_money_001" and _plausible_short_term_money_choice(text):
        return "high"
    if card_id == "social_emergency_choice_001" and _plausible_emergency_options_excluded(text):
        return "high"
    return severity


def _plausible_rented_old_device(text: str) -> bool:
    if any(term in text for term in ("五十块", "50块", "五十元", "50元", "保险丝", "胶布", "抵租", "抵房租")):
        return False
    source_ok = any(term in text for term in ("租来", "租的", "租赁", "工作室淘汰", "网吧淘汰", "旧设备"))
    flow_ok = any(term in text for term in ("验证账号", "校准", "安全提示", "正规流程", "客户端"))
    return source_ok and flow_ok


def _plausible_vr_login_flow(text: str) -> bool:
    return "头盔" in text and "校准" in text and any(term in text for term in ("验证账号", "安全提示", "正规流程", "客户端", "紧急退出"))


def _plausible_limited_social_knowledge(text: str) -> bool:
    source_ok = any(
        term in text
        for term in (
            "送柴", "送米", "送药", "送柴米", "柴米药草", "供货",
            "山下人留意", "让山下人留意", "巡山弟子", "听说", "留意",
        )
    )
    if not source_ok:
        return False
    return not any(term in text for term in ("库房丢", "整理库房", "外门管事上回跟我打过招呼", "内部"))


def _plausible_short_term_money_choice(text: str) -> bool:
    has_gap = any(term in text for term in ("欠租", "房租", "兜里只剩", "换锁"))
    has_payoff = any(term in text for term in ("结八百", "八百", "订金", "担保", "工单号"))
    has_risk_check = any(term in text for term in ("没有马上点接单", "先翻", "备注", "最坏", "风险", "能查"))
    has_alternatives = any(term in text for term in ("借钱", "日结零工", "临时搬砖", "赶不上"))
    return has_gap and has_payoff and has_risk_check and has_alternatives


def _plausible_emergency_options_excluded(text: str) -> bool:
    exit_blocked = any(term in text for term in ("退不出去", "退出按钮", "灰掉", "紧急退出", "一动不动"))
    no_normal_help = any(term in text for term in ("没摸到手机", "手机也摸不到", "摸不到手机", "喊了两声", "等人救", "迷路"))
    immediate_need = any(term in text for term in ("伤", "流血", "活到天亮", "找条活着", "指路"))
    return exit_blocked and no_normal_help and immediate_need
