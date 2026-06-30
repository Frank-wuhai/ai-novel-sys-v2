from __future__ import annotations

import hashlib
import json
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, FeedbackAdjustment, GenerationTask, QualityReport
from app.services.bias import apply_model_drift_local_patch, evaluate_generation_bias
from app.services.brief_sanitizer import sanitize_chapter_brief_fields, sanitize_prompt_contract_text
from app.services.chapter_standards import ensure_chapter_production_standard
from app.services.chapter_unit_plans import align_chapter_unit_plan
from app.services.feedback import REVISION_MODE_FRESH, REVISION_MODE_LOCAL_PATCH, build_rewrite_contract
from app.services.production_llm import (
    expand_short_draft_output,
    llm_parameter_snapshot,
    llm_usage_payload,
    parse_or_repair_json_object,
    parse_or_repair_draft_output,
    record_generation_llm_log,
    repair_humanized_unit_flow,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.production_gate import assert_production_gate
from app.services.production_optimization import apply_skeleton_preflight_to_brief
from app.services.production_run_review import record_production_run_review
from app.services.production_state import brief_has_revision_artifacts, latest_foundation, latest_story_brief, next_version_number
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.services.quality import chinese_chars
from app.services.paragraph_aesthetic import format_paragraph_aesthetic_contract
from app.services.revision_success_boost import apply_revision_success_boost
from app.services.revision_contract_manager import normalize_active_revision_contract, prepare_new_revision_contract
from app.services.writer_loop import build_writer_loop_plan, local_revision_brief_lines


def create_revision_brief(session: Session, *, book_id: int, chapter_number: int) -> ChapterBrief:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not version:
        raise ValueError("chapter version not found")
    if version.status != "needs_revision":
        raise ValueError("revision brief requires latest chapter version to be needs_revision")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == version.id).order_by(QualityReport.id.desc())
    )
    if not quality:
        raise ValueError("quality report is required before revision brief")
    prepare_new_revision_contract(session, chapter_id=chapter.id)
    try:
        quality_data = json.loads(quality.report)
    except json.JSONDecodeError:
        quality_data = {"raw_report": quality.report}
    dimensions = quality_data.get("dimensions", {}) if isinstance(quality_data, dict) else {}
    issues = quality_data.get("issues", []) if isinstance(quality_data, dict) else []
    llm_review = quality_data.get("llm_review", {}) if isinstance(quality_data, dict) else {}
    base_brief = _latest_story_brief_for_revision(session, chapter=chapter)
    goal = _revision_story_goal(chapter_number=chapter_number, base_brief=base_brief)
    failure_class = quality_data.get("production_failure_classification") if isinstance(quality_data.get("production_failure_classification"), dict) else {}
    if failure_class.get("category") == "structure_rewrite":
        required = _structural_rewrite_required_beats(
            chapter_number=chapter_number,
            base_brief=base_brief,
            quality_data=quality_data,
            failure_class=failure_class,
        )
    else:
        weak_dimensions = [name for name, score in dimensions.items() if isinstance(score, int) and score < 70]
        required = "；".join(
            [
                _revision_story_intent(chapter_number=chapter_number, base_brief=base_brief),
                *_revision_dimension_beats(weak_dimensions),
                *_revision_issue_beats(issues),
                *_chapter_unit_beats(quality_data),
                *_llm_review_diagnostic_beats(llm_review),
                *_editor_in_chief_beats(quality_data),
                *_paragraph_aesthetic_beats(quality_data),
                *local_revision_brief_lines(quality_data, chapter_number=chapter_number),
            ]
        )
    feedback_requirements = _latest_feedback_requirements(session, book_id=book_id, chapter_number=chapter_number)
    if feedback_requirements:
        required = "；".join([item for item in [required, *feedback_requirements] if item])
    if not required:
        required = "根据质量报告补足章节完整度、连续性和平台可发布性。"
    constraints = ensure_chapter_production_standard(
        _revision_story_constraints(base_brief=base_brief),
        chapter_number=chapter_number,
    )
    if failure_class.get("category") != "structure_rewrite":
        writer_loop = build_writer_loop_plan(
            chapter_number=chapter_number,
            goal=goal,
            required_beats=required,
            constraints=constraints,
            quality_report=quality_data,
            previous_content=version.content or "",
            mode="revision_brief",
        )
        required = "；".join([required, *writer_loop.rewrite_directives, *writer_loop.acceptance_checks])
    else:
        constraints = "；".join(
            [
                constraints,
                "revision_mode:rewrite",
                "结构性失败必须整章重构：按 6-8 个连续小单元重写，禁止继续局部补丁。",
                "正文必须控制在3000-4500中文字符；超过4500直接视为失败。",
            ]
        )
    goal, required, constraints = sanitize_chapter_brief_fields(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        goal=goal,
        required_beats=required,
        constraints=constraints,
    )
    brief = ChapterBrief(chapter_id=chapter.id, goal=goal, required_beats=required, constraints=constraints, status="revision_ready")
    session.add(brief)
    session.flush()
    normalize_active_revision_contract(session, chapter_id=chapter.id, quality=quality)
    return brief


