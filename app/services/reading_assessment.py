from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport, StoryFoundation
from app.services.brief_sanitizer import sanitize_existing_chapter_brief
from app.services.feedback import submit_revision_suggestion
from app.services.production_state import latest_story_brief
from app.services.status_language import editorial_blocker_text, editorial_summary_text
from app.workflows.state_machine import move


WATCHED_READING_DIMS = {
    "author_intent": 60,
    "brief_coverage": 60,
    "readability": 60,
    "reader_momentum": 60,
    "hook_strength": 65,
    "scene_atmosphere": 55,
    "payoff_grounding": 65,
    "chapter_necessity": 65,
    "dialogue_fullness": 55,
    "character_voice": 60,
    "prose_voice": 65,
    "chapter_unit_flow": 65,
    "imageable_paragraphs": 60,
}
READING_ASSESSMENT_POLICY_VERSION = "v6_hard_issue_acceptance"
REVISION_ACTIONS = {"auto_polish", "auto_revise", "auto_rebuild"}
APPROVAL_ACTIONS = {"approve_ready"}


def _is_effective_approval(assessment) -> bool:
    """Sprint 2 P0-1: recognise auto_polish@polish_ready with no blockers
    as an *effective* approval.

    Rationale: when the LLM chief editor returns action=auto_polish and
    level=polish_ready with no outstanding blockers, the summary is literally
    "主编认可，轻润色即可" — the draft is publishable, machine polish is a
    nicety not a gate. Treating this as REVISION_ACTION would (a) flip
    ``quality.passed`` back to False, (b) demote ``version.status`` from
    reviewed_pass to needs_revision, and (c) trap the chapter in the
    accept_early_stop → reviewed_pass → maybe_apply → needs_revision loop.

    Callers use this to decide whether to promote to reviewed_pass or leave
    it needing revision.
    """
    action = getattr(assessment, "action", None) if not isinstance(assessment, dict) else assessment.get("action")
    if action in APPROVAL_ACTIONS:
        return True
    if action != "auto_polish":
        return False
    level = getattr(assessment, "level", None) if not isinstance(assessment, dict) else assessment.get("level")
    blockers = getattr(assessment, "blockers", None) if not isinstance(assessment, dict) else assessment.get("blockers")
    return level == "polish_ready" and not blockers
MACHINE_APPROVAL_SCORE = 82
MACHINE_APPROVAL_MIN_DIMENSION = 70
MACHINE_APPROVAL_CORE_DIMENSIONS = {
    "author_intent": 78,
    "brief_coverage": 78,
    "reader_momentum": 75,
    "hook_strength": 75,
    "payoff_grounding": 75,
    "chapter_necessity": 75,
    "chapter_unit_flow": 75,
}


@dataclass(frozen=True)
class ReadingAssessment:
    level: str
    action: str
    label: str
    summary: str
    revision_mode: str
    preserve: list[str]
    improve: list[str]
    blockers: list[str]
    quality_id: int | None = None
    revision_brief_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "action": self.action,
            "label": self.label,
            "summary": self.summary,
            "revision_mode": self.revision_mode,
            "preserve": self.preserve,
            "improve": self.improve,
            "blockers": self.blockers,
            "blocker_notes": [editorial_blocker_text(item) for item in self.blockers],
            "team_summary": editorial_summary_text(self.summary, self.blockers),
            "quality_id": self.quality_id,
            "revision_brief_id": self.revision_brief_id,
            "source": "reading_assessment@v1",
            "policy_version": READING_ASSESSMENT_POLICY_VERSION,
        }


