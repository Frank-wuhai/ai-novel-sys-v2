from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, ProductionRunReview, QualityReport
from app.services.quality import HARD_FLOOR as _QUALITY_SOFT_PASS_FLOOR


OPTIMIZATION_MARKER = "production_optimization@v1"
OPTIMIZATION_END_MARKER = "production_optimization@end"


@dataclass(frozen=True)
class ChapterTypeProfile:
    code: str
    label: str
    pass_score: int
    required_dimensions: dict[str, int]
    reader_experience: str
    skeleton_requirements: tuple[str, ...]


@dataclass(frozen=True)
class RevisionEfficiencyDecision:
    tier: str
    label: str
    confidence: int
    predicted_pass_delta: int
    should_rebuild: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class SkeletonPreflightResult:
    passed: bool
    missing: tuple[str, ...]
    block: str


def chapter_type_profile(chapter_number: int, *, goal: str = "", required_beats: str = "", constraints: str = "") -> ChapterTypeProfile:
    text = "\n".join([goal or "", required_beats or "", constraints or ""])
    if chapter_number == 1:
        return ChapterTypeProfile(
            code="opening",
            label="开篇章",
            pass_score=72,
            required_dimensions={
                "reader_momentum": 65,
                "hook_strength": 68,
                "author_intent": 65,
                # 2026-07-26 · brief_coverage 60→45 对齐 early_serial 既定决定。
                # 全库均为 reading_assessment 重建型 brief，覆盖点是生产指令+被拆碎的
                # 剧情基线，brief_coverage 天然只到 47-54（book#2 线上已过审章同样 51-54）。
                # early_serial(2-5章)早已把此维降到45并注明"关键词命中率不该硬卡"，opening
                # 遗留在60是不一致。降此维不放水：reader_momentum/hook_strength/
                # chapter_unit_flow + hard_gate + 主编采样仍独立守开局章红线。
                "brief_coverage": 45,
                "chapter_unit_flow": 62,
            },
            reader_experience="让读者立刻明白主角处境、核心卖点和章末期待。",
            # ★ 借鉴 C:细纲 13 项模板(业界 Novel Writer V3.2 验证)
            # 5 项骨架 → 13 项细纲(POV/场景/人物/字数/节奏/核心事件/爽点/伏笔/情绪/对话/能力/钩子/衔接)
            skeleton_requirements=(
                "POV(谁视角:第一/第三人称/主角独白)",
                "场景(具体场所:宿舍/教室/街边/网络/虚拟空间)",
                "出场人物(明确点名,含敌友关系)",
                "字数目标(1800-2600 汉字,绝对上限 2600)",
                "节奏(快/中/慢,动作密集/对话密集/心理密集)",
                "核心事件(本章必须完成的剧情推进 1-3 条)",
                "爽点位置(打脸/抽奖/撩妹/反转/揭秘,具体场景)",
                "伏笔埋设(本章新增悬念,给后续章用)",
                "情绪曲线(主角情绪从X到Y的递进路径)",
                "对话要点(必出现的对白 2-3 句,带声线)",
                "能力展示(金手指/系统/技能的具体展示场景)",
                "章末钩子(章末3段必须留的具体事件/发现/悬念)",
                "前章衔接(承接上一章末的人物/事件/状态)",
            ),
        )
    # P0-2 · 前 5 章一律 early_serial（早期章都在铺垫远处高潮 · 极易误命中转折关键词）
    if chapter_number <= 5:
        return ChapterTypeProfile(
            code="early_serial",
            label="前五章推进章",
            pass_score=72,
            # P0-2 v2 · 降 brief_coverage/reader_momentum 门槛 · 加逻辑&套路防线
            required_dimensions={
                "brief_coverage": 45,        # 62→45 · 关键词命中率不该硬卡
                "reader_momentum": 55,       # 64→55 · 有推动力就够
                "choice_and_cost": 60,       # 62→60 · 微降
                "hook_strength": 62,         # 65→62 · 微降
                "chapter_unit_flow": 62,     # 64→62 · 微降
                # 保险：这三项是新加的硬门槛 · 防逻辑崩/套路化/因果断
                "narrative_logic": 70,       # 逻辑不能乱
                "anti_ai_flavor": 65,        # 不能太 AI 味/套路
                "causal_continuity_quality": 60,  # 因果不能断
            },
            reader_experience="让读者确认这本书能连续追：上一章后果、本章新压力、章末新期待都要清楚。",
            # ★ 借鉴 C:细纲 13 项模板
            skeleton_requirements=(
                "POV(主角/配角视角切换,日记/独白/旁白)",
                "场景(具体场所,镜头感强)",
                "出场人物(主角+配角+反派各占什么戏份)",
                "字数目标(1800-2600 汉字,绝对上限 2600)",
                "节奏(中速推进:不爆不闷,5-7 个小单元)",
                "核心事件(承接前章后果+本章新压力+推进主弧)",
                "爽点位置(小爽点 1-2 个,持续给读者追读理由)",
                "伏笔埋设(本章新埋 1-2 条伏笔,给后续章回收)",
                "情绪曲线(主角从警觉→试探→推进→小满足)",
                "对话要点(2-3 句带声线的对白,推动剧情)",
                "能力展示(金手指/技能的实际使用场景)",
                "章末钩子(具体未完事件/新危险/新疑问,非抽象压力)",
                "前章衔接(必须接住上一章结尾的后果+情绪+悬念)",
            ),
        )
    # P0-2 · turning_point 关键词收窄：只在 goal 前 40 字明确"本章是转折/高潮"才判定
    #        · payoff_grounding 移出硬门槛（学院派维度不该做硬门）
    #        · 硬门槛全线降 5-8 分（原门槛过严）
    goal_head = (goal or "")[:40]
    turning_markers = ("本章高潮", "本章决战", "本章反转", "本章转折", "本章爆发", "章节高潮", "章节决战")
    if any(marker in goal_head for marker in turning_markers):
        return ChapterTypeProfile(
            code="turning_point",
            label="转折/高潮章",
            pass_score=72,
            required_dimensions={
                "conflict_pressure": 62,
                "choice_and_cost": 62,
                "earned_payoff": 58,
                "chapter_unit_flow": 60,
            },
            reader_experience="让读者看到选择、代价和局面变化同时落地。",
            # ★ 借鉴 C:细纲 13 项模板
            skeleton_requirements=(
                "POV(主角视角深度内心戏,带倒计时压力)",
                "场景(高压力场景:对峙/打斗/谈判/选择节点)",
                "出场人物(主角+核心对手+至少 1 个支线人物)",
                "字数目标(1800-2600 汉字,绝对上限 2600)",
                "节奏(快节奏:动作+对话密集,1-2 个情绪爆发点)",
                "核心事件(冲突升级→主动选择→代价→回报→反转 5 步)",
                "爽点位置(高潮爽点,本章最大的打脸/揭秘/反转)",
                "伏笔埋设(1-2 条关键伏笔,为后续高潮蓄力)",
                "情绪曲线(压力累积→爆发→情绪释放→新起点)",
                "对话要点(3-5 句带声线对白,带权力关系变化)",
                "能力展示(金手指/技能的高阶使用,带代价)",
                "章末钩子(新格局/新对手/新任务,留 1-2 个未解悬念)",
                "前章衔接(必须接住前文铺垫的高潮引线)",
            ),
        )
    return ChapterTypeProfile(
        code="serial_progress",
        label="连载推进章",
        pass_score=70,
        required_dimensions={
            "brief_coverage": 60,
            "chapter_unit_flow": 64,
            "dialogue_fullness": 55,
            "scene_atmosphere": 55,
            "hook_strength": 62,
        },
        reader_experience="让读者顺着场景推进往下读：目标、阻碍、信息增量和章末压力不断交接。",
        # ★ 借鉴 C:细纲 13 项模板
        skeleton_requirements=(
            "POV(主角视角,可灵活切到 1-2 个配角视角)",
            "场景(主场景 + 1 个过渡场景,空间切换有交代)",
            "出场人物(主角+1-2 配角,新人物首次出场需交代身份)",
            "字数目标(1800-2600 汉字,绝对上限 2600)",
            "节奏(中速:5-7 个小单元,场景推进连贯)",
            "核心事件(承接前章+本章新压力+1-2 步主弧推进)",
            "爽点位置(小爽点 1 个,持续给读者追读动力)",
            "伏笔埋设(本章新埋 1 条伏笔,不要硬塞)",
            "情绪曲线(主角从承接→处理→小满足或新压力)",
            "对话要点(2-3 句对白,带声线,推动信息释放)",
            "能力展示(金手指/技能的实际使用或展示)",
            "章末压力(具体未完事件/新压力/新疑问)",
            "前章衔接(必须接住上一章末的人物/事件/状态)",
        ),
    )


