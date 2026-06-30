from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.exc import OperationalError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Book, MarketSignal, PlatformFeedback, StoryArc, StoryBible, StoryFoundation, Volume


@dataclass(frozen=True)
class SkeletonIssue:
    code: str
    severity: str
    message: str
    sources: list[str]
    recommendation: str

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "sources": self.sources,
            "recommendation": self.recommendation,
        }


@dataclass(frozen=True)
class SkeletonGovernanceReport:
    status: str
    score: int
    issues: list[SkeletonIssue]
    recommendations: list[str]
    source_hits: dict[str, list[str]]
    dimensions: dict[str, dict] = field(default_factory=dict)
    evidence_summary: list[str] = field(default_factory=list)
    team_decisions: list[str] = field(default_factory=list)
    human_decisions: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "passed": self.passed,
            "score": self.score,
            "issues": [issue.to_dict() for issue in self.issues],
            "recommendations": self.recommendations,
            "source_hits": self.source_hits,
            "dimensions": self.dimensions,
            "evidence_summary": self.evidence_summary,
            "team_decisions": self.team_decisions or self.human_decisions,
            "human_decisions": self.human_decisions,
        }


@dataclass(frozen=True)
class SkeletonRepairResult:
    before: SkeletonGovernanceReport
    repaired_skeleton: dict[str, str]
    after: SkeletonGovernanceReport
    applied_strategy: str
    next_actions: list[str]

    @property
    def passed(self) -> bool:
        return self.after.passed

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "applied_strategy": self.applied_strategy,
            "skeleton": self.repaired_skeleton,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "next_actions": self.next_actions,
        }


@dataclass(frozen=True)
class MarketRepairContext:
    signal_count: int
    expectations: list[str]
    avoid_rules: list[str]
    source_ids: list[int]

    def to_dict(self) -> dict:
        return {
            "signal_count": self.signal_count,
            "expectations": self.expectations,
            "avoid_rules": self.avoid_rules,
            "source_ids": self.source_ids,
        }


def repair_story_skeleton_with_market_evidence(session: Session, *, book_id: int, skeleton: dict[str, str], max_rounds: int = 2) -> dict:
    context = _market_repair_context(session, book_id=book_id)
    before_sources = {f"form.{key}": str(value or "") for key, value in skeleton.items()}
    before = audit_skeleton_sources({**before_sources, **_market_context_sources(context)})
    repaired = repair_skeleton_draft(skeleton, before)
    repaired = _apply_market_repair_context(repaired, context)
    after = audit_skeleton_sources({**{f"form.{key}": str(value or "") for key, value in repaired.items()}, **_market_context_sources(context)})
    strategy = "market_evidence_repair" if context.signal_count else "standard_repair"
    rounds = max(1, min(3, int(max_rounds or 2)))
    for _ in range(1, rounds):
        if after.passed:
            break
        repaired = _force_repair_blockers(repaired, after)
        repaired = _apply_market_repair_context(repaired, context)
        after = audit_skeleton_sources({**{f"form.{key}": str(value or "") for key, value in repaired.items()}, **_market_context_sources(context)})
        strategy = "market_forced_boundary_repair" if context.signal_count else "forced_boundary_repair"
    result = SkeletonRepairResult(
        before=before,
        repaired_skeleton=repaired,
        after=after,
        applied_strategy=strategy,
        next_actions=_repair_next_actions(after),
    )
    payload = result.to_dict()
    payload["market_context"] = context.to_dict()
    if context.signal_count:
        payload["next_actions"] = [
            *payload["next_actions"],
            "复核市场信号是否适合本书：平台期待只能增强读者承诺，不能替代作者的核心方向。",
        ]
    return payload


def repair_skeleton_until_pass(skeleton: dict[str, str], *, max_rounds: int = 2) -> SkeletonRepairResult:
    before = audit_skeleton_sources({f"form.{key}": str(value or "") for key, value in skeleton.items()})
    repaired = repair_skeleton_draft(skeleton, before)
    after = audit_skeleton_sources({f"form.{key}": str(value or "") for key, value in repaired.items()})
    strategy = "standard_repair"
    rounds = max(1, min(3, int(max_rounds or 2)))
    for _ in range(1, rounds):
        if after.passed:
            break
        repaired = _force_repair_blockers(repaired, after)
        after = audit_skeleton_sources({f"form.{key}": str(value or "") for key, value in repaired.items()})
        strategy = "forced_boundary_repair"
    next_actions = _repair_next_actions(after)
    return SkeletonRepairResult(
        before=before,
        repaired_skeleton=repaired,
        after=after,
        applied_strategy=strategy,
        next_actions=next_actions,
    )