def maybe_apply_reading_assessment(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    quality: QualityReport,
) -> ReadingAssessment:
    data = _loads_json(quality.report)
    data.setdefault("passed", bool(quality.passed))
    data.setdefault("base_quality_passed", bool(data.get("passed", quality.passed)))
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    version = session.get(ChapterVersion, quality.chapter_version_id)
    existing = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
    if (
        existing.get("source") == "reading_assessment@v1"
        and existing.get("action") != "author_review"
        and _existing_assessment_matches_quality(existing, quality_id=quality.id)
        and existing.get("policy_version") == READING_ASSESSMENT_POLICY_VERSION
    ):
        assessment = _assessment_from_dict(existing)
        if _is_effective_approval(assessment):
            # Sprint 2 P0-1: existing approval-effective assessment — close
            # any dangling revision briefs (they'd otherwise flip
            # ``version.status`` back to needs_revision via
            # ``_revision_brief_blocks_quality_reconcile`` on the next planner
            # pass) and reconcile version state to reviewed_pass.
            if chapter is not None:
                _close_revision_briefs(session, chapter_id=chapter.id)
            if (
                version is not None
                and version.status == "needs_revision"
                and bool(quality.passed)
            ):
                version.status = move(
                    "chapter_version", version.status, "reviewed_pass", "quality_pass"
                )
                session.flush()
            return assessment
        if assessment.action in REVISION_ACTIONS and chapter and version:
            brief = _ensure_revision_brief(
                session,
                book_id=book_id,
                chapter_number=chapter_number,
                chapter_id=chapter.id,
                version=version,
                quality=quality,
                assessment=assessment,
            )
            assessment = ReadingAssessment(
                assessment.level,
                assessment.action,
                assessment.label,
                assessment.summary,
                assessment.revision_mode,
                assessment.preserve,
                assessment.improve,
                assessment.blockers,
                quality_id=quality.id,
                revision_brief_id=brief.id,
            )
            stored = _store_assessment(quality, data, assessment, version=version)
            session.flush()
            return stored
        _apply_final_quality_decision(quality=quality, version=version, data=data, assessment=assessment)
        session.flush()
        return assessment
    if existing.get("source") == "reading_assessment@v1" and (
        not _existing_assessment_matches_quality(existing, quality_id=quality.id)
        or existing.get("policy_version") != READING_ASSESSMENT_POLICY_VERSION
    ):
        data.pop("reading_assessment", None)

    if not chapter or not version:
        assessment = ReadingAssessment(
            "system_error",
            "inspect",
            "系统异常",
            "找不到章节或版本，不能做阅读评估。",
            "inspect",
            [],
            [],
            ["missing_chapter_or_version"],
            quality_id=quality.id,
        )
        return _store_assessment(quality, data, assessment, version=version)

    assessment = assess_reading_quality(data, quality_id=quality.id)
    external_brief = _active_external_revision_order(session, chapter_id=chapter.id)
    if external_brief and _revision_brief_produced_version(session, brief_id=external_brief.id, version_id=version.id):
        external_brief.status = "superseded"
        external_brief = None
    if external_brief:
        assessment = ReadingAssessment(
            "external_revision_contract",
            "auto_revise",
            "修订单未完成",
            "主笔还有一张有效修订单没有兑现，必须先按这张修订单继续处理。",
            "targeted",
            assessment.preserve,
            assessment.improve,
            [f"active_revision_order#{external_brief.id}"],
            quality_id=quality.id,
            revision_brief_id=external_brief.id,
        )
        # Sprint 2 P2-Ch27: skip demote when chapter is already closed.
        from app.services.chapter_state import chapter_is_in_closed_state
        if version.status == "reviewed_pass" and not chapter_is_in_closed_state(session, chapter.id):
            version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
        stored = _store_assessment(quality, data, assessment, version=version)
        session.flush()
        return stored
    if _is_effective_approval(assessment):
        _close_revision_briefs(session, chapter_id=chapter.id)
        if version.status == "needs_revision":
            version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")
    elif assessment.action in REVISION_ACTIONS:
        brief = _ensure_revision_brief(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            chapter_id=chapter.id,
            version=version,
            quality=quality,
            assessment=assessment,
        )
        assessment = ReadingAssessment(
            assessment.level,
            assessment.action,
            assessment.label,
            assessment.summary,
            assessment.revision_mode,
            assessment.preserve,
            assessment.improve,
            assessment.blockers,
            quality_id=quality.id,
            revision_brief_id=brief.id,
        )
        # Sprint 2 P2-Ch27: skip demote when chapter is already closed.
        from app.services.chapter_state import chapter_is_in_closed_state
        if version.status == "reviewed_pass" and not chapter_is_in_closed_state(session, chapter.id):
            version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
    stored = _store_assessment(quality, data, assessment, version=version)
    session.flush()
    return stored


