from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class EvidenceFinding:
    basis_type: str
    basis: str
    severity: str
    evidence: str
    impact: str
    suggestion: str
    location: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def build_quality_evidence_report(
    *,
    content: str,
    brief_text: str = "",
    canon_text: str = "",
    approved_context: str = "",
) -> dict:
    """Build an evidence-backed quality report.

    This is intentionally narrower than the full quality gate. It does not
    replace scoring; it gives every human-facing issue an explicit basis.
    """
    findings: list[EvidenceFinding] = []
    findings.extend(_hard_fact_findings(content=content, brief_text=brief_text, canon_text=canon_text))
    findings.extend(_reality_logic_findings(content))
    findings.extend(_craft_relation_findings(content))
    findings.extend(_internal_sample_findings(content=content, approved_context=approved_context))
    findings.extend(_reader_experience_findings(content, brief_text=brief_text))
    findings = _dedupe(findings)
    severity_order = {"blocker": 0, "high": 1, "medium": 2, "low": 3}
    findings.sort(key=lambda item: (severity_order.get(item.severity, 9), item.basis_type, item.location))
    return {
        "schema": "quality_evidence_report_v1",
        "summary": {
            "finding_count": len(findings),
            "blocker_count": sum(1 for item in findings if item.severity == "blocker"),
            "high_count": sum(1 for item in findings if item.severity == "high"),
            "basis_types": sorted({item.basis_type for item in findings}),
        },
        "findings": [item.to_dict() for item in findings],
    }


def format_quality_evidence_markdown(report: dict) -> str:
    summary = report.get("summary") or {}
    lines = [
        "# 依据型质检报告",
        "",
        f"- 问题数: {summary.get('finding_count', 0)}",
        f"- blocker: {summary.get('blocker_count', 0)}",
        f"- high: {summary.get('high_count', 0)}",
        f"- 依据类型: {'、'.join(summary.get('basis_types') or []) or '无'}",
        "",
    ]
    findings = report.get("findings") or []
    if not findings:
        lines.append("未发现需要进入人审的依据型问题。")
        return "\n".join(lines)
    for idx, item in enumerate(findings, start=1):
        lines.extend(
            [
                f"## {idx}. {item.get('basis_type')} / {item.get('severity')}",
                f"- 依据: {item.get('basis')}",
                f"- 证据: {item.get('evidence')}",
                f"- 影响: {item.get('impact')}",
                f"- 建议: {item.get('suggestion')}",
                "",
            ]
        )
    return "\n".join(lines)


def _hard_fact_findings(*, content: str, brief_text: str, canon_text: str) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    required_from_brief = [
        ("沈渡", "主角锚点"),
        ("是丢了，还是借出去了", "沈渡反问"),
        ("三天后你不到石阶", "三天期限"),
        ("前两个", "前两个持牌人伏笔"),
        ("柴棚里是不是有东西", "章末强钩子"),
    ]
    for marker, label in required_from_brief:
        if marker in brief_text and marker not in content:
            findings.append(
                EvidenceFinding(
                    basis_type="硬事实依据",
                    basis=f"当前 brief 要求保留「{label}」",
                    severity="blocker",
                    evidence=f"正文未出现关键锚点: {marker}",
                    impact="正文偏离已确认 brief，后续修订会沿错误方向推进。",
                    suggestion=f"补回「{marker}」对应场景功能，不要用同义解释替代。",
                )
            )
    if "当前作品名《我不是剑仙》" in brief_text and "沈渡" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="硬事实依据",
                basis="当前作品锚点要求主角沈渡承接",
                severity="blocker",
                evidence="正文未出现沈渡",
                impact="可能生成到了其他书或旧测试稿。",
                suggestion="废弃该稿，重新按当前 book/chapter brief 生成。",
            )
        )
    if canon_text and "蜀山问道" in canon_text and any(term in content for term in ("药铺治伤", "镖局试手")):
        findings.append(
            EvidenceFinding(
                basis_type="硬事实依据",
                basis="作者确认当前方向是《蜀山问道》入口后的真实蜀山，不沿用旧药铺/镖局线",
                severity="blocker",
                evidence=_first_hit_excerpt(content, ("药铺治伤", "镖局试手")),
                impact="旧方向污染当前章节。",
                suggestion="废弃旧线内容，回到当前 brief。",
            )
        )
    return findings