def revise_chapter(session: Session, *, book_id: int, chapter_number: int, dry_run: bool = True) -> ChapterVersion:
    assert_production_gate(session, book_id=book_id, action="revise_chapter")
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    source_version = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
    if not source_version:
        raise ValueError("chapter version not found")
    if source_version.status != "needs_revision":
        raise ValueError("latest chapter version must be needs_revision before revise")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == source_version.id).order_by(QualityReport.id.desc())
    )
    revision_brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not revision_brief:
        raise ValueError("revision brief is required before revise")
    if not quality:
        quality = _fallback_quality_for_recovery_revision(
            session,
            source_version=source_version,
            revision_brief=revision_brief,
        )
    if not quality and not _brief_has_feedback_marker(revision_brief) and not _brief_has_actionable_revision_plan(revision_brief):
        raise ValueError("quality report is required before revise")
    boost = apply_revision_success_boost(session, book_id=book_id, chapter_number=chapter_number)
    if boost.applied:
        revision_brief = session.get(ChapterBrief, revision_brief.id) or revision_brief
    apply_skeleton_preflight_to_brief(session, book_id=book_id, chapter_number=chapter_number, brief=revision_brief)
    foundation = latest_foundation(session, book_id)
    if not foundation:
        raise ValueError("story foundation is required before revising")
    seed_prompt_templates(session)
    fresh_rewrite = _revision_is_fresh_rewrite(revision_brief)
    rewrite_mode = fresh_rewrite or _revision_requires_rewrite(revision_brief)
    revision_required_beats = _revision_required_beats(revision_brief, rewrite_mode=rewrite_mode, fresh_rewrite=fresh_rewrite)
    revision_prompt_goal = sanitize_prompt_contract_text(revision_brief.goal) or revision_brief.goal
    revision_context_mode = (
        "fresh"
        if fresh_rewrite
        else ("rewrite" if rewrite_mode else ("local_patch" if _revision_is_local_patch(revision_brief) else "targeted"))
    )
    packet = build_chapter_production_packet(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=revision_brief.goal,
        required_beats=revision_brief.required_beats,
        constraints=revision_brief.constraints,
        mode="fresh" if fresh_rewrite else "revision",
        revision_goal=revision_prompt_goal,
        revision_required_beats=revision_required_beats,
        revision_constraints=revision_brief.constraints,
        quality_report=quality.report if quality else None,
        previous_content=source_version.content,
        revision_context_mode=revision_context_mode,
        fresh_rewrite=fresh_rewrite,
        rewrite_mode=rewrite_mode,
        chapter_id=chapter.id,
        chapter_brief_id=revision_brief.id,
    )
    local_patch_version = _try_local_patch_revision(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        canon_context=packet.context.canon_context,
        dry_run=dry_run,
    )
    if local_patch_version:
        return local_patch_version
    template = get_prompt_template(session, name="revise_chapter", version="v4" if rewrite_mode else "v3")
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        previous_content=packet.context.previous_content,
        quality_report=packet.context.quality_report,
        revision_goal=packet.blueprint.goal,
        revision_required_beats=packet.blueprint.required_beats,
        revision_constraints=packet.blueprint.constraints,
        **packet.prompt_values,
        premise=foundation.premise,
        reader_promise=foundation.reader_promise,
    )
    provider = get_provider(dry_run)
    model = settings.llm_revision_model
    temperature = settings.llm_revision_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
    )
    response = provider.generate(
        prompt,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    try:
        draft = parse_or_repair_draft_output(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            max_tokens=settings.llm_revision_max_tokens,
            temperature=temperature,
            model=model,
            task_label="章节修订",
        )
    except StructuredOutputError as exc:
        task = GenerationTask(
            book_id=book_id,
            task_type="revise_chapter",
            status="failed",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "dry_run": dry_run,
                    "prompt_template": f"{template.name}@{template.version}",
                    "llm_parameters": llm_parameters,
                    "source_version_id": source_version.id,
                    "quality_report_id": quality.id if quality else None,
                    "revision_brief_id": revision_brief.id,
                    "rewrite_mode": rewrite_mode,
                    "fresh_rewrite": fresh_rewrite,
                    **packet.task_payload,
                },
                ensure_ascii=False,
            ),
            output_json=json.dumps(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "llm_parameters": llm_parameters,
                    "error": str(exc),
                    "raw": response.text[:2000],
                    **llm_usage_payload(response, prompt=prompt),
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="structured_output",
        )
        raise
    min_chars = packet.blueprint.target_min_chars
    draft, length_repair = expand_short_draft_output(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节修订",
    )
    draft, unit_flow_repair = repair_humanized_unit_flow(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=settings.llm_revision_max_tokens,
        temperature=temperature,
        model=model,
        task_label="章节修订",
    )
    unit_report = (unit_flow_repair.get("after") if unit_flow_repair.get("accepted") else None) or unit_flow_repair.get("before")
    unit_plan_alignment = align_chapter_unit_plan(packet.chapter_unit_plan, unit_report)
    if dry_run and _same_revision_content(source_version.content, draft.content):
        draft.content = "\n\n".join(
            [
                draft.content,
                "【dry-run修订验证段】主角重新审视刚才的选择，意识到章末线索已经把下一步压力推到眼前；他必须主动承担代价，而不是等待局面自行解决。",
            ]
        )
        draft.self_check = [*draft.self_check, "dry-run detected duplicate output and appended a deterministic revision delta."]
    if _same_revision_content(source_version.content, draft.content):
        task = GenerationTask(
            book_id=book_id,
            task_type="revise_chapter",
            status="failed",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "dry_run": dry_run,
                    "prompt_template": f"{template.name}@{template.version}",
                    "llm_parameters": llm_parameters,
                    "source_version_id": source_version.id,
                    "quality_report_id": quality.id if quality else None,
                    "revision_brief_id": revision_brief.id,
                    "rewrite_mode": rewrite_mode,
                    "fresh_rewrite": fresh_rewrite,
                    "min_chars": min_chars,
                    "max_chars": packet.blueprint.target_max_chars,
                    **packet.task_payload,
                },
                ensure_ascii=False,
            ),
            output_json=json.dumps(
                {
                    "provider": response.provider,
                    "model": response.model,
                    "llm_parameters": llm_parameters,
                    "error_category": "duplicate_revision_output",
                    "error": "revision output is identical to source version",
                    "self_check": draft.self_check,
                    **llm_usage_payload(response, prompt=prompt),
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template=f"{template.name}@{template.version}",
            prompt=prompt,
            status="failed",
            error_category="duplicate_revision_output",
        )
        raise ValueError("revision output is identical to source version; blocked duplicate version creation")
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="draft",
        source=f"revision:{response.provider}",
    )
    session.add(version)
    session.flush()
    output_data = {
        "version_id": version.id,
        "provider": response.provider,
        "model": response.model,
        "llm_parameters": llm_parameters,
        **llm_usage_payload(response, prompt=prompt),
        "self_check": draft.self_check,
        "used_brief_points": draft.used_brief_points,
        "length_repair": length_repair,
        "unit_flow_repair": unit_flow_repair,
        "unit_plan_alignment": unit_plan_alignment,
    }
    task = GenerationTask(
        book_id=book_id,
        task_type="revise_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
                "source_version_id": source_version.id,
                "quality_report_id": quality.id if quality else None,
                "revision_brief_id": revision_brief.id,
                "rewrite_mode": rewrite_mode,
                "fresh_rewrite": fresh_rewrite,
                "min_chars": min_chars,
                "max_chars": packet.blueprint.target_max_chars,
                **packet.task_payload,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(output_data, ensure_ascii=False),
    )
    session.add(task)
    session.flush()
    record_production_run_review(
        session,
        book_id=book_id,
        chapter_id=chapter.id,
        chapter_number=chapter_number,
        version=version,
        task=task,
        output_data=output_data,
    )
    record_generation_llm_log(
        session,
        task=task,
        response=response,
        prompt_template=f"{template.name}@{template.version}",
        prompt=prompt,
        status="completed",
    )
    return version