def assess_reading_quality(report_data: dict, *, quality_id: int | None = None) -> ReadingAssessment:
    score = int(report_data.get("score") or 0)
    passed = bool(report_data.get("base_quality_passed", report_data.get("passed")))
    dimensions = report_data.get("dimensions") if isinstance(report_data.get("dimensions"), dict) else {}
    review = report_data.get("llm_review") if isinstance(report_data.get("llm_review"), dict) else {}
    editorial = report_data.get("editorial_stratification") if isinstance(report_data.get("editorial_stratification"), dict) else {}
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    hard_gate_passed = bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS")
    failure_class = (
        report_data.get("production_failure_classification")
        if isinstance(report_data.get("production_failure_classification"), dict)
        else {}
    )
    report_gate_blockers = _report_gate_blockers(report_data)
    editor_score = int(review.get("score") or 0)
    editor_verdict = str(review.get("verdict") or "")
    blockers = _reading_blockers(dimensions)
    severe = _severe_blockers(dimensions)
    structural_rebuild = _structural_rebuild_blockers(dimensions)
    approval_blockers = _machine_approval_blockers(score=score, dimensions=dimensions, blockers=blockers)
    preserve = [str(item) for item in review.get("strengths") or []][:6]
    improve = _improvement_targets(dimensions, review)
    editor_passed = editor_score >= 78 and editor_verdict == "pass"

    if failure_class.get("category") == "structure_rewrite":
        reasons = [str(item) for item in failure_class.get("structural_reasons") or []]
        # Change C (2026-07-02): if the LLM chief editor already reviewed and
        # cleared the version — pass verdict, strong LLM score, hard_gate PASS,
        # editorial tier B or above, and the editorial_gate applied a soft
        # rule override — respect that judgement and downgrade to auto_polish
        # instead of forcing another expensive rebuild. Rule-side
        # brief_coverage/author_intent scores are known to lag behind actual
        # prose quality, and rebuild loops on LLM-approved drafts bleed
        # tokens without changing verdict.
        editorial_gate = report_data.get("editorial_gate") if isinstance(report_data.get("editorial_gate"), dict) else {}
        llm_override_qualifies = (
            editor_verdict == "pass"
            and editor_score >= 78
            and hard_gate_passed
            and editorial.get("tier") in {"A_approve", "B_solid_draft", "C_polish"}
            and bool(editorial_gate.get("soft_rule_override"))
        )
        if llm_override_qualifies:
            return ReadingAssessment(
                "polish_ready",
                "auto_polish",
                "主编认可，轻润色即可",
                "主编复核判定 pass 且硬门禁通过；规则侧结构分数偏低但正文已达合格底稿，只需按主编建议做轻润色，无需重建。",
                "machine_polish",
                preserve,
                improve or reasons,
                [],
                quality_id=quality_id,
            )
        return ReadingAssessment(
            "structure_rebuild_required",
            "auto_rebuild",
            "结构需重建",
            "当前稿存在长度、单元或章节承诺层面的结构性失败；系统必须按稳定蓝图重构，不能降级为局部修补。",
            "rewrite",
            preserve,
            improve or reasons,
            report_gate_blockers or reasons,
            quality_id=quality_id,
        )

    if report_gate_blockers:
        return ReadingAssessment(
            "quality_gate_reopen_required",
            "auto_revise",
            "质量门禁未关闭",
            "当前稿仍有章节类型/硬门禁失败项，不能被主编分数直接放行；系统必须继续修订到门禁关闭。",
            "targeted",
            preserve,
            improve or report_gate_blockers[:4],
            report_gate_blockers,
            quality_id=quality_id,
        )

    if score < 60 or editorial.get("tier") in {"D_rebuild", "E_contaminated"}:
        rebuild_mode = "fresh"
        return ReadingAssessment(
            "rebuild_required",
            "auto_rebuild",
            "需重建",
            "当前稿不能沿局部修补继续，系统应回到章节承诺重建场景链。",
            rebuild_mode,
            preserve,
            improve,
            blockers or severe,
            quality_id=quality_id,
        )
    if not passed and (score < 70 or not hard_gate_passed):
        return ReadingAssessment(
            "rebuild_required",
            "auto_rebuild",
            "未过底线，需重建",
            "当前稿未达到可用底稿线，系统应回到章节承诺重建场景链。",
            "rewrite",
            preserve,
            improve,
            blockers or severe,
            quality_id=quality_id,
        )
    if structural_rebuild:
        return ReadingAssessment(
            "structure_rebuild_required",
            "auto_rebuild",
            "结构需重建",
            "当前稿基础质检过线，但开篇牵引、章节必要性或段落审美已经形成结构性失败；系统必须切断旧稿开场和场景顺序，按章节承诺重新生成。",
            "fresh",
            preserve,
            improve,
            structural_rebuild + blockers,
            quality_id=quality_id,
        )
    if passed and hard_gate_passed and editor_passed and not report_gate_blockers:
        return ReadingAssessment(
            "editorial_pass_candidate",
            "approve_ready",
            "主编准定稿",
            "硬门禁、规则底稿线和主编审稿均已通过；剩余问题属于发布前可接受的软优化，不再继续自动修坏底稿。",
            "none",
            preserve,
            improve[:4],
            [],
            quality_id=quality_id,
        )
    if score >= 75 and hard_gate_passed and int(dimensions.get("author_intent") or 100) >= 55:
        return ReadingAssessment(
            "usable_plateau_needs_local_patch",
            "auto_polish",
            "可用底稿，局部补强",
            "当前稿已经越过可用线，继续整章修订容易破坏已成立结构；系统只做低风险局部补强。",
            "local_patch",
            preserve,
            improve,
            blockers or severe or approval_blockers[:4],
            quality_id=quality_id,
        )
    if severe or int(dimensions.get("author_intent") or 100) < 45 or int(dimensions.get("brief_coverage") or 100) < 55:
        return ReadingAssessment(
            "usable_draft_needs_revision",
            "auto_revise",
            "可用底稿，需自动升华",
            "当前稿有可保留结构，但关键承诺或读者体验不足；系统应自动生成定点升华修订。",
            "targeted",
            preserve,
            improve,
            blockers or severe,
            quality_id=quality_id,
        )
    if passed and not approval_blockers and (not editor_score or editor_score >= 78) and editor_verdict in {"", "pass"}:
        return ReadingAssessment(
            "publish_candidate",
            "approve_ready",
            "主编准定稿",
            "主编判断当前稿已经达到准定稿标准，可以关闭修订单并进入采用确认。",
            "none",
            preserve,
            improve[:3],
            [],
            quality_id=quality_id,
        )
    if len(blockers) >= 3:
        return ReadingAssessment(
            "readable_needs_polish",
            "auto_polish",
            "可读但需润色",
            "当前稿可读，但多个读感维度偏弱；系统先做一轮低风险局部润色。",
            "local_patch",
            preserve,
            improve,
            blockers,
            quality_id=quality_id,
        )
    if blockers or approval_blockers:
        return ReadingAssessment(
            "readable_needs_targeted_polish",
            "auto_polish",
            "可读但未达准定稿",
            "当前稿已能读，但还没达到主编准定稿标准；主笔继续做低风险定点润色。",
            "local_patch",
            preserve,
            improve or approval_blockers,
            (blockers or approval_blockers)[:4],
            quality_id=quality_id,
        )
    return ReadingAssessment(
        "near_final_needs_machine_polish",
        "auto_polish",
        "准定稿前润色",
        "当前稿接近可用，但关键读感还差一口气；主笔做一轮轻量润色后再交给主编判断。",
        "local_patch",
        preserve,
        improve[:4] or ["提升场景颗粒度、对白承载和章末后果，使主编准定稿标准可验证。"],
        approval_blockers[:4],
        quality_id=quality_id,
    )


def _existing_assessment_matches_quality(existing: dict, *, quality_id: int | None) -> bool:
    existing_id = existing.get("quality_id")
    if existing_id in {None, ""}:
        return False
    try:
        return int(existing_id) == int(quality_id or 0)
    except (TypeError, ValueError):
        return False


def reading_assessment_requires_revision(report_data: dict) -> bool:
    assessment = report_data.get("reading_assessment") if isinstance(report_data.get("reading_assessment"), dict) else {}
    if assessment.get("action") not in REVISION_ACTIONS:
        return False
    # Sprint 2 P0-1: effective approval doesn't require revision even if the
    # raw action label is auto_polish.
    if _is_effective_approval(assessment):
        return False
    return True