def repair_skeleton_draft(skeleton: dict[str, str], report: SkeletonGovernanceReport | None = None) -> dict[str, str]:
    report = report or audit_skeleton_sources({f"draft.{key}": str(value or "") for key, value in skeleton.items()})
    repaired = {key: str(value or "").strip() for key, value in skeleton.items()}
    premise = repaired.get("premise") or "主角进入核心矛盾后发现，核心卖点不是万能钥匙；他只能凭现场证据、风险选择和承担代价逐步争取主动。"
    current_book = _looks_like_current_wuxia_sync(repaired)
    if any(
        issue.code in {"protagonist_crutch_overdefined", "world_logic_conflict"}
        or issue.code.startswith("sellpoint_crutch_overdefined")
        for issue in report.issues
    ):
        repaired["premise"] = _repair_premise(premise)
        if current_book:
            repaired["reader_promise"] = (
                "看主角在真实江湖或拟真游戏世界里凭现场证据、武侠套路知识和风险选择争取主动权；每次机缘都来自人物因果、误判修正和可见代价。"
            )
            repaired["world_engine"] = (
                "游戏/江湖世界按真实人物因果运行，本地人有利益、恐惧和立场；桥段触发只能在真实事件后被识别，不能机械刷取、骗取或操控本地人配合。收益必须对应风险、伤势、人情债或门派后果。"
            )
            repaired["protagonist_engine"] = (
                "主角可保留懂武侠桥段和会观察的优势，但任何履历或能力都不能成为万能解法；有效解法来自现场证据、风险判断、人物误读、小步试探和承担代价。"
            )
            repaired["conflict_engine"] = (
                "主线压力来自真实江湖因果与现实同步危机：门派追查、人情债、资源交换、身份暴露和玩家势力介入逐步升级。"
            )
        else:
            repaired["reader_promise"] = (
                "看主角把核心卖点当成有限工具，在具体人物、场景证据和风险选择中争取主动；每次收益都对应误判修正、关系变化或明确代价。"
            )
            repaired["world_engine"] = (
                "世界规则必须稳定自洽，核心卖点只能扩大选择空间，不能替代调查、行动、交易和后果。任何收益都要绑定信息差、失败概率、资源消耗或关系代价。"
            )
            repaired["protagonist_engine"] = (
                "主角不能靠单一能力自动解题；有效解法来自观察、试探、选择、承担后果和阶段性成长。能力越好用，限制和代价越要清楚。"
            )
            repaired["conflict_engine"] = (
                "长期冲突来自外部压力、人物立场、资源约束和主角选择的连锁后果；每一阶段都要让卖点遇到新的限制，而不是重复兑现同一种爽点。"
            )
        repaired["forbidden_rules"] = _merge_rule(
            repaired.get("forbidden_rules", ""),
            "禁止把核心卖点、职业履历、系统能力、重生记忆、推演预知或特殊资源写成解题捷径；禁止刷收益、骗取机缘、NPC配合表演、主动制造固定桥段；禁止单一桥段反复充当主线。所有收益必须有可见代价、证据链和人物因果。",
        )
        repaired["style_guide"] = (
            (
                "轻松吐槽可以保留，但江湖本身要真实可感；正文优先写真实场景、人物立场、感官压力和行动后果。"
                "主角判断要先有现场证据，再有误判修正，不能把职业履历写成万能解法。"
            )
            if current_book
            else (
                "正文优先写具体场景、人物立场、感官压力和行动后果；主角判断要先有现场证据，再有误判修正。"
                "核心卖点只能改变选择空间，不能直接替代冲突、人物反应和代价。"
            )
        )
        repaired["volume_summary"] = _repair_volume_summary(current_book)
        if not current_book:
            repaired["arc_goal"] = _repair_arc_goal(current_book)
            repaired["arc_climax"] = _repair_arc_climax(current_book)
            repaired["arc_turn"] = _repair_arc_turn(current_book)
    if any(issue.code == "single_set_piece_overanchored" for issue in report.issues):
        repaired["arc_climax"] = _repair_arc_climax(current_book)
        repaired["volume_summary"] = _repair_volume_summary(current_book)
        repaired["arc_goal"] = _repair_arc_goal(current_book)
        repaired["arc_turn"] = _repair_arc_turn(current_book)
    repaired = _repair_editorial_dimension_gaps(repaired, report)
    return {key: repaired.get(key, "") for key, _ in _approval_fields()}


def _force_repair_blockers(skeleton: dict[str, str], report: SkeletonGovernanceReport) -> dict[str, str]:
    repaired = {key: str(value or "").strip() for key, value in skeleton.items()}
    issue_codes = {issue.code for issue in report.issues}
    issue_codes.update(issue.code.split(":", 1)[0] for issue in report.issues)
    current_book = _looks_like_current_wuxia_sync(repaired)
    if {"protagonist_crutch_overdefined", "sellpoint_crutch_overdefined", "world_logic_conflict"} & issue_codes:
        repaired.update(
            {
                "premise": _repair_premise(repaired.get("premise", "")),
                "reader_promise": (
                    "看主角在真实江湖里凭现场证据、风险选择和人物因果争取主动；每次收益都要付出伤势、人情、身份暴露或资源代价。"
                    if current_book
                    else "看主角把核心卖点当成有限工具，在具体人物、场景证据和风险选择中争取主动；每次收益都绑定代价。"
                ),
                "world_engine": (
                    "世界按真实江湖运行，本地人有利益、恐惧、门派关系和生活逻辑；桥段只能被事后识别，不能刷取、骗取或让人配合表演。"
                    if current_book
                    else "世界规则稳定自洽，核心卖点只能扩大选择空间；收益必须依赖调查、行动、交易、失败概率和后果承担。"
                ),
                "protagonist_engine": (
                    "主角的有效解法来自观察证据、小步试探、误判修正和承担代价；旧职业履历或特殊能力只作有限工具，不能替他自动解题。"
                    if current_book
                    else "主角不能靠单一能力自动解题；有效解法来自观察、试探、选择、承担后果和阶段性成长。"
                ),
                "conflict_engine": (
                    "长期冲突来自门派追查、人情债、资源交换、身份暴露和现实同步危机的连锁升级。"
                    if current_book
                    else "长期冲突来自外部压力、人物立场、资源约束和主角选择的连锁后果。"
                ),
                "forbidden_rules": _merge_rule(
                    repaired.get("forbidden_rules", ""),
                    "禁止把核心卖点、职业履历、系统能力、重生记忆、推演预知或特殊资源写成解题捷径；禁止刷收益、骗取机缘、NPC配合表演、主动制造固定桥段；所有收益必须有可见代价、证据链和人物因果。",
                ),
                "style_guide": (
                    "正文优先写真实场景、人物立场、感官压力和行动后果；主角判断必须先有现场证据，再有误判修正。"
                ),
            }
        )
    if any(issue.code == "single_set_piece_overanchored" for issue in report.issues):
        repaired["volume_summary"] = _repair_volume_summary(current_book)
        repaired["arc_goal"] = _repair_arc_goal(current_book)
        repaired["arc_climax"] = _repair_arc_climax(current_book)
        repaired["arc_turn"] = _repair_arc_turn(current_book)
    return {key: _remove_repeated_shortcut_terms(repaired.get(key, ""), field=key) for key, _ in _approval_fields()}


def _repair_next_actions(report: SkeletonGovernanceReport) -> list[str]:
    if report.passed:
        return ["检查草案是否符合作者真实想法；确认后保存并批准生产骨架。"]
    actions = ["自动草案仍有结构风险；请优先处理 blocker，而不是继续生成正文。"]
    for issue in report.issues[:4]:
        actions.append(f"{issue.code}: {issue.recommendation}")
    return actions


def _remove_repeated_shortcut_terms(value: str, *, field: str) -> str:
    if field == "forbidden_rules":
        return value
    replacements = {
        "龙套": "旧职业履历",
        "演员": "旧职业履历",
        "演技": "临场判断",
        "表演": "现场应对",
        "片场": "现实经历",
        "横店": "现实经历",
        "导演": "旁观者",
    }
    patched = str(value or "")
    for old, new in replacements.items():
        patched = patched.replace(old, new)
    return patched


def audit_story_skeleton(session: Session, *, book_id: int) -> SkeletonGovernanceReport:
    sources = _skeleton_sources(session, book_id=book_id)
    return audit_skeleton_sources(sources)