def enrich_quality_report_with_optimization(
    report_data: dict[str, Any],
    *,
    chapter_number: int,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    enforce_gate: bool = True,
) -> dict[str, Any]:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    gate_failures = [
        f"{name}={int(dimensions.get(name) or 0)}<{threshold}"
        for name, threshold in profile.required_dimensions.items()
        if int(dimensions.get(name) or 0) < threshold
    ]
    base_score = int(report_data.get("score") or 0)
    predicted = predict_revision_pass(
        report_data,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
    )
    type_gate_passed = base_score >= profile.pass_score and not gate_failures
    # A 方案（2026-07-23）· 生死线章型保留 type_gate 高标准否决：opening/early_serial
    # (前5章·留存率生死线)、turning_point(转折章·爽点兑现)。其余(serial_progress)
    # 裁决权归 quality 层。此标记同时写入 chapter_type_gate 字段供 downstream blocker 读。
    STRICT_GATE_CODES = ("opening", "early_serial", "turning_point")
    is_strict_chapter = profile.code in STRICT_GATE_CODES
    # Sprint 2 P2-Ch44 soft-pass: architectural fix for the "LLM says pass but
    # structural gate blocks" trap. When base quality + editorial gate agree
    # the draft is acceptable AND every failing structural dimension is within
    # a small gap of its threshold (<=15pt), promote to soft-pass. Preserves
    # the audit trail (gate.passed stays False, failures preserved) but stops
    # the pipeline from burning 20+ revisions on drafts the LLM already
    # accepted. Matches the user's manual open-loop policy exactly:
    # editorial pass + base pass + gap<=15 -> soft accept. See
    # scripts/chapter_type_gate_soft_pass_regression.py for the invariant set.
    soft_pass_gap_ceiling = 15
    soft_pass_active = False
    soft_pass_reason: str | None = None
    if not type_gate_passed:
        # Compute the max single-dimension gap; blocking condition is any
        # single dim >soft_pass_gap_ceiling from threshold OR score too low.
        # 注意：gate_failures 为空但 type_gate 未过的情形 = 所有必需维度达标、
        # 仅总分卡在 pass_score 门槛下（口语化 B 版典型）。此时 max_gap 自然=0，
        # 比有 failures 的情形更该放行——不能因 failures 空而漏进本分支。
        max_gap = 0
        try:
            for name, threshold in profile.required_dimensions.items():
                actual = int(dimensions.get(name) or 0)
                if actual < threshold:
                    max_gap = max(max_gap, threshold - actual)
        except Exception:
            max_gap = soft_pass_gap_ceiling + 1  # fail-closed on shape errors
        hard_gate_ok = bool((report_data.get("hard_gate") or {}).get("passed", True))
        editorial_ok = bool((report_data.get("editorial_gate") or {}).get("passed", False))
        base_ok = bool(report_data.get("base_quality_passed", report_data.get("passed", False)))
        # A 方案（2026-07-23）修复死逻辑：原 score_ok = base_score >= profile.pass_score
        # 与进入本分支的前提(not type_gate_passed → base_score < pass_score)互斥，
        # soft_pass 永远无法激活。改用 quality 层 soft_pass 底线(HARD_FLOOR=65)：
        # 只要 quality 已判可发布(≥65)且各结构维度接近门槛(gap≤15)，type_gate 认可。
        # 这打通"quality soft_pass 但 type_gate(70/72) 二次否决"的口语化 B 版陷阱。
        score_ok = base_score >= _QUALITY_SOFT_PASS_FLOOR
        if hard_gate_ok and editorial_ok and base_ok and score_ok and max_gap <= soft_pass_gap_ceiling:
            soft_pass_active = True
            _fail_desc = ','.join(gate_failures[:3]) if gate_failures else 'none(all_dims_pass,score<pass_score)'
            soft_pass_reason = (
                f"soft_pass:editorial+base agree,max_gap={max_gap}pt<="
                f"{soft_pass_gap_ceiling}pt,failures={_fail_desc}"
            )
    report_data["chapter_type_gate"] = {
        "schema": "chapter_type_gate_v1",
        "chapter_type": profile.code,
        "label": profile.label,
        "pass_score": profile.pass_score,
        "reader_experience": profile.reader_experience,
        "required_dimensions": profile.required_dimensions,
        "failures": gate_failures,
        "passed": type_gate_passed,
        "soft_pass": soft_pass_active,
        "soft_pass_reason": soft_pass_reason,
        # A 方案（2026-07-23）· strict=True 表示该章型(opening/early_serial/
        # turning_point)保留 type_gate 否决权；False(serial_progress)则裁决权
        # 归 quality 层。downstream blocker 靠此标记区分是否放行。
        "strict": is_strict_chapter,
    }
    report_data["revision_pass_prediction"] = {
        "schema": "revision_pass_prediction_v1",
        "tier": predicted.tier,
        "label": predicted.label,
        "confidence": predicted.confidence,
        "predicted_pass_delta": predicted.predicted_pass_delta,
        "should_rebuild": predicted.should_rebuild,
        "reasons": list(predicted.reasons),
    }
    # A 方案（2026-07-23）· 清理 type_gate 越权否决（精准边界）：
    # quality 层(evaluate_chapter)已是完整质量裁决 —— hard_gate(篇幅/世界观矛盾/
    # bias/番茄硬指标)一票否决 + 65 分 soft_pass 门槛 + intent LLM 语义复核。
    # 常规连载推进章(serial_progress)的 type_gate(pass_score=70 + scene_atmosphere
    # 等学院派维度)对 quality 已放行的口语化 B 版是越权二次否决 → 归还 quality 层。
    #
    # 例外(保留 type_gate 高标准否决)：opening/early_serial/turning_point —— 开局前
    # 5 章是留存率生死线、转折章是爽点兑现点，高标准有真实商业价值，非学院派冗余。
    # 这些章型即使 hard_gate 过、type_gate 未过仍否决(维持原逻辑)。is_strict_chapter
    # 已在上方(line~145)定义并写入 chapter_type_gate.strict 字段。
    hard_gate_ok = bool((report_data.get("hard_gate") or {}).get("passed"))
    # serial_progress(常规章): hard_gate 过即放行; 生死线章型: 保留 type_gate 否决
    type_gate_defers_to_quality = hard_gate_ok and not is_strict_chapter
    if (
        report_data.get("passed")
        and enforce_gate
        and not type_gate_passed
        and not soft_pass_active
        and not type_gate_defers_to_quality  # ← 常规章 hard_gate 过则不否决
    ):
        report_data["passed"] = False
        report_data["status"] = "FAIL"
        issues = [str(item) for item in report_data.get("issues") or []]
        issues.append("chapter_type_gate_failed:" + ",".join(gate_failures[:5]))
        report_data["issues"] = list(dict.fromkeys(issues))
    elif type_gate_passed or type_gate_defers_to_quality:
        # type_gate 过 OR 常规章 quality 层已放行 · 清理过期 type_gate issue
        # （否则残留的 chapter_type_gate_failed 会被下游 blocker 当阻塞）
        cleaned = [
            str(item) for item in (report_data.get("issues") or [])
            if not str(item).startswith("chapter_type_gate_failed")
        ]
        report_data["issues"] = cleaned
    return report_data


