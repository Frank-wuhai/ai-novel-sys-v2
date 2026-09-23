from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import parse_review_output
from app.models.entities import Book, Chapter, ChapterBrief, ChapterReview, ChapterVersion, GenerationTask, QualityReport
from app.services.canon import format_canon_context
from app.services.chapter_standards import extract_max_chars, extract_min_chars
from app.services.llm_errors import classify_exception
from app.services.editorial_stratification import maybe_apply_editorial_stratification, maybe_rollback_failed_elevation
from app.services.production_llm import (
    llm_parameter_snapshot,
    llm_usage_payload,
    record_generation_llm_log,
)
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.services.prose_judgement import run_prose_judgement
from app.services.production_optimization import enrich_quality_report_with_optimization
from app.services.quality import evaluate_chapter
from app.services.quality_evidence import build_quality_evidence_chain
from app.services.production_blueprint import classify_quality_failure
from app.services.production_gate import assert_production_gate
from app.services.reading_assessment import maybe_apply_reading_assessment
from app.services.revision_comparison import compare_and_restore_if_regressed
from app.services.review_decision import ReviewRuleResult, apply_review_decision, soft_override_blockers
from app.workflows.state_machine import move


def review_chapter(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    llm_review: bool = False,
    review_dry_run: bool = True,
    auto_revision_brief: bool = False,
    prose_judge: bool = False,
) -> QualityReport:
    assert_production_gate(session, book_id=book_id, action="review_chapter")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError("chapter not found")
    # 2026-09-23 选版口径对齐 revise（治本, 用户批准放行 ch3 时撞上）:
    # 此前取全表最新 id, 链尾留 discarded 证据行会卡死 review
    # (invalid transition: discarded --quality_fail--> needs_revision, 第 14.4 节实案)。
    version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )
    if not version:
        raise ValueError("chapter version not found")
    was_approved = version.status == "approved"
    brief = _latest_brief(session, chapter.id)
    book = session.get(Book, book_id)
    review_goal = brief.goal if brief else ""
    review_required_beats = brief.required_beats if brief else ""
    review_constraints = brief.constraints if brief else ""
    if book and brief:
        try:
            from app.services.production_packet import build_chapter_production_packet
            packet = build_chapter_production_packet(
                session,
                book=book,
                goal=brief.goal,
                required_beats=brief.required_beats,
                constraints=brief.constraints,
                chapter_number=chapter_number,
                mode="review",
                chapter_id=chapter.id,
                chapter_brief_id=brief.id,
            )
            review_goal = packet.blueprint.goal or brief.goal
            review_required_beats = packet.blueprint.required_beats or brief.required_beats
            review_constraints = "\n\n".join(
                item for item in [packet.blueprint.constraints or "", packet.constraints or ""] if item
            ) or brief.constraints
        except Exception:
            review_goal = brief.goal
            review_required_beats = brief.required_beats
            review_constraints = brief.constraints
    canon_context, _ = format_canon_context(session, book_id=book_id)
    # 2026-08-18: 注入 active CanonAuthorityProfile 到 canon_context, 让 _canon_score
    # 读 active profile 的 must_keep / opening_contract 关键词命中 bonus
    # （避免被"没写玩家/面板"误判扣分 / 也不让 brief 6011/6012 旧描述当硬扣分）
    from app.models.entities import CanonAuthorityProfile
    active_profile = session.scalar(
        select(CanonAuthorityProfile).where(
            CanonAuthorityProfile.book_id == book_id,
            CanonAuthorityProfile.status == "active",
        )
    )
    if active_profile and active_profile.profile_json:
        import json as _json
        pj = _json.loads(active_profile.profile_json)
        profile_block_parts: list[str] = ["[active Canon Authority Profile]"]
        for k in ("must_keep", "opening_contract", "allowed_but_limited", "forbidden_misread", "deprecated_pollution"):
            items = pj.get(k) or []
            if items:
                profile_block_parts.append(f"{k}:")
                for item in items:
                    profile_block_parts.append(f"  - {item}")
        canon_context = (canon_context or "") + "\n\n" + "\n".join(profile_block_parts)
    from app.services.context_contamination import context_anchor_terms
    from app.services.production_packet import load_previous_hook_keywords
    _anchor_terms = context_anchor_terms(session, book_id=book_id)
    _prev_kws = load_previous_hook_keywords(session, book_id=book_id, chapter_number=chapter_number)
    # 2026-07-14 · 项目规范：番茄章节字数区间 1800-2500 · 老 brief 可能残留 3000-4500 硬编码
    # 硬 clip：min_chars 不得超过 2500 · 避免残留老规范阻塞 review
    _min_chars_raw = extract_min_chars(
        brief.goal if brief else "",
        brief.required_beats if brief else "",
        brief.constraints if brief else "",
        default=1800,
    )
    _min_chars = min(_min_chars_raw, 1800)  # 与 blueprint target_min 一致 · 番茄区间 1800-2500
    # 字数上限单一真源（消除裸字数漂移·2026-07-29 D3门归并）：default 直接取
    # REBUILD_MAX_CHARS，不再硬编码 2800。这样 chapter_standards 改上限时本处自动跟随。
    from app.services.chapter_standards import REBUILD_MAX_CHARS
    _max_chars_raw = extract_max_chars(
        brief.goal if brief else "",
        brief.required_beats if brief else "",
        brief.constraints if brief else "",
        default=REBUILD_MAX_CHARS,
    )
    # 字数门（2026-07-28 D方案 Phase1·容差带·单一真源见 chapter_standards）：
    # 传给 evaluate_chapter 的 max_chars 用 REBUILD_MAX(2800) — 决定 too_long 硬 issue 与
    # thresholds.max_chars（classify 的 over_target_max_chars 判定基准）的触发点。
    # 2600-2800 容差带内只记 length_soft_over 观感 warning（软·不触发硬重建），
    # 实现用户「字数不管=比旧版±200可接受」；真超 2800（失控膨胀）才强制重建。
    # （REBUILD_MAX_CHARS 已在上方 line65 导入）
    _max_chars = min(max(_max_chars_raw, 2500), REBUILD_MAX_CHARS)
    result = evaluate_chapter(
        version.content,
        min_chars=_min_chars,
        max_chars=_max_chars,
        goal=review_goal,
        required_beats=review_required_beats,
        constraints=review_constraints,
        canon_context=canon_context,
        authority_terms=_anchor_terms,
        previous_hook_keywords=_prev_kws,
        book_id=book_id,
        chapter_number=chapter_number,
        session=session,
    )
    report_data = json.loads(result.report)
    report_data.setdefault("passed", bool(result.passed))
    report_data["production_failure_classification"] = classify_quality_failure(report_data)
    # 打通 soft_pass 死锁 (2026-07-26 A方案): soft_pass 在 enrich_quality_report_with_optimization
    # (下方) 内计算,其判定条件之一 editorial_ok 读 report_data["editorial_gate"].passed。
    # 但 editorial_gate 原先只在 LLM 复核分支(更下方)生成,纯规则质检链路(llm_review=False,
    # 如 promote 入库脚本)从不写此字段 → soft_pass 恒 editorial_ok=False → 死锁:
    # quality 层判可发(≥65)但 type_gate 结构维度贴门槛(gap≤15)的口语化B版永远救不了。
    # 必须在 enrich 之前写入 editorial_gate,soft_pass 才能拿到数据。apply_review_decision
    # 在 llm_review 缺失时写 editorial_gate.passed=True(采用规则结果),顶层 passed 仍取
    # rule_result.passed,硬伤/规则fail 依旧被拦,不放水。LLM 复核分支会在下方重算覆盖。
    if "editorial_gate" not in report_data:
        _apply_editorial_gate(result, report_data)
    report_data = enrich_quality_report_with_optimization(
        report_data,
        chapter_number=chapter_number,
        goal=review_goal,
        required_beats=review_required_beats,
        constraints=review_constraints,
        enforce_gate=not _is_dry_run_version(version),
    )
    # Enrich report_data with per-chapter revision history so
    # `_should_run_llm_review` can enforce the plateau_llm_skip guard.
    prior_reports = session.execute(
        select(QualityReport)
        .join(ChapterVersion, QualityReport.chapter_version_id == ChapterVersion.id)
        .where(ChapterVersion.chapter_id == chapter.id)
        .order_by(QualityReport.id)
    ).scalars().all()
    if prior_reports:
        recent_scores: list[int] = []
        llm_review_history: list[dict] = []
        for qr in prior_reports:
            if qr.score is not None:
                recent_scores.append(int(qr.score))
            try:
                prev_data = json.loads(qr.report or "{}")
            except Exception:
                prev_data = {}
            if isinstance(prev_data.get("llm_review"), dict):
                llm_review_history.append({"score": qr.score})
        report_data.setdefault("recent_scores", recent_scores)
        report_data.setdefault("llm_review_history", llm_review_history)

    should_llm_review, llm_skip_reason = _should_run_llm_review(result, report_data)
    if llm_review and should_llm_review:
        report_data["llm_review"] = _run_llm_chapter_review(
            session,
            book=session.get(Book, book_id),
            version=version,
            chapter_number=chapter_number,
            goal=review_goal,
            required_beats=review_required_beats,
            constraints=review_constraints,
            canon_context=canon_context,
            rule_report=result.report,
            dry_run=review_dry_run,
        )
        _apply_editorial_gate(result, report_data)
    elif llm_review:
        report_data["llm_review"] = {
            "status": "skipped",
            "reason": llm_skip_reason,
            "source": "rule_precondition",
        }
    # 成文判据判卷 (2026-09-10 第3步): prose_judgement_v1 J1-J5 缺口表, 与 llm_review
    # (主编审稿) 物理分开。不打分、不给 verdict、不碰 passed/score/editorial_gate——
    # 判据规范明确"无自动 FAIL, 不自动拦稿", 缺口表仅供人工裁决退修或放行。
    if prose_judge:
        report_data["prose_judgement"] = run_prose_judgement(
            session,
            book=book,
            version=version,
            chapter_number=chapter_number,
            dry_run=review_dry_run,
        )
    report_data["evidence_chain"] = build_quality_evidence_chain(version.content or "", report_data)
    if was_approved and not bool(report_data.get("passed", result.passed)):
        existing_pass = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id, QualityReport.passed.is_(True))
            .order_by(QualityReport.id.desc())
        )
        if existing_pass is not None:
            return existing_pass
    quality = QualityReport(
        chapter_version_id=version.id,
        score=int(report_data.get("score") or result.score),
        passed=bool(report_data.get("passed", result.passed)),
        report=json.dumps(report_data, ensure_ascii=False),
    )
    review = ChapterReview(
        chapter_version_id=version.id,
        verdict="pass" if quality.passed else "needs_revision",
        notes=result.report,
        reviewer="system-quality-gate",
    )
    session.add(quality)
    session.add(review)
    target = "approved" if quality.passed and version.status == "approved" else ("reviewed_pass" if quality.passed else "needs_revision")
    action = "quality_pass" if quality.passed else "quality_fail"
    version.status = move("chapter_version", version.status, target, action)
    session.flush()
    maybe_apply_editorial_stratification(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        quality=quality,
    )
    if not (quality.passed and _is_dry_run_version(version)):
        maybe_apply_reading_assessment(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            quality=quality,
        )
    review.verdict = "pass" if quality.passed else "needs_revision"
    review.notes = quality.report
    session.flush()
    maybe_rollback_failed_elevation(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        failed_version=version,
        quality=quality,
    )
    # 2026-09-20 第 4.5 步: QC 评审只读。对比结论仍记入报告 revision_comparison 节,
    # 但评审命令不再改版本状态(曾把用户裁决驳回的正文按噪声分自动恢复回最新);
    # 回退恢复收归修订管线 revise-chapter 入口(见 chapter_revision.revise_chapter)。
    comparison = compare_and_restore_if_regressed(
        session, current_version=version, current_quality=quality, allow_restore=False
    )
    if comparison.restored_version_id is not None:
        restored_quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == comparison.restored_version_id)
            .order_by(QualityReport.id.desc())
        )
        if restored_quality is not None:
            maybe_apply_reading_assessment(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                quality=restored_quality,
            )
    if auto_revision_brief and not quality.passed and not _has_protected_revision_brief(session, chapter_id=chapter.id):
        from app.services.production import create_revision_brief

        create_revision_brief(session, book_id=book_id, chapter_number=chapter_number)
    return quality