def _reality_logic_findings(content: str) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    if "走不到山门" in content and "腿还在流血" not in content and "膝盖" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="现实认知依据",
                basis="伤情与行动限制必须匹配",
                severity="medium",
                evidence=_first_hit_excerpt(content, ("走不到山门",)),
                impact="读者会质疑轻伤为什么构成无法移动的理由。",
                suggestion="补可见伤情或改成拖延话术，不把它写成客观事实。",
            )
        )
    if "人在我院里三天" in content and "出了这门" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="现实认知依据",
                basis="陌生人担保要有边界，否则显得无条件收留",
                severity="high",
                evidence=_first_hit_excerpt(content, ("人在我院里三天",)),
                impact="老丈会像工具人，而不是自保的市井人。",
                suggestion="补一句切割边界，如不认来路、不认身份、出门不管。",
            )
        )
    return findings


def _craft_relation_findings(content: str) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    relation_issues = {
        "正好对上": "物件关系被写成拼图机关，违背同一制式的关系链。",
        "赌赢半局": "人物心理被写成赌徒基调，不贴沈渡谨慎求生。",
        "像同一批刻出来的印": "比喻容易把制度物件写成装饰性关系，需确认是否过度。",
    }
    for marker, impact in relation_issues.items():
        if marker in content:
            findings.append(
                EvidenceFinding(
                    basis_type="范文/语料依据",
                    basis="句子关系合法性要求物件、动作、心理之间关系顺直，不硬拼、不乱关联",
                    severity="high" if marker != "像同一批刻出来的印" else "low",
                    evidence=_first_hit_excerpt(content, (marker,)),
                    impact=impact,
                    suggestion="改为可验证关系，如同一制式、断口新旧、牌面磨损，不写成机关式对照。",
                )
            )
    return findings


def _internal_sample_findings(*, content: str, approved_context: str) -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    if approved_context and "活到天亮" in approved_context and "活到天亮" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="作品内部样本依据",
                basis="前章已确认弱面板只作轻提示，不解题",
                severity="low",
                evidence="本章未出现【活到天亮】弱提示",
                impact="不一定是错误，但可能削弱 ch1 到 ch2 的状态承接。",
                suggestion="如不影响节奏可不补；若补，只允许一闪而过，不解释下一步。",
            )
        )
    if content.count("看不出来") > 2:
        findings.append(
            EvidenceFinding(
                basis_type="作品内部样本依据",
                basis="本书已确认要贴沈渡视角，但不应反复用同一句旁白声明不确定",
                severity="medium",
                evidence=f"「看不出来」出现 {content.count('看不出来')} 次",
                impact="不确定性会变成说明腔，而不是现场动作。",
                suggestion="保留关键处 1-2 次，其余改成停顿、动作、眼神。",
            )
        )
    return findings