def predict_revision_pass(
    report_data: dict[str, Any],
    *,
    chapter_number: int,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
) -> RevisionEfficiencyDecision:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    score = int(report_data.get("score") or 0)
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    issues = [str(item) for item in report_data.get("issues") or []]
    low = {name: int(value) for name, value in dimensions.items() if _int(value) < 60}
    hard_failed = not bool(hard_gate.get("passed", True)) or any(
        issue.startswith(("too_short", "too_long", "forbidden_marker", "setting_contradiction", "bias_blocker"))
        for issue in issues
    )
    structure_low = [name for name in ("brief_coverage", "author_intent", "arc_alignment", "chapter_necessity") if _int(dimensions.get(name)) < 55]
    craft_low = [
        name
        for name in ("dialogue_fullness", "scene_atmosphere", "imageable_paragraphs", "prose_voice", "chapter_unit_flow")
        if _int(dimensions.get(name)) < 65
    ]
    gate_failures = [
        name for name, threshold in profile.required_dimensions.items() if _int(dimensions.get(name)) < threshold
    ]
    if hard_failed or len(structure_low) >= 2 or score < 62:
        return RevisionEfficiencyDecision(
            tier="rebuild",
            label="候选重建",
            confidence=90 if hard_failed else 82,
            predicted_pass_delta=18,
            should_rebuild=True,
            reasons=tuple([*structure_low[:4], *issues[:3]] or ["结构或硬门禁风险高"]),
        )
    if score >= profile.pass_score - 4 and len(gate_failures) <= 3 and len(craft_low) <= 4:
        return RevisionEfficiencyDecision(
            tier="light",
            label="轻修",
            confidence=78,
            predicted_pass_delta=5,
            should_rebuild=False,
            reasons=tuple(gate_failures[:4] or craft_low[:4] or ["接近通过，只需局部补强"]),
        )
    return RevisionEfficiencyDecision(
        tier="targeted",
        label="定点重修",
        confidence=74,
        predicted_pass_delta=10,
        should_rebuild=False,
        reasons=tuple([*gate_failures[:4], *list(low)[:4]] or ["需要场景级定点重修"]),
    )