def _has_protected_revision_brief(session: Session, *, chapter_id: int) -> bool:
    markers = (
        "reading_assessment_contract",
        "reading_assessment_auto_quality#",
        "clean_rebuild_contract@",
        "当前稿不是正式批准稿",
        "阅读评估结论",
    )
    for brief in session.scalars(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
        .limit(12)
    ):
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if any(marker in text for marker in markers):
            return True
    return False


def _should_run_llm_review(rule_result, report_data: dict) -> tuple[bool, str]:
    if settings.production_profile == "fast":
        return False, "production_profile_fast"
    score = int(report_data.get("score") or getattr(rule_result, "score", 0) or 0)
    passed = bool(report_data.get("passed", getattr(rule_result, "passed", False)))
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    hard_passed = bool(hard_gate.get("passed", passed))
    blockers = report_data.get("blockers") if isinstance(report_data.get("blockers"), list) else []
    severe_blockers = [str(item) for item in blockers if any(marker in str(item) for marker in ("contamination", "canon", "length", "min_chars"))]
    if passed:
        return True, "base_quality_passed"
    if score >= 78:
        return True, "high_score_rule_disagreement"
    if severe_blockers:
        return False, "hard_rule_blockers:" + ",".join(severe_blockers[:3])
    if score >= 72 and hard_passed:
        return True, "near_pass_needs_editorial_judgment"
    # ------------------------------------------------------------------
    # Editorial recovery window (added 2026-07-02).
    #
    # Rule-based scoring alone deadlocked the baseline chapter 1 run at
    # score=45 for three consecutive revise rounds because rule scores are
    # insensitive to revised prose content. Escalating to LLM review when
    # (a) the hard gate PASSes and (b) score sits in [55, 72) unlocks the
    # editorial layer so a human-style verdict can break the plateau.
    if hard_passed and 55 <= score < 72:
        # Plateau guard: if we're already flat AND we've spent an LLM review
        # once, further LLM escalations rarely change the verdict — skip so
        # we don't bleed tokens on a stuck chapter.
        recent_scores = report_data.get("recent_scores") if isinstance(report_data.get("recent_scores"), list) else []
        llm_history = report_data.get("llm_review_history") if isinstance(report_data.get("llm_review_history"), list) else []
        if len(recent_scores) >= 3 and _plateau(recent_scores[-3:]) and llm_history:
            return False, "plateau_llm_skip: 3 flat rule scores after >=1 LLM review"
        return True, "editorial_recovery: hard_gate_pass_but_rule_score_low"
    return False, f"rule_score_too_low:{score}"