def audit_story_skeleton_with_agent_evidence(session: Session, *, book_id: int) -> SkeletonGovernanceReport:
    sources = _skeleton_sources(session, book_id=book_id)
    evidence_sources, evidence_summary, team_decisions = _agent_plan_skeleton_evidence(session, book_id=book_id, skeleton_sources=sources)
    if evidence_sources:
        sources.update(evidence_sources)
    report = audit_skeleton_sources(sources)
    source_hits = {key: list(values) for key, values in report.source_hits.items()}
    if evidence_sources:
        source_hits["agent_plan_evidence"] = sorted(evidence_sources.keys())
    return SkeletonGovernanceReport(
        status=report.status,
        score=report.score,
        issues=report.issues,
        recommendations=report.recommendations,
        source_hits=source_hits,
        dimensions=report.dimensions,
        evidence_summary=[*report.evidence_summary, *evidence_summary],
        team_decisions=[*report.team_decisions, *team_decisions],
        human_decisions=[*report.human_decisions, *team_decisions],
    )


def audit_skeleton_sources(sources: dict[str, str]) -> SkeletonGovernanceReport:
    issues: list[SkeletonIssue] = []
    actor_sources = _sources_with_any(
        sources,
        ("龙套", "演员", "演技", "表演", "片场", "横店", "导演", "代入角色", "现场表演"),
    )
    actor_sources = [
        source
        for source in actor_sources
        if "forbidden_rules" not in source and not _negative_actor_context(sources.get(source, ""))
    ]
    actor_solution_sources = _sources_with_any(
        sources,
        ("靠演技", "靠演员", "演员观察力", "临场表演", "用演员", "靠表演", "配合他完成这场表演"),
    )
    actor_solution_sources = [
        source
        for source in actor_solution_sources
        if "forbidden_rules" not in source and not _negative_actor_context(sources.get(source, ""))
    ]
    real_world_sources = _sources_with_any(
        sources,
        ("真实武侠世界", "真实存在", "有血有肉", "不是任务 NPC", "近似穿越", "真实江湖"),
    )
    gamey_sources = _sources_with_any(
        sources,
        ("骗取奇遇", "刷奇遇", "制造坠崖", "主动制造", "NPC配合", "评分系统", "相似度越高"),
    )
    gamey_sources = [source for source in gamey_sources if "forbidden_rules" not in source]
    sellpoint_hits = _derive_sellpoint_risks(sources)
    if len(actor_sources) >= 3 and actor_solution_sources:
        issues.append(
            SkeletonIssue(
                code="protagonist_crutch_overdefined",
                severity="blocker",
                message="主角职业/技能被反复写成万能解法，后续生成会自然滑向同一套路。",
                sources=sorted(set(actor_sources + actor_solution_sources))[:8],
                recommendation="把演员/龙套降级为背景履历；主角解法改成现场证据、风险判断、人物误读、武侠套路知识和承担代价。",
            )
        )
    if not any(issue.code == "protagonist_crutch_overdefined" for issue in issues):
        for domain, hit in sellpoint_hits.items():
            if len(hit["sources"]) < 3 or not hit["shortcut_sources"]:
                continue
            issues.append(
                SkeletonIssue(
                    code=f"sellpoint_crutch_overdefined:{domain}",
                    severity="blocker",
                    message=f"核心卖点“{hit['label']}”被多处写成过强解法，后续生成容易变成同一套捷径。",
                    sources=sorted(set(hit["sources"] + hit["shortcut_sources"]))[:8],
                    recommendation=f"把“{hit['label']}”降级为有边界的工具：必须依赖现场证据、人物因果、失败概率和可见代价，不能直接替主角解决冲突。",
                )
            )
    if real_world_sources and gamey_sources:
        issues.append(
            SkeletonIssue(
                code="world_logic_conflict",
                severity="blocker",
                message="骨架同时要求真实江湖，又要求骗取/刷/制造桥段，世界逻辑互相拉扯。",
                sources=sorted(set(real_world_sources + gamey_sources))[:8],
                recommendation="保留真实江湖；把桥段触发改成事后识别和高风险尝试，禁止把本地人当配合表演的 NPC。",
            )
        )
    repeated_set_piece = _dominant_set_piece_sources(sources)
    if repeated_set_piece:
        label, set_piece_sources = repeated_set_piece
        issues.append(
            SkeletonIssue(
                code="single_set_piece_overanchored",
                severity="warning",
                message=f"“{label}”被骨架多处锚定，容易导致章节反复回到同一场面。",
                sources=set_piece_sources[:8],
                recommendation=f"把“{label}”降为桥段库之一，并补充至少五个同等级触发方向，让每章换一种场面发动机。",
            )
        )
    if not _sources_with_any(sources, ("禁止", "不得", "不能", "边界", "代价", "限制")):
        issues.append(
            SkeletonIssue(
                code="missing_negative_constraints",
                severity="warning",
                message="骨架缺少明确禁区和能力边界，生成容易把卖点扩大成万能钥匙。",
                sources=[],
                recommendation="补一段 forbidden_rules：能力不能解决什么、哪些套路禁用、何时必须失败或付代价。",
            )
        )
    source_hits = {
        "actor_crutch": sorted(set(actor_sources)),
        "actor_solution": sorted(set(actor_solution_sources)),
        "real_world": sorted(set(real_world_sources)),
        "gamey_bridge": sorted(set(gamey_sources)),
        "derived_sellpoint_risks": [
            f"{hit['label']}({len(hit['sources'])}处)"
            for hit in sellpoint_hits.values()
            if len(hit["sources"]) >= 2
        ],
    }
    dimensions = _editorial_dimensions(sources, issues=issues, source_hits=source_hits)
    issues.extend(_dimension_issues(dimensions))
    score = 100
    for issue in issues:
        score -= 30 if issue.severity == "blocker" else 10
    dimension_average = int(sum(item["score"] for item in dimensions.values()) / max(1, len(dimensions)))
    score = min(score, dimension_average + 8)
    score = max(0, min(100, score))
    status = "pass" if score >= 70 and not any(issue.severity == "blocker" for issue in issues) else "attention"
    recommendations = [issue.recommendation for issue in issues]
    evidence_summary = _evidence_summary(sources, source_hits=source_hits, dimensions=dimensions)
    team_decisions = _team_decisions(dimensions, issues)
    return SkeletonGovernanceReport(
        status=status,
        score=score,
        issues=issues,
        recommendations=recommendations,
        source_hits=source_hits,
        dimensions=dimensions,
        evidence_summary=evidence_summary,
        team_decisions=team_decisions,
        human_decisions=team_decisions,
    )


