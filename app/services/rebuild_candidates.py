from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.llm.providers import get_provider
from app.llm.schemas import StructuredOutputError
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.canon import format_canon_context
from app.services.chapter_standards import extract_max_chars
from app.services.readability import chinese_chars
from app.services.production_blueprint import classify_quality_failure
from app.services.production_optimization import enrich_quality_report_with_optimization
from app.services.production_context import sanitize_quality_report
from app.services.production_llm import (
    expand_short_draft_output,
    llm_parameter_snapshot,
    llm_usage_payload,
    parse_or_repair_draft_output,
    repair_humanized_unit_flow,
)
from app.services.production_packet import build_chapter_production_packet
from app.services.production_state import latest_foundation, next_version_number
from app.services.prompts import get_prompt_template, render_template, seed_prompt_templates
from app.services.quality import evaluate_chapter
from app.services.reading_assessment import maybe_apply_reading_assessment


TASK_TYPE_REBUILD_CANDIDATES = "rebuild_chapter_candidates"


@dataclass(frozen=True)
class CandidateScore:
    value: int
    passed: bool
    blocker_count: int
    contract_preservation: int
    sample_adoption_preservation: int
    structural_divergence: int
    readability_floor: int
    canon_consistency: int

    @property
    def rank_tuple(self) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            int(self.passed),
            -self.blocker_count,
            self.value,
            self.contract_preservation,
            self.sample_adoption_preservation,
            self.structural_divergence,
            self.readability_floor,
            self.canon_consistency,
        )


@dataclass(frozen=True)
class RebuildCandidateResult:
    task_id: int
    selected_version_id: int
    selected_score: int
    candidate_count: int


@dataclass(frozen=True)
class IncumbentDraft:
    version: ChapterVersion
    quality: QualityReport
    score: int
    passed: bool