def _plateau(scores: list[int]) -> bool:
    """Return True when the given scores vary by <= 2 points (rule-flat)."""
    if len(scores) < 2:
        return False
    numeric = [int(s) for s in scores if isinstance(s, (int, float))]
    if len(numeric) < 2:
        return False
    return (max(numeric) - min(numeric)) <= 2


def reconcile_existing_quality_report(
    session: Session,
    *,
    version: ChapterVersion,
    quality: QualityReport,
) -> bool:
    try:
        report_data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return False
    final_verdict = report_data.get("final_verdict") if isinstance(report_data.get("final_verdict"), dict) else {}
    if final_verdict.get("source") == "unified_quality_verdict@v1" and final_verdict.get("status") == "needs_revision":
        quality.passed = False
        return False
    if bool(report_data.get("passed")) and quality.passed and version.status == "reviewed_pass":
        return True
    llm_review = report_data.get("llm_review") if isinstance(report_data.get("llm_review"), dict) else {}
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    if llm_review.get("status") != "completed" or llm_review.get("verdict") != "pass":
        return False
    if int(llm_review.get("score") or 0) < 75 or not bool(hard_gate.get("passed")):
        return False
    rule_result = ReviewRuleResult(passed=bool(report_data.get("passed")), score=int(report_data.get("score") or quality.score or 0))
    _apply_editorial_gate(rule_result, report_data)
    if not bool(report_data.get("passed")):
        return False
    quality.passed = True
    quality.score = int(report_data.get("score") or quality.score or 0)
    quality.report = json.dumps(report_data, ensure_ascii=False)
    if version.status == "needs_revision":
        version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    session.flush()
    return True