def _editorial_dimensions(sources: dict[str, str], *, issues: list[SkeletonIssue], source_hits: dict[str, list[str]]) -> dict[str, dict]:
    premise = _joined_sources(sources, ("premise", "positioning"))
    promise = _joined_sources(sources, ("reader_promise",))
    world = _joined_sources(sources, ("world_engine", "power_curve", "forbidden_rules"))
    protagonist = _joined_sources(sources, ("protagonist_engine", "protagonist_arc"))
    conflict = _joined_sources(sources, ("conflict_engine", "main_plot", "arc.goal", "arc.climax", "arc.turn", "arc_goal", "arc_climax", "arc_turn"))
    volume = _joined_sources(sources, ("volume.summary", "volume_summary", "arc.goal", "arc.climax", "arc.turn", "arc_goal", "arc_climax", "arc_turn"))
    style = _joined_sources(sources, ("style_guide",))
    market = _joined_sources(sources, ("agent.market_signal",))
    semantic = _joined_sources(sources, ("agent.semantic_hit", "agent.semantic_status"))
    dimensions = {
        "core_concept": _dimension(
            "核心设定",
            premise,
            strong=("主角", "能力", "冲突", "危机", "目标", "代价"),
            weak=("不知道", "待定", "随便", "万能", "轻松"),
            action="用一句话说清主角是谁、核心卖点是什么、主要压力从哪里来。",
        ),
        "reader_promise": _dimension(
            "读者承诺",
            promise,
            strong=("看", "期待", "爽点", "情绪", "钩子", "代价", "反差", "追读"),
            weak=("好看", "精彩", "有趣", "热血"),
            action="把读者承诺写成可持续期待：读者每章想看什么情绪、爽点或反转。",
        ),
        "protagonist_agency": _dimension(
            "主角能动性",
            protagonist,
            strong=("主动", "选择", "欲望", "缺陷", "误判", "代价", "成长", "承担"),
            weak=("被迫", "等待", "天生", "自动", "躺赢"),
            action="补清主角欲望、主动选择、会犯什么错、每次收益要承担什么后果。",
        ),
        "world_boundary": _dimension(
            "世界规则与边界",
            world,
            strong=("规则", "限制", "禁止", "不得", "不能", "代价", "失败", "后果"),
            weak=("无代价", "随便", "无限", "必中", "万能"),
            action="补清能力/资源不能解决什么、什么时候会失败、违反规则的后果是什么。",
        ),
        "conflict_engine": _dimension(
            "长期冲突引擎",
            conflict,
            strong=("长期", "压力", "升级", "敌", "追查", "危机", "资源", "关系", "暴露", "连锁"),
            weak=("一直", "反复", "重复", "轻松", "永远"),
            action="把长期冲突写成会升级的外部压力，而不是一个桥段反复发生。",
        ),
        "longform_capacity": _longform_dimension(volume),
        "platform_readability": _dimension(
            "平台可读性",
            "\n".join([promise, style, conflict]),
            strong=("开篇", "节奏", "钩子", "爽点", "压力", "章末", "正文", "场景"),
            weak=("说明", "百科", "设定集", "慢热", "铺垫很久"),
            action="补清开篇牵引、章末钩子、正文呈现方式，避免设定说明压过场景。",
        ),
    }
    if market.strip() or semantic.strip():
        dimensions["agent_plan_evidence"] = _agent_plan_evidence_dimension(market=market, semantic=semantic)
    if any(issue.severity == "blocker" for issue in issues):
        dimensions["risk_control"] = {
            "label": "结构风险控制",
            "score": 35,
            "status": "blocker",
            "reason": "硬规则发现结构 blocker，自动修复只能先降风险，不能替代作者方向判断。",
            "evidence": [issue.code for issue in issues if issue.severity == "blocker"][:4],
            "action": "先处理 blocker，再判断卖点是否仍然足够有吸引力。",
            "repairable": True,
        }
    else:
        dimensions["risk_control"] = {
            "label": "结构风险控制",
            "score": 88,
            "status": "pass",
            "reason": "未发现硬性结构 blocker。",
            "evidence": [item for values in source_hits.values() for item in values][:4],
            "action": "继续检查作者真实偏好和市场适配。",
            "repairable": False,
        }
    return dimensions


def _agent_plan_evidence_dimension(*, market: str, semantic: str) -> dict:
    market_hits = [item for item in ("趋势", "爆款", "开篇", "爽点", "避雷", "读者", "榜单", "平台") if item in market]
    semantic_hits = [item for item in ("indexed_count", "ready", "stale", "foundation", "bible", "chapter", "canon") if item in semantic]
    score = 45 + min(5, len(market_hits)) * 7 + min(3, len(semantic_hits)) * 6
    if market.strip():
        score += 10
    if semantic.strip():
        score += 10
    if "stale=True" in semantic or "ready=False" in semantic:
        score -= 20
    score = max(30, min(100, score))
    status = "pass" if score >= 75 else ("attention" if score >= 55 else "blocker")
    evidence = [f"市场:{item}" for item in market_hits[:4]] + [f"语义:{item}" for item in semantic_hits[:3]]
    if "stale=True" in semantic:
        evidence.append("语义:stale")
    return {
        "label": "Agent Plan 证据层",
        "score": score,
        "status": status,
        "reason": "已接入市场/语义证据，可辅助判断骨架。" if status == "pass" else "Agent Plan 证据不足或语义记忆需要更新。",
        "evidence": evidence,
        "action": "先导入联网搜索结果并重建语义记忆，再让骨架修复参考这些证据。",
        "repairable": False,
    }


def _dimension(label: str, text: str, *, strong: tuple[str, ...], weak: tuple[str, ...], action: str) -> dict:
    value = str(text or "")
    strong_hits = [item for item in strong if item in value]
    weak_hits = [item for item in weak if item in value]
    length_score = 25 if len(value.strip()) >= 35 else (12 if value.strip() else 0)
    score = min(100, 35 + length_score + len(strong_hits) * 8 - len(weak_hits) * 8)
    if not value.strip():
        score = 20
    status = "pass" if score >= 75 else ("attention" if score >= 50 else "blocker")
    return {
        "label": label,
        "score": score,
        "status": status,
        "reason": _dimension_reason(label, score, strong_hits, weak_hits, bool(value.strip())),
        "evidence": strong_hits[:5] + [f"弱信号:{item}" for item in weak_hits[:3]],
        "action": action,
        "repairable": status != "pass",
    }