def generate_rebuild_candidates(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    candidate_count: int = 3,
    dry_run: bool = False,
    existing_task_id: int | None = None,
) -> RebuildCandidateResult:
    candidate_count = max(2, min(5, int(candidate_count or 3)))
    book = session.get(Book, book_id)
    if not book:
        raise ValueError(f"book not found: {book_id}")
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter:
        raise ValueError(f"chapter not found: {chapter_number}")
    # Sprint 2 P0-1 stage-4: exclude discarded versions when finding latest.
    # accept_early_stop / _execute_accept_early_stop discards stale versions
    # after promoting a candidate; if the discarded version happens to be the
    # highest id, the naive latest query returns it and the pre-check below
    # rejects the rebuild attempt with "latest ... must be needs_revision".
    # Observed on book=3 Ch7: v579 (discarded) shadowed v577 (needs_revision),
    # blocking 3 subsequent rebuild rounds and stranding the chapter.
    source_version = session.scalar(
        select(ChapterVersion)
        .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.status != "discarded")
        .order_by(ChapterVersion.id.desc())
    )
    if not source_version or source_version.status != "needs_revision":
        raise ValueError("latest chapter version must be needs_revision before candidate rebuild")
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    if not brief:
        raise ValueError("active revision brief is required before candidate rebuild")
    quality = session.scalar(
        select(QualityReport).where(QualityReport.chapter_version_id == source_version.id).order_by(QualityReport.id.desc())
    )
    foundation = latest_foundation(session, book_id)
    if not foundation:
        raise ValueError("story foundation is required before candidate rebuild")

    seed_prompt_templates(session)
    provider = get_provider(dry_run)
    template = get_prompt_template(session, name="revise_chapter", version="v4")
    model = settings.llm_revision_model
    temperature = max(float(settings.llm_revision_temperature or 0.55), 0.62)
    max_tokens = settings.llm_revision_max_tokens
    llm_parameters = llm_parameter_snapshot(dry_run=dry_run, max_tokens=max_tokens, temperature=temperature, model=model)
    protected_rebuild_constraints = _protected_rebuild_constraints(brief)
    task = session.get(GenerationTask, existing_task_id) if existing_task_id else None
    if task is None:
        task = GenerationTask(
            book_id=book_id,
            task_type=TASK_TYPE_REBUILD_CANDIDATES,
            status="running",
            input_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "dry_run": dry_run,
                    "candidate_count": candidate_count,
                    "source_version_id": source_version.id,
                    "revision_brief_id": brief.id,
                    "quality_report_id": quality.id if quality else None,
                    "protected_rebuild_constraints": protected_rebuild_constraints,
                    "prompt_template": f"{template.name}@{template.version}",
                    "llm_parameters": llm_parameters,
                },
                ensure_ascii=False,
            ),
            output_json="{}",
        )
        session.add(task)
        session.flush()
        session.commit()
    else:
        input_data = json.loads(task.input_json or "{}")
        input_data.update(
            {
                "chapter_number": chapter_number,
                "dry_run": dry_run,
                "candidate_count": candidate_count,
                "source_version_id": source_version.id,
                "revision_brief_id": brief.id,
                "quality_report_id": quality.id if quality else None,
                "protected_rebuild_constraints": protected_rebuild_constraints,
                "prompt_template": f"{template.name}@{template.version}",
                "llm_parameters": llm_parameters,
            }
        )
        task.input_json = json.dumps(input_data, ensure_ascii=False)
        task.status = "running"
        session.flush()

    try:
        rows: list[dict] = []
        _skip_reasons: list[dict] = []
        # Sprint 2 P1-4 A1: retry empty/malformed candidate up to 2 times
        # (total 3 attempts) with jittered temperature before giving up.
        # Prior behaviour skipped on first failure, wasting a full candidate
        # slot on transient LLM issues (empty body / structured-output parse
        # failure).  Retry preserves candidate diversity while making the
        # rebuild step robust to transient provider misfires.
        MAX_ATTEMPTS_PER_CANDIDATE = 3
        for index, strategy in enumerate(_candidate_strategies(chapter_number)[:candidate_count], start=1):
            attempts_used = 0
            last_error: Exception | None = None
            for attempt in range(1, MAX_ATTEMPTS_PER_CANDIDATE + 1):
                attempts_used = attempt
                savepoint = session.begin_nested()
                try:
                    # bump temperature slightly per retry to escape a
                    # deterministic empty-body state
                    retry_bump = (attempt - 1) * 0.06
                    candidate = _generate_one_candidate(
                        session,
                        provider=provider,
                        book=book,
                        chapter=chapter,
                        chapter_number=chapter_number,
                        source_version=source_version,
                        brief=brief,
                        quality=quality,
                        foundation_premise=foundation.premise,
                        reader_promise=foundation.reader_promise,
                        template=template,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=min(0.95, temperature + (index - 1) * 0.04 + retry_bump),
                        strategy=strategy,
                        task_id=task.id,
                        candidate_index=index,
                        dry_run=dry_run,
                    )
                    rows.append(candidate)
                    session.flush()
                    savepoint.commit()
                    last_error = None
                    break  # success — exit retry loop
                except StructuredOutputError as candidate_exc:
                    # rollback this attempt's half-written ChapterVersion;
                    # earlier successful candidates and the outer task are
                    # untouched.
                    savepoint.rollback()
                    last_error = candidate_exc
                    # keep retrying up to MAX_ATTEMPTS_PER_CANDIDATE
            if last_error is not None:
                _skip_reasons.append({
                    "candidate_index": index,
                    "error": str(last_error),
                    "attempts": attempts_used,
                })
    except Exception as exc:
        _mark_rebuild_task_failed(
            session,
            task_id=task.id,
            exc=exc,
            payload={"candidate_count": candidate_count},
        )
        raise

    try:
        if not rows:
            task.status = "failed"
            task.output_json = json.dumps({"error": "no rebuild candidates generated"}, ensure_ascii=False)
            session.flush()
            raise ValueError("no rebuild candidates generated")

        best = max(rows, key=lambda row: (_candidate_score(row, session.get(QualityReport, int(row["quality_report_id"]))).rank_tuple, int(row.get("version_id") or 0)))
        incumbent = _best_incumbent_draft(session, chapter_id=chapter.id, exclude_task_id=task.id, exclude_version_id=source_version.id)
        best_version = session.get(ChapterVersion, int(best["version_id"]))
        best_quality = session.get(QualityReport, int(best["quality_report_id"]))
        selected_from_incumbent = _should_restore_incumbent_over_candidate(incumbent=incumbent, candidate=best, candidate_quality=best_quality)
        if selected_from_incumbent:
            best_version = incumbent.version if incumbent else best_version
            best_quality = incumbent.quality if incumbent else best_quality
        selected = ChapterVersion(
            chapter_id=chapter.id,
            version_number=next_version_number(session, chapter.id),
            title=best_version.title if best_version else f"第{chapter_number}章",
            content=best_version.content if best_version else "",
            status="needs_revision",
            source=(
                f"rebuild_candidate_incumbent_restore:v{best_version.id}"
                if selected_from_incumbent and best_version
                else f"rebuild_candidate_selected:v{best.get('version_id')}"
            ),
        )
        session.add(selected)
        session.flush()
        report_data = json.loads(best_quality.report) if best_quality else {}
        if selected_from_incumbent and best_version:
            report_data["selected_from_incumbent_version_id"] = best_version.id
            report_data["rejected_best_candidate_version_id"] = best.get("version_id")
            report_data["rejected_best_candidate_score"] = best.get("score")
            report_data["selection_reason"] = "incumbent_ranked_higher_than_candidates"
        else:
            report_data["selected_from_candidate_version_id"] = best.get("version_id")
            report_data["selection_score"] = _candidate_score(best, best_quality).__dict__
        report_data["rebuild_candidate_task_id"] = task.id
        for active in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")):
            active.status = "superseded"
        # Change C part 2 (2026-07-02): for freshly rebuilt candidates re-run
        # full review_chapter on the selected version so it goes through LLM
        # chief editor + editorial_gate + reading_assessment. Previously we
        # copied best_quality.report into a hand-crafted QualityReport, which
        # meant tier=None + llm_review=None in the stored report and Change C's
        # LLM-override could never fire, keeping the planner routing selected
        # drafts back through another expensive rebuild loop.
        #
        # Incumbent restore keeps the old copy-quality path: the incumbent
        # already passed a full review previously and its stored quality
        # (including LLM review results and reading_assessment) is authoritative;
        # re-running review would waste tokens and could destabilize the score.
        if selected_from_incumbent:
            selected_quality = QualityReport(
                chapter_version_id=selected.id,
                score=incumbent.score if incumbent else int(best.get("score") or 0),
                passed=incumbent.passed if incumbent else bool(best.get("passed")),
                report=json.dumps(report_data, ensure_ascii=False),
            )
            session.add(selected_quality)
            session.flush()
            maybe_apply_reading_assessment(session, book_id=book_id, chapter_number=chapter_number, quality=selected_quality)
        else:
            from app.services.production_reviewing import review_chapter as _review_selected
            selected_quality = _review_selected(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                llm_review=True,
                review_dry_run=dry_run,
                auto_revision_brief=False,
            )
            # Merge candidate-selection metadata into the freshly generated report.
            try:
                fresh_report = json.loads(selected_quality.report or "{}")
            except Exception:
                fresh_report = {}
            for key, value in report_data.items():
                if key.startswith("selected_") or key.startswith("rejected_") or key in {"selection_reason", "selection_score", "rebuild_candidate_task_id"}:
                    fresh_report[key] = value
            selected_quality.report = json.dumps(fresh_report, ensure_ascii=False)
            session.flush()
        task.status = "completed"
        task.output_json = json.dumps(
            {
                "source_version_id": source_version.id,
                "selected_version_id": selected.id,
                "selected_candidate_version_id": None if selected_from_incumbent else best.get("version_id"),
                "selected_incumbent_version_id": best_version.id if selected_from_incumbent and best_version else None,
                "selection_reason": "incumbent_ranked_higher_than_candidates" if selected_from_incumbent else "best_ranked_candidate",
                "selected_score": selected_quality.score,
                "selected_passed": selected_quality.passed,
                "best_candidate_score": best.get("score"),
                "candidates": rows,
                "skipped_candidates": _skip_reasons,
            },
            ensure_ascii=False,
        )
        session.flush()
    except Exception as exc:
        _mark_rebuild_task_failed(
            session,
            task_id=task.id,
            exc=exc,
            payload={"candidate_count": candidate_count, "generated_candidates": rows},
        )
        raise
    return RebuildCandidateResult(
        task_id=task.id,
        selected_version_id=selected.id,
        selected_score=int(selected_quality.score or 0),
        candidate_count=len(rows),
    )