def apply_skeleton_preflight_to_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    brief: ChapterBrief,
) -> SkeletonPreflightResult:
    profile = chapter_type_profile(
        chapter_number,
        goal=brief.goal or "",
        required_beats=brief.required_beats or "",
        constraints=brief.constraints or "",
    )
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    missing = tuple(item for item in profile.skeleton_requirements if not _skeleton_requirement_covered(item, text))
    block = _optimization_block(profile=profile, missing=missing, memory=passed_chapter_memory(session, book_id=book_id, chapter_number=chapter_number))
    if missing:
        brief.required_beats = _replace_optimization_block(brief.required_beats or "", block)
        session.flush()
    return SkeletonPreflightResult(passed=not missing, missing=missing, block=block)


def optimization_prompt_block(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
) -> str:
    profile = chapter_type_profile(chapter_number, goal=goal, required_beats=required_beats, constraints=constraints)
    missing = tuple(item for item in profile.skeleton_requirements if not _skeleton_requirement_covered(item, "\n".join([goal, required_beats, constraints])))
    return _optimization_block(profile=profile, missing=missing, memory=passed_chapter_memory(session, book_id=book_id, chapter_number=chapter_number))


def passed_chapter_memory(session: Session, *, book_id: int, chapter_number: int, limit: int = 5) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(ProductionRunReview)
            .where(ProductionRunReview.book_id == book_id, ProductionRunReview.status == "pass")
            .order_by(ProductionRunReview.id.desc())
            .limit(max(limit, 1))
        )
    )
    lessons: list[str] = []
    chapters: list[int] = []
    for row in rows:
        data = _loads_json(row.review_json)
        source_chapter = int(data.get("chapter_number") or 0)
        if source_chapter >= chapter_number:
            continue
        chapters.append(source_chapter)
        headline = str(data.get("headline") or "").strip()
        if headline:
            lessons.append(f"第{source_chapter}章有效经验：{headline}")
        for item in data.get("recommendations") or []:
            lessons.append(str(item))
    return {
        "schema": "passed_chapter_memory_v1",
        "source_chapters": chapters[:limit],
        "lessons": list(dict.fromkeys(lessons))[:6],
    }


