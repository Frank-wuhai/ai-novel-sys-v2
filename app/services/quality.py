from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from app.services.anti_ai_flavor import evaluate_anti_ai_flavor
from app.services.bias import evaluate_generation_bias
from app.services.chapter_units import evaluate_chapter_units
from app.services.design_quality import evaluate_design_quality
from app.services.expression_precision import evaluate_expression_precision
from app.services.humanized_quality import evaluate_humanized_delivery
from app.services.intent_acceptance import evaluate_author_intent
from app.services.literary_relation import evaluate_literary_relation
from app.services.naming_governance import evaluate_naming_governance
from app.services.narrative_logic import evaluate_narrative_logic
from app.services.chapter_continuity_gate import evaluate_chapter_continuity
from app.services.world_logic import evaluate_world_logic
from app.services.paragraph_aesthetic import evaluate_paragraph_aesthetic
from app.services.fanqie_hard_metrics import evaluate_fanqie_metrics
from app.services.prose_naturalness import evaluate_prose_naturalness
from app.services.prose_voice import evaluate_prose_voice
from app.services.production_contract import production_contract_for_quality
from app.services.prompt_isolation import isolate_generation_inputs, line_conflicts_with_authority
from app.services.reality_logic import evaluate_reality_logic
from app.services.reference_craft import evaluate_reference_craft
from app.services.readability import evaluate_readability
from app.services.story_bible_logic_gate import evaluate_story_bible_logic
from app.services.system_artifact_gate import evaluate_system_artifacts
from app.services.writer_craft import evaluate_writer_craft


HARD_FLOOR = 65
PASS_FLOOR = 75

# ------------------------------------------------------------------
# tomato_relevance 维度分层（P0-1 · 体检报告 F1）
#   strong   ：番茄读者真会在意 · 满权重进 score
#   weak     ：偏文学但能提升体验 · 半权重进 score
#   misaligned：学院派错位 · 不进 score · 仅作 warning
# ------------------------------------------------------------------
TOMATO_STRONG_DIMENSIONS = frozenset({
    "basic_publishability",
    "reader_momentum",
    "conflict_pressure",
    "choice_and_cost",
    "hook_strength",
    "earned_payoff",
    "dialogue_fullness",
    "prose_naturalness",
    "character_action",
    "chapter_necessity",
    "chapter_unit_flow",
    "arc_alignment",
    "brief_coverage",
    "platform_risk",
    "setting_risk",
})
TOMATO_WEAK_DIMENSIONS = frozenset({
    "readability",
    "prose_density",
    "prose_voice",
    "native_chinese_flow",
    "character_voice",
    "anti_ai_flavor",
    "natural_sentence_glue",
    "non_checklist_narration",
    "diction_fit",
    "decorative_restraint",
    "dialogue_particle_flow",
    "production_standard",
    "author_intent",
    "narrative_logic",
    "causal_continuity_quality",
    "cost_plausibility",
    "expression_precision",
    "object_verb_collocation",
    "reference_craft",
    "scene_craft",
    "psychological_chain",
    "rhetoric_specificity",
    "diction_vividness",
    "action_reaction_chain",
    "writer_craft",
    "literary_relation",
    "relation_legality",
    "lexical_naturalness",
    "sensory_chain",
    "scene_technique_fit",
    "beauty_grounding",
})
TOMATO_MISALIGNED_DIMENSIONS = frozenset({
    "scene_atmosphere",
    "visual_staging",
    "imageable_paragraphs",
    "payoff_grounding",
    "design_texture",
    "designed_nomenclature",
    "designed_asset",
    "naming_governance",
    "paragraph_aesthetic",
    "memorable_image",
    "memorable_dialogue",
    "embodied_pov",
    "scene_expansion",
    "opening_variety",
    "causal_scene_chain",
    "reaction_chain",
    "observation_logic",
    "inference_chain",
    "wording_specificity",
    "canon_consistency",
})


def _weighted_tomato_score(dimensions: dict) -> tuple[int, dict]:
    """按 tomato_relevance 分层加权求分。

    strong 权重 1.0 · weak 权重 0.5 · misaligned 权重 0（仅作 warning）。
    返回 (聚合分数 0-100, 分层明细 dict)。
    """
    strong_vals = [v for k, v in dimensions.items() if k in TOMATO_STRONG_DIMENSIONS]
    weak_vals = [v for k, v in dimensions.items() if k in TOMATO_WEAK_DIMENSIONS]
    misaligned_vals = [v for k, v in dimensions.items() if k in TOMATO_MISALIGNED_DIMENSIONS]
    strong_avg = round(sum(strong_vals) / len(strong_vals)) if strong_vals else 0
    weak_avg = round(sum(weak_vals) / len(weak_vals)) if weak_vals else 0
    misaligned_avg = round(sum(misaligned_vals) / len(misaligned_vals)) if misaligned_vals else 0
    # 加权平均：strong 权 1.0 · weak 权 0.5 · misaligned 权 0
    weighted = (strong_avg * 1.0 + weak_avg * 0.5) / 1.5 if strong_vals or weak_vals else 0
    breakdown = {
        "strong_avg": strong_avg,
        "weak_avg": weak_avg,
        "misaligned_avg": misaligned_avg,
        "strong_count": len(strong_vals),
        "weak_count": len(weak_vals),
        "misaligned_count": len(misaligned_vals),
        "weighted_score": round(weighted),
        "weighting": {"strong": 1.0, "weak": 0.5, "misaligned": 0.0},
    }
    return round(weighted), breakdown


def classify_quality_verdict(*, score: int, hard_dimension_ok: bool, has_blocking_issues: bool) -> str:
    """Three-tier quality verdict: hard_fail / soft_pass / pass.

    Phase 2/3 quality gate stratification. Pure function so callers, tests,
    and dashboards can share one canonical definition.

    - ``hard_fail``: score < 65 OR blocking issues present OR hard dimension
      floor breached. Must continue revising.
    - ``soft_pass``: 65 <= score < 75 with hard gate cleared. Publishable
      with human acceptance; early-stop still nudges revisions upward.
    - ``pass``: score >= 75 with hard gate cleared. Recommended stop.
    """
    hard_gate_ok = hard_dimension_ok and not has_blocking_issues and score >= HARD_FLOOR
    if not hard_gate_ok:
        return "hard_fail"
    if score >= PASS_FLOOR:
        return "pass"
    return "soft_pass"


@dataclass
class QualityResult:
    passed: bool
    score: int
    report: str
    dimensions: dict[str, int]
    issues: list[str]


FORBIDDEN_MARKERS = ["Runtime Draft", "generated_by_agent", "model_used", "系统提示", "作为AI"]
BLOCKING_CONTRADICTIONS = ["无代价", "没有代价", "无需代价", "无限使用", "永久无敌"]
MOMENTUM_MARKERS = [
    "压力", "危机", "选择", "代价", "发现", "钩子", "异象", "秘密",
    # 2026-07-26 · 第6批根治：口语化爽文用具象推进信号，非抽象词。追读动力来自
    # 悬念/反转/威胁逼近/爽点兑现/身体紧张，用真实叙事变体补全（纯流水账仍低分）。
    "居然", "竟然", "突然", "忽然", "没想到", "反转", "打脸", "逼近", "催", "警告",
    "副作用", "倒计时", "还没", "没等", "紧接着", "标记", "暴露", "死路", "惨叫",
    "盯着", "攥紧", "心跳", "喉咙", "停住", "顿住", "咬上来", "找上门",
]
CONFLICT_MARKERS = [
    # 抽象冲突词（原表）
    "压力", "危机", "冲突", "阻碍", "危险", "逼近", "追查", "失控",
    # Phase E.3: 动作性冲突表达——好文章往往用动作代替抽象词表达冲突
    # 审视/揭穿类
    "盯", "盘问", "追问", "质问", "揭穿", "识破", "戳穿", "试探",
    # 逼迫/僵持类
    "逼", "堵", "困住", "对峙", "紧盯", "戒备", "警觉",
    # 悬疑/揭破类
    "警告", "怀疑", "起疑", "不对劲", "破绽", "露馅",
]
CHOICE_COST_MARKERS = [
    # 抽象选择词（原表）
    "选择", "代价", "付出", "损耗", "承担", "交换", "收益", "后果",
    # Phase E.3: 动作性选择/代价表达
    # 决断类
    "决定", "咬牙", "豁出去", "赌", "拼",
    # 损失类
    "赔上", "折损", "失去", "丢掉", "亏",
    # 承担类
    "扛", "顶", "硬撑", "认",
    # 后果表达
    "反噬", "麻烦",
]
HOOK_MARKERS = [
    "钩子", "秘密", "发现", "转折", "章末", "疑问", "源头", "倒影", "异象", "陌生",
    "消息", "脚步", "门外", "黑影", "下一次",
    # 世界观/系统异常型章末压力：不限定为“黑影敲门”式悬疑，也能识别规则变化带来的下一章牵引。
    "异常", "倒计时", "倒数", "坐标", "锚定", "锁定", "追踪", "清除", "通道", "巡检",
    # 写实仙侠入口型章末压力：来自具体异常证据和未完成代价，而不是抽象悬疑词。
    "第二个", "上一个", "半个月", "后颈", "印子", "旧盔壳", "旧盔", "往回拽", "拖没", "冷意",
]
HOOK_PRESSURE_MARKERS = [
    "倒计时", "倒数", "少了一秒", "强制清除", "清除驻留痕迹", "坐标已同步", "坐标",
    "身份锚定", "锚定进度", "通道开启", "下次通道", "非标驻留", "巡检序列", "壁垒异常",
    "数据壁垒异常", "锁定", "追踪", "异常提示",
    "第二个", "上一个", "半个月拽一个", "往回收", "往回拽", "被拖没", "没抓住",
]
HOOK_CONCRETE_MARKERS = [
    "竹牌", "豆油灯", "窗棂", "柴房", "门窗", "提示文字", "冷灰色的小字", "血红小字", "灭了",
    "旧盔壳", "蓝印", "药刀", "门框", "陶壶", "药汤", "竹门", "木栓",
]
FILLER_MARKERS = ["水字数", "无意义", "随便", "重复一遍", "占位", "凑字数"]


