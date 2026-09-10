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
from app.services.story_bible_logic_gate import evaluate_story_bible_logic
from app.services.world_logic import evaluate_world_logic, game_world_meta_leak_reasons
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
    candidate_count: int = 1,
    dry_run: bool = False,
    existing_task_id: int | None = None,
) -> RebuildCandidateResult:
    candidate_count = max(1, min(3, int(candidate_count or 1)))
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
        # 幂等检查：如果最新非 discarded 是 candidate/approved · 说明前次 rebuild 已产出 · 视为成功而非错误
        if source_version and source_version.status in ("candidate", "approved", "published_review"):
            raise ValueError(
                f"rebuild_already_completed: latest cv#{source_version.id} status={source_version.status}"
            )
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
    template = get_prompt_template(session, name="revise_chapter", version="v5")
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

    # === Sprint 3 P0 lock-hold fix (2026-07-08) ==================================
    # Commit the task setup row eagerly so the SQLite write lock is released
    # BEFORE we spend 2–4 minutes / candidate inside the LLM stack. Previously
    # the outer session_scope() kept a write tx open across all 3 candidates ×
    # 4 LLM calls (≈12 min), starving audit approval / feishu handler / any
    # other writer for the same duration (observed on Ch32 task#1681 – held
    # for 13h until manually killed).
    #
    # Committing here also happens to give crash-recovery a durable 'running'
    # row (the exact goal the earlier P2-Ch27 fix punted on to keep the
    # legacy begin_nested() path alive). Since we no longer use SAVEPOINT
    # (SQLAlchemy 2.x auto-begins the next tx on the following DML), this is
    # now safe.
    task_id = task.id
    session.commit()
    # =============================================================================

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
        strategies = _rotated_candidate_strategies(session, book_id=book_id, chapter_number=chapter_number)
        for index, strategy in enumerate(strategies[:candidate_count], start=1):
            attempts_used = 0
            last_error: Exception | None = None
            for attempt in range(1, MAX_ATTEMPTS_PER_CANDIDATE + 1):
                attempts_used = attempt
                # bump temperature slightly per retry to escape a
                # deterministic empty-body state
                retry_bump = (attempt - 1) * 0.06
                attempt_temperature = min(0.95, temperature + (index - 1) * 0.04 + retry_bump)

                # --- Phase A: build prompt (short DB tx: packet + unit plan writes) ---
                try:
                    prep = _prepare_candidate_prompt(
                        session,
                        book=book,
                        chapter=chapter,
                        chapter_number=chapter_number,
                        source_version=source_version,
                        brief=brief,
                        quality=quality,
                        foundation_premise=foundation.premise,
                        reader_promise=foundation.reader_promise,
                        template=template,
                        strategy=strategy,
                    )
                    # Release SQLite write lock BEFORE 4 LLM calls run.
                    session.commit()
                except StructuredOutputError as prep_exc:
                    session.rollback()
                    last_error = prep_exc
                    continue
                except Exception:
                    session.rollback()
                    raise

                # --- Phase B: 4 LLM calls (no session activity → no write lock held) ---
                try:
                    llm_out = _run_candidate_llm(
                        provider,
                        prompt=prep["prompt"],
                        min_chars=prep["min_chars"],
                        max_tokens=max_tokens,
                        temperature=attempt_temperature,
                        model=model,
                        candidate_index=index,
                        dry_run=dry_run,
                    )
                except StructuredOutputError as llm_exc:
                    last_error = llm_exc
                    continue  # retry this candidate slot

                # --- Phase C: persist candidate (short DB tx: version + quality) ---
                try:
                    candidate = _persist_candidate(
                        session,
                        book=book,
                        chapter=chapter,
                        chapter_number=chapter_number,
                        brief=brief,
                        packet=prep["packet"],
                        required_beats=prep["required_beats"],
                        draft=llm_out["draft"],
                        length_repair=llm_out["length_repair"],
                        unit_flow_repair=llm_out["unit_flow_repair"],
                        response=llm_out["response"],
                        prompt=prep["prompt"],
                        strategy=strategy,
                        task_id=task_id,
                        candidate_index=index,
                        dry_run=dry_run,
                    )
                    rows.append(candidate)
                    session.commit()
                    last_error = None
                    break  # success — exit retry loop
                except StructuredOutputError as persist_exc:
                    session.rollback()
                    last_error = persist_exc
                    # keep retrying
                except Exception:
                    session.rollback()
                    raise
            if last_error is not None:
                _skip_reasons.append({
                    "candidate_index": index,
                    "error": str(last_error),
                    "attempts": attempts_used,
                })
    except Exception as exc:
        _mark_rebuild_task_failed(
            session,
            task_id=task_id,
            exc=exc,
            payload={"candidate_count": candidate_count},
        )
        raise

    # --- Selection phase (short DB tx: build final version + re-review) ---
    # Re-attach ORM objects since our per-candidate commits expired them.
    task = session.get(GenerationTask, task_id)
    chapter = session.get(Chapter, chapter.id)
    source_version = session.get(ChapterVersion, source_version.id)

    try:
        if not rows:
            task.status = "failed"
            task.output_json = json.dumps({"error": "no rebuild candidates generated"}, ensure_ascii=False)
            session.commit()
            raise ValueError("no rebuild candidates generated")

        best = max(rows, key=lambda row: (_candidate_score(row, session.get(QualityReport, int(row["quality_report_id"]))).rank_tuple, int(row.get("version_id") or 0)))
        best_version = session.get(ChapterVersion, int(best["version_id"]))
        best_quality = session.get(QualityReport, int(best["quality_report_id"]))
        incumbent = (
            None
            if _active_rebuild_contract_forbids_incumbent_restore(brief)
            else _best_incumbent_draft(
                session,
                chapter_id=chapter.id,
                exclude_task_id=task_id,
                exclude_version_id=source_version.id,
            )
        )
        selected_from_incumbent = _should_restore_incumbent_over_candidate(
            session=session,
            book_id=book_id,
            chapter_number=chapter_number,
            incumbent=incumbent,
            candidate=best,
            candidate_quality=best_quality,
        )
        if not bool(best.get("passed")) and not selected_from_incumbent:
            task.status = "completed"
            task.output_json = json.dumps(
                {
                    "source_version_id": source_version.id,
                    "selected_version_id": best_version.id if best_version else None,
                    "selected_candidate_version_id": best.get("version_id"),
                    "selection_reason": "best_failed_candidate_retained",
                    "selected_score": int(best_quality.score or 0) if best_quality else int(best.get("score") or 0),
                    "selected_passed": False,
                    "candidates": rows,
                    "skipped_candidates": _skip_reasons,
                    "needs_revision": True,
                },
                ensure_ascii=False,
            )
            if best_quality is not None:
                maybe_apply_reading_assessment(session, book_id=book_id, chapter_number=chapter_number, quality=best_quality)
            session.commit()
            return RebuildCandidateResult(
                task_id=task_id,
                selected_version_id=best_version.id if best_version else int(best.get("version_id") or 0),
                selected_score=int(best_quality.score or 0) if best_quality else int(best.get("score") or 0),
                candidate_count=len(rows),
            )
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
        report_data["rebuild_candidate_task_id"] = task_id
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
            task_id=task_id,
            exc=exc,
            payload={"candidate_count": candidate_count, "generated_candidates": rows},
        )
        raise
    return RebuildCandidateResult(
        task_id=task_id,
        selected_version_id=selected.id,
        selected_score=int(selected_quality.score or 0),
        candidate_count=len(rows),
    )