def _apply_editorial_gate(rule_result, report_data: dict) -> None:
    if not isinstance(rule_result, ReviewRuleResult):
        rule_result = ReviewRuleResult(passed=bool(rule_result.passed), score=int(rule_result.score))
    apply_review_decision(rule_result, report_data)


def _soft_override_blockers(dimensions: dict) -> list[str]:
    return soft_override_blockers(dimensions)


def _aggregate_reviews(reviews: list):
    """聚合多次主编采样，消除单次 LLM 评分抖动，保证入库判定可复现。

    - score: 取中位数（对 [70,73,86] 这类抖动取 73，屏蔽极值）。
    - verdict: 多数决；平局时按中位数分数 >=75 判 pass，否则 needs_revision。
    - strengths/issues/suggestions/risk_flags: 采用最接近中位数分数的那次采样的
      完整内容（保留可读性与一致性，不做跨样本拼接以免语义错乱）。
    单次采样时直接返回该次，行为与旧逻辑一致。
    """
    import statistics as _stats

    if len(reviews) == 1:
        return reviews[0]
    scores = [int(getattr(r, "score", 0) or 0) for r in reviews]
    median_score = int(round(_stats.median(scores)))
    verdicts = [str(getattr(r, "verdict", "") or "") for r in reviews]
    pass_votes = sum(1 for v in verdicts if v == "pass")
    nonpass_votes = len(verdicts) - pass_votes
    if pass_votes > nonpass_votes:
        median_verdict = "pass"
    elif nonpass_votes > pass_votes:
        # 多数为非 pass；沿用出现最多的非 pass verdict（通常 needs_revision）
        nonpass = [v for v in verdicts if v != "pass"] or ["needs_revision"]
        median_verdict = max(set(nonpass), key=nonpass.count)
    else:
        median_verdict = "pass" if median_score >= 75 else "needs_revision"
    # 选内容代表：分数最接近中位数的那次采样
    representative = min(reviews, key=lambda r: abs(int(getattr(r, "score", 0) or 0) - median_score))
    from app.llm.schemas import ReviewOutput

    return ReviewOutput(
        verdict=median_verdict,
        score=median_score,
        strengths=list(getattr(representative, "strengths", []) or []),
        issues=list(getattr(representative, "issues", []) or []),
        revision_suggestions=list(getattr(representative, "revision_suggestions", []) or []),
        risk_flags=list(getattr(representative, "risk_flags", []) or []),
    )