def chinese_chars(text: str) -> int:
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


def evaluate_chapter(
    text: str,
    *,
    min_chars: int = 1200,
    max_chars: int = 8000,
    goal: str = "",
    required_beats: str = "",
    constraints: str = "",
    canon_context: str = "",
    authority_terms: dict | None = None,
    previous_hook_keywords: list[str] | None = None,
    book_id: int | None = None,
    chapter_number: int | None = None,
    chapter_type: str = "",
    session=None,
) -> QualityResult:
    issues: list[str] = []
    isolated_inputs = isolate_generation_inputs(
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
        previous_chapter_context="",
        strict_authority_fields=False,
    )
    goal = isolated_inputs.goal
    required_beats = isolated_inputs.required_beats
    constraints = isolated_inputs.constraints
    canon_context = isolated_inputs.canon_context
    # B 方案（2026-08-10）· 自动从 chapter_number 推断 chapter_type（向旧调用方兼容）
    if not chapter_type and chapter_number is not None:
        from app.services.chapter_standards import _resolve_chapter_type
        chapter_type = _resolve_chapter_type(chapter_number)
    warnings: list[str] = []
    count = chinese_chars(text)
    if count < min_chars:
        issues.append(f"too_short: {count} < {min_chars}")
    # 字数门（2026-07-28 D方案 Phase1·容差带）：
    #   DISPLAY_MAX(2600) = APP 显示口径 · 超此记 length_soft_over 观感 warning（软·不判结构失败）
    #   REBUILD_MAX(2800) = 硬结构重建门 · 超此才记 too_long issue（→ length_out_of_range 硬重建）
    # 用户口径「字数不管=比旧版±200可接受」：2600-2800 内不强制返工（返修扩写自然落点）。
    # 注意 max_chars 入参默认 8000，此处用 REBUILD_MAX 兜底对齐 too_long 触发点。
    from app.services.chapter_standards import DISPLAY_MAX_CHARS, REBUILD_MAX_CHARS
    display_max = min(max(max_chars, DISPLAY_MAX_CHARS), REBUILD_MAX_CHARS) if max_chars < 8000 else DISPLAY_MAX_CHARS
    hard_max = max(max_chars, REBUILD_MAX_CHARS) if max_chars < 8000 else max_chars
    if count > hard_max:
        issues.append(f"too_long: {count} > {hard_max}")
    elif count > display_max:
        warnings.append(f"length_soft_over: {count} > {display_max}(容差带内·不强制返工)")
    system_artifacts = evaluate_system_artifacts(text)
    for issue in system_artifacts.issues:
        issues.append(f"system_artifact:{issue}")
    _book_profile = None
    _story_bible_text = ""
    if book_id and session:
        try:
            from app.services.book_profile import build_book_profile
            _book_profile = build_book_profile(session, book_id=book_id)
        except Exception:
            _book_profile = None
        try:
            from sqlalchemy import select
            from app.models.entities import StoryBible
            bible = session.scalar(select(StoryBible).where(StoryBible.book_id == book_id))
            if bible:
                _story_bible_text = "\n".join(
                    part
                    for part in [
                        bible.positioning,
                        bible.reader_promise,
                        bible.main_plot,
                        bible.protagonist_arc,
                        bible.relationship_arc,
                        bible.power_curve,
                        bible.forbidden_rules,
                        bible.style_guide,
                    ]
                    if part
                )
        except Exception:
            _story_bible_text = ""
    # 抽奖系统爽文（"系统任务"不在 avoid 里）允许叙述性系统面板
    _allow_system_panel = bool(_book_profile and "系统任务" not in (_book_profile.avoid_markers or ()))
    for marker in FORBIDDEN_MARKERS:
        if _has_forbidden_marker(text, marker, allow_system_panel=_allow_system_panel):
            issues.append(f"forbidden_marker: {marker}")
    for marker in BLOCKING_CONTRADICTIONS:
        if _has_blocking_contradiction(text, marker):
            issues.append(f"setting_contradiction: {marker}")
    # P1-anchor · 强制正文含主角/世界锚点，防止跑题到别的作品
    if authority_terms:
        prot = [t for t in (authority_terms.get("protagonists") or []) if t]
        world = [t for t in (authority_terms.get("world_titles") or []) if t]
        if prot and not any(name in text for name in prot):
            issues.append(f"anchor_missing_protagonist: {'/'.join(prot[:3])}")
        if world and not _world_anchor_satisfied(text, world):
            issues.append(f"anchor_missing_world: {'/'.join(world[:3])}")
    # 一致性校验门（2026-07-28）：跨章名字漂移 / 章内整句重复 / 相邻状态矛盾。
    # 根治"分单元生成→拼接无全局一致性层"缺口（book2 ch1 林默/林北漂移即此症）。
    # 纯规则零 LLM·确定性·高置信度只挑硬断层，产出 name_drift/sentence_repeat/state_contradiction。
    try:
        from app.services.consistency_gate import evaluate_consistency
        _prot = None
        if authority_terms:
            _prot = [t for t in (authority_terms.get("protagonists") or []) if t]
        for _cissue in evaluate_consistency(text, protagonists=_prot):
            issues.append(_cissue)
    except Exception:
        pass
    story_bible_logic = evaluate_story_bible_logic(
        text,
        story_bible_text=_story_bible_text,
        canon_context=canon_context,
        constraints=constraints,
    )
    for issue in story_bible_logic.issues:
        issues.append(f"story_bible_logic:{issue}")
    continuity_gate = evaluate_chapter_continuity(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        current_text=text,
    )
    for issue in continuity_gate.issues:
        issues.append(f"chapter_continuity:{issue}")
    # P1-hook-continuity · 前章钩子承接校验 · 命中 < 2 → hard_gate FAIL
    # 拆词匹配：4 字整词拆成 2×2 字词 · 命中任一子词即算命中（防止 LLM 同义变体误伤）
    # 净化: 系统摘要词 blocklist 过滤 (章末后果/主角状态/未解压力/第X章已通过质检 等)
    #       过滤后如果关键词为空, 不触发 hook_missing
    if previous_hook_keywords:
        kws = [k for k in previous_hook_keywords if k and isinstance(k, str)]
        # 净化: 过滤系统摘要词
        _HOOK_BLOCKED = {
            "第2章已通过质检", "第3章已通过质检", "第1章已通过质检", "第N章已通过质检",
            "章末后果", "主角状态", "未解压力", "审核", "质检", "通过", "待审", "评审",
            "状态快照", "承接", "下一章", "本章", "硬门禁", "硬拦截", "提示", "要求",
            "必须", "系统", "面板", "作者", "修订",
        }
        def _is_hook_blocked(kw: str) -> bool:
            if kw in _HOOK_BLOCKED:
                return True
            for sub in ("已通过", "审核", "质检", "已发布", "已批准"):
                if sub in kw:
                    return True
            return False
        kws = [k for k in kws if not _is_hook_blocked(k)]
        if kws:
            def _kw_hit(kw: str, txt: str) -> bool:
                if kw in txt:
                    return True
                # 4 字词拆 2×2（e.g. 青衫折扇 → 青衫/折扇）
                if len(kw) == 4:
                    return kw[:2] in txt or kw[2:] in txt
                # 3 字词拆 2+1（e.g. 老陈头 → 老陈）
                if len(kw) == 3:
                    return kw[:2] in txt
                return False
            hits = [k for k in kws if _kw_hit(k, text)]
            if len(hits) < 2:
                issues.append(f"hook_missing: hits={len(hits)}/{len(kws)} kws={'/'.join(kws[:5])}")
    bias = evaluate_generation_bias(
        content=text,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
        profile=_book_profile,
    )
    for blocker in bias.blockers:
        issues.append(f"bias_blocker: {blocker}")
    intent = evaluate_author_intent(
        content=text,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        canon_context=canon_context,
        enable_llm=os.environ.get("QUALITY_INTENT_LLM", "1") == "1",
    )
    readability = evaluate_readability(text)
    humanized = evaluate_humanized_delivery(text)
    chapter_units = evaluate_chapter_units(text)
    design = evaluate_design_quality(text, canon_context=canon_context)
    prose_voice = evaluate_prose_voice(text)
    prose_naturalness = evaluate_prose_naturalness(text)
    expression_precision = evaluate_expression_precision(text)
    literary_relation = evaluate_literary_relation(text)
    naming = evaluate_naming_governance(text, canon_context=canon_context)
    narrative_logic = evaluate_narrative_logic(text)
    world_logic = evaluate_world_logic(text)
    reality_logic = evaluate_reality_logic(text)
    anti_ai = evaluate_anti_ai_flavor(design=design, prose_voice=prose_voice, humanized=humanized)
    writer_craft = evaluate_writer_craft(text)
    reference_craft = evaluate_reference_craft(text, session=session, book_id=book_id)
    paragraph_aesthetic = evaluate_paragraph_aesthetic(text)
    # 番茄爆款硬指标（2026-07-10）: 段均≤45 · 段/千≥25 · 游戏词/千≤10 (game_wuxia 放宽)
    fanqie = evaluate_fanqie_metrics(text, game_word_limit=10.0)
    for f_issue in fanqie.issues:
        issues.append(f_issue)
    for blocker in intent.blockers:
        if blocker != "intent_underfulfilled":
            issues.append(f"intent_blocker: {blocker}")
    if design.score < 60:
        issues.append(f"design_underdeveloped: {design.score}")
    # B 方案（2026-08-10）· 章节类型阈值表
    # 开篇章预期 imageable/visual_staging/scene_atmosphere 偏低（铺垫期/对话密集/背景交代），
    # 适当放宽硬拦阈值；其他章维持原阈值。
    # 注意：放宽是"放行"而非"加分"——visual_staging=54 在开篇章不再算 underdev
    # 但 chapter_type_gate 仍会按 opening 标尺独立打 reader_momentum/hook_strength/brief_coverage
    _is_opening = (chapter_type == "opening")
    _vis_threshold_under = 50 if not _is_opening else 45
    _vis_threshold_soft = 55 if not _is_opening else 50
    _img_threshold_under = 48 if not _is_opening else 43
    _img_threshold_soft = 55 if not _is_opening else 50
    if design.checks.get("visual_staging", 100) < _vis_threshold_under or (design.checks.get("visual_staging", 100) < _vis_threshold_soft and design.score < 65):
        issues.append(f"visual_underdeveloped: {design.checks.get('visual_staging', 0)}")
    if design.checks.get("imageable_paragraphs", 100) < _img_threshold_under or (
        design.checks.get("imageable_paragraphs", 100) < _img_threshold_soft and design.score < 65
    ):
        issues.append(f"imageable_underdeveloped: {design.checks.get('imageable_paragraphs', 0)}")
    if prose_voice.checks.get("native_chinese_flow", 100) < 60:
        issues.append(f"translationese_risk: {prose_voice.checks.get('native_chinese_flow', 0)}")
    if prose_naturalness.score < 65:
        issues.append(f"prose_naturalness_blocker: {prose_naturalness.score}")
    if prose_voice.checks.get("dialogue_fullness", 100) < 45 or (
        prose_voice.checks.get("dialogue_fullness", 100) < 50 and prose_voice.score < 65
    ):
        issues.append(f"dialogue_underdeveloped: {prose_voice.checks.get('dialogue_fullness', 0)}")
    if expression_precision.score < 60:
        issues.append(f"expression_precision_risk: {expression_precision.score}")
    if literary_relation.score < 60:
        issues.append(f"literary_relation_risk: {literary_relation.score}")
    if literary_relation.checks.get("relation_legality", 100) < 50:
        issues.append(f"relation_legality_blocker: {literary_relation.checks.get('relation_legality', 0)}")
    # Naming/aesthetic evaluators are diagnostic by default. They are useful for
    # editor guidance, but treating them as production blockers made readable
    # chapters fall into endless revision loops.
    if narrative_logic.score < 60:
        issues.append(f"narrative_logic_risk: {narrative_logic.score}")
    if world_logic.score < 60:
        issues.append(f"world_logic_blocker: {world_logic.score}")
    for issue in reality_logic.issues:
        issues.append(issue)
    if anti_ai.score < 60:
        issues.append(f"ai_flavor_risk: {anti_ai.score}")
    if writer_craft["score"] < 55:
        issues.append(f"writer_craft_underdeveloped: {writer_craft['score']}")
    if writer_craft["checks"].get("embodied_pov", 100) < 55:
        issues.append(f"embodied_pov_underdeveloped: {writer_craft['checks'].get('embodied_pov', 0)}")
    if writer_craft["checks"].get("scene_expansion", 100) < 55:
        issues.append(f"scene_expansion_underdeveloped: {writer_craft['checks'].get('scene_expansion', 0)}")
    for issue in reference_craft.issues:
        issues.append(issue)

    dimensions = {
        "basic_publishability": _basic_publishability_score(count, min_chars, max_chars, text),
        "brief_coverage": _coverage_score(text, coverage_points_for_brief(goal, required_beats, constraints, canon_context)),
        "canon_consistency": _canon_score(text, canon_context),
        "reader_momentum": _marker_score(text, MOMENTUM_MARKERS),
        "conflict_pressure": _marker_score(text, CONFLICT_MARKERS),
        "choice_and_cost": _marker_score(text, CHOICE_COST_MARKERS),
        "hook_strength": _hook_score(text),
        "prose_density": _prose_density_score(text),
        "arc_alignment": _arc_alignment_score(text, goal=goal, required_beats=required_beats, constraints=constraints, canon_context=canon_context),
        "production_standard": _production_standard_score(text, min_chars=min_chars),
        "setting_risk": _setting_risk_score(text),
        "platform_risk": _platform_risk_score(text),
        "author_intent": intent.score,
        "readability": readability.score,
        "opening_variety": _opening_variety_score(text),
        "causal_scene_chain": _causal_scene_chain_score(text),
        "reaction_chain": _reaction_chain_score(text),
        "earned_payoff": _earned_payoff_score(text),
        "design_texture": design.score,
        "visual_staging": design.checks.get("visual_staging", 0),
        "designed_nomenclature": design.checks.get("designed_nomenclature", 0),
        "naming_governance": naming.score,
        "narrative_logic": narrative_logic.score,
        "world_logic": world_logic.score,
        "reality_logic": reality_logic.score,
        "character_knowledge_boundary": world_logic.checks.get("character_knowledge_boundary", 0),
        "quest_source_plausibility": world_logic.checks.get("quest_source_plausibility", 0),
        "npc_agency": world_logic.checks.get("npc_agency", 0),
        "player_layer_intrusion": world_logic.checks.get("player_layer_intrusion", 0),
        "causal_continuity_quality": narrative_logic.checks.get("causal_continuity", 0),
        "cost_plausibility": narrative_logic.checks.get("cost_plausibility", 0),
        "scene_atmosphere": narrative_logic.checks.get("scene_atmosphere", 0),
        "payoff_grounding": narrative_logic.checks.get("payoff_grounding", 0),
        "imageable_paragraphs": design.checks.get("imageable_paragraphs", 0),
        "prose_voice": prose_voice.score,
        "prose_naturalness": prose_naturalness.score,
        "natural_sentence_glue": prose_naturalness.checks.get("natural_sentence_glue", 0),
        "non_checklist_narration": prose_naturalness.checks.get("non_checklist_narration", 0),
        "diction_fit": prose_naturalness.checks.get("diction_fit", 0),
        "decorative_restraint": prose_naturalness.checks.get("decorative_restraint", 0),
        "dialogue_particle_flow": prose_naturalness.checks.get("dialogue_particle_flow", 0),
        "expression_precision": expression_precision.score,
        "object_verb_collocation": expression_precision.checks.get("object_verb_collocation", 0),
        "observation_logic": expression_precision.checks.get("observation_logic", 0),
        "inference_chain": expression_precision.checks.get("inference_chain", 0),
        "wording_specificity": expression_precision.checks.get("wording_specificity", 0),
        "literary_relation": literary_relation.score,
        "relation_legality": literary_relation.checks.get("relation_legality", 0),
        "lexical_naturalness": literary_relation.checks.get("lexical_naturalness", 0),
        "sensory_chain": literary_relation.checks.get("sensory_chain", 0),
        "scene_technique_fit": literary_relation.checks.get("scene_technique_fit", 0),
        "beauty_grounding": literary_relation.checks.get("beauty_grounding", 0),
        "native_chinese_flow": prose_voice.checks.get("native_chinese_flow", 0),
        "dialogue_fullness": prose_voice.checks.get("dialogue_fullness", 0),
        "character_voice": prose_voice.checks.get("character_voice", 0),
        "anti_ai_flavor": anti_ai.score,
        "chapter_unit_flow": chapter_units.score,
        "reference_craft": reference_craft.score,
        "scene_craft": reference_craft.checks.get("scene_craft", 0),
        "psychological_chain": reference_craft.checks.get("psychological_chain", 0),
        "rhetoric_specificity": reference_craft.checks.get("rhetoric_specificity", 0),
        "diction_vividness": reference_craft.checks.get("diction_vividness", 0),
        "action_reaction_chain": reference_craft.checks.get("action_reaction_chain", 0),
        "writer_craft": writer_craft["score"],
        "memorable_image": writer_craft["checks"].get("memorable_image", 0),
        "memorable_dialogue": writer_craft["checks"].get("memorable_dialogue", 0),
        "designed_asset": writer_craft["checks"].get("designed_asset", 0),
        "character_action": writer_craft["checks"].get("character_action", 0),
        "chapter_necessity": writer_craft["checks"].get("chapter_necessity", 0),
        "embodied_pov": writer_craft["checks"].get("embodied_pov", 0),
        "scene_expansion": writer_craft["checks"].get("scene_expansion", 0),
        "paragraph_aesthetic": paragraph_aesthetic.score,
    }
    if dimensions["brief_coverage"] < 45:
        issues.append(f"brief_coverage_underfulfilled: {dimensions['brief_coverage']}")
    if dimensions["object_verb_collocation"] < 50:
        issues.append(f"expression_collocation_blocker: {dimensions['object_verb_collocation']}")
    if dimensions["cost_plausibility"] < 50:
        issues.append(f"cost_plausibility_blocker: {dimensions['cost_plausibility']}")
    if dimensions["causal_continuity_quality"] < 50:
        issues.append(f"causal_continuity_blocker: {dimensions['causal_continuity_quality']}")
    # 2026-07-24 架构矛盾修复：payoff_grounding 属于 TOMATO_MISALIGNED_DIMENSIONS(line65-86)，
    # 按设计权重0(仅作 warning·见 _weighted_tomato_score line92)，不应有 blocker 否决权。
    # 原 line369-370 把它当 blocker 硬拦(<50)，与 misaligned 权重0 语义矛盾——同一维度两套
    # 判定，导致番茄口语化 B 版(payoff_grounding 天然偏低·非硬伤)被越权拦截。降级为 warning，
    # 与同属 misaligned 的 scene_atmosphere(只在 warning 列表)处理一致。真正硬伤由
    # cost_plausibility/causal_continuity(TOMATO_STRONG)+hard_gate 守住。
    # payoff_grounding 已在下方 warnings 列表(line391)登记，删除此处 blocker 即完成降级。
    # (warnings 已在函数开头初始化，此处不再重复定义)
    for name in (
        "brief_coverage",
        "conflict_pressure",
        "choice_and_cost",
        "hook_strength",
        "prose_density",
        "arc_alignment",
        "production_standard",
        "causal_scene_chain",
        "reaction_chain",
        "earned_payoff",
        "design_texture",
        "visual_staging",
        "designed_nomenclature",
        "naming_governance",
        "narrative_logic",
        "reality_logic",
        "causal_continuity_quality",
        "cost_plausibility",
        "scene_atmosphere",
        "payoff_grounding",
        "imageable_paragraphs",
        "prose_voice",
        "expression_precision",
        "object_verb_collocation",
        "observation_logic",
        "inference_chain",
        "wording_specificity",
        "literary_relation",
        "relation_legality",
        "lexical_naturalness",
        "sensory_chain",
        "scene_technique_fit",
        "beauty_grounding",
        "native_chinese_flow",
        "dialogue_fullness",
        "character_voice",
        "anti_ai_flavor",
        "chapter_unit_flow",
        "reference_craft",
        "scene_craft",
        "psychological_chain",
        "rhetoric_specificity",
        "diction_vividness",
        "action_reaction_chain",
        "writer_craft",
        "memorable_image",
        "memorable_dialogue",
        "designed_asset",
        "character_action",
        "chapter_necessity",
            "embodied_pov",
            "paragraph_aesthetic",
        ):
        if dimensions[name] < 50:
            warnings.append(f"weak_narrative_dimension: {name}={dimensions[name]}")
        elif name in {
            "design_texture",
            "visual_staging",
            "designed_nomenclature",
            "naming_governance",
            "narrative_logic",
            "reality_logic",
            "causal_continuity_quality",
            "cost_plausibility",
            "scene_atmosphere",
            "payoff_grounding",
            "imageable_paragraphs",
            "prose_voice",
            "prose_naturalness",
            "natural_sentence_glue",
            "non_checklist_narration",
            "diction_fit",
            "decorative_restraint",
            "dialogue_particle_flow",
            "expression_precision",
            "object_verb_collocation",
            "observation_logic",
            "inference_chain",
            "wording_specificity",
            "literary_relation",
            "relation_legality",
            "lexical_naturalness",
            "sensory_chain",
            "scene_technique_fit",
            "beauty_grounding",
            "native_chinese_flow",
            "dialogue_fullness",
            "character_voice",
            "anti_ai_flavor",
            "chapter_unit_flow",
            "reference_craft",
            "scene_craft",
            "psychological_chain",
            "rhetoric_specificity",
            "diction_vividness",
            "action_reaction_chain",
            "writer_craft",
            "memorable_image",
            "memorable_dialogue",
            "designed_asset",
            "character_action",
            "chapter_necessity",
            "embodied_pov",
            "paragraph_aesthetic",
        } and dimensions[name] < 65:
            warnings.append(f"weak_design_dimension: {name}={dimensions[name]}")
    for issue in readability.issues:
        warnings.append(issue)
    for issue in humanized.issues:
        warnings.append(f"humanized_delivery: {issue}")
    for issue in design.issues:
        warnings.append(f"design_quality: {issue}")
    for issue in prose_voice.issues:
        warnings.append(f"prose_voice: {issue}")
    for issue in prose_naturalness.issues:
        warnings.append(f"prose_naturalness: {issue}")
    for issue in expression_precision.issues:
        warnings.append(f"expression_precision: {issue}")
    for issue in literary_relation.issues:
        warnings.append(f"literary_relation: {issue}")
    for issue in naming.issues:
        warnings.append(f"naming_governance: {issue}")
    for issue in narrative_logic.issues:
        warnings.append(f"narrative_logic: {issue}")
    for issue in anti_ai.issues:
        warnings.append(f"anti_ai_flavor: {issue}")
    for issue in chapter_units.issues:
        warnings.append(f"chapter_unit_flow: {issue}")
    for issue in writer_craft["issues"]:
        warnings.append(f"writer_craft: {issue}")
    for issue in reference_craft.warnings:
        warnings.append(f"reference_craft: {issue}")
    for issue in story_bible_logic.warnings:
        warnings.append(f"story_bible_logic: {issue}")
    for issue in continuity_gate.warnings:
        warnings.append(f"chapter_continuity: {issue}")
    for issue in reality_logic.warnings:
        warnings.append(issue)
    for issue in paragraph_aesthetic.issues:
        warnings.append(f"paragraph_aesthetic: {issue}")
    blocking = [issue for issue in issues if issue.startswith(("forbidden_marker", "setting_contradiction"))]
    # P0-1 · 分层加权求分：strong 满权 · weak 半权 · misaligned 权 0
    score, tomato_breakdown = _weighted_tomato_score(dimensions)
    if count < min_chars:
        score = min(score, dimensions["basic_publishability"])
    if blocking:
        score = min(score, 40)
    if bias.blockers:
        score = min(score, 45)
    if intent.blockers:
        score = min(score, max(45, intent.score))
    if design.issues:
        score = min(score, max(50, design.score))
    score = max(0, min(100, score))
    hard_dimension_ok = all(
        dimensions[name] >= 50
        for name in ("basic_publishability", "production_standard", "setting_risk", "platform_risk", "prose_naturalness")
    )
    hard_dimension_ok = hard_dimension_ok and not bias.blockers
    # ------------------------------------------------------------------
    # Phase 2/3: three-tier quality verdict via classify_quality_verdict.
    # ``passed`` remains a boolean (backwards compatibility): it means
    # "cleared the hard gate", i.e. verdict in {"soft_pass", "pass"}.
    # ------------------------------------------------------------------
    # A 方案（2026-07-23）：hard_issues 上移到 verdict 之前，让 verdict 只被
    # 真正的硬问题（篇幅/世界观矛盾/bias/hook/番茄硬指标）一票否决。学院派软
    # 维度（scene_expansion / embodied_pov / design 铺陈等）不再一票否决 ——
    # 它们已计入 score，交给 65 分门槛裁决。否则口语化短段爽文会被"场景扩写
    # 不足""可视化段落不足"这类与番茄风格相反的维度反复打回。
    hard_issues = [
        issue
        for issue in issues
        if issue.startswith((
            "too_short", "too_long", "forbidden_marker", "setting_contradiction",
            "system_artifact", "story_bible_logic", "chapter_continuity", "reference_craft_underlearned",
            "reality_logic_blocker",
            "bias_blocker", "anchor_missing_protagonist", "hook_missing",
            "fanqie_para_avg_too_long", "fanqie_para_density_too_low", "fanqie_game_word_density_too_high",
            "prose_naturalness_blocker",
            # 一致性校验门硬断层（2026-07-28）：名字漂移/整句重复/状态矛盾一票否决
            "name_drift", "sentence_repeat", "state_contradiction",
            # 注：anchor_missing_world 已降为 soft warn · 章节内可只写门派/镖局而不点名"万象江湖"
        ))
    ]
    verdict = classify_quality_verdict(
        score=score,
        hard_dimension_ok=hard_dimension_ok,
        has_blocking_issues=bool(hard_issues),
    )
    passed = verdict in {"soft_pass", "pass"}
    hard_gate = {
        "status": "PASS" if not hard_issues and hard_dimension_ok else "FAIL",
        "passed": bool(not hard_issues and hard_dimension_ok),
        "dimensions": {
            name: dimensions[name]
            for name in ("basic_publishability", "production_standard", "setting_risk", "platform_risk", "prose_naturalness")
        },
        "issues": hard_issues,
        "threshold": 50,
    }
    report = json.dumps(
        {
            "status": "PASS" if passed else "FAIL",
            "verdict": verdict,
            "score": score,
            "chinese_chars": count,
            "hard_gate": hard_gate,
            "production_contract": production_contract_for_quality(chapter_type=chapter_type),
            "dimensions": dimensions,
            "issues": issues,
            "warnings": warnings,
            "bias_report": bias.to_dict(),
            "intent_acceptance": intent.to_dict(),
            "readability_report": readability.to_dict(),
            "humanized_report": humanized.to_dict(),
            "design_quality_report": design.to_dict(),
            "prose_voice_report": prose_voice.to_dict(),
            "prose_naturalness_report": prose_naturalness.to_dict(),
            "expression_precision_report": expression_precision.to_dict(),
            "literary_relation_report": literary_relation.to_dict(),
            "naming_governance_report": naming.to_dict(),
            "narrative_logic_report": narrative_logic.to_dict(),
            "world_logic_report": world_logic.to_dict(),
            "reality_logic_report": reality_logic.to_dict(),
            "anti_ai_flavor_report": anti_ai.to_dict(),
            "chapter_unit_report": chapter_units.to_dict(),
            "writer_craft_report": writer_craft,
            "reference_craft_report": reference_craft.to_dict(),
            "story_bible_logic_report": story_bible_logic.to_dict(),
            "chapter_continuity_report": continuity_gate.to_dict(),
            "system_artifact_report": system_artifacts.to_dict(),
            "paragraph_aesthetic_report": paragraph_aesthetic.to_dict(),
            "fanqie_hard_metrics": fanqie.to_dict(),
            "thresholds": {
                "pass_score": PASS_FLOOR,
                "soft_pass_floor": HARD_FLOOR,
                "hard_floor": HARD_FLOOR,
                "hard_min_dimension": 50,
                "min_chars": min_chars,
                "max_chars": max_chars,
            },
            "tomato_breakdown": tomato_breakdown,
        },
        ensure_ascii=False,
    )
    return QualityResult(passed=passed, score=score, report=report, dimensions=dimensions, issues=issues)