def _revision_dimension_beats(dimensions: list[str]) -> list[str]:
    beats: list[str] = []
    if "brief_coverage" in dimensions:
        beats.append("补足本章核心承诺，让读者能在正文里看见人物目标、场景阻碍和局面变化")
    if "reader_momentum" in dimensions:
        beats.append("开场尽快进入具体处境，可用人物欲望、关系张力、异常细节、利益交换、行动后果或悬念建立阅读牵引")
    if "conflict_pressure" in dimensions:
        beats.append("增加可见阻碍、利益冲突、误判或逼近风险，让主角必须做出回应")
    if "choice_and_cost" in dimensions:
        beats.append("主角必须做出选择，并付出清晰代价或承担后果")
    if "hook_strength" in dimensions:
        beats.append("章末由本章行动自然引出新危险、新机会、新关系或未解决问题")
    if "prose_density" in dimensions:
        beats.append("减少解释和重复，增加动作、感官、对话和信息增量")
    if "arc_alignment" in dimensions:
        beats.append("修订必须服务本章阶段目标，结尾要推动主线进入下一步")
    if "production_standard" in dimensions:
        beats.append("必须按通用章节生产标准重写成完整章节：3000字以上、开篇有牵引、主角行动链完整、场景推进清楚、章末钩子由本章行动引发")
    return beats