def _mark_rebuild_task_failed(session: Session, *, task_id: int, exc: Exception, payload: dict) -> None:
    session.rollback()
    task = session.get(GenerationTask, task_id)
    if not task:
        return
    task.status = "failed"
    task.output_json = json.dumps(
        {
            "error_type": type(exc).__name__,
            "error": str(exc),
            **payload,
        },
        ensure_ascii=False,
    )
    session.flush()
    session.commit()


def _best_incumbent_draft(
    session: Session,
    *,
    chapter_id: int,
    exclude_task_id: int | None = None,
    exclude_version_id: int | None = None,
) -> IncumbentDraft | None:
    rows: list[IncumbentDraft] = []
    excluded_candidate_prefix = f"rebuild_candidate:{exclude_task_id}:" if exclude_task_id else ""
    for version in session.scalars(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter_id).order_by(ChapterVersion.id.desc())):
        source = str(version.source or "")
        if exclude_version_id and version.id == exclude_version_id:
            continue
        if excluded_candidate_prefix and source.startswith(excluded_candidate_prefix):
            continue
        if version.status == "candidate":
            continue
        quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == version.id)
            .order_by(QualityReport.id.desc())
        )
        if not quality or quality.score is None:
            continue
        rows.append(
            IncumbentDraft(
                version=version,
                quality=quality,
                score=int(quality.score or 0),
                passed=bool(quality.passed),
            )
        )
    if not rows:
        return None
    return max(rows, key=lambda row: (row.score, int(row.passed), row.version.id))