def split_points(value: str) -> list[str]:
    normalized = value.replace("\r", "\n").replace("\n", ",").replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",")
    return [item.strip() for item in normalized.split(",") if item.strip()]


def coverage_points_for_brief(goal: str, required_beats: str, constraints: str = "", canon_context: str = "") -> list[str]:
    points: list[str] = []
    if goal and not _is_background_or_material_point(goal):
        points.append(goal)
    for line in _brief_coverage_lines(required_beats):
        if _is_background_or_material_point(line):
            continue
        structured = _structured_delivery_points(line)
        if structured:
            points.extend(structured)
            continue
        for item in split_points(line):
            if _is_background_or_material_point(item):
                continue
            item_structured = _structured_delivery_points(item)
            if item_structured:
                points.extend(item_structured)
            else:
                points.append(item)
    for line in _brief_coverage_lines(constraints):
        structured = _structured_delivery_points(line)
        if structured:
            points.extend(structured)
            continue
        for item in split_points(line):
            if _is_background_or_material_point(item):
                continue
            item_structured = _structured_delivery_points(item)
            if item_structured:
                points.extend(item_structured)
            elif _is_constraint_coverage_candidate(item):
                points.append(item)
    authority_text = "\n".join([constraints or "", canon_context or ""])
    return [
        point
        for point in list(dict.fromkeys(points))
        if not line_conflicts_with_authority(point, authority_text)
    ]