def _revision_issue_beats(issues: list[str]) -> list[str]:
    beats: list[str] = []
    for issue in issues:
        if issue.startswith("forbidden_marker"):
            beats.append("把弹窗式奖励、UI播报或面板提醒改成角色可感知的身体变化、物证、人物怀疑或江湖传闻；不要让机械提示替代场景因果")
        elif "hook_strength" in issue:
            beats.append("重写章末钩子，让最后一幕出现秘密、发现、转折或疑问")
        elif "too_short" in issue:
            beats.append("补足关键场景，使正文字数达到最低要求")
        elif "too_long" in issue:
            beats.append("压缩冗余段落，使正文不超过平台长度上限")
        else:
            beats.append(f"修复质检问题：{issue}")
    return beats


def _chapter_unit_beats(quality_data: dict) -> list[str]:
    if not isinstance(quality_data, dict):
        return []
    report = quality_data.get("chapter_unit_report")
    if not isinstance(report, dict):
        return []
    rows: list[str] = []
    score = int(report.get("score") or 0)
    unit_count = int(report.get("unit_count") or 0)
    if score < 70:
        rows.append(
            f"拟人化小单元修复：当前单元流评分 {score}，共 {unit_count} 个单元；"
            "修订必须按 300-700 字小单元重建目标、阻碍、动作后果和承接点"
        )
    for item in (report.get("repair_contract") or [])[:3]:
        if item:
            rows.append(str(item))
    for unit in report.get("units") or []:
        if not isinstance(unit, dict) or int(unit.get("score") or 0) >= 70:
            continue
        issues = "、".join(str(item) for item in (unit.get("issues") or [])[:4])
        summary = str(unit.get("summary") or "").strip()
        detail = f"第{unit.get('index')}单元验收：修复 {issues or '单元推进不足'}，保留有效信息但补清承接和后果"
        if summary:
            detail += f"；当前片段：{summary}"
        rows.append(detail)
    return list(dict.fromkeys(rows))[:5]