def _reader_experience_findings(content: str, brief_text: str = "") -> list[EvidenceFinding]:
    findings: list[EvidenceFinding] = []
    requires_takeaway_pressure = any(
        marker in brief_text for marker in ("跟我回观里", "带回观里", "带走沈渡", "被带走")
    )
    if requires_takeaway_pressure and "跟我回观里" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="读者体验依据",
                basis="场景压力需要明确的当下威胁",
                severity="blocker",
                evidence="正文缺少「跟我回观里」或等价带走压力",
                impact="查牌场景会变成问答，没有压迫递进。",
                suggestion="补道士要带走沈渡的明确动作或台词。",
            )
        )
    requires_woodshed_hook = "柴棚里是不是有东西" in brief_text
    if requires_woodshed_hook and "柴棚里是不是有东西" not in content:
        findings.append(
            EvidenceFinding(
                basis_type="读者体验依据",
                basis="章末必须给下一章行动钩子",
                severity="blocker",
                evidence="正文缺少「柴棚里是不是有东西」或等价章末发现",
                impact="读者不知道下一章为什么必须继续看。",
                suggestion="章末保留柴棚疑物、老丈沉默、沈渡空腰间三件事。",
            )
        )
    if content.count("也许") >= 3:
        findings.append(
            EvidenceFinding(
                basis_type="读者体验依据",
                basis="不确定性应由动作呈现，不能靠旁白枚举可能性",
                severity="medium",
                evidence=f"「也许」出现 {content.count('也许')} 次",
                impact="读起来像分析说明，而不是小说现场。",
                suggestion="删掉旁白枚举，保留动作和沉默。",
            )
        )
    overpressure_markers = ("命得交给", "落在他心口", "血一下冲上头顶", "半点由不得自己")
    overpressure_hits = [marker for marker in overpressure_markers if marker in content]
    if len(overpressure_hits) >= 3:
        findings.append(
            EvidenceFinding(
                basis_type="读者体验依据",
                basis="紧张程度必须由场景风险逐步抬升，不能开头就把普通盘问写成生死压迫",
                severity="high",
                evidence="、".join(overpressure_hits),
                impact="读感会持续压抑，人物反应显得过激，章节基调容易滑向鬼故事式悬疑。",
                suggestion="降低开头生死词和压迫词密度，把主角反应改成警惕、憋话、观察、等待；等道士明确要带走时再抬高压力。",
            )
        )
    compressed_markers = ("全在嘴里打转", "一句都用不上", "心反倒跟着冷下来")
    compressed_hits = [marker for marker in compressed_markers if marker in content]
    if compressed_hits:
        findings.append(
            EvidenceFinding(
                basis_type="读者体验依据",
                basis="句子精炼不能牺牲清楚表达和自然语法，心理过渡要让读者知道人物因何收住话",
                severity="medium",
                evidence=_first_hit_excerpt(content, tuple(compressed_hits)),
                impact="读者会觉得句子不伦不类，像压缩提纲而不是自然叙事。",
                suggestion="把抽象压缩句改成场景因果句：先写旧告诫触发，再写将出口的话被憋回去，再点明具体该由谁答。",
            )
        )
    return findings


def _first_hit_excerpt(text: str, markers: tuple[str, ...], width: int = 120) -> str:
    for marker in markers:
        idx = text.find(marker)
        if idx >= 0:
            start = max(0, idx - width // 2)
            end = min(len(text), idx + len(marker) + width // 2)
            return text[start:end].strip()
    return ""


def _dedupe(findings: list[EvidenceFinding]) -> list[EvidenceFinding]:
    seen: set[tuple[str, str, str]] = set()
    out: list[EvidenceFinding] = []
    for item in findings:
        key = (item.basis_type, item.severity, item.evidence)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# STOPGAP (2026-09-10, Claude 接管会话): production_reviewing.py 引用了
# build_quality_evidence_chain(content, report_data)，但该函数从未被写出——
# stabilization WIP 是改名/重构到一半的状态，HEAD 因此无法 import
# production_reviewing。此处按调用点契约补一个最小实现：第二个位置参数
# (report_data) 目前仅收下不使用，底层委托 build_quality_evidence_report。
# TODO: 由用户确认 evidence_chain 节的最终语义后替换为正式实现。
# ---------------------------------------------------------------------------
def build_quality_evidence_chain(content: str, report_data: dict | None = None) -> dict:
    report = build_quality_evidence_report(content=content or "")
    report["stopgap"] = "build_quality_evidence_chain 为 2026-09-10 过渡实现，语义待用户确认"
    return report