def reading_assessment_approval_ready(report_data: dict) -> bool:
    assessment = report_data.get("reading_assessment") if isinstance(report_data.get("reading_assessment"), dict) else {}
    if assessment.get("action") == "approve_ready":
        return True
    # Sprint 2 P0-1: an ``auto_polish`` verdict with level=polish_ready and
    # no outstanding blockers means the LLM chief editor already endorsed
    # the draft ("主编认可，轻润色即可"). Downstream planning must treat
    # this as an approval-ready state — otherwise
    # ``maybe_apply_reading_assessment`` re-runs on every planner pass and
    # flips ``quality.passed`` back to False, defeating any earlier
    # accept_early_stop promotion. Machine-polish is a downstream nicety,
    # not a gate.
    if (
        assessment.get("action") == "auto_polish"
        and assessment.get("level") == "polish_ready"
        and not assessment.get("blockers")
    ):
        return True
    return False


def _ensure_revision_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    chapter_id: int,
    version: ChapterVersion,
    quality: QualityReport,
    assessment: ReadingAssessment,
) -> ChapterBrief:
    marker = f"reading_assessment_auto_quality#{quality.id}"
    existing = _active_brief_with_marker(session, chapter_id=chapter_id, marker=marker)
    revision_mode = _revision_mode_for_assessment(
        session,
        chapter_id=chapter_id,
        assessment=assessment,
    )
    if existing:
        brief = existing
    else:
        for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
            brief.status = "superseded"
        suggestion = _revision_suggestion(chapter_number=chapter_number, version=version, quality=quality, assessment=assessment, marker=marker)
        _feedback, _adjustment, brief, _version = submit_revision_suggestion(
            session,
            book_id=book_id,
            chapter_number=chapter_number,
            platform="reading_assessment",
            suggestion_text=suggestion,
            revision_mode=revision_mode if revision_mode != "none" else "targeted",
        )
    rebuilding = assessment.action == "auto_rebuild"
    brief.goal = (
        f"阅读评估重建第{chapter_number}章：以当前作品剧情承诺为准，旧稿 v{version.id} 只保留可用素材。"
        if rebuilding
        else f"阅读评估自动修订第{chapter_number}章：以 v{version.id} 为底稿，把“能读”修到“想追”。"
    )
    story_commitments = _story_commitment_lines(session, chapter_id=chapter_id, chapter_number=chapter_number)
    source_policy = (
        (
            "失败结构不得沿用；必须替换失败开场、段落顺序、问路铺垫和失败场景链；"
            "只保留 Canon 中仍有效的必要事实。"
        )
        if rebuilding
        else f"源版本锁定：v{version.id}；不得换开场、不得换主事件、不得新开故事线。"
    )
    preserve_label = "可复用素材：" if rebuilding else "必须保留："
    brief.required_beats = "\n".join(
        [
            marker,
            f"当前阅读层级：{assessment.label}",
            source_policy,
            *story_commitments,
            preserve_label + "；".join(assessment.preserve[:6]),
            "本轮只解决：" + "；".join((assessment.improve or assessment.blockers)[:6]),
        ]
    )
    brief.constraints = "\n".join(
        _assessment_constraint_lines(
            previous_constraints=brief.constraints or "",
            revision_mode=revision_mode,
            rebuilding=rebuilding,
        )
    )
    sanitize_existing_chapter_brief(session, book_id=book_id, brief=brief)
    session.flush()
    return brief


def _revision_mode_for_assessment(session: Session, *, chapter_id: int, assessment: ReadingAssessment) -> str:
    if assessment.action != "auto_rebuild":
        return assessment.revision_mode
    if assessment.revision_mode == "fresh":
        return "fresh"
    if _recent_reading_rebuild_failures(session, chapter_id=chapter_id) >= 2:
        return "fresh"
    return assessment.revision_mode


def _assessment_constraint_lines(*, previous_constraints: str, revision_mode: str, rebuilding: bool) -> list[str]:
    base = [
        "3000-4500 中文字符，正文优先，不用自检内容凑字数。",
        "不要输出导演单、质检报告、修订合同、验收清单或系统说明。",
        "少量界面/提示只能作为人物感知层点到为止，不能替代真实人物行动、因果和代价。",
        "对白和动作必须承接上一段后果，不能另起炉灶。",
    ]
    preserved = _compact_assessment_constraints(previous_constraints)
    lines = [
        *base,
        *preserved,
        "reading_assessment_contract: 系统自动阅读评估生成；下一版必须解决上述读感问题。",
        f"revision_mode:{revision_mode}",
        "禁止：追杀模板、现实机构关注、门派通缉、系统面板直接解题、冷硬装酷式精炼。",
    ]
    if rebuilding:
        lines.append(
            "fresh 重建：旧稿只作为失败反例；必须重新组织具体场景、人物行动、对白和后果。"
            if revision_mode == "fresh"
            else "重建时禁止照抄失败结构；必须重新组织具体场景、人物行动、对白和后果。"
        )
    else:
        lines.extend(
            [
                "禁止推翻合格底稿；禁止只替换形容词；必须把问题落到场景、动作、对白、后果。",
                "本轮必须优先补：场景空间/声音/触感、对白试探与情绪、主角行动选择、奖励/代价、章末后果。",
            ]
        )
    return list(dict.fromkeys(line.strip(" -") for line in lines if line and line.strip(" -")))


def _compact_assessment_constraints(text: str) -> list[str]:
    keep_markers = (
        "不要写成系统文",
        "不要用“更高层势力”",
        "禁止靠追杀",
        "不能压成冷硬悬疑",
        "奖励优先表现为身体记忆",
    )
    drop_markers = (
        "必须遵守最新作品DNA",
        "作品DNA",
        "章节发动机库",
        "题材主味",
        "核心钩子",
        "核心设定必须落在场景里",
        "世界规则必须可被人物感知",
        "主角行动必须体现",
        "目标读者期待",
    )
    kept: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip(" -")
        if not line:
            continue
        normalized = line.replace("：", ":")
        if normalized.startswith(("revision_mode:", "修订模式:", "reading_assessment_contract:")):
            continue
        if any(marker in line for marker in drop_markers):
            continue
        if any(marker in line for marker in keep_markers):
            kept.append(line[:180])
        if len(kept) >= 3:
            break
    return kept