def _should_restore_incumbent_over_candidate(
    *,
    incumbent: IncumbentDraft | None,
    candidate: dict,
    candidate_quality: QualityReport | None,
) -> bool:
    if not incumbent:
        return False
    candidate_score = _candidate_score(candidate, candidate_quality)
    incumbent_candidate = {"score": incumbent.score, "passed": incumbent.passed, "strategy": {"name": "incumbent"}}
    incumbent_score = _candidate_score(incumbent_candidate, incumbent.quality)
    return incumbent_score.rank_tuple > candidate_score.rank_tuple


def _candidate_score(candidate: dict, quality: QualityReport | None) -> CandidateScore:
    blockers = _quality_blockers(quality)
    report = _quality_report_data(quality)
    strategy = candidate.get("strategy") if isinstance(candidate.get("strategy"), dict) else {}
    contract_preservation = _marker_score(report, ("revision_contract_preserved", "protected_rebuild_constraints", "修订方向", "保留"))
    sample_adoption_preservation = _marker_score(report, ("sample_adoption", "小样", "本章已采用小样方向"))
    structural_divergence = _strategy_divergence_score(strategy)
    readability_floor = min(100, int(candidate.get("score") or 0)) if not blockers else max(0, int(candidate.get("score") or 0) - len(blockers) * 3)
    canon_consistency = _marker_score(report, ("canon", "continuity", "承接", "设定"))
    return CandidateScore(
        value=int(candidate.get("score") or 0),
        passed=bool(candidate.get("passed")),
        blocker_count=len(blockers),
        contract_preservation=contract_preservation,
        sample_adoption_preservation=sample_adoption_preservation,
        structural_divergence=structural_divergence,
        readability_floor=readability_floor,
        canon_consistency=canon_consistency,
    )


def _quality_report_data(quality: QualityReport | None) -> dict:
    if not quality:
        return {}
    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _marker_score(data: dict, markers: tuple[str, ...]) -> int:
    text = json.dumps(data, ensure_ascii=False)
    return sum(1 for marker in markers if marker in text)


def _strategy_divergence_score(strategy: dict) -> int:
    text = "\n".join(str(strategy.get(key, "")) for key in ("name", "opening", "middle", "ending"))
    return min(4, sum(1 for marker in ("压力", "行动", "关系", "异常", "后果", "误判", "交易") if marker in text))


def _quality_blockers(quality: QualityReport | None) -> list[str]:
    if not quality:
        return []
    try:
        data = json.loads(quality.report or "{}")
    except json.JSONDecodeError:
        return []
    rows = [str(item) for item in data.get("issues") or []]
    assessment = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
    rows.extend(str(item) for item in assessment.get("blockers") or [])
    return [item for item in rows if item]