def _brief_coverage_lines(text: str) -> list[str]:
    return [line.strip() for line in str(text or "").replace("\r", "\n").splitlines() if line.strip()]


def _is_background_or_material_point(point: str) -> bool:
    text = str(point or "").strip()
    if not text:
        return True
    background_markers = (
        "剧情基线",
        "本轮只解决",
        "可复用素材",
        "核心悬念贯穿全书",
        "每章至少",
        "二本大三学生",
        "为一笔内测奖金",
        "武侠网游",
        "游戏里受的伤",
        "他一边在现实里",
        "每强一分",
        "《列子·汤问》",
        "形为影之质",
        "影为形之用",
        "影身练",
        "真身受",
        "松风十三剑",
        "绵掌",
        "因果和代价",
        "人物行动",
        "对白和后果",
        "段落顺序",
    )
    return any(marker in text for marker in background_markers)


def _structured_delivery_points(point: str) -> list[str]:
    text = str(point or "")
    points: list[str] = []
    if "本章剧情承诺" in text:
        points.extend(["主动选择", "可见代价", "能力回报", "章末变化"])
    if "现实底座" in text or "现实侧处境" in text:
        points.append("第一章现实底座")
    if "世界观入口" in text or "世界入口" in text:
        points.append("第一章世界入口")
    if "核心卖点" in text or "异常体验" in text or "世界奇观" in text:
        points.append("第一章核心卖点")
    if "世界承诺伏笔" in text or "背景伏笔" in text or "设定承诺" in text or "追读问题" in text:
        points.append("第一章世界承诺伏笔")
    if "桥段复刻" in text or "前1500字" in text:
        points.extend(["桥段复刻", "行动尝试", "误判试探反应"])
    if "明确奖励" in text or "能力痕迹" in text or "副作用线索" in text:
        points.extend(["奖励能力痕迹", "身体副作用线索"])
    if "章末钩子" in text and "本次复刻" in text:
        points.append("章末具体钩子")
    return list(dict.fromkeys(points))