def _longform_dimension(text: str) -> dict:
    value = str(text or "")
    engine_markers = ("求医", "护送", "追查", "交易", "误认", "门派", "身份", "资源", "关系", "危机", "试炼", "调查", "竞争", "暴露")
    hits = [item for item in engine_markers if item in value]
    score = min(100, 30 + min(6, len(hits)) * 10 + (20 if len(value) >= 50 else 0))
    status = "pass" if score >= 75 else ("attention" if score >= 50 else "blocker")
    return {
        "label": "长篇续航",
        "score": score,
        "status": status,
        "reason": "已出现多种章节发动机。" if status == "pass" else "前几章可用的冲突发动机不足，容易重复同一桥段。",
        "evidence": hits[:6],
        "action": "补至少五种同等级章节发动机：人物关系、资源交换、外部追查、身份暴露、规则惩罚等。",
        "repairable": status != "pass",
    }


def _dimension_reason(label: str, score: int, strong_hits: list[str], weak_hits: list[str], has_text: bool) -> str:
    if not has_text:
        return f"{label}缺失。"
    if score >= 75:
        return f"{label}已有可用信号：{','.join(strong_hits[:4]) or '表达较完整'}。"
    if weak_hits:
        return f"{label}存在泛化或捷径信号：{','.join(weak_hits[:3])}。"
    return f"{label}信息不足，需要写得更具体。"


def _dimension_issues(dimensions: dict[str, dict]) -> list[SkeletonIssue]:
    issues: list[SkeletonIssue] = []
    for key, item in dimensions.items():
        if item["status"] == "pass":
            continue
        issues.append(
            SkeletonIssue(
                code=f"editorial_{key}",
                severity="blocker" if item["status"] == "blocker" and key in {"core_concept", "world_boundary", "conflict_engine"} else "warning",
                message=f"{item['label']}不足：{item['reason']}",
                sources=[str(value) for value in item.get("evidence", [])],
                recommendation=str(item.get("action") or ""),
            )
        )
    return issues


def _evidence_summary(sources: dict[str, str], *, source_hits: dict[str, list[str]], dimensions: dict[str, dict]) -> list[str]:
    source_count = sum(1 for value in sources.values() if str(value or "").strip())
    low_dimensions = [item["label"] for item in dimensions.values() if item["status"] != "pass"]
    hit_count = sum(len(values) for values in source_hits.values())
    return [
        f"已读取骨架来源 {source_count} 项。",
        f"规则命中 {hit_count} 处。",
        "低分维度：" + ("，".join(low_dimensions) if low_dimensions else "无"),
    ]


def _team_decisions(dimensions: dict[str, dict], issues: list[SkeletonIssue]) -> list[str]:
    decisions: list[str] = []
    if dimensions.get("reader_promise", {}).get("status") != "pass":
        decisions.append("需要主编确认：这本书最核心的读者承诺到底是什么。")
    if dimensions.get("longform_capacity", {}).get("status") != "pass":
        decisions.append("需要主编确认：第一卷是否有足够多的章节发动机，而不是只靠一个桥段。")
    if any(issue.severity == "blocker" for issue in issues):
        decisions.append("需要主编复核：自动修复会降低结构风险，但可能削弱原始卖点刺激度。")
    return decisions


def _human_decisions(dimensions: dict[str, dict], issues: list[SkeletonIssue]) -> list[str]:
    return _team_decisions(dimensions, issues)


def _joined_sources(sources: dict[str, str], markers: tuple[str, ...]) -> str:
    rows = [
        str(value or "")
        for key, value in sources.items()
        if any(marker in key for marker in markers)
    ]
    return "\n".join(rows)


def _repair_editorial_dimension_gaps(skeleton: dict[str, str], report: SkeletonGovernanceReport) -> dict[str, str]:
    repaired = dict(skeleton)
    dimensions = report.dimensions or {}
    if dimensions.get("reader_promise", {}).get("status") != "pass" and not repaired.get("reader_promise"):
        repaired["reader_promise"] = "看主角在具体压力中主动选择，把核心卖点用成有限工具；每章都有可见代价、关系变化和章末钩子。"
    if dimensions.get("protagonist_agency", {}).get("status") != "pass":
        repaired["protagonist_engine"] = repaired.get("protagonist_engine") or "主角有明确欲望和缺陷，必须通过观察、试探、主动选择和承担后果推进成长。"
    if dimensions.get("world_boundary", {}).get("status") != "pass":
        repaired["forbidden_rules"] = _merge_rule(
            repaired.get("forbidden_rules", ""),
            "禁止无代价收益、万能能力、机械任务链和重复桥段；能力必须有边界、失败条件和后果。",
        )
    if dimensions.get("conflict_engine", {}).get("status") != "pass":
        repaired["conflict_engine"] = repaired.get("conflict_engine") or "长期冲突来自外部压力、人物立场、资源约束和主角选择的连锁后果，并逐章升级。"
    if dimensions.get("longform_capacity", {}).get("status") != "pass":
        repaired["volume_summary"] = repaired.get("volume_summary") or _repair_volume_summary(_looks_like_current_wuxia_sync(repaired))
        repaired["arc_goal"] = repaired.get("arc_goal") or _repair_arc_goal(_looks_like_current_wuxia_sync(repaired))
        repaired["arc_climax"] = repaired.get("arc_climax") or _repair_arc_climax(_looks_like_current_wuxia_sync(repaired))
        repaired["arc_turn"] = repaired.get("arc_turn") or _repair_arc_turn(_looks_like_current_wuxia_sync(repaired))
    return repaired


def _market_repair_context(session: Session, *, book_id: int) -> MarketRepairContext:
    try:
        book = session.get(Book, book_id)
        if not book:
            return MarketRepairContext(0, [], [], [])
        rows = list(
            session.scalars(
                select(MarketSignal)
                .where(MarketSignal.genre == (book.genre or ""), MarketSignal.confidence >= 60)
                .order_by(MarketSignal.confidence.desc(), MarketSignal.id.desc())
                .limit(12)
            )
        )
    except OperationalError:
        session.rollback()
        return MarketRepairContext(0, [], [], [])
    combined = "\n".join(item.signal_text or "" for item in rows)
    expectations = _market_expectations(combined)
    avoid_rules = _market_avoid_rules(combined)
    return MarketRepairContext(
        signal_count=len(rows),
        expectations=expectations,
        avoid_rules=avoid_rules,
        source_ids=[item.id for item in rows],
    )