def _generate_one_candidate(
    session: Session,
    *,
    provider,
    book: Book,
    chapter: Chapter,
    chapter_number: int,
    source_version: ChapterVersion,
    brief: ChapterBrief,
    quality: QualityReport | None,
    foundation_premise: str,
    reader_promise: str,
    template,
    model: str,
    max_tokens: int,
    temperature: float,
    strategy: dict,
    task_id: int,
    candidate_index: int,
    dry_run: bool,
) -> dict:
    strategy_text = _strategy_text(strategy)
    protected_constraints = _protected_rebuild_constraints(brief)
    required_beats = "\n".join([brief.required_beats or "", strategy_text])
    constraints = "\n".join(
        [
            brief.constraints or "",
            "revision_mode:rewrite",
            "候选重建：本候选必须和其他候选采用不同开篇压力、人物互动和章末副作用；重建的是无效写法，不是清空用户修订意图。",
            protected_constraints,
        ]
    )
    packet = build_chapter_production_packet(
        session,
        book=book,
        chapter_number=chapter_number,
        goal=brief.goal,
        required_beats=required_beats,
        constraints=constraints,
        mode="fresh",
        revision_goal=brief.goal,
        revision_required_beats=required_beats,
        revision_constraints=constraints,
        quality_report=quality.report if quality else None,
        previous_content=source_version.content,
        revision_context_mode="fresh",
        fresh_rewrite=True,
        rewrite_mode=True,
        chapter_id=chapter.id,
        chapter_brief_id=brief.id,
    )
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
        premise=foundation_premise,
        reader_promise=reader_promise,
    )
    response = provider.generate(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    draft = parse_or_repair_draft_output(
        provider,
        response_text=response.text,
        original_prompt=prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        task_label=f"候选重建{candidate_index}",
    )
    min_chars = packet.blueprint.target_min_chars
    draft, length_repair = expand_short_draft_output(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        task_label=f"候选重建{candidate_index}",
    )
    draft, unit_flow_repair = repair_humanized_unit_flow(
        provider,
        draft=draft,
        original_prompt=prompt,
        min_chars=min_chars,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        task_label=f"候选重建{candidate_index}",
    )
    # Sprint 2 P1-3: guard against empty / near-empty candidate content.
    # Observed on book=3 Ch8 (v639) / Ch9 (v665): LLM occasionally returns a
    # syntactically-valid but empty-body draft (title fine, content=""). The
    # candidate was persisted with content_len=0 and evaluate_chapter scored
    # it 30 (basic_publishability floor). When such a candidate becomes the
    # selector's best, the selected version inherits the empty content and
    # the 30-score outlier destroys plateau_stop's drift window (Ch8/9 could
    # not converge because scores swung 30→77 across rounds).
    #
    # Raise instead of persisting — the outer loop already treats candidate
    # generation exceptions as fatal (rebuild task marked failed, next
    # planner round issues a fresh rebuild).  Losing one candidate is
    # strictly better than poisoning the plateau window.
    if not (draft.content or "").strip() or chinese_chars(draft.content) < 300:
        raise StructuredOutputError(
            f"rebuild candidate {candidate_index} produced empty/near-empty draft "
            f"(content_chars={chinese_chars(draft.content or '')}); refusing to persist"
        )
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=next_version_number(session, chapter.id),
        title=draft.title,
        content=draft.content,
        status="candidate",
        source=f"rebuild_candidate:{task_id}:{candidate_index}",
    )
    session.add(version)
    session.flush()
    canon_context, _ = format_canon_context(session, book_id=book.id, chapter_number=chapter_number)
    result = evaluate_chapter(
        draft.content,
        min_chars=min_chars,
        max_chars=packet.blueprint.target_max_chars
        or extract_max_chars(brief.goal, required_beats, packet.constraints, default=4500),
        goal=brief.goal,
        required_beats=required_beats,
        constraints=packet.constraints,
        canon_context=canon_context,
    )
    report_data = json.loads(result.report)
    report_data["production_failure_classification"] = classify_quality_failure(report_data)
    # Sprint 2 P0-1 stage-5: apply chapter_type_gate to candidates so the
    # selector uses the same passed/score that review_chapter would later
    # compute on the selected version.  Previously candidates were scored
    # without the gate — selector could pick a candidate with passed=True
    # that immediately flipped to passed=False after the selected-version
    # re-review ran enrich_quality_report_with_optimization(enforce_gate=True),
    # stranding the chapter (observed on book=3 Ch8: v603 candidate score
    # 78/passed=True → v605 selected re-review score 75/passed=False, same
    # content, brief_coverage=47<60).
    report_data.setdefault("passed", bool(result.passed))
    report_data = enrich_quality_report_with_optimization(
        report_data,
        chapter_number=chapter_number,
        goal=brief.goal or "",
        required_beats=required_beats,
        constraints=packet.constraints,
        enforce_gate=not dry_run,
    )
    report_data["rebuild_candidate"] = {
        "task_id": task_id,
        "index": candidate_index,
        "strategy": strategy,
        "length_repair": length_repair,
        "unit_flow_repair": unit_flow_repair,
    }
    # Sprint 2 P0-1 stage-5: gate-adjusted passed becomes authoritative for
    # both the DB row and the selector-facing return payload.
    gate_passed = bool(report_data.get("passed", result.passed))
    gate_score = int(report_data.get("score") or result.score)
    quality = QualityReport(
        chapter_version_id=version.id,
        score=gate_score,
        passed=gate_passed,
        report=json.dumps(report_data, ensure_ascii=False),
    )
    session.add(quality)
    session.flush()
    return {
        "index": candidate_index,
        "version_id": version.id,
        "quality_report_id": quality.id,
        "score": gate_score,
        "passed": gate_passed,
        "strategy": strategy,
        "provider": response.provider,
        "model": response.model,
        **llm_usage_payload(response, prompt=prompt),
    }