def _basic_publishability_score(text_len: int, min_chars: int, max_chars: int, text: str) -> int:
    score = 100
    if text_len < min_chars:
        score -= min(70, (min_chars - text_len) // 10)
    if text_len > max_chars:
        score -= min(50, (text_len - max_chars) // 50)
    score -= 25 * sum(1 for marker in FORBIDDEN_MARKERS if _has_forbidden_marker(text, marker))
    return max(0, min(100, score))


def _coverage_score(text: str, points: list[str]) -> int:
    meaningful = _meaningful_coverage_points(points)
    if not meaningful:
        return 70
    hits = sum(1 for point in meaningful if _point_is_covered(text, point))
    partial_hits = sum(1 for point in meaningful if not _point_is_covered(text, point) and _point_has_partial_coverage(text, point))
    ratio = (hits + partial_hits * 0.5) / len(meaningful)
    return max(35, min(100, round(45 + ratio * 55)))


def _meaningful_coverage_points(points: list[str]) -> list[str]:
    normalized: list[str] = []
    for point in points:
        point = point.strip()
        if len(point) < 2 or _is_diagnostic_point(point):
            continue
        if len(point) > 80:
            tokens = [
                token
                for token in _coverage_tokens(point)
                if 2 <= len(token) <= 12 and not _is_diagnostic_point(token)
            ]
            normalized.extend(tokens[:5])
            continue
        normalized.append(point)
    return list(dict.fromkeys(normalized))[:24]


def _is_diagnostic_point(point: str) -> bool:
    if point in SEMANTIC_COVERAGE_MARKERS:
        return False
    diagnostic_markers = [
        "质检报告 #",
        "提升维度：",
        "weak_narrative_dimension",
        "hook_strength",
        "brief_coverage",
        "reader_momentum",
        "conflict_pressure",
        "choice_and_cost",
        "prose_density",
        "arc_alignment",
        "正文必须",
        "必要节拍",
        "不要只改",
        "开场三百字",
        "增加可见",
        "主角必须",
        "付出清晰代价",
        "章末最后三百字",
        "减少解释",
        "修订必须",
        "修订执行摘要",
        "修订合同",
        "修订模式",
        "定点修订合同",
        "原始机器修订建议",
        "意见理解规则",
        "目标读者体验",
        "必须满足",
        "禁止项",
        "禁止:",
        "验收:",
        "验收清单",
        "修订方向",
        "范围:",
        "系统修订判定",
        "处理强度",
        "置信度",
        "判定理由",
        "保留:",
        "替换:",
        "升级规则",
        "原始意见",
        "system_revision_loop_guard",
        "system_revision_trend_recovery",
        "恢复底稿",
        "废弃劣化稿",
        "换策略修订",
        "不沿坏稿继续",
        "不得继续沿最新劣化稿",
        "当前主角锚点",
        "当前世界/作品锚点",
        "当前能力/卖点锚点",
        "必须遵守最新作品DNA",
        "作品DNA",
        "禁区",
        "少量界面/提示",
        "不要输出导演单",
        "对白和动作必须承接",
        "前五章每章",
        "当前阻断问题",
        "当前优化提醒",
        "局部修复合同",
        "不要继续 fresh",
        "保留当前稿",
        "不要求逐字复刻",
        "结尾要推动",
        "删除系统提示",
        "保留已登记 Canon",
        "不引入",
        "不输出系统元信息",
        "修订后必须",
        "必须响应修订方向",
        "修复质检问题",
        "采纳二审建议",
        "规避风险",
        "词语或短段落",
        "必须按最小范围处理",
        "保留其余正文",
        "保留当前最佳稿已验证",
        "除非它违反最新骨架",
        "下一版必须能被修订方向逐条验收",
        "质检术语",
        "通用章节生产标准",
        "正文字数",
        "章节阶段",
        "开篇牵引",
        "开篇反雷同",
        "主角行动链",
        "人物反应链",
        "拟人化小单元",
        "场景推进",
        "信息释放",
        "爽点/期待",
        "每个约",
        "后一单元",
        "每2个单元",
        "设定只能",
        "至少完成",
        "本章只能",
        "必须凭判断",
        "真实存在的武侠世界",
        "成长不靠",
        "套路触发器",
        "少量游戏界面",
        "必须保持",
        "补足本章核心承诺",
        "让读者能",
        "人物目标",
        "场景阻碍",
        "局面变化",
        "具体处境",
        "人物欲望",
        "关系张力",
        "异常细节",
        "利益交换",
        "行动后果",
        "阅读牵引",
        "reading_assessment_auto_quality",
        "当前阅读层级",
        "源版本锁定",
        "第1章硬性交付",
        "不得以",
        "醒来",
        "睁眼",
        "摸手机",
        "宿舍回忆",
        "系统菜单",
        "环境确认",
        "利益冲突",
        "逼近风险",
        "个单元",
        "单元需局部重修",
        "目标不清",
        "动作链弱",
        "阻碍不足",
        "后果没落地",
        "信息增量弱",
        "人物反应弱",
        "保留本单元有效信息",
        "补清目标",
        "动作后果",
        "承接点",
        "当前片段",
        "单元验收",
        "局部修订闭环",
        "imageable_paragraphs",
        "抽象设定句",
        "关键段落",
        "画面中心",
        "goal",
        "action",
        "obstacle",
        "consequence",
        "info_gain",
        "reaction",
        "handoff",
        # 2026-07-26 · 第6批根治：reading_assessment 重建型 brief 的审核指令元数据。
        # 这些是"怎么写"的生产指令，不是"写什么"的剧情要点，混入 coverage_points
        # 会让 brief_coverage 拿正文去匹配指令句→永远命不中→系统性拉低。过滤它们
        # 是修数据污染非放水：真剧情要点（剧情基线里的人物/事件）仍参与覆盖考核。
        "阅读评估重建",
        "以当前作品剧情承诺为准",
        "只保留可用素材",
        "失败结构不得沿用",
        "必须替换失败开场",
        "问路铺垫",
        "失败场景链",
        "Canon 中仍有效",
        "本章剧情承诺",
        "硬性交付",
        "第一句必须",
        "现场盘问",
        "交易催促",
        "冲突后果或人物动作开场",
        "前700字",
        "前1500字",
        "中段完成一次行动尝试",
        "因主角演法产生误判",
        "试探或反应",
        "明确奖励或能力痕迹",
        "副作用线索",
        "章末钩子必须",
        "泛泛麻烦",
        "任务刚触发收尾",
        "可复用素材",
        "本轮只解决",
        "补足可画面化段落",
        "让读者看见场景",
        "把氛围从概括词",
        "让读者能",
    ]
    stripped = point.strip()
    if stripped.startswith(("不要", "不能", "禁止", "不得", "避免", "只修改", "只修复", "只改", "未被点名", "开场", "修订必须", "共 ")):
        return True
    return any(marker in point for marker in diagnostic_markers)


def _is_constraint_coverage_candidate(point: str) -> bool:
    stripped = point.strip()
    if len(stripped) < 4 or len(stripped) > 60 or _is_diagnostic_point(stripped):
        return False
    if "\n" in stripped or "\r" in stripped:
        return False
    if any(marker in stripped for marker in ("禁止", "修订说明", "质检术语", "系统信息", "主编验收", "读感目标", "因果链和章末事实")):
        return False
    if stripped.startswith(("-", "【", "当前", "通用", "正文字数", "章节阶段")):
        return False
    positive_markers = ("完成", "出现", "发现", "选择", "代价", "后果", "目标", "阻碍", "行动", "章末", "钩子", "承接", "回报")
    return any(marker in stripped for marker in positive_markers)


SEMANTIC_COVERAGE_MARKERS = {
    "主动选择": (
        "决定", "选择", "赌", "咬牙", "编", "不接话", "只能", "得先",
        "扣紧", "留下", "不走", "跟我来", "问", "试", "跪下", "接过", "迈出",
    ),
    "可见代价": (
        "代价", "精气", "抽取", "副作用", "发麻", "麻", "五百", "警告", "脱力",
        "磨破", "破皮", "流血", "疼", "痛", "欠", "罚", "丢", "记住脸", "受伤",
    ),
    "能力回报": (
        "松风剑法", "第一式", "练剑", "留下", "同步率", "奖励", "通过",
        "收留", "杂役", "竹牌", "站桩", "短打", "设卡", "给条活路", "活路可以给",
    ),
    "章末变化": (
        "卯时", "子时", "下一次", "同步率", "抽取精气", "明天", "留下",
        "线索", "信物", "玉佩", "信", "钥匙", "裂缝", "异常", "追查", "继续", "决定",
        "下次登录", "重启", "追踪", "坐标", "锁定", "提示", "血红小字",
        "身份锚定", "倒计时", "强制清除", "驻留痕迹", "青痕", "银纹", "走字",
    ),
    "第一章现实底座": ("现实", "出租屋", "头盔", "脑机", "神经直连", "房租", "身份", "设备", "内测"),
    "第一章世界入口": ("入梦", "游戏", "江湖", "清虚观", "门派", "异世界", "登录", "头盔", "神经直连"),
    "第一章核心卖点": ("同步", "影身", "真身", "武功", "功法", "异常", "反馈", "变强", "世界", "奇观"),
    "第一章世界承诺伏笔": ("异常", "规则", "反馈", "不对", "没有消失", "下一次", "同步", "提示", "痕迹"),
    "桥段复刻": ("松风剑法", "第一式", "剑", "招式", "复刻"),
    "行动尝试": ("试", "刺", "抖", "歪", "画", "握", "抬手"),
    "误判试探反应": ("你以前练过", "天才", "沉默", "试探", "留下", "要么", "三息"),
    "奖励能力痕迹": ("留下", "通过", "练剑", "松风剑法", "同步率", "奖励"),
    "身体副作用线索": ("现实", "醒", "指腹", "食指", "麻", "抽取", "精气", "副作用"),
    "章末具体钩子": ("同步率", "下一次", "子时", "抽取精气", "通知", "提示", "麻"),
}


def _point_is_covered(text: str, point: str) -> bool:
    if point in text:
        return True
    semantic = SEMANTIC_COVERAGE_MARKERS.get(point)
    if semantic:
        scope = _semantic_scope(text, point)
        hits = sum(1 for marker in semantic if marker in scope)
        return hits >= max(1, min(3, len(semantic) // 3))
    tokens = _coverage_tokens(point)
    if not tokens:
        return False
    hits = sum(1 for token in tokens if token in text)
    return hits >= max(1, min(3, len(tokens)))


def _point_has_partial_coverage(text: str, point: str) -> bool:
    semantic = SEMANTIC_COVERAGE_MARKERS.get(point)
    if semantic:
        scope = _semantic_scope(text, point)
        return any(marker in scope for marker in semantic)
    return any(token in text for token in _coverage_tokens(point))


def _semantic_scope(text: str, point: str) -> str:
    value = str(text or "")
    if point in {"第一章现实底座", "第一章世界入口", "第一章核心卖点", "第一章世界承诺伏笔"}:
        return value[:1200] if point != "第一章世界承诺伏笔" else value[-900:]
    if point in {"身体副作用线索", "章末具体钩子", "章末变化"}:
        return value[-700:]
    return value


def _coverage_tokens(point: str) -> list[str]:
    raw_tokens: list[str] = []
    for part in split_points(point):
        raw_tokens.extend(re.split(r"(?:或|和|与|必须|成为|推动|引出|自然|推向|修复|保留|补清|[\\/：:（）()《》“”\"'，。！？、\\s])+", part))
    tokens: list[str] = []
    for token in raw_tokens:
        token = token.strip(" ：:#0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_")
        for suffix in ("压力", "桥段", "恐惧", "恩怨", "关系", "后果", "承接点", "不足", "不清", "没落地"):
            if token.endswith(suffix) and len(token) > len(suffix) + 1:
                token = token[: -len(suffix)]
                break
        if len(token) >= 2 and not _is_diagnostic_point(token):
            tokens.append(token)
    return tokens


def _canon_score(text: str, canon_context: str) -> int:
    """Canon consistency 评分。

    算法（2026-08-18 B 方案修订）：
    1. 仍按 character 名字命中算基础分（55 + 命中/总数*45）· 反映人物在正文出现度
    2. 加 active CanonAuthorityProfile 的 must_keep / opening_contract 关键词命中 bonus
       - 命中 must_keep 中的人物名/核心元素 → +5
       - 命中 opening_contract 中至少 2 条 → +10
    3. allowed_but_limited 是允许但需克制的设定（头盔/数据化提示）· 命中 +3 bonus
    4. forbidden_misread 命中 → -20（VR/虚拟接入/赛博/主动进游戏等）
    """
    if not canon_context or "未登记 Canon" in canon_context:
        return 60
    names = []
    for line in canon_context.splitlines():
        if line.startswith("- character#"):
            parts = line.split()
            if len(parts) >= 3:
                names.append(parts[2].split("｜")[0])
    if not names:
        return 75
    hits = sum(1 for name in names if name and name in text)
    base = 55 + (hits / len(names)) * 45
    score = base
    # 抽 must_keep / opening_contract 关键词，命中 bonus（2026-08-18）
    must_keep_keys: list[str] = []
    opening_contract_keys: list[str] = []
    allowed_but_limited_keys: list[str] = []
    in_must_keep = False
    in_opening = False
    in_allowed = False
    for line in canon_context.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # 段标记 (命中起始行, 例如 "must_keep:")
        if stripped.startswith("must_keep") and (":" in stripped or "：" in stripped):
            in_must_keep = True
            in_opening = False
            in_allowed = False
            continue
        if stripped.startswith("opening_contract") and (":" in stripped or "：" in stripped):
            in_opening = True
            in_must_keep = False
            in_allowed = False
            continue
        if stripped.startswith("allowed_but_limited") and (":" in stripped or "：" in stripped):
            in_allowed = True
            in_must_keep = False
            in_opening = False
            continue
        if (stripped.startswith("forbidden_misread") or stripped.startswith("deprecated_pollution")) and (":" in stripped or "：" in stripped):
            in_must_keep = False
            in_opening = False
            in_allowed = False
            continue
        if in_must_keep and (stripped.startswith("-") or stripped.startswith("·")):
            cleaned = stripped.lstrip("-").lstrip("·").strip()
            for kw in _extract_canon_keywords(cleaned):
                must_keep_keys.append(kw)
        elif in_opening and (stripped.startswith("-") or stripped.startswith("·")):
            cleaned = stripped.lstrip("-").lstrip("·").strip()
            for kw in _extract_canon_keywords(cleaned):
                opening_contract_keys.append(kw)
        elif in_allowed and (stripped.startswith("-") or stripped.startswith("·")):
            cleaned = stripped.lstrip("-").lstrip("·").strip()
            for kw in _extract_canon_keywords(cleaned):
                allowed_but_limited_keys.append(kw)
    mk_hits = sum(1 for k in must_keep_keys if k and k in text)
    if must_keep_keys and mk_hits >= max(1, len(must_keep_keys) // 2):
        score += 5
    op_hits = sum(1 for k in opening_contract_keys if k and k in text)
    if opening_contract_keys and op_hits >= 2:
        score += 10
    ab_hits = sum(1 for k in allowed_but_limited_keys if k and k in text)
    if allowed_but_limited_keys and ab_hits >= 1:
        score += 3
    return max(45, min(100, round(score)))


def _extract_canon_keywords(text: str) -> list[str]:
    """从 must_keep / opening_contract 文本中抽关键词作为命中证据。

    抽取策略（2026-08-18 修订）：
    1. 书名号《》内容 → 必抽
    2. 双引号 / 中文引号内容 → 抽
    3. 顿号/分号/竖线/中英点等分隔的短语 → 全部抽
    4. 中文连续 2-4 字名词（无标点也抽）→ 兜底（核心词如"头盔"/"数据异常"/"物理坠入"）
    """
    if not text:
        return []
    import re as _re
    candidates: list[str] = []
    # 书名号
    for m in _re.finditer(r"《([^》]+)》", text):
        if 2 <= len(m.group(1)) <= 12:
            candidates.append(m.group(1))
    # 引号
    for m in _re.finditer(r"[\"“]([^\"”]+)[\"”]", text):
        if 2 <= len(m.group(1)) <= 12:
            candidates.append(m.group(1))
    # 顿号等分隔
    for sep in ("、", ";", "；", "|", "｜", "·", "／", "/", "／"):
        if sep in text:
            for piece in text.split(sep):
                piece = piece.strip()
                for prefix in ("主角：", "主角:", "机制：", "机制:", "设定：", "设定:", "参考作品名", "包括", "如", "例如"):
                    if piece.startswith(prefix):
                        piece = piece[len(prefix):].strip()
                # 去尾部杂质
                for suffix in ("不得作为正文世界名", "刷屏污染正文", "提前下场", "外溢", "点到为止，不能替代人物行动", "作为长期设定但需克制"):
                    if piece.endswith(suffix):
                        piece = piece[: -len(suffix)].strip()
                if 2 <= len(piece) <= 10 and _re.search(r"[\u4e00-\u9fff]", piece):
                    candidates.append(piece)
    # 兜底: 抽中文连续 2-4 字短语, 过滤通用词
    if not any(_re.match(r"[\u4e00-\u9fff]{2,4}$", c) for c in candidates):
        for m in _re.finditer(r"[\u4e00-\u9fff]{2,5}", text):
            piece = m.group(0)
            # 过滤停用词
            if piece in (
                "当前", "设定", "主角", "包括", "机制", "参考", "作品", "不主", "但是",
                "需要", "克制", "现实", "长期", "面板", "口吻", "提升", "修真", "高阶",
                "意识", "凡人", "无血", "血统", "血统/", "血统/无", "血统/无大能转世",
            ):
                continue
            if 2 <= len(piece) <= 5:
                candidates.append(piece)
            if len([c for c in candidates if 2 <= len(c) <= 5]) >= 6:
                break
    # 去重保留前 8
    seen: set[str] = set()
    out: list[str] = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
            if len(out) >= 8:
                break
    return out


def _marker_score(text: str, markers: list[str]) -> int:
    hits = sum(1 for marker in markers if marker in text)
    return max(45, min(100, 50 + hits * 8))


def _hook_score(text: str) -> int:
    tail = text[-300:] if len(text) > 300 else text
    full_hits = sum(1 for marker in HOOK_MARKERS if marker in text)
    tail_hits = sum(1 for marker in HOOK_MARKERS if marker in tail)
    question_bonus = 12 if any(marker in tail for marker in ("？", "?", "为什么", "是谁", "怎么会", "如果")) else 0
    cliffhanger_bonus = 10 if any(marker in tail for marker in ("响了", "亮起", "推开", "出现", "盯上", "别再", "来不及")) else 0
    pressure_hits = sum(1 for marker in HOOK_PRESSURE_MARKERS if marker in tail)
    concrete_hits = sum(1 for marker in HOOK_CONCRETE_MARKERS if marker in tail)
    pressure_bonus = min(24, pressure_hits * 8)
    concrete_bonus = min(10, concrete_hits * 5)
    return max(
        40,
        min(
            100,
            45
            + full_hits * 5
            + tail_hits * 8
            + question_bonus
            + cliffhanger_bonus
            + pressure_bonus
            + concrete_bonus,
        ),
    )


def _prose_density_score(text: str) -> int:
    paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
    if not paragraphs:
        return 0
    unique_ratio = len(set(paragraphs)) / len(paragraphs)
    average_len = chinese_chars(text) / len(paragraphs)
    score = 55
    score += min(25, int(unique_ratio * 25))
    if average_len >= 80:
        score += 15
    elif average_len < 30:
        score -= 15
    score -= 12 * sum(1 for marker in FILLER_MARKERS if marker in text)
    return max(0, min(100, score))


def _arc_alignment_score(text: str, *, goal: str, required_beats: str, constraints: str, canon_context: str = "") -> int:
    arc_points = [
        point
        for point in [goal, *split_points(required_beats), *split_points(constraints)]
        if any(marker in point for marker in ("剧情段", "阶段", "目标", "高潮", "转折", "边界", "Story Bible", "Canon"))
        and not _is_background_or_material_point(point)
    ]
    if not arc_points:
        return max(70, min(85, _coverage_score(text, coverage_points_for_brief(goal, required_beats, constraints, canon_context))))
    hits = sum(1 for point in arc_points if point in text)
    partial_hits = sum(1 for point in arc_points if point not in text and any(token in text for token in split_points(point)))
    ratio = (hits + partial_hits * 0.5) / len(arc_points)
    literal_score = max(45, min(100, round(50 + ratio * 50)))
    contract_score = _coverage_score(text, coverage_points_for_brief(goal, required_beats, constraints, canon_context))
    if contract_score >= 85:
        return max(literal_score, 75)
    return literal_score


def _production_standard_score(text: str, *, min_chars: int) -> int:
    count = chinese_chars(text)
    paragraphs = [item.strip() for item in text.splitlines() if item.strip()]
    opening = text[:350]
    tail = text[-350:] if len(text) > 350 else text
    score = 35
    if count >= min_chars:
        score += 20
    elif count >= int(min_chars * 0.85):
        score += 10
    if len(paragraphs) >= 12:
        score += 10
    elif len(paragraphs) >= 7:
        score += 6
    if any(marker in opening for marker in CONFLICT_MARKERS + ["门", "手机", "屏幕", "声音", "脚步", "订单", "短信", "血", "雨", "灯"]):
        score += 10
    if sum(1 for marker in CHOICE_COST_MARKERS if marker in text) >= 3:
        score += 10
    if any(marker in text for marker in ("他冲", "她冲", "伸手", "咬牙", "抬手", "转身", "抓起", "推开", "按下", "挡住")):
        score += 5
    if sum(1 for marker in ("对话", "问", "喊", "低声", "咬牙", "笑", "沉默", "盯着")) and "“" in text:
        score += 5
    if any(marker in tail for marker in HOOK_MARKERS + ["倒计时", "通知", "裂开", "响起", "推门", "盯上"]):
        score += 5
    return max(0, min(100, score))


def _setting_risk_score(text: str) -> int:
    penalties = sum(1 for marker in BLOCKING_CONTRADICTIONS if _has_blocking_contradiction(text, marker))
    return max(0, 100 - penalties * 35)


def _has_blocking_contradiction(text: str, marker: str) -> bool:
    if marker not in text:
        return False
    # 否定/禁止式前缀：设定约束句（"不得无代价"），marker 被否定，不算矛盾
    allowed_prefixes = ("不得", "不能", "不可", "禁止", "避免", "拒绝", "不许", "别让", "别把", "不要")
    # 反问式前缀：语义上强调"有代价/有限制"，与 marker 字面相反，须豁免
    # （如"怎么可能没有代价""哪有无限使用""不可能永久无敌"）
    rhetorical_prefixes = ("怎么可能", "怎会", "哪有", "哪能", "岂能", "岂会", "不可能", "怎能", "焉能")
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return False
        prefix = text[max(0, index - 8):index]
        if any(prefix.endswith(item) for item in allowed_prefixes):
            start = index + len(marker)
            continue
        # 反问式：前缀窗口内出现反问引导词即豁免（反问句读点可能隔字）
        if any(rp in prefix for rp in rhetorical_prefixes):
            start = index + len(marker)
            continue
        return True


def _has_forbidden_marker(text: str, marker: str, allow_system_panel: bool = False) -> bool:
    if marker not in text:
        return False
    if marker != "系统提示":
        return True
    if not _has_meta_system_prompt_leak(text, allow_system_panel=allow_system_panel):
        return False
    allowed_prefixes = ("没有", "无", "不是", "不再", "不会", "别写", "不要", "禁止", "避免", "不得", "不能", "不可", "不许")
    start = 0
    while True:
        index = text.find(marker, start)
        if index == -1:
            return False
        prefix = text[max(0, index - 10):index]
        if not any(prefix.endswith(item) for item in allowed_prefixes):
            return True
        start = index + len(marker)


def _world_anchor_satisfied(text: str, world_titles: list[str]) -> bool:
    content = text or ""
    if any(name and name in content for name in world_titles):
        return True
    entry_markers = (
        "头盔",
        "全感",
        "脑机",
        "神经直连",
        "入境",
        "登录",
        "游戏",
        "面罩",
        "悬浮广告",
        "广告屏",
    )
    world_texture_markers = (
        "蜀山",
        "剑光",
        "云海",
        "群峰",
        "镇",
        "村",
        "武馆",
        "拳谱",
        "镖",
        "药铺",
        "山门",
        "石碑",
        "铜牌",
        "木桩",
    )
    return any(marker in content for marker in entry_markers) and any(
        marker in content for marker in world_texture_markers
    )


def _has_meta_system_prompt_leak(text: str, allow_system_panel: bool = False) -> bool:
    meta_patterns = (
        "系统提示词",
        "系统提示语",
        "系统提示、作者说明",
        "系统提示或作者说明",
        "系统提示进入正文",
        "输出系统提示",
        "不要输出系统提示",
        "禁止系统提示",
        "避免系统提示",
    )
    if any(pattern in text for pattern in meta_patterns):
        return True
    if allow_system_panel:
        # 抽奖系统爽文允许叙述性系统面板："系统提示：xxx"
        return False
    for line in (text or "").splitlines():
        stripped = line.strip(" \t-")
        if stripped.startswith(("系统提示:", "系统提示：")):
            return True
    return False


def _platform_risk_score(text: str) -> int:
    penalties = sum(1 for marker in FORBIDDEN_MARKERS if _has_forbidden_marker(text, marker))
    meta_markers = ["JSON", "数据库", "发布任务链路"]
    penalties += sum(1 for marker in meta_markers if marker in text)
    return max(0, 100 - penalties * 15)


def _opening_variety_score(text: str) -> int:
    opening = (text or "")[:700]
    score = 55
    strategy_hits = 0
    strategies = [
        ("异常细节", ("异样", "声音", "脚步", "血", "灯", "门", "痕迹", "规矩")),
        ("人物欲望", ("想要", "必须拿到", "不甘心", "等不起", "要去")),
        ("关系张力", ("看着", "盯着", "沉默", "误会", "旧账", "师", "掌柜")),
        ("利益交换", ("交易", "人情", "欠", "账", "银", "换", "价")),
        ("行动后果", ("昨夜", "伤", "疼", "追", "后果", "醒来", "没来得及")),
        ("悬念误导", ("以为", "原来", "不是", "却", "直到")),
    ]
    for _name, markers in strategies:
        if any(marker in opening for marker in markers):
            strategy_hits += 1
    score += min(30, strategy_hits * 10)
    if any(marker in opening for marker in ("世界观", "设定", "说明", "简单来说", "众所周知")):
        score -= 25
    if chinese_chars(opening) < 180:
        score -= 10
    return max(0, min(100, score))


def _causal_scene_chain_score(text: str) -> int:
    markers = ("于是", "因此", "所以", "刚", "还没", "话音未落", "下一刻", "却", "但", "因为", "只见", "逼得", "换来", "导致")
    summary_markers = ("几天后", "很快过去", "总之", "一番", "随后众人", "接下来")
    paragraphs = [item.strip() for item in (text or "").splitlines() if item.strip()]
    score = 45 + min(35, sum(1 for marker in markers if marker in text) * 5)
    if len(paragraphs) >= 10:
        score += 10
    score -= min(30, sum(text.count(marker) for marker in summary_markers) * 8)
    return max(0, min(100, score))


def _reaction_chain_score(text: str) -> int:
    stages = [
        ("感知", ("看见", "听见", "摸到", "闻到", "眼神", "声音", "脚步", "疼")),
        ("普通解释", ("以为", "只当", "原本", "按理", "大概", "可能")),
        ("证据推翻", ("却", "不是", "不对", "直到", "证据", "痕迹", "偏偏")),
        ("试探", ("试探", "开口", "伸手", "观察", "判断", "问", "交涉", "表演")),
        ("修正行动", ("决定", "转身", "改口", "收回", "换了", "咬牙", "主动", "选择")),
    ]
    hits = sum(1 for _name, markers in stages if any(marker in text for marker in markers))
    return max(30, min(100, 30 + hits * 14))


def _earned_payoff_score(text: str) -> int:
    body = text or ""
    tail = body[-500:]
    action_hits = sum(1 for marker in ("试探", "选择", "决定", "出手", "观察", "交涉", "交易", "修炼", "冒险", "判断") if marker in body)
    cost_hits = sum(1 for marker in ("代价", "后果", "反噬", "伤", "欠", "损耗", "失去", "暴露", "误会") if marker in body)
    tail_causal = any(marker in tail for marker in ("所以", "因此", "换来", "导致", "这才", "原来", "下一", "门外", "消息", "机会", "危险"))
    score = 35 + min(25, action_hits * 5) + min(25, cost_hits * 5)
    if tail_causal:
        score += 15
    if any(marker in tail for marker in ("突然出现一个危险", "莫名其妙", "毫无征兆")):
        score -= 20
    return max(0, min(100, score))