def _recent_reading_rebuild_failures(session: Session, *, chapter_id: int, limit: int = 4) -> int:
    rows = list(
        session.execute(
            select(QualityReport.report)
            .join(ChapterVersion, QualityReport.chapter_version_id == ChapterVersion.id)
            .where(ChapterVersion.chapter_id == chapter_id, QualityReport.passed.is_(False))
            .order_by(QualityReport.id.desc())
            .limit(limit)
        )
    )
    count = 0
    for (report,) in rows:
        assessment = _loads_json(report).get("reading_assessment")
        if isinstance(assessment, dict) and assessment.get("action") == "auto_rebuild":
            count += 1
    return count


def _strip_revision_mode_lines(text: str) -> str:
    rows: list[str] = []
    for raw in str(text or "").splitlines():
        line = raw.strip()
        normalized = line.lstrip("- ").replace("：", ":")
        if normalized.startswith(("revision_mode:", "修订模式:")):
            continue
        rows.append(raw)
    return "\n".join(rows).strip()


def _revision_suggestion(*, chapter_number: int, version: ChapterVersion, quality: QualityReport, assessment: ReadingAssessment, marker: str) -> str:
    return "\n".join(
        [
            marker,
            f"第{chapter_number}章阅读评估：{assessment.summary}",
            f"源版本：v{version.id}；质量报告：#{quality.id}。",
            "保留项：",
            *[f"- {item}" for item in assessment.preserve[:6]],
            "修订目标：",
            *[f"- {item}" for item in (assessment.improve or assessment.blockers)[:8]],
            "边界：只做阅读体验升华，不换题材路线，不扩大到整章重写，除非评估层级为需重建。",
        ]
    )


def rebind_revision_brief_source(brief: ChapterBrief, *, version_id: int) -> ChapterBrief:
    brief.goal = re.sub(r"以 v\d+ 为底稿", f"以 v{version_id} 为底稿", brief.goal or "")
    brief.goal = re.sub(r"旧稿 v\d+", f"旧稿 v{version_id}", brief.goal or "")
    brief.required_beats = re.sub(r"源版本锁定：v\d+", f"源版本锁定：v{version_id}", brief.required_beats or "")
    brief.required_beats = re.sub(r"重建素材来源：v\d+", f"重建素材来源：v{version_id}", brief.required_beats or "")
    marker = f"合同当前底稿：v{version_id}"
    if marker not in (brief.required_beats or ""):
        brief.required_beats = "\n".join([brief.required_beats or "", marker]).strip()
    return brief


def downgrade_rebound_brief_to_targeted(brief: ChapterBrief, *, version_id: int, quality: QualityReport | None = None) -> ChapterBrief:
    """After restoring a better source draft, keep that draft and stop repeating failed fresh rewrites."""
    rebind_revision_brief_source(brief, version_id=version_id)
    report = _loads_json(quality.report) if quality else {}
    failure_class = report.get("production_failure_classification") if isinstance(report.get("production_failure_classification"), dict) else {}
    if failure_class.get("category") == "structure_rewrite":
        rebind_revision_brief_source(brief, version_id=version_id)
        reasons = [str(item) for item in failure_class.get("structural_reasons") or []]
        brief.goal = f"阅读评估结构重构第{_chapter_number_from_brief(brief) or ''}章：以 v{version_id} 为素材，按稳定蓝图重写。".replace("第章", "本章")
        brief.required_beats = "\n".join(
            [
                "reading_assessment_contract: 系统自动阅读评估生成；结构失败不得降级为局部修补。",
                f"源版本锁定：v{version_id}；只保留有效事实，不沿用失败长度和散乱结构。",
                "结构失败原因：" + "；".join(reasons[:5]),
                "正文必须压缩到3000-4500中文字符，按6-8个连续小单元重构。",
                f"合同当前底稿：v{version_id}",
            ]
        )
        brief.constraints = "\n".join(
            [
                "revision_mode:rewrite",
                "reading_assessment_contract: 系统自动阅读评估生成；下一版必须关闭结构门禁。",
                "禁止继续局部补丁；禁止保留超长章节结构；禁止新增支线凑信息量。",
                "必须承接上一章后果，并用目标、阻碍、动作、反应、后果推进。",
            ]
        ).strip()
        return brief
    assessment = report.get("reading_assessment") if isinstance(report.get("reading_assessment"), dict) else {}
    dims = report.get("dimensions") if isinstance(report.get("dimensions"), dict) else {}
    blockers = [str(item) for item in assessment.get("blockers") or []] or _reading_blockers(dims)
    improve = [str(item) for item in assessment.get("improve") or []] or _improvement_targets(dims, {})
    targets = list(dict.fromkeys([*improve, *blockers]))[:6]
    if not targets:
        targets = ["保留当前最佳稿的事件顺序，只补强场景颗粒度、对白承载、奖励/代价和章末后果。"]
    brief.goal = f"阅读评估定点修订第{_chapter_number_from_brief(brief) or ''}章：以 v{version_id} 当前最佳稿为底稿，禁止整章重写。".replace("第章", "本章")
    required_lines = [
        "reading_assessment_contract: 系统自动阅读评估生成；当前底稿已恢复为近期最佳稿。",
        f"源版本锁定：v{version_id}；不得换开场、不得换主事件、不得新开故事线。",
        "恢复策略：上轮整章重写已被判定劣化，本轮只能做保留结构的定点修订。",
        "必须保留当前最佳稿已有的外部压力、桥段复刻、奖励/能力痕迹、现实副作用和章末后果链。",
        "本轮只解决：" + "；".join(targets),
        f"合同当前底稿：v{version_id}",
    ]
    brief.required_beats = "\n".join(required_lines)
    brief.constraints = "\n".join(
        [
            "revision_mode:targeted",
            "reading_assessment_contract: 系统自动阅读评估生成；下一版必须解决上述读感问题。",
            "恢复后定点修订：当前最佳稿已通过基础质量底线，本轮只补最低读感维度。",
            "禁止：追杀模板、现实机构关注、门派通缉、系统面板直接解题、冷硬装酷式精炼。",
            "禁止整章重写；禁止替换已合格的开场压力和主事件链；只允许增加、压缩、调序小段落来补足读感维度。",
        ]
    ).strip()
    return brief