def _llm_review_diagnostic_beats(llm_review: dict) -> list[str]:
    if not isinstance(llm_review, dict) or llm_review.get("status") != "completed":
        return []
    beats: list[str] = []
    if llm_review.get("verdict") in {"needs_revision", "fail"}:
        beats.append("参考主编二审的抽象失败原因修复读者体验，但不得继承二审里的具体旧桥段、旧名词、旧场景要求")
    if llm_review.get("risk_flags"):
        beats.append("重新检查连续性、平台可读性、爽点节奏和章末钩子风险；具体处理以最新生产骨架和修订方向为准")
    return beats


def _editor_in_chief_beats(quality_data: dict) -> list[str]:
    chief = quality_data.get("editor_in_chief") if isinstance(quality_data.get("editor_in_chief"), dict) else {}
    if not chief:
        return []
    rows: list[str] = []
    decision = str(chief.get("decision") or "").strip()
    largest = str(chief.get("largest_problem") or "").strip()
    if decision:
        rows.append(f"主编裁决：{decision}")
    if largest:
        rows.append(f"最大问题：{largest}")
    for item in (chief.get("minimum_effective_revision") or [])[:4]:
        rows.append(f"最小有效修法：{item}")
    forbidden = "；".join(str(item) for item in (chief.get("forbidden_revision") or [])[:5])
    if forbidden:
        rows.append(f"禁止修法：{forbidden}")
    for item in (chief.get("acceptance_checks") or [])[:3]:
        rows.append(f"主编验收：{item}")
    return rows


def _paragraph_aesthetic_beats(quality_data: dict) -> list[str]:
    report = quality_data.get("paragraph_aesthetic_report") if isinstance(quality_data.get("paragraph_aesthetic_report"), dict) else {}
    contract = format_paragraph_aesthetic_contract(report)
    return [line for line in contract.splitlines() if line.strip()][:8]


def _latest_story_brief_for_revision(session: Session, *, chapter: Chapter) -> ChapterBrief | None:
    return latest_story_brief(session, chapter.id)


def _brief_is_diagnostic(text: str) -> bool:
    return brief_has_revision_artifacts(text)


def _revision_story_goal(*, chapter_number: int, base_brief: ChapterBrief | None) -> str:
    if base_brief and base_brief.goal and not _brief_is_diagnostic(base_brief.goal):
        return base_brief.goal
    return f"第{chapter_number}章：按最新生产骨架重修为可读章节，重点写真实场景、人物动机、江湖因果和主角主动选择。"


def _revision_story_intent(*, chapter_number: int, base_brief: ChapterBrief | None) -> str:
    if base_brief and base_brief.required_beats and not _brief_is_diagnostic(base_brief.required_beats):
        return base_brief.required_beats
    return (
        f"第{chapter_number}章必须像真实作者重写章节：先承接人物处境，再推进人物欲望、阻碍、误判或冲突；"
        "游戏世界要像真实武侠世界，人物有欲望、顾虑、利益和误判；"
        "主角成长来自观察规则、修炼、人情、冒险和承担后果，不靠打怪升级或机械任务。"
    )