def _candidate_strategies(chapter_number: int) -> list[dict]:
    if chapter_number == 1:
        return [
            {
                "name": "关系盘问破局",
                "opening": "第一句从门外逼问切入，但必须在前500字给盘问者明确私心、误判和可交易筹码。",
                "middle": "桥段复刻靠主角观察人物欲望并主动换取临时信任，不靠面板解题。",
                "ending": "章末副作用落在现实身体失控和社交尴尬，不用机构关注。",
            },
            {
                "name": "利益交换破局",
                "opening": "第一句从交易催促或赔偿争执切入，让主角先被迫解决一个能立刻验收的小问题。",
                "middle": "桥段复刻通过一次具体劳动、押送、验货或救场完成，NPC因行动结果改变态度。",
                "ending": "章末把奖励同步成一个具体身体动作，并带来现实误会。",
            },
            {
                "name": "误判社死破局",
                "opening": "第一句从主角一句说错话或动作露怯引发现场误判切入，压力来自人群反应。",
                "middle": "主角靠补救、嘴硬和观察细节把误判转成有用身份，不新增复杂势力。",
                "ending": "章末现实副作用必须具体到室友、宿舍物件或身体动作的尴尬后果。",
            },
            {
                "name": "异常细节破局",
                "opening": "第一句从一个可见异常物件或身体反应切入，并立即引出外部盘问。",
                "middle": "主角用异常细节推断江湖规矩，完成一次有代价的主动选择。",
                "ending": "章末钩子来自这个异常在现实中复现。",
            },
        ]
    return [
        {"name": "承接后果", "opening": "第一句承接上一章直接后果。", "middle": "用行动解决本章小目标。", "ending": "章末产生新代价。"},
        {"name": "关系压力", "opening": "第一句从人物关系压力切入。", "middle": "通过对话和选择推进。", "ending": "章末关系反转。"},
        {"name": "异常线索", "opening": "第一句从异常线索切入。", "middle": "用调查和误判推进。", "ending": "章末发现新问题。"},
    ]


def _strategy_text(strategy: dict) -> str:
    return "\n".join(
        [
            f"候选策略：{strategy.get('name')}",
            f"开篇策略：{strategy.get('opening')}",
            f"中段策略：{strategy.get('middle')}",
            f"章末策略：{strategy.get('ending')}",
        ]
    )


def _protected_rebuild_constraints(brief: ChapterBrief) -> str:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    protections: list[str] = [
        "候选重建保护输入：必须继承当前有效修订合同中的用户修订建议、已采用小样方向、跨章承接要求和明确禁止项。",
        "如果当前合同要求只修首屏衔接、保留主线、保留茶棚遇同行或保留既有主事件，候选不得改写成无关新章。",
        "允许更换无效段落顺序、对白推进和局部场景写法，但不得丢失用户明确指出的问题、保留项和禁止项。",
        "self_check 必须说明候选如何回应当前修订方向，而不是只说明生成了新结构。",
    ]
    preserved_markers = (
        "修订方向:",
        "必须在开头",
        "只做定向",
        "保留第",
        "保留当前",
        "不要推翻",
        "不推翻",
        "本章已采用小样方向",
        "小样名：",
        "后续推进骨架",
        "必须承接上一章",
        "禁止：",
        "不要出现",
        "不新增",
    )
    for line in text.splitlines():
        item = line.strip()
        if not item:
            continue
        if any(marker in item for marker in preserved_markers):
            protections.append(item)
    return "\n".join(protections)