def _mark_rebuild_task_failed(session: Session, *, task_id: int, exc: Exception, payload: dict) -> None:
    # Sprint 3 P0 lock-hold fix (2026-07-08): under the new multi-commit
    # scheme the task row is already durable when this handler runs
    # (committed right after task setup). A ``session.rollback()`` here now
    # only discards uncommitted in-flight state and does NOT erase the task.
    # We then flip status='failed' + persist so worker crash between the
    # exception and the outer llm_queue re-classification cannot leave a
    # ghost 'running' row.
    try:
        session.rollback()
    except Exception:
        pass
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


def _active_rebuild_contract_forbids_incumbent_restore(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return any(
        marker in text
        for marker in (
            "用户作者样稿约束",
            "revision_mode:fresh",
            "旧稿只作为失败反例",
            "旧稿只作为失败参照",
            "不得沿用失败读感模板",
            "不得照抄段落顺序",
            "标题：旧盔",
        )
    )


def _should_restore_incumbent_over_candidate(
    *,
    session: Session,
    book_id: int,
    chapter_number: int,
    incumbent: IncumbentDraft | None,
    candidate: dict,
    candidate_quality: QualityReport | None,
) -> bool:
    if not incumbent:
        return False
    if _incumbent_current_world_logic_blocked(
        session,
        book_id=book_id,
        chapter_number=chapter_number,
        incumbent=incumbent,
    ):
        return False
    candidate_score = _candidate_score(candidate, candidate_quality)
    incumbent_candidate = {"score": incumbent.score, "passed": incumbent.passed, "strategy": {"name": "incumbent"}}
    incumbent_score = _candidate_score(incumbent_candidate, incumbent.quality)
    return incumbent_score.rank_tuple > candidate_score.rank_tuple


def _incumbent_current_world_logic_blocked(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    incumbent: IncumbentDraft,
) -> bool:
    content = getattr(incumbent.version, "content", None)
    if not content:
        return False
    canon_context, _ = format_canon_context(session, book_id=book_id, chapter_number=chapter_number)
    story_bible_logic = evaluate_story_bible_logic(content, canon_context=canon_context)
    if not story_bible_logic.passed:
        return True
    report = evaluate_world_logic(content)
    if report.score < 60:
        return True
    if report.checks.get("player_layer_intrusion", 100) < 60:
        return True
    if report.checks.get("character_knowledge_boundary", 100) < 60:
        return True
    return False


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


# =============================================================================
# Sprint 3 P0 lock-hold fix (2026-07-08)
#
# Previously ``_generate_one_candidate`` did everything (packet + prompt +
# 4×LLM + persist) inside one SAVEPOINT.  We split it into 3 phases so the
# SQLite write lock is released around the LLM calls:
#
#   Phase A  _prepare_candidate_prompt  – short DB tx (packet + unit plan)
#   Phase B  _run_candidate_llm         – NO session use, 4× LLM calls
#   Phase C  _persist_candidate         – short DB tx (version + quality)
#
# The caller commits between A→B and B→C, so during the 2–4 min LLM burn
# no other writer is blocked.
# =============================================================================


def _prepare_candidate_prompt(
    session: Session,
    *,
    book: Book,
    chapter: Chapter,
    chapter_number: int,
    source_version: ChapterVersion,
    brief: ChapterBrief,
    quality: QualityReport | None,
    foundation_premise: str,
    reader_promise: str,
    template,
    strategy: dict,
) -> dict:
    """Phase A: build packet + rendered prompt.  Session tx stays open —
    caller commits immediately after this returns so the write lock is
    released before Phase B's LLM calls."""

    strategy_text = _strategy_text(strategy)
    protected_constraints = _protected_rebuild_constraints(brief)
    hard_constraints = _rebuild_world_logic_prompt_constraints(book=book, brief=brief, source_version=source_version)
    required_beats = "\n".join([brief.required_beats or "", strategy_text, hard_constraints])
    constraints = "\n".join(
        [
            brief.constraints or "",
            "revision_mode:rewrite",
            "候选重建：本候选必须和其他候选采用不同开篇压力、人物互动和章末副作用；重建的是无效写法，不是清空用户修订意图。",
            hard_constraints,
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
    if len(prompt) > 9000 or "用户作者样稿约束" in constraints:
        prompt = _compact_rebuild_prompt(
            book=book,
            chapter_number=chapter_number,
            source_version=source_version,
            brief=brief,
            packet=packet,
            quality=quality,
            required_beats=required_beats,
            constraints=constraints,
            foundation_premise=foundation_premise,
            reader_promise=reader_promise,
            strategy_text=strategy_text,
        )
    return {
        "packet": packet,
        "prompt": prompt,
        "min_chars": packet.blueprint.target_min_chars,
        "required_beats": required_beats,
        "protected_constraints": protected_constraints,
    }


def _compact_rebuild_prompt(
    *,
    book: Book,
    chapter_number: int,
    source_version: ChapterVersion,
    brief: ChapterBrief,
    packet,
    quality: QualityReport | None,
    required_beats: str,
    constraints: str,
    foundation_premise: str,
    reader_promise: str,
    strategy_text: str,
) -> str:
    """Build a short formal rebuild prompt for long/hang-prone contracts."""

    values = getattr(packet, "prompt_values", {}) or {}
    director = _clip(values.get("director_sheet", ""), 1800)
    canon = _clip(values.get("canon_context", ""), 1800)
    author_preferences = _clip(values.get("author_preferences", ""), 900)
    previous_context = _clip(values.get("previous_chapter_context", ""), 700)
    quality_summary = _quality_summary_for_compact_prompt(quality)
    source_summary = (
        f"旧稿 v{source_version.version_number} title={source_version.title!r} "
        f"status={source_version.status} chinese_chars={chinese_chars(source_version.content or '')}。"
        "旧稿只作为失败参照，不得照抄段落顺序、句式或章末处理。"
    )
    parts = [
        "你是受控网文生产链路里的章节重建工位。请严格输出 JSON 对象，不要 Markdown，不要解释。",
        'JSON schema: {"title": "...", "content": "...", "self_check": ["..."], "used_brief_points": ["..."]}',
        "",
        f"作品：{book.title}",
        f"题材：{book.genre}",
        f"章节：第{chapter_number}章",
        "标题硬要求：本章标题用《旧盔》，短、有指向，不要直白剧透。",
        "",
        "本章核心任务：",
        _clip(brief.goal or "", 700),
        "",
        "最新设定裁决/Canon：",
        canon,
        "",
        "故事地基：",
        _clip(foundation_premise, 900),
        "",
        "读者承诺：",
        _clip(reader_promise, 700),
        "",
        "导演单/生产骨架：",
        director,
        "",
        "本轮必须写进正文的节拍：",
        _clip(required_beats, 2200),
        "",
        "硬约束：",
        _clip(constraints, 1600),
        "",
        "候选策略：",
        _clip(strategy_text, 500),
        "",
        _author_sample_anchor_prompt_block(brief),
        "",
        "旧稿/质检摘要：",
        source_summary,
        quality_summary,
        "",
        "作者样稿方向：",
        "1. 现实侧可以写二手游戏头盔、内测/代练/登录流程作为赚钱接单动机；不要写成主角兴奋玩游戏。",
        "2. 买旧盔目的必须在前500字内讲清：房租/饭钱压力，五十块旧盔，底层接单赚钱。",
        "3. 头盔只是触发设备；真正入口是雪屏/数据异常把沈渡整个人物理拽入写实仙侠现场。",
        "4. 用沈渡当下感知、误判、身体反应、再确认承载设定；少用硬解释和僵硬“不是X，是Y”。",
        "5. 老板对白要像市井活人，例如“想要就五十拿去，到手不退不换啊”“坏了可别拿回来找我”。",
        "6. 后半段写山地真实痛感、人烟痕迹、药味、敲门求伤药；章末留下“这个月你是第二个”的压力。",
        "",
        "正文硬指标：1800-2500 中文字符；22-32 个自然短段；第三人称有限 POV；不要输出单元标题。",
        "正文禁区：普通VR游玩、意识上传、虚拟接入、赛博空间、玩家/NPC/论坛口吻刷屏、现实修为外溢、修真高阶提前下场。",
        "进入当前仙侠现场后，不要再用内测/玩家/NPC/论坛解释世界内人物和规则。",
        "",
        "现在直接输出 JSON。",
    ]
    return "\n".join(part for part in parts if part is not None)


def _author_sample_anchor_prompt_block(brief: ChapterBrief) -> str:
    if not _brief_uses_author_sample_anchor(brief):
        return ""
    return "\n".join(
        [
            "用户样稿不可漂移锚点：",
            "- 本章事件链必须是：旧盔/接单赚钱 -> 雪屏/数据异常 -> 整个人物理坠落山地 -> 药味/窝棚/人烟 -> 敲门求伤药/拿活抵 -> 章末“第二个”压力。",
            "- 章末接应人物必须承担“药/伤/交易/第二个”的压力，不得改写成拜师、投师、门派入门、道童盘问或身份牌验明。",
            "- 必须出现并服务剧情：旧盔、雪花、星星、血、药味、伤药、第二个。",
            "- 不得出现：道观、道童、清虚观、铁牌、身份牌、外门执事、投师、拜师、收徒。",
            "- 可以换句式和细节，但不能替换上述事件功能；否则候选稿直接作废。",
        ]
    )


def _quality_summary_for_compact_prompt(quality: QualityReport | None) -> str:
    if quality is None:
        return "无质检摘要。"
    try:
        data = json.loads(quality.report or "{}")
    except Exception:
        data = {}
    bits = [
        f"latest_quality_report_id={quality.id}",
        f"score={quality.score}",
        f"passed={quality.passed}",
    ]
    for key in ("verdict", "reading_assessment", "final_verdict"):
        value = data.get(key)
        if value:
            bits.append(f"{key}={_clip(str(value), 160)}")
    blockers = data.get("blockers") or data.get("issues") or []
    if blockers:
        bits.append("blockers=" + _clip("；".join(map(str, blockers[:6])), 500))
    return "；".join(bits)


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    return value[:limit].rstrip() + f"\n...[clipped {len(value) - limit} chars]"


def _run_candidate_llm(
    provider,
    *,
    prompt: str,
    min_chars: int,
    max_tokens: int,
    temperature: float,
    model: str,
    candidate_index: int,
    dry_run: bool,
) -> dict:
    """Phase B: 4 LLM calls.  Deliberately takes NO session — this runs
    with zero SQLite lock held so audit approval / feishu handler / other
    writers can proceed while the model is thinking."""

    response = provider.generate(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        model=model,
        # Kimi-style thinking models can hang at response headers when forced
        # into provider JSON mode on long creative prompts. Keep the prompt's
        # strict JSON instruction and let parse_or_repair_draft_output handle
        # malformed text instead of requiring transport-level JSON mode.
        response_format=None,
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
    # 候选重建阶段只负责产出可评估候选。单元流修复会额外触发多次 LLM
    # 局部返修，曾导致候选在落库前长时间卡住；这里延后到质检/下一步路由处理。
    unit_flow_repair = {
        "attempted": False,
        "accepted": False,
        "deferred": True,
        "reason": "deferred_for_rebuild_candidate_persistence",
    }
    # Sprint 2 P1-3 guard (unchanged intent, moved to Phase B): reject
    # empty/near-empty drafts before we ever touch the DB.  The caller
    # treats StructuredOutputError as retryable.
    if not (draft.content or "").strip() or chinese_chars(draft.content) < 300:
        raise StructuredOutputError(
            f"rebuild candidate {candidate_index} produced empty/near-empty draft "
            f"(content_chars={chinese_chars(draft.content or '')}); refusing to persist"
        )
    anchor_rejection = _author_sample_anchor_rejection(prompt, draft.content or "")
    if anchor_rejection:
        raise StructuredOutputError(
            f"rebuild candidate {candidate_index} violates author sample anchors before persistence: {anchor_rejection}"
        )
    leak_reasons = game_world_meta_leak_reasons(draft.content or "")
    if leak_reasons:
        draft, meta_leak_repair = _repair_game_world_meta_leak(
            provider,
            draft=draft,
            original_prompt=prompt,
            leak_reasons=leak_reasons,
            max_tokens=max_tokens,
            temperature=temperature,
            model=model,
            candidate_index=candidate_index,
            dry_run=dry_run,
        )
        length_repair["meta_leak_repair"] = meta_leak_repair
        if not (draft.content or "").strip() or chinese_chars(draft.content) < 300:
            raise StructuredOutputError(
                f"rebuild candidate {candidate_index} produced near-empty draft after game-world meta repair "
                f"(content_chars={chinese_chars(draft.content or '')}); refusing to persist"
            )
        repaired_leak_reasons = game_world_meta_leak_reasons(draft.content or "")
        if repaired_leak_reasons:
            raise StructuredOutputError(
                "rebuild candidate contains game-world meta leakage before persistence after repair: "
                + "；".join(repaired_leak_reasons[:4])
            )
        anchor_rejection = _author_sample_anchor_rejection(prompt, draft.content or "")
        if anchor_rejection:
            raise StructuredOutputError(
                f"rebuild candidate {candidate_index} violates author sample anchors after repair: {anchor_rejection}"
            )
    return {
        "draft": draft,
        "length_repair": length_repair,
        "unit_flow_repair": unit_flow_repair,
        "response": response,
    }


def _brief_uses_author_sample_anchor(brief: ChapterBrief) -> bool:
    text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
    return "用户作者样稿约束" in text or ("标题：旧盔" in text and "第二个" in text and "伤药" in text)


def _author_sample_anchor_rejection(prompt: str, content: str) -> str:
    if "用户样稿不可漂移锚点" not in (prompt or ""):
        return ""
    text = content or ""
    forbidden = [term for term in ("道观", "道童", "清虚观", "铁牌", "身份牌", "外门执事", "投师", "拜师", "收徒") if term in text]
    if forbidden:
        return "forbidden_storyline:" + ",".join(forbidden[:6])
    required_groups = [
        ("旧盔", ("旧盔", "二手头盔", "头盔")),
        ("雪屏", ("雪花", "雪屏", "满屏的雪")),
        ("物理坠落", ("摔", "掉", "坠", "滚下", "砸上")),
        ("山地真实感", ("星星", "山", "血", "疼")),
        ("药味人烟", ("药味", "草药", "窝棚", "灯")),
        ("求伤药", ("伤药", "药", "拿活抵", "什么都干")),
        ("第二个压力", ("第二个", "上一个", "这个月")),
    ]
    missing = [label for label, options in required_groups if not any(option in text for option in options)]
    if missing:
        return "missing_anchor:" + ",".join(missing)
    return ""


def _repair_game_world_meta_leak(
    provider,
    *,
    draft,
    original_prompt: str,
    leak_reasons: list[str],
    max_tokens: int,
    temperature: float,
    model: str,
    candidate_index: int,
    dry_run: bool,
) -> tuple[object, dict]:
    repair_prompt = "\n".join(
        [
            "你是长篇网文重建候选的世界内化修复器。",
            "任务：只修复正文里的玩家层/系统层元概念泄漏，保持原章节事件、人物压力、字数规模和可读节奏。",
            "这不是润色，也不是继续解释规则。把玩家视角解释翻译成江湖现场可见凭据和人物反应。",
            "",
            "硬禁词：系统分配、内测、论坛、玩家、NPC、任务栏、任务面板、界面、系统提示、新手村。",
            "禁写方式：主角不能对道士/门派人物说系统、任务、内测、随机分配、攻略；内心独白也不能用这些词补解释。",
            "替代方式：木牌、拜帖、山门规矩、旧衣着误判、拂尘压迫、捏骨试探、挑水/扫院/入门规矩、江湖话套问。",
            "",
            "坏例：顾晚想说‘系统分配的’，话到嘴边咽回去。",
            "改法：顾晚按住腰间木牌，把半截实话吞回去，只说山门口有人把牌塞给他，清虚观认牌不认人。",
            "坏例：内测期间必须完成一次门派入门任务。",
            "改法：那张雇书只写三日内拜入一门，过期银钱作废，旁的规矩没人肯明说。",
            "坏例：这是界面里唯一能点开的东西。",
            "改法：木牌背面夹着一封皱巴巴的拜帖，火漆还没干，他只能先拿它试探。",
            "",
            "已检测到的问题：",
            "；".join(leak_reasons[:8]),
            "",
            "输出严格 JSON：{\"title\": \"...\", \"content\": \"...\", \"self_check\": [\"...\"], \"used_brief_points\": [\"...\"]}",
            "不得输出说明文字，不得保留任何硬禁词。",
            "",
            "原始生成要求：",
            original_prompt,
            "",
            "待修复候选 JSON：",
            json.dumps(
                {
                    "title": getattr(draft, "title", "") or "",
                    "content": getattr(draft, "content", "") or "",
                    "self_check": getattr(draft, "self_check", []) or [],
                    "used_brief_points": getattr(draft, "used_brief_points", []) or [],
                },
                ensure_ascii=False,
            ),
        ]
    )
    repair_temperature = min(0.35, float(temperature or 0.5))
    response = provider.generate(
        repair_prompt,
        max_tokens=max(max_tokens, 4500),
        temperature=repair_temperature,
        model=model,
        response_format={"type": "json_object"} if not dry_run else None,
    )
    repaired = parse_or_repair_draft_output(
        provider,
        response_text=response.text,
        original_prompt=repair_prompt,
        max_tokens=max(max_tokens, 4500),
        temperature=repair_temperature,
        model=model,
        task_label=f"候选重建{candidate_index}元概念修复",
    )
    repair_meta = {
        "attempted": True,
        "accepted": True,
        "before": leak_reasons[:8],
        **llm_usage_payload(response, prompt=repair_prompt),
    }
    return repaired, repair_meta


def _persist_candidate(
    session: Session,
    *,
    book: Book,
    chapter: Chapter,
    chapter_number: int,
    brief: ChapterBrief,
    packet,
    required_beats: str,
    draft,
    length_repair: dict,
    unit_flow_repair: dict,
    response,
    prompt: str,
    strategy: dict,
    task_id: int,
    candidate_index: int,
    dry_run: bool,
) -> dict:
    """Phase C: write ChapterVersion + QualityReport.  Short DB tx —
    caller commits immediately after this returns."""

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
    from app.services.context_contamination import context_anchor_terms
    from app.services.production_packet import load_previous_hook_keywords
    _anchor_terms = context_anchor_terms(session, book_id=book.id)
    _prev_kws = load_previous_hook_keywords(session, book_id=book.id, chapter_number=chapter_number)
    result = evaluate_chapter(
        draft.content,
        min_chars=packet.blueprint.target_min_chars,
        max_chars=packet.blueprint.target_max_chars
        or extract_max_chars(brief.goal, required_beats, packet.constraints, default=2800),
        goal=brief.goal,
        required_beats=required_beats,
        constraints=packet.constraints,
        canon_context=canon_context,
        authority_terms=_anchor_terms,
        previous_hook_keywords=_prev_kws,
    )
    report_data = json.loads(result.report)
    report_data["production_failure_classification"] = classify_quality_failure(report_data)
    # Sprint 2 P0-1 stage-5: apply chapter_type_gate to candidates so the
    # selector uses the same passed/score that review_chapter would later
    # compute on the selected version.
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
    gate_passed = bool(report_data.get("passed", result.passed))
    gate_score = int(report_data.get("score") or result.score)
    quality_row = QualityReport(
        chapter_version_id=version.id,
        score=gate_score,
        passed=gate_passed,
        report=json.dumps(report_data, ensure_ascii=False),
    )
    session.add(quality_row)
    if not gate_passed:
        version.status = "needs_revision"
    session.flush()
    return {
        "index": candidate_index,
        "version_id": version.id,
        "quality_report_id": quality_row.id,
        "score": gate_score,
        "passed": gate_passed,
        "strategy": strategy,
        "provider": response.provider,
        "model": response.model,
        **llm_usage_payload(response, prompt=prompt),
    }


def _rotated_candidate_strategies(session: Session, *, book_id: int, chapter_number: int) -> list[dict]:
    strategies = _candidate_strategies(chapter_number)
    if len(strategies) <= 1:
        return strategies
    completed = 0
    for task in session.scalars(
        select(GenerationTask).where(
            GenerationTask.book_id == book_id,
            GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES,
            GenerationTask.status == "completed",
        )
    ):
        try:
            data = json.loads(task.input_json or "{}")
        except Exception:
            continue
        if int(data.get("chapter_number") or 0) == chapter_number:
            completed += 1
    offset = completed % len(strategies)
    return [*strategies[offset:], *strategies[:offset]]


def _candidate_strategies(chapter_number: int) -> list[dict]:
    if chapter_number == 1:
        return [
            {
                "name": "关系盘问破局",
                "opening": "第一句从门外逼问切入，但必须在前500字给盘问者明确私心、误判和可交易筹码。",
                "middle": "桥段复刻靠主角观察人物欲望并主动换取临时信任；对白要有试探、急躁和找补，不靠面板解题。",
                "ending": "章末副作用落在现实身体失控和社交尴尬，不用机构关注。",
            },
            {
                "name": "利益交换破局",
                "opening": "第一句从交易催促或赔偿争执切入，让主角先被迫解决一个能立刻验收的小问题。",
                "middle": "桥段复刻通过一次具体劳动、押送、验货或救场完成；对话要围绕利益、怀疑和条件交换，江湖人因行动结果改变态度。",
                "ending": "章末把奖励同步成一个具体身体动作，并带来现实误会。",
            },
            {
                "name": "误判社死破局",
                "opening": "第一句从主角一句说错话或动作露怯引发现场误判切入，压力来自人群反应。",
                "middle": "主角靠补救、嘴硬、半截话和观察细节把误判转成有用身份，不新增复杂势力。",
                "ending": "章末现实副作用必须具体到室友、宿舍物件或身体动作的尴尬后果。",
            },
            {
                "name": "异常细节破局",
                "opening": "第一句从一个可见异常物件或身体反应切入，并立即引出外部盘问。",
                "middle": "主角用异常细节推断江湖规矩，靠自然对白套话，完成一次有代价的主动选择。",
                "ending": "章末钩子来自这个异常在现实中复现。",
            },
        ]
    return [
        {"name": "承接后果", "opening": "第一句承接上一章直接后果。", "middle": "用行动解决本章小目标。", "ending": "章末产生新代价。"},
        {"name": "关系压力", "opening": "第一句从人物关系压力切入。", "middle": "通过对话和选择推进。", "ending": "章末关系反转。"},
        {"name": "异常线索", "opening": "第一句从异常线索切入。", "middle": "用调查和误判推进。", "ending": "章末发现新问题。"},
    ]


def _rebuild_world_logic_prompt_constraints(*, book: Book, brief: ChapterBrief, source_version: ChapterVersion) -> str:
    context = "\n".join([
        str(book.title or ""),
        str(book.genre or ""),
        str(brief.goal or ""),
        str(brief.required_beats or ""),
        str(brief.constraints or ""),
        str(source_version.content or "")[:1800],
    ])
    if not any(marker in context for marker in ("入梦", "清虚观", "网游", "游戏", "内测", "NPC", "玩家", "论坛")):
        return ""
    return "\n".join([
        "候选重建硬禁区：游戏内世界现场零元概念泄漏。",
        "清虚观/山门/拜师/盘问/试炼/门派交涉现场，正文、对白、内心独白都不得出现：内测、论坛、玩家、NPC、新手村、任务栏、任务面板、系统分配我来的、系统不会给你第二家门派。",
        "主角不能向道士、师父、门派人物解释系统、任务、内测、随机分配、攻略或玩家规则；世界内人物也不能接住这些词。",
        "必须改用世界内可见凭据推进：山门规矩、旧木牌/拜帖/衣着误判、道士盘问、拂尘压迫、捏骨试探、挑水/扫院/入门规矩、人物反应和江湖话。",
        "若需要交代内测奖金、论坛信息或现实身份，只能放在入场前/现实侧轻描一笔；进入清虚观现场后立刻切换为世界内认知。",
        "界面/提示若出现须落到主角独处的极少量感知后果；禁止用界面/提示作为开场压力、任务来源、NPC对白依据或破局手段。",
    ])


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