def create_clean_rebuild_brief(
    session: Session,
    *,
    book_id: int,
    chapter_number: int,
    version: ChapterVersion,
    quality: QualityReport,
    reason: str,
) -> ChapterBrief:
    chapter = session.scalar(select(Chapter).where(Chapter.book_id == book_id, Chapter.chapter_number == chapter_number))
    if not chapter or chapter.id != version.chapter_id:
        raise ValueError("chapter/version mismatch while rebuilding revision brief")
    for existing in session.scalars(
        select(ChapterBrief).where(ChapterBrief.chapter_id == chapter.id, ChapterBrief.status == "revision_ready")
    ):
        existing.status = "superseded"
    data = _loads_json(quality.report)
    assessment_data = data.get("reading_assessment") if isinstance(data.get("reading_assessment"), dict) else {}
    review = data.get("llm_review") if isinstance(data.get("llm_review"), dict) else {}
    targets = [str(item) for item in assessment_data.get("improve") or []]
    targets.extend(str(item) for item in review.get("revision_suggestions") or [])
    targets = list(dict.fromkeys(item for item in targets if item))[:6]
    if not targets:
        targets = [
            "删除重复拼接和说明书式系统面板，让冲突在前300字内发生。",
            "让人物行动产生明确回报、代价和章末局面变化。",
            "增加主角的活人反应、对白声线和轻松释放点。",
        ]
    brief = ChapterBrief(
        chapter_id=chapter.id,
        goal=f"第{chapter_number}章干净重建：按当前作品剧情承诺重新组织可读正文，旧稿 v{version.id} 只作素材参考。",
        required_beats="\n".join(
            [
                "clean_rebuild_contract@v1",
                f"触发原因：{reason}",
                f"重建素材来源：v{version.id}；允许替换失败开场、场景顺序和行动链。",
                "失败结构不得沿用；必须替换失败开场、段落顺序、问路铺垫和失败场景链；只保留 Canon 中仍有效的必要事实。",
                *_story_commitment_lines(session, chapter_id=chapter.id, chapter_number=chapter_number),
                "本轮只解决：" + "；".join(targets),
            ]
        ),
        constraints="\n".join(
            [
                "revision_mode:fresh",
                "以当前 Canon 和作品设定为准，不引用旧质检分数、旧版本号或后台恢复说明作为正文内容。",
                "禁止复刻失败稿的重复开头、整段系统面板、冷硬装酷短句和空泛章末感叹。",
                "必须输出完整小说正文；用场景、动作、对白和后果兑现剧情承诺。",
            ]
        ),
        status="revision_ready",
    )
    session.add(brief)
    session.flush()
    return brief


def _story_commitment_lines(session: Session, *, chapter_id: int, chapter_number: int) -> list[str]:
    chapter = session.get(Chapter, chapter_id)
    foundation = (
        session.scalar(
            select(StoryFoundation)
            .where(StoryFoundation.book_id == chapter.book_id)
            .order_by(StoryFoundation.id.desc())
        )
        if chapter
        else None
    )
    story_brief = latest_story_brief(session, chapter_id)
    focus = (foundation.premise or "").strip()[:420] if foundation else _story_focus(story_brief)
    reader_promise = (foundation.reader_promise or "").strip()[:320] if foundation else ""
    commitments = [
        f"本章剧情承诺：主角在具体外部压力下主动选择并承担可见代价；核心能力必须通过行动触发并产生明确回报；章末出现改变下一章局面的具体变化。",
    ]
    if chapter_number == 1:
        commitments.extend(
            [
                "第1章硬性交付：第一句必须从门外逼问、现场盘问、交易催促、冲突后果或人物动作开场；不得以醒来、睁眼、摸手机、宿舍回忆、系统菜单或环境确认开场。",
                "第1章硬性交付：前700字内必须出现具体外部压力或关系盘问，不得只写醒来、问路和环境确认。",
                "第1章硬性交付：桥段复刻任务必须在前1500字内触发，中段完成一次行动尝试，且让NPC或玩家因主角演法产生误判、试探或反应。",
                "第1章硬性交付：结尾前必须写出明确奖励或能力痕迹；最后300字必须同步出现现实或身体层面的副作用线索。",
                "第1章硬性交付：章末钩子必须来自本次复刻的后果，不得只用远处响动、泛泛麻烦或任务刚触发收尾。",
            ]
        )
    if focus:
        commitments.append(f"剧情基线：{focus}")
    else:
        commitments.append(f"剧情基线：第{chapter_number}章承接当前作品设定，以人物目标、阻碍、选择和后果推进主线。")
    if reader_promise:
        commitments.append(f"剧情基线：{reader_promise}")
    return commitments


def _story_focus(brief: ChapterBrief | None) -> str:
    if not brief:
        return ""
    text = "\n".join([brief.goal or "", brief.required_beats or ""])
    candidates = [item.strip(" -") for item in re.split(r"[\n；]", text) if item.strip()]
    preferred = next((item for item in candidates if "核心作者意图" in item), "")
    if not preferred:
        preferred = next((item for item in candidates if "服务目标" in item or "核心设定" in item), "")
    if not preferred:
        preferred = candidates[0] if candidates else ""
    return preferred[:320]