def _run_llm_chapter_review(
    session: Session,
    *,
    book: Book | None,
    version: ChapterVersion,
    chapter_number: int,
    goal: str,
    required_beats: str,
    constraints: str,
    canon_context: str,
    rule_report: str,
    dry_run: bool,
) -> dict:
    if not book:
        return {"status": "failed", "error_category": "validation", "error": "book not found"}
    seed_prompt_templates(session)
    template = get_prompt_template(session, name="review_chapter", version="v2")
    prompt = render_template(
        template,
        book_title=book.title,
        genre=book.genre,
        target_platform=book.target_platform,
        goal=goal,
        required_beats=required_beats,
        constraints=constraints,
        rule_report=rule_report,
        canon_context=canon_context,
        chapter_content=version.content,
    )
    provider = get_provider(dry_run)
    model = settings.llm_review_model
    temperature = settings.llm_review_temperature
    llm_parameters = llm_parameter_snapshot(
        dry_run=dry_run,
        max_tokens=settings.llm_review_max_tokens,
        temperature=temperature,
        model=model,
    )
    input_json = {
        "chapter_number": chapter_number,
        "dry_run": dry_run,
        "prompt_template": f"{template.name}@{template.version}",
        "llm_parameters": llm_parameters,
        "version_id": version.id,
    }
    try:
        samples = max(1, int(getattr(settings, "llm_review_samples", 1) or 1))
        reviews: list = []
        responses: list = []
        last_exc: Exception | None = None
        for _ in range(samples):
            try:
                resp = provider.generate(
                    prompt,
                    max_tokens=settings.llm_review_max_tokens,
                    temperature=temperature,
                    model=model,
                )
                rev = parse_review_output(resp.text)
            except Exception as exc:  # noqa: BLE001 — 单次采样失败不应中断，容错继续
                last_exc = exc
                continue
            reviews.append(rev)
            responses.append(resp)
        if not reviews:
            # 全部采样失败 → 抛出最后一次异常走下方 except 的失败落库
            raise last_exc if last_exc is not None else RuntimeError("llm review produced no samples")
        response = responses[-1]
        review = _aggregate_reviews(reviews)
    except Exception as exc:
        classification = classify_exception(exc)
        task = GenerationTask(
            book_id=book.id,
            task_type="llm_review_chapter",
            status="failed",
            input_json=json.dumps(input_json, ensure_ascii=False),
            output_json=json.dumps(
                {
                    "error_category": classification.category,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "llm_parameters": llm_parameters,
                },
                ensure_ascii=False,
            ),
        )
        session.add(task)
        session.flush()
        return {
            "status": "failed",
            "generation_task_id": task.id,
            "error_category": classification.category,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
    # 2026-08-19 新增: 合理/舒适/基础 三评审 (注入 WRITING_STANDARD.md 硬指标)
    # 在主评审完成后, 跑 3 个独立 LLM 调, 各自评 0-1, 结果合并到 llm_review 子字段.
    style_results = _run_style_review(
        session,
        book=book,
        version=version,
        chapter_number=chapter_number,
        rule_report=rule_report,
        dry_run=dry_run,
    )
    task = GenerationTask(
        book_id=book.id,
        task_type="llm_review_chapter",
        status="completed",
        input_json=json.dumps(input_json, ensure_ascii=False),
        output_json=json.dumps(
            {
                "version_id": version.id,
                "provider": response.provider,
                "model": response.model,
                "llm_parameters": llm_parameters,
                **llm_usage_payload(response, prompt=prompt),
                "review": review.to_dict(),
                "style_review": style_results,
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
        status="completed",
    )
    result = {
        "status": "completed",
        "generation_task_id": task.id,
        "provider": response.provider,
        "model": response.model,
        "request_id": response.request_id,
        **review.to_dict(),
        "style_review": style_results,
    }
    # 2026-08-19 新增: 合并 style_review 到总分, 公式: 总分 = (合理×0.45) + (舒适×0.45) + (基础×0.10)
    # 任一条款 = 0 时, 给 hard_issue 标记, 不可放行.
    _merge_style_score(result, style_results)
    return result


def _run_style_review(
    session: Session,
    *,
    book: Book,
    version: ChapterVersion,
    chapter_number: int,
    rule_report: str,
    dry_run: bool,
) -> dict:
    """2026-08-19 新增: 跑 3 个独立 LLM 评审 (合理/舒适/基础), 注入 WRITING_STANDARD.md 硬指标.

    每个评审只评 0/1 二元, 不打主观分. 评审失败时不阻塞, 写 status=failed 即可.
    """
    provider = get_provider(dry_run)
    model = settings.llm_review_model
    temperature = 0.1  # 硬指标评审温度低, 减少噪声
    results: dict = {"status": "completed", "logic_review": None, "comfort_review": None, "base_review": None}

    for slot, template_name, max_tok in (
        ("logic_review", "style_review_logic", 2500),
        ("comfort_review", "style_review_comfort", 3000),
        ("base_review", "style_review_base", 2000),
    ):
        try:
            template = get_prompt_template(session, name=template_name, version="v1")
            prompt = render_template(
                template,
                chapter_content=version.content,
                rule_report=rule_report,
            )
            resp = provider.generate(
                prompt,
                max_tokens=max_tok,
                temperature=temperature,
                model=model,
            )
            # 解析 JSON, 容错
            import re as _re
            txt = resp.text
            json_match = _re.search(r"\{[\s\S]*\}", txt)
            if not json_match:
                results[slot] = {"status": "parse_failed", "raw": txt[:500]}
                continue
            data = json.loads(json_match.group(0))
            data["status"] = "completed"
            data["model"] = resp.model
            results[slot] = data
            # 落库 GenerationTask (审计轨迹)
            task = GenerationTask(
                book_id=book.id,
                task_type=f"llm_{template_name}",
                status="completed",
                input_json=json.dumps(
                    {"chapter_number": chapter_number, "version_id": version.id, "template": f"{template_name}@v1"},
                    ensure_ascii=False,
                ),
                output_json=json.dumps(data, ensure_ascii=False),
            )
            session.add(task)
        except Exception as exc:
            # 单评审失败不阻塞其他评审
            results[slot] = {"status": "failed", "error": str(exc), "error_type": type(exc).__name__}
    session.flush()
    return results


def _merge_style_score(llm_review_result: dict, style_results: dict) -> None:
    """2026-08-19 新增: 把 style_review 的硬指标分合并到 LLM 评审结果.

    公式: 总分 = (合理×0.45) + (舒适×0.45) + (基础×0.10)
    阈值: logic_passed = (logic_score >= 6), comfort_passed = (comfort_score >= 7), base_passed = (base_score >= 3)
    任一 hard_issue (score=0 的条款) → 整体 verdict 改 needs_revision
    """
    if not isinstance(style_results, dict):
        return
    logic = style_results.get("logic_review") or {}
    comfort = style_results.get("comfort_review") or {}
    base = style_results.get("base_review") or {}
    logic_score = int(logic.get("logic_score") or 0) if isinstance(logic, dict) else 0
    comfort_score = int(comfort.get("comfort_score") or 0) if isinstance(comfort, dict) else 0
    base_score = int(base.get("base_score") or 0) if isinstance(base, dict) else 0
    # 0-100 标尺: 把 9/10/4 折算成百分制, 加权
    style_pct = (logic_score / 9.0) * 100 * 0.45 + (comfort_score / 10.0) * 100 * 0.45 + (base_score / 4.0) * 100 * 0.10
    # 把 style_pct 覆盖 score (只在原 score < style_pct 时, 提升; 否则保留原 score)
    original_score = int(llm_review_result.get("score") or 0)
    new_score = max(original_score, int(round(style_pct)))
    llm_review_result["score"] = new_score
    # 把 3 个分项写到顶层, 方便 dashboard 看
    llm_review_result["logic_score"] = logic_score
    llm_review_result["comfort_score"] = comfort_score
    llm_review_result["base_score"] = base_score
    llm_review_result["style_pct"] = int(round(style_pct))
    # 任一 hard_issue 触发, verdict 改 needs_revision
    hard_issues = []
    for k in ("logic_hard_issues", "comfort_hard_issues", "base_hard_issues"):
        v = (logic if "logic" in k else comfort if "comfort" in k else base).get(k) or []
        if isinstance(v, list):
            hard_issues.extend([str(x) for x in v])
    if hard_issues:
        llm_review_result["style_hard_issues"] = hard_issues
        if llm_review_result.get("verdict") == "pass":
            llm_review_result["verdict"] = "needs_revision"
            llm_review_result["score"] = min(new_score, 70)


def _latest_brief(session: Session, chapter_id: int) -> ChapterBrief | None:
    active = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status != "superseded")
        .order_by(ChapterBrief.id.desc())
    )
    if active:
        return active
    return session.scalar(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id).order_by(ChapterBrief.id.desc()))


def _is_dry_run_version(version: ChapterVersion) -> bool:
    source = str(version.source or "")
    return source == "dry_run" or source.startswith("revision:dry_run")