def _structural_rewrite_required_beats(
    *,
    chapter_number: int,
    base_brief: ChapterBrief | None,
    quality_data: dict,
    failure_class: dict,
) -> str:
    base_intent = _revision_story_intent(chapter_number=chapter_number, base_brief=base_brief)
    dimensions = quality_data.get("dimensions") if isinstance(quality_data.get("dimensions"), dict) else {}
    issues = [str(item) for item in quality_data.get("issues") or []]
    reasons = [str(item) for item in failure_class.get("structural_reasons") or []]
    rows = [
        "revision_mode:rewrite",
        "本轮不是局部润色；按稳定生产蓝图整章重构。",
        "正文硬目标：3000-4500中文字符，6-8个连续小单元；不得膨胀成多支线长章。",
        base_intent,
        "开头必须承接上一章结尾的具体后果、人物状态或未解决压力。",
        "每个小单元只完成一个清晰动作：目标、阻碍、反应、信息增量、后果承接必须可见。",
        "章末只留一个由本章行动自然引发的具体钩子，不再额外开新线。",
    ]
    if "length_out_of_range" in reasons or "over_target_max_chars" in reasons or any(item.startswith("too_long") for item in issues):
        rows.append("压缩策略：删除旁支解释、重复对话和未兑现专名；保留主角行动链、关键交易/冲突和章末物证。")
    if "unit_count_exploded" in reasons or "unit_flow_structural" in reasons:
        rows.append("结构策略：合并碎片场景，按承接/试探/受阻/转圜/反压/变局/代价/钩子推进，不要写成14个散片。")
    if int(dimensions.get("brief_coverage") or 100) < 60:
        rows.append("覆盖策略：只兑现本章最关键的3-5个承诺；每个承诺必须落到场景、动作、对白或后果。")
    if int(dimensions.get("dialogue_fullness") or 100) < 60:
        rows.append("对白策略：对白必须承担试探、遮掩、交易、威胁或情绪变化，不能只解释设定。")
    if int(dimensions.get("imageable_paragraphs") or 100) < 60:
        rows.append("画面策略：每个主要场景交代空间边界、关键物件、人物站位和动作轨迹。")
    return "；".join(list(dict.fromkeys(row for row in rows if row)))[:1800]


def _revision_story_constraints(*, base_brief: ChapterBrief | None) -> str:
    base = ""
    if base_brief and base_brief.constraints and not _brief_is_diagnostic(base_brief.constraints):
        base = base_brief.constraints
    additions = [
        "保留已登记 Canon，不引入无代价能力。",
        "不要输出模型说明、JSON、数据库、任务链路、质检报告、修订合同或作者说明。",
        "少量玩家感知层信息只能点到为止，不能替代真实江湖人物、因果和现场反应。",
        "如果前一版因弹窗式奖励或机械系统提示失败，下一版要改成身体反应、环境异象、物证、误会或人物怀疑。",
        "修订后必须重新走 review-chapter。",
    ]
    return "；".join([item for item in [base, *additions] if item])


def _latest_feedback_requirements(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    limit: int = 1,
) -> list[str]:
    adjustments = session.scalars(
        select(FeedbackAdjustment)
        .where(
            FeedbackAdjustment.book_id == book_id,
            FeedbackAdjustment.target_chapter_number == chapter_number,
            FeedbackAdjustment.status == "applied",
        )
        .order_by(FeedbackAdjustment.id.desc())
        .limit(limit)
    )
    requirements: list[str] = []
    for adjustment in adjustments:
        text = adjustment.adjustment_text.strip()
        if not text:
            continue
        requirements.append(build_rewrite_contract(text, chapter_number=chapter_number))
    return requirements


def _brief_has_feedback_marker(brief: ChapterBrief) -> bool:
    marker = "反馈调整#"
    return marker in brief.goal or marker in brief.required_beats or marker in brief.constraints