def _reading_blockers(dimensions: dict) -> list[str]:
    rows: list[str] = []
    for name, threshold in WATCHED_READING_DIMS.items():
        value = int(dimensions.get(name) or 0)
        if value and value < threshold:
            rows.append(f"{name}={value}<{threshold}")
    return rows


def _machine_approval_blockers(*, score: int, dimensions: dict, blockers: list[str]) -> list[str]:
    rows = list(blockers)
    if score < MACHINE_APPROVAL_SCORE:
        rows.append(f"score={score}<{MACHINE_APPROVAL_SCORE}")
    for name, threshold in MACHINE_APPROVAL_CORE_DIMENSIONS.items():
        value = int(dimensions.get(name) or 0)
        if value and value < threshold:
            rows.append(f"{name}={value}<{threshold}")
    for name, value in dimensions.items():
        if not isinstance(value, int):
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        if value and value < MACHINE_APPROVAL_MIN_DIMENSION:
            rows.append(f"{name}={value}<{MACHINE_APPROVAL_MIN_DIMENSION}")
    return list(dict.fromkeys(rows))


def _report_gate_blockers(report_data: dict) -> list[str]:
    rows: list[str] = []
    issues = [str(item) for item in report_data.get("issues") or []]
    for issue in issues:
        if issue.startswith(
            (
                "chapter_type_gate_failed",
                "too_short",
                "too_long",
                "forbidden_marker",
                "setting_contradiction",
                "bias_blocker",
                "intent_blocker",
                "brief_coverage_underfulfilled",
                "payoff_grounding_blocker",
                "causal_continuity_blocker",
                "cost_plausibility_blocker",
                "expression_collocation_blocker",
            )
        ):
            rows.append(issue)
    chapter_type_gate = report_data.get("chapter_type_gate") if isinstance(report_data.get("chapter_type_gate"), dict) else {}
    if chapter_type_gate and not bool(chapter_type_gate.get("passed")):
        failures = ",".join(str(item) for item in (chapter_type_gate.get("failures") or [])[:5])
        rows.append("chapter_type_gate_failed:" + failures)
    hard_gate = report_data.get("hard_gate") if isinstance(report_data.get("hard_gate"), dict) else {}
    if hard_gate and not bool(hard_gate.get("passed") or hard_gate.get("status") == "PASS"):
        rows.append("hard_gate_failed")
    return list(dict.fromkeys(item for item in rows if item))


def _chapter_number_from_brief(brief: ChapterBrief) -> str:
    text = "\n".join([brief.goal or "", brief.required_beats or ""])
    match = re.search(r"第(\d+)章", text)
    return match.group(1) if match else ""


def _severe_blockers(dimensions: dict) -> list[str]:
    severe_thresholds = {
        "author_intent": 45,
        "brief_coverage": 50,
        "readability": 55,
        "chapter_unit_flow": 55,
        "hook_strength": 55,
        "prose_voice": 55,
    }
    rows: list[str] = []
    for name, threshold in severe_thresholds.items():
        value = int(dimensions.get(name) or 0)
        if value and value < threshold:
            rows.append(f"{name}={value}<{threshold}")
    return rows


def _structural_rebuild_blockers(dimensions: dict) -> list[str]:
    opening = int(dimensions.get("opening_grip") or dimensions.get("hook_opening") or 0)
    necessity = int(dimensions.get("chapter_necessity") or 0)
    paragraph = int(dimensions.get("paragraph_aesthetic") or 0)
    atmosphere = int(dimensions.get("scene_atmosphere") or 0)
    brief = int(dimensions.get("brief_coverage") or 0)
    dialogue = int(dimensions.get("dialogue_fullness") or 0)
    imageable = int(dimensions.get("imageable_paragraphs") or 0)
    rows: list[str] = []
    if opening and opening <= 30:
        rows.append(f"opening_grip={opening}<=30")
    if paragraph and paragraph <= 45:
        rows.append(f"paragraph_aesthetic={paragraph}<=45")
    if necessity and necessity < 50:
        rows.append(f"chapter_necessity={necessity}<50")
    secondary_failures = [
        f"scene_atmosphere={atmosphere}<55" if atmosphere and atmosphere < 55 else "",
        f"brief_coverage={brief}<55" if brief and brief < 55 else "",
        f"dialogue_fullness={dialogue}<55" if dialogue and dialogue < 55 else "",
        f"imageable_paragraphs={imageable}<60" if imageable and imageable < 60 else "",
    ]
    secondary_failures = [item for item in secondary_failures if item]
    if (rows and secondary_failures) or len(rows) >= 2:
        return rows + secondary_failures
    return []


def _improvement_targets(dimensions: dict, review: dict) -> list[str]:
    labels = {
        "author_intent": "把作者承诺写进具体行动和后果，不只停留在设定说明。",
        "brief_coverage": "补齐章节 brief 的关键节拍，尤其是本章承诺、回报、代价和章末压力。",
        "readability": "压缩说明性内心独白，更快进入可见冲突。",
        "scene_atmosphere": "把氛围从概括词改成空间、声音、触感、人物站位和现场反应。",
        "dialogue_fullness": "让对白承担试探、遮掩、交易或情绪变化。",
        "hook_strength": "章末钩子要具体到动作或异常后果。",
        "chapter_necessity": "强化本章不可替代的变化：主角获得什么、失去什么、发现什么。",
        "imageable_paragraphs": "补足可画面化段落，让读者看见场景而不是只知道事件。",
    }
    weak = sorted(
        [(name, int(dimensions.get(name) or 0)) for name in labels if int(dimensions.get(name) or 0)],
        key=lambda row: row[1],
    )
    rows = [labels[name] for name, value in weak if value < WATCHED_READING_DIMS.get(name, 60)]
    rows.extend(str(item) for item in review.get("revision_suggestions") or [])
    return list(dict.fromkeys(rows))[:8]