def _optimization_block(*, profile: ChapterTypeProfile, missing: tuple[str, ...], memory: dict[str, Any]) -> str:
    lines = [
        OPTIMIZATION_MARKER,
        f"章节类型：{profile.label}；目标读者体验：{profile.reader_experience}",
        "章节骨架验收：目标、阻碍、行动、代价/回报、章末变化必须在正文场景里出现。",
    ]
    if missing:
        lines.append("当前 brief 缺口：" + "、".join(missing))
        lines.append("生成前必须先把缺口落成具体人物行动、对话试探、空间阻碍或章末后果。")
    lessons = [str(item) for item in memory.get("lessons") or [] if item]
    if lessons:
        lines.append("合格章样本记忆：")
        lines.extend(f"- {item}" for item in lessons[:4])
    lines.append(OPTIMIZATION_END_MARKER)
    return "\n".join(lines)


def _replace_optimization_block(text: str, block: str) -> str:
    cleaned = re.sub(
        rf"\n?{re.escape(OPTIMIZATION_MARKER)}.*?{re.escape(OPTIMIZATION_END_MARKER)}\n?",
        "\n",
        text or "",
        flags=re.S,
    ).strip()
    return "\n".join(item for item in [cleaned, block] if item).strip()


def _skeleton_requirement_covered(requirement: str, text: str) -> bool:
    aliases = {
        "主角处境": ("主角", "处境", "身份", "困境"),
        "核心卖点": ("卖点", "能力", "金手指", "核心"),
        "可见阻碍": ("阻碍", "压力", "冲突", "误判", "危险"),
        "主动选择": ("选择", "主动", "决定", "答应", "拒绝"),
        "章末钩子": ("章末", "钩子", "下一章", "新线索", "变化"),
        "承接上一章": ("承接", "上一章", "后果", "前章"),
        "本章目标": ("目标", "本章", "任务", "想要"),
        "外部阻碍": ("阻碍", "压力", "冲突", "对手", "环境"),
        "行动代价": ("代价", "后果", "损耗", "交换", "风险"),
        "章末变化": ("章末", "变化", "局面", "新危险", "新机会"),
        "冲突升级": ("冲突", "升级", "逼近", "爆发"),
        "明确代价": ("代价", "后果", "付出", "损耗"),
        "回报落地": ("回报", "奖励", "收益", "结果", "落地"),
        "局面反转": ("反转", "转折", "变局", "改变"),
        "信息释放": ("信息", "线索", "发现", "证据"),
        "行动后果": ("行动", "后果", "代价", "影响"),
        "章末压力": ("章末", "压力", "危险", "未解决"),
    }
    return any(alias in text for alias in aliases.get(requirement, (requirement,)))


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