def _market_expectations(text: str) -> list[str]:
    value = str(text or "")
    rows: list[str] = []
    if any(marker in value for marker in ("开篇", "前三章", "开局")):
        rows.append("开篇必须尽快给出可感压力、主角主动选择和明确追读钩子。")
    if any(marker in value for marker in ("爽点", "情绪", "期待感", "追读")):
        rows.append("读者承诺要落成每章可见的爽点/情绪回报，而不是停留在设定说明。")
    if any(marker in value for marker in ("章末", "钩子", "悬念")):
        rows.append("章节发动机需要自带章末钩子：新线索、新代价、新敌意或关系反转。")
    if any(marker in value for marker in ("差异化", "新鲜", "反套路", "辨识度")):
        rows.append("核心卖点要有差异化呈现，避免同质化系统任务、套路升级或固定桥段。")
    if any(marker in value for marker in ("节奏", "短平快", "密度")):
        rows.append("第一卷要提高事件密度，用行动推进设定，减少长段解释。")
    if any(marker in value for marker in ("人物", "关系", "互动", "配角")):
        rows.append("爽点应通过人物关系和立场碰撞兑现，避免主角独自解题。")
    if not rows and value.strip():
        rows.append("市场信号提示：读者预期需要被写进读者承诺、开篇压力和章末追读钩子。")
    return _dedupe_text(rows)[:6]


def _market_avoid_rules(text: str) -> list[str]:
    value = str(text or "")
    rows: list[str] = []
    if any(marker in value for marker in ("避雷", "劝退", "毒点")):
        rows.append("避免开篇长设定、主角被动等待、收益无代价和重复桥段。")
    if any(marker in value for marker in ("同质化", "套路", "模板")):
        rows.append("避免把核心卖点写成平台常见模板；每个爽点都要有本书专属场景和代价。")
    if any(marker in value for marker in ("慢热", "铺垫")):
        rows.append("避免慢热铺垫压过场景行动；设定必须跟冲突一起出现。")
    return _dedupe_text(rows)[:5]


def _market_context_sources(context: MarketRepairContext) -> dict[str, str]:
    if not context.signal_count:
        return {}
    rows: dict[str, str] = {}
    for index, item in enumerate(context.expectations, start=1):
        rows[f"market.expectation.{index}"] = item
    for index, item in enumerate(context.avoid_rules, start=1):
        rows[f"market.avoid.{index}"] = item
    return rows


def _apply_market_repair_context(skeleton: dict[str, str], context: MarketRepairContext) -> dict[str, str]:
    if not context.signal_count:
        return skeleton
    repaired = dict(skeleton)
    if context.expectations:
        promise_addition = "平台读者预期：" + "；".join(context.expectations[:3])
        repaired["reader_promise"] = _append_sentence(repaired.get("reader_promise", ""), promise_addition)
        repaired["style_guide"] = _append_sentence(
            repaired.get("style_guide", ""),
            "市场执行要求：正文优先用场景行动兑现读者承诺，开篇压力、爽点回报和章末钩子必须可见。",
        )
        repaired["volume_summary"] = _append_sentence(
            repaired.get("volume_summary", ""),
            "第一卷按平台追读逻辑轮换章节发动机：开篇压力、人物关系碰撞、资源交换、身份风险、章末新钩子。",
        )
        repaired["arc_goal"] = _append_sentence(
            repaired.get("arc_goal", ""),
            "前五章每章都要完成一个可见读者回报，并留下下一章追读问题。",
        )
    if context.avoid_rules:
        repaired["forbidden_rules"] = _merge_rule(repaired.get("forbidden_rules", ""), "；".join(context.avoid_rules))
    if context.expectations and not repaired.get("conflict_engine"):
        repaired["conflict_engine"] = "长期冲突必须服务平台追读：外部压力逐章升级，人物关系不断产生新代价，每章留下新的选择难题。"
    elif context.expectations:
        repaired["conflict_engine"] = _append_sentence(
            repaired.get("conflict_engine", ""),
            "冲突推进要兼顾平台追读：每轮收益后立刻出现新代价、新敌意或新悬念。",
        )
    return {key: repaired.get(key, "") for key, _ in _approval_fields()}


def _append_sentence(value: str, addition: str) -> str:
    base = str(value or "").strip()
    extra = str(addition or "").strip()
    if not extra or extra in base:
        return base
    if not base:
        return extra
    return f"{base}\n{extra}"