def _active_brief_with_marker(session: Session, *, chapter_id: int, marker: str) -> ChapterBrief | None:
    return session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    ) if marker in _latest_active_brief_text(session, chapter_id=chapter_id) else None


def _latest_active_brief_text(session: Session, *, chapter_id: int) -> str:
    brief = session.scalar(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
    )
    return "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""]) if brief else ""


def _close_revision_briefs(session: Session, *, chapter_id: int) -> None:
    for brief in session.scalars(select(ChapterBrief).where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")):
        brief.status = "superseded"


def _active_external_revision_order(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    for brief in session.scalars(
        select(ChapterBrief)
        .where(ChapterBrief.chapter_id == chapter_id, ChapterBrief.status == "revision_ready")
        .order_by(ChapterBrief.id.desc())
        .limit(8)
    ):
        text = "\n".join([brief.goal or "", brief.required_beats or "", brief.constraints or ""])
        if "reading_assessment_auto_quality#" in text or "reading_assessment_contract" in text:
            continue
        if any(marker in text for marker in ("反馈调整#", "修订方向#", "机器修订建议#", "原始机器修订建议")):
            return brief
    return None


def _active_human_revision_brief(session: Session, *, chapter_id: int) -> ChapterBrief | None:
    return _active_external_revision_order(session, chapter_id=chapter_id)


def _revision_brief_produced_version(session: Session, *, brief_id: int, version_id: int) -> bool:
    tasks = session.scalars(
        select(GenerationTask)
        .where(GenerationTask.task_type == "revise_chapter", GenerationTask.status == "completed")
        .order_by(GenerationTask.id.desc())
        .limit(40)
    )
    for task in tasks:
        input_data = _loads_json(task.input_json)
        output_data = _loads_json(task.output_json)
        if int(input_data.get("revision_brief_id") or 0) != brief_id:
            continue
        if int(output_data.get("version_id") or 0) == version_id:
            return True
    return False


def _store_assessment(
    quality: QualityReport,
    data: dict,
    assessment: ReadingAssessment,
    *,
    version: ChapterVersion | None,
) -> ReadingAssessment:
    data["reading_assessment"] = assessment.to_dict()
    _apply_final_quality_decision(quality=quality, version=version, data=data, assessment=assessment)
    return assessment


def _apply_final_quality_decision(
    *,
    quality: QualityReport,
    version: ChapterVersion | None,
    data: dict,
    assessment: ReadingAssessment,
) -> None:
    base_passed = bool(data.get("base_quality_passed", data.get("passed", quality.passed)))
    approval_ready = _is_effective_approval(assessment)
    # Sprint 2 P0-1: effective approval (auto_polish@polish_ready no blockers)
    # is NOT a revision requirement — machine polish downstream is optional.
    requires_revision = (assessment.action in REVISION_ACTIONS) and not approval_ready
    final_passed = bool(base_passed and approval_ready)
    if assessment.action == "inspect":
        final_passed = False
    data["base_quality_passed"] = base_passed
    data["passed"] = final_passed
    data["status"] = "PASS" if final_passed else "NEEDS_REVISION"
    data["final_verdict"] = {
        "status": "pass" if final_passed else "needs_revision",
        "label": "综合评估通过" if final_passed else "综合评估需修订",
        "reason": assessment.summary,
        "team_reason": editorial_summary_text(assessment.summary, assessment.blockers),
        "reading_level": assessment.level,
        "reading_action": assessment.action,
        "base_quality_passed": base_passed,
        "source": "unified_quality_verdict@v1",
    }
    # Sprint 2 P2-Ch27: skip quality mutation entirely when chapter is closed.
    # Otherwise a stale reading_assessment pass writes final_passed=False into
    # the QR, unblocking the planner's fresh revision loop even though
    # accept_early_stop has already promoted the best version. We keep the
    # in-memory ``final_verdict`` dict for other callers but do not persist it.
    from sqlalchemy.orm import object_session
    from app.services.chapter_state import chapter_is_in_closed_state
    _s = object_session(quality)
    _closed = (
        _s is not None
        and version is not None
        and chapter_is_in_closed_state(_s, version.chapter_id)
    )
    if not _closed:
        quality.passed = final_passed
        quality.report = json.dumps(data, ensure_ascii=False)
    if not version:
        return
    if _closed:
        # Closed chapters must not have version status flipped by state repair.
        return
    if requires_revision and version.status in {"reviewed_pass", "approved"}:
        version.status = move("chapter_version", version.status, "needs_revision", "feedback_reopen")
    elif final_passed and version.status == "needs_revision":
        version.status = move("chapter_version", version.status, "reviewed_pass", "quality_pass")


def _assessment_from_dict(data: dict) -> ReadingAssessment:
    return ReadingAssessment(
        level=str(data.get("level") or ""),
        action=str(data.get("action") or ""),
        label=str(data.get("label") or ""),
        summary=str(data.get("summary") or ""),
        revision_mode=str(data.get("revision_mode") or ""),
        preserve=[str(item) for item in data.get("preserve") or []],
        improve=[str(item) for item in data.get("improve") or []],
        blockers=[str(item) for item in data.get("blockers") or []],
        quality_id=int(data.get("quality_id") or 0) or None,
        revision_brief_id=int(data.get("revision_brief_id") or 0) or None,
    )


def _loads_json(value: str | None) -> dict[str, Any]:
    try:
        data = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