def _brief_has_actionable_revision_plan(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    if len(text.strip()) < 120:
        return False
    required_markers = ("第", "核心设定", "主角", "3000-4500")
    if not all(marker in text for marker in required_markers):
        return False
    stale_markers = ("修订合同:", "原始修订方向:", "验收清单:", "质检报告 #")
    return not any(marker in text for marker in stale_markers)


def _brief_has_budget_recovery_marker(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return (
        "system_revision_budget_recovery" in text
        or "persistent_revision_budget:" in text
        or "自动修订预算触顶" in text
    )


def _source_version_id(source: str | None) -> int | None:
    prefix, _, raw_id = str(source or "").partition(":v")
    if prefix not in {"revision_budget_recovery", "revision_budget_readable_restore"} or not raw_id:
        return None
    try:
        return int(raw_id.split(":", 1)[0])
    except ValueError:
        return None


def _fallback_quality_for_recovery_revision(
    session: Session,
    *,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
) -> QualityReport | None:
    source_id = _source_version_id(source_version.source)
    if not source_id or not _brief_has_budget_recovery_marker(revision_brief):
        return None
    quality = session.scalar(
        select(QualityReport)
        .where(QualityReport.chapter_version_id == source_id)
        .order_by(QualityReport.id.desc())
    )
    if quality:
        return quality
    return session.scalar(
        select(QualityReport)
        .join(ChapterVersion, QualityReport.chapter_version_id == ChapterVersion.id)
        .where(ChapterVersion.chapter_id == source_version.chapter_id, QualityReport.passed.is_(False))
        .order_by(QualityReport.score.desc(), QualityReport.id.desc())
    )


def _revision_requires_rewrite(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    primary_mode = _primary_revision_mode(text)
    if primary_mode in {REVISION_MODE_FRESH, "rewrite"}:
        return True
    if primary_mode in {REVISION_MODE_LOCAL_PATCH, "polish"}:
        return False
    modes = _revision_modes(text)
    if primary_mode == "targeted" and (REVISION_MODE_FRESH in modes or "rewrite" in modes):
        return True
    if primary_mode == "targeted":
        return False
    markers = (
        "修订模式:rewrite",
        "修订模式:fresh",
        "revision_mode:rewrite",
        "revision_mode:fresh",
        "重写",
        "重做",
        "重新组织",
        "最新生产骨架",
        "不要只做局部润色",
        "替换无效桥段",
    )
    return any(marker in text for marker in markers)


def _revision_is_fresh_rewrite(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return _primary_revision_mode(text) == REVISION_MODE_FRESH


def _revision_is_local_patch(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return _primary_revision_mode(text) == REVISION_MODE_LOCAL_PATCH


def _revision_modes(text: str) -> set[str]:
    modes: set[str] = set()
    normalized = (text or "").replace("：", ":")
    for match in re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", normalized):
        modes.add(match.group(1).strip().lower())
    return modes


def _primary_revision_mode(text: str) -> str:
    normalized = (text or "").replace("：", ":")
    matches = list(re.finditer(r"(?:revision_mode|修订模式):\s*([a-zA-Z_]+)", normalized))
    return matches[-1].group(1).strip().lower() if matches else ""


def _same_revision_content(source: str | None, revised: str | None) -> bool:
    def normalize(value: str | None) -> str:
        return "".join(str(value or "").split())

    source_text = normalize(source)
    revised_text = normalize(revised)
    if not source_text or not revised_text:
        return False
    return hashlib.sha256(source_text.encode("utf-8")).hexdigest() == hashlib.sha256(revised_text.encode("utf-8")).hexdigest()


def _try_local_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    canon_context: str,
    dry_run: bool,
) -> ChapterVersion | None:
    if not _revision_is_local_patch(revision_brief):
        return None
    bias = evaluate_generation_bias(
        content=source_version.content or "",
        goal=revision_brief.goal or "",
        required_beats=revision_brief.required_beats or "",
        constraints=revision_brief.constraints or "",
        canon_context=canon_context,
    )
    if not bias.model_bias_hits:
        return _try_llm_local_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    patched_content, replacements = apply_model_drift_local_patch(source_version.content or "", bias.model_bias_hits)
    if not replacements or patched_content == (source_version.content or ""):
        return _try_llm_local_patch_revision(
            session,
            book_id=book_id,
            chapter=chapter,
            source_version=source_version,
            revision_brief=revision_brief,
            dry_run=dry_run,
        )
    return _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="deterministic_local_patch",
        output_extra={"replacements": replacements, "bias_report": bias.to_dict()},
        dry_run=dry_run,
    )


def _try_llm_local_patch_revision(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    dry_run: bool,
) -> ChapterVersion | None:
    source_content = source_version.content or ""
    if chinese_chars(source_content) > 9000:
        return None
    provider = get_provider(dry_run)
    model = settings.llm_revision_model
    temperature = min(settings.llm_revision_temperature, 0.35)
    prompt = f"""
你是主笔，只做局部补丁，不重写整章。

请严格输出 JSON：{{"title":"章节标题","content":"局部补丁后的完整章节正文","patch_note":"说明改了哪里"}}

局部补丁要求：
- 只能修订修订单命中的句子、词语、短段落或轻微承接问题。
- 不得重排整章结构，不得改变章末事实，不得新增大设定。
- 保留原文已经有效的场景、动作链、人物关系和信息顺序。
- content 必须是完整章节正文，不要输出说明、Markdown 或系统信息。

修订单：
{revision_brief.goal}
{revision_brief.required_beats}
{revision_brief.constraints}

原章节：
{source_content}
""".strip()
    try:
        response = provider.generate(
            prompt,
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            response_format={"type": "json_object"} if provider.name != "dry_run" else None,
        )
        data = parse_or_repair_json_object(
            provider,
            response_text=response.text,
            original_prompt=prompt,
            expected_schema='{"title":"章节标题","content":"局部补丁后的完整章节正文","patch_note":"说明改了哪里"}',
            max_tokens=min(settings.llm_revision_max_tokens, 7600),
            temperature=temperature,
            model=model,
            task_label="局部补丁修订",
        )
    except Exception:
        return None
    patched_content = str(data.get("content") or "").strip()
    if not patched_content or patched_content == source_content:
        return None
    before_chars = chinese_chars(source_content)
    after_chars = chinese_chars(patched_content)
    if after_chars < max(800, int(before_chars * 0.75)) or after_chars > min(8000, int(max(before_chars, 1) * 1.18)):
        return None
    version = _store_local_patch_version(
        session,
        book_id=book_id,
        chapter=chapter,
        source_version=source_version,
        revision_brief=revision_brief,
        patched_content=patched_content,
        strategy="llm_local_patch",
        output_extra={
            "patch_note": str(data.get("patch_note") or ""),
            "provider": response.provider,
            "model": response.model,
            **llm_usage_payload(response, prompt=prompt),
        },
        dry_run=False,
    )
    task = session.scalar(select(GenerationTask).where(GenerationTask.book_id == book_id).order_by(GenerationTask.id.desc()))
    if task and task.task_type == "revise_chapter":
        record_generation_llm_log(
            session,
            task=task,
            response=response,
            prompt_template="local_patch@v1",
            prompt=prompt,
            status="completed",
        )
    return version


def _store_local_patch_version(
    session: Session,
    *,
    book_id: int,
    chapter: Chapter,
    source_version: ChapterVersion,
    revision_brief: ChapterBrief,
    patched_content: str,
    strategy: str,
    output_extra: dict,
    dry_run: bool,
) -> ChapterVersion:
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=source_version.title,
        content=patched_content,
        status="draft",
        source=f"revision:{strategy}",
    )
    session.add(version)
    session.flush()
    task = GenerationTask(
        book_id=book_id,
        task_type="revise_chapter",
        status="completed",
        input_json=json.dumps(
            {
                "chapter_number": chapter.chapter_number,
                "dry_run": dry_run,
                "source_version_id": source_version.id,
                "revision_brief_id": revision_brief.id,
                "revision_mode": REVISION_MODE_LOCAL_PATCH,
            },
            ensure_ascii=False,
        ),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "strategy": strategy,
                "content_chars": chinese_chars(patched_content),
                **output_extra,
            },
            ensure_ascii=False,
        ),
    )
    session.add(task)
    session.flush()
    return version


def _revision_required_beats(brief: ChapterBrief, *, rewrite_mode: bool, fresh_rewrite: bool) -> str:
    if not fresh_rewrite and not rewrite_mode:
        return sanitize_prompt_contract_text(brief.required_beats)
    keep: list[str] = []
    for part in brief.required_beats.replace("\n", "；").split("；"):
        item = part.strip()
        if not item:
            continue
        if item.startswith(("采纳二审建议：", "规避风险：", "修复质检问题：")):
            continue
        keep.append(item)
    if rewrite_mode and not fresh_rewrite:
        keep.append("结构重写时二审建议只作为抽象诊断，不得继承具体旧桥段、旧名词或旧场景要求")
    return sanitize_prompt_contract_text("；".join(keep))