def _dedupe_text(rows: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for row in rows:
        value = row.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _skeleton_sources(session: Session, *, book_id: int) -> dict[str, str]:
    foundation = session.scalar(select(StoryFoundation).where(StoryFoundation.book_id == book_id).order_by(StoryFoundation.id.desc()))
    bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
    volume = session.scalar(select(Volume).where(Volume.book_id == book_id, Volume.volume_number == 1))
    arc = session.scalar(select(StoryArc).where(StoryArc.book_id == book_id, StoryArc.arc_number == 1))
    rows: dict[str, str] = {}
    if foundation:
        rows.update(
            {
                "foundation.premise": foundation.premise or "",
                "foundation.reader_promise": foundation.reader_promise or "",
                "foundation.world_engine": foundation.world_engine or "",
                "foundation.protagonist_engine": foundation.protagonist_engine or "",
                "foundation.conflict_engine": foundation.conflict_engine or "",
            }
        )
    if bible:
        rows.update(
            {
                "bible.positioning": bible.positioning or "",
                "bible.reader_promise": bible.reader_promise or "",
                "bible.main_plot": bible.main_plot or "",
                "bible.protagonist_arc": bible.protagonist_arc or "",
                "bible.power_curve": bible.power_curve or "",
                "bible.forbidden_rules": bible.forbidden_rules or "",
                "bible.style_guide": bible.style_guide or "",
            }
        )
    if volume:
        rows["volume.summary"] = volume.summary or ""
    if arc:
        rows.update({"arc.goal": arc.goal or "", "arc.climax": arc.climax or "", "arc.turn": arc.turn or ""})
    approvals = session.scalars(
        select(PlatformFeedback)
        .where(PlatformFeedback.book_id == book_id, PlatformFeedback.metric_name == "skeleton_approval")
        .order_by(PlatformFeedback.id.desc())
        .limit(20)
    )
    for item in approvals:
        rows.setdefault(f"approval.{item.metric_value}", item.raw_text or "")
    return rows


def _agent_plan_skeleton_evidence(session: Session, *, book_id: int, skeleton_sources: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    try:
        book = session.get(Book, book_id)
        if not book:
            return {}, [], []
        sources: dict[str, str] = {}
        summaries: list[str] = []
        decisions: list[str] = []
        market_signals = list(
            session.scalars(
                select(MarketSignal)
                .where(MarketSignal.genre == (book.genre or ""))
                .order_by(MarketSignal.confidence.desc(), MarketSignal.id.desc())
                .limit(8)
            )
        )
        for item in market_signals:
            sources[f"agent.market_signal.{item.id}"] = f"confidence={item.confidence} {item.signal_text or ''}"
        if market_signals:
            summaries.append(f"Agent Plan 市场信号已接入 {len(market_signals)} 条。")
        else:
            decisions.append("Agent Plan 联网搜索结果尚未导入；市场适配判断仍主要依赖本地骨架。")
        semantic_sources, semantic_summary, semantic_decisions = _agent_plan_semantic_sources(
            session,
            book_id=book_id,
            skeleton_sources=skeleton_sources,
        )
        sources.update(semantic_sources)
        summaries.extend(semantic_summary)
        decisions.extend(semantic_decisions)
        return sources, summaries, decisions
    except OperationalError:
        session.rollback()
        return {}, ["Agent Plan 证据表暂不可用，已回退到本地骨架审计。"], []


def _agent_plan_semantic_sources(session: Session, *, book_id: int, skeleton_sources: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    try:
        from app.services.agent_plan_intelligence import retrieve_book_knowledge, summarize_semantic_memory

        status = summarize_semantic_memory(session, book_id=book_id)
        sources = {
            "agent.semantic_status": (
                f"ready={status.get('ready')} stale={status.get('stale')} indexed_count={status.get('indexed_count')} "
                f"expected_count={status.get('expected_count')} source_types={','.join(status.get('source_types') or [])}"
            )
        }
        summaries = [
            f"Agent Plan 语义记忆：{status.get('indexed_count', 0)}/{status.get('expected_count', 0)} 条，"
            f"{'可用' if status.get('ready') else '需更新'}。"
        ]
        decisions: list[str] = []
        if not status.get("indexed_count"):
            decisions.append("建议先执行“Agent Plan 增强一轮”或“重建语义记忆”，再做最终骨架批准。")
            return sources, summaries, decisions
        if status.get("stale"):
            decisions.append("语义记忆已过期；最终批准骨架前建议重建索引。")
        query = _semantic_query_from_skeleton(skeleton_sources)
        hits = retrieve_book_knowledge(session, book_id=book_id, query=query, limit=5, dry_run=True)
        useful_hits = [hit for hit in hits if hit.score >= 0.15]
        for hit in useful_hits[:5]:
            sources[f"agent.semantic_hit.{hit.embedding_id}"] = (
                f"score={hit.score:.3f} type={hit.source_type} label={hit.source_label} {hit.text[:500]}"
            )
        if useful_hits:
            summaries.append(f"Agent Plan 语义召回命中 {len(useful_hits)} 条，可用于检查设定遗漏。")
        else:
            decisions.append("语义记忆存在，但本次骨架召回弱；建议补全 Story Bible/Canon 后重建。")
        return sources, summaries, decisions
    except (RuntimeError, ValueError, OperationalError):
        session.rollback()
        return {}, ["Agent Plan 语义证据读取失败，已回退到本地骨架审计。"], []


def _semantic_query_from_skeleton(sources: dict[str, str]) -> str:
    preferred = [
        sources.get("foundation.premise", ""),
        sources.get("foundation.reader_promise", ""),
        sources.get("foundation.conflict_engine", ""),
        sources.get("bible.positioning", ""),
        sources.get("bible.main_plot", ""),
    ]
    text = " ".join(item.strip() for item in preferred if item and item.strip())
    return text[:1000] or "故事核心设定 读者承诺 世界规则 长期冲突"


def _sources_with_any(sources: dict[str, str], markers: tuple[str, ...]) -> list[str]:
    return [name for name, text in sources.items() if any(marker in text for marker in markers)]


def _derive_sellpoint_risks(sources: dict[str, str]) -> dict[str, dict[str, list[str] | str]]:
    hits: dict[str, dict[str, list[str] | str]] = {}
    for domain, label, markers in _sellpoint_domains():
        domain_sources = [
            name
            for name, text in sources.items()
            if "forbidden_rules" not in name
            and not _negative_constraint_context(text)
            and not _benign_sellpoint_context(domain, text)
            and any(marker in text for marker in markers)
        ]
        shortcut_sources = [
            name
            for name in domain_sources
            if _has_shortcut_context(sources.get(name, ""), markers)
        ]
        if domain_sources:
            hits[domain] = {"label": label, "sources": domain_sources, "shortcut_sources": shortcut_sources}
    return hits


def _sellpoint_domains() -> list[tuple[str, str, tuple[str, ...]]]:
    return [
        ("rebirth_memory", "重生/未来记忆", ("重生", "前世", "上一世", "未来记忆", "预知未来", "先知")),
        ("system_panel", "系统/面板", ("系统", "面板", "任务", "奖励", "签到", "抽奖", "加点")),
        ("simulation_deduction", "推演/模拟", ("推演", "模拟", "沙盘", "演算", "预演", "复盘")),
        ("space_resource", "空间/资源", ("随身空间", "空间", "灵泉", "仓库", "储物", "资源点")),
        ("copy_talent", "复制/吞噬天赋", ("复制", "吞噬", "掠夺", "提取", "天赋", "词条")),
        ("business_knowledge", "商业/专业知识", ("商业", "金融", "股市", "投资", "专业知识", "经验")),
        ("tech_blackbox", "科技/黑箱装置", ("科技", "芯片", "AI", "算法", "装置", "黑科技")),
        ("actor_craft", "演员/表演履历", ("龙套", "演员", "演技", "表演", "片场", "横店", "导演")),
    ]


def _has_shortcut_context(text: str, domain_markers: tuple[str, ...]) -> bool:
    text = str(text or "")
    if not any(marker in text for marker in domain_markers):
        return False
    shortcut_markers = (
        "万能",
        "无代价",
        "无风险",
        "不会失败",
        "必然成功",
        "轻松",
        "随便",
        "直接解决",
        "秒杀",
        "碾压",
        "稳赚",
        "全知",
        "预知一切",
        "骗取",
        "刷取",
        "刷奇遇",
        "配合",
        "只要",
        "靠",
    )
    return any(marker in text for marker in shortcut_markers)


def _benign_sellpoint_context(domain: str, text: str) -> bool:
    text = str(text or "")
    dramatic_system_markers = (
        "剧情演绎系统",
        "剧情演绎",
        "演绎系统",
        "经典桥段",
        "复刻桥段",
        "复刻程度",
        "参演人员",
        "好感度",
        "失败无法获得奖励",
        "失败无奖励",
        "桥段复刻",
        "演绎相似度",
        "即兴演出",
        "演出痕迹",
    )
    if domain == "system_panel":
        hard_panel_markers = (
            "系统面板",
            "属性面板",
            "签到系统",
            "签到",
            "抽奖",
            "加点",
            "最优答案",
            "自动解题",
            "秒杀",
            "碾压",
            "无代价",
            "任务大厅",
            "刷经验",
            "刷副本",
        )
        return any(marker in text for marker in dramatic_system_markers) and not any(marker in text for marker in hard_panel_markers)
    if domain == "actor_craft":
        hard_actor_markers = ("龙套", "演员", "演技", "片场", "横店", "导演", "靠表演", "靠演技", "职业履历")
        return any(marker in text for marker in dramatic_system_markers) and not any(marker in text for marker in hard_actor_markers)
    return False


def _dominant_set_piece_sources(sources: dict[str, str]) -> tuple[str, list[str]] | None:
    set_pieces = (
        ("坠崖得功", ("坠崖", "追杀后坠崖", "跌落山崖")),
        ("退婚打脸", ("退婚", "打脸", "悔婚")),
        ("拍卖会", ("拍卖会", "竞拍", "压轴拍品")),
        ("秘境试炼", ("秘境", "试炼", "遗迹")),
        ("生死擂台", ("擂台", "比武", "约战")),
        ("家族大比", ("家族大比", "族比", "宗门大比")),
        ("末日囤货", ("囤货", "零元购", "物资仓库")),
        ("副本刷怪", ("副本", "刷怪", "刷本")),
    )
    for label, markers in set_pieces:
        hits = [
            name
            for name in _sources_with_any(sources, markers)
            if "forbidden_rules" not in name and not _negative_constraint_context(sources.get(name, ""))
        ]
        if len(hits) >= 2:
            return label, hits
    return None


def _negative_constraint_context(text: str) -> bool:
    text = str(text or "")
    negative_markers = ("禁止", "不得", "不能", "不许", "避免", "不要", "禁用", "边界", "限制")
    return any(marker in text for marker in negative_markers)


def _negative_actor_context(text: str) -> bool:
    text = str(text or "")
    negative_markers = (
        "禁止演员",
        "禁止把演员",
        "禁止演员/龙套",
        "不能靠演员",
        "不得靠演员",
        "不能把演员",
        "不能让人配合表演",
        "作为解题捷径",
        "不能成为万能解法",
        "不能把职业履历写成万能解法",
    )
    return any(marker in text for marker in negative_markers)


def _repair_premise(value: str) -> str:
    if _looks_like_wuxia_sync_text(value):
        return "主角进入真实江湖或拟真游戏世界后发现，这里不能刷怪升级；他只能凭现场证据、武侠套路知识和承担代价，在真实人物因果中争取机缘，并逐步面对现实同步或世界升维带来的危机。"
    return "主角进入核心矛盾后发现，核心卖点不是万能钥匙；他只能凭现场证据、风险选择和承担代价逐步争取主动。"


def _looks_like_current_wuxia_sync(skeleton: dict[str, str]) -> bool:
    text = "\n".join(str(value or "") for value in skeleton.values())
    return _looks_like_wuxia_sync_text(text)


def _looks_like_wuxia_sync_text(text: str) -> bool:
    value = str(text or "")
    wuxia_markers = ("武侠", "江湖", "门派", "修炼", "内力", "轻功")
    game_sync_markers = ("全真", "网游", "游戏", "现实同步", "同步现实", "拟真", "内测")
    return any(marker in value for marker in wuxia_markers) and any(marker in value for marker in game_sync_markers + ("真实",))


def _repair_volume_summary(current_book: bool) -> str:
    if current_book:
        return "第一卷写主角从误入真实江湖或拟真游戏世界，到学会用证据和代价试探桥段触发：求医、护送、身份误认、门派规矩、人情债轮换推进，最终引出现实同步或世界升维危机。"
    return "第一卷写主角从误用核心卖点到理解其边界：通过调查、交易、误判修正、关系变化和代价支付逐步打开局面，并在卷末暴露更高层压力。"


def _repair_arc_goal(current_book: bool) -> str:
    if current_book:
        return "前五章建立真实江湖规则、主角误判修正链、第一笔人情债和现实同步隐患；每章换一种桥段发动机。"
    return "前五章建立世界规则、核心卖点边界、主角误判修正链和第一组关系代价；每章换一种冲突发动机。"


def _repair_arc_climax(current_book: bool) -> str:
    if current_book:
        return "第一卷高潮不固定为单一奇遇，而是让主角在门派追查、身份误认、资源交换、伤病求医、护送失物等多种桥段中主动选择一条高风险路线，并为此欠下关键人情或暴露线索。"
    return "第一卷高潮不固定为单一桥段，而是让主角在身份压力、资源交换、关系误读、规则惩罚和外部追逼中选择一条高风险路线，并留下清晰后果。"


def _repair_arc_turn(current_book: bool) -> str:
    if current_book:
        return "主角发现所谓桥段不是可刷任务，而是会改变人物关系和门派追查的真实因果。"
    return "主角发现核心卖点不能直接兑现胜利，它每次使用都会改变人物关系、暴露信息或引来新的代价。"


def _merge_rule(existing: str, addition: str) -> str:
    existing = str(existing or "").strip()
    if not existing:
        return addition
    if addition in existing:
        return existing
    return f"{existing}\n{addition}"


def _approval_fields() -> list[tuple[str, str]]:
    return [
        ("premise", "一句话核心设定"),
        ("reader_promise", "读者承诺"),
        ("world_engine", "世界规则/能力曲线"),
        ("protagonist_engine", "主角动力/成长弧"),
        ("conflict_engine", "长期冲突/主线"),
        ("forbidden_rules", "禁忌规则"),
        ("style_guide", "文风指南"),
        ("volume_summary", "第一卷摘要"),
        ("arc_goal", "剧情段目标"),
        ("arc_climax", "剧情段高潮"),
        ("arc_turn", "剧情段转折"),
    ]
