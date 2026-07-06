from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.chapter_revision import _revision_is_fresh_rewrite
from app.services.planning import _revision_budget_guard_should_defer, build_human_decision_package, plan_chapters
from app.services.production import approve_chapter
from app.services.production_decision import decide_chapter_production
from app.services.reading_assessment import maybe_apply_reading_assessment
from regression_db import isolated_database


def main() -> int:
    isolated_database("reading-assessment-state-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="Reading Assessment State Regression", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="已过基础质检但仍待阅读评估的正文" * 1000,
            status="needs_revision",
            source="revision:regression",
        )
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估结论：当前稿不是正式批准稿，需要阅读判断。",
            required_beats="reading_assessment_contract: 检查开头、场景、人物动机是否真正变好。",
            constraints="当前稿不是正式批准稿；通过阅读评估后才能关闭修订合同。",
            status="revision_ready",
        )
        session.add_all([version, brief])
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=78,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 78, "passed": True}, ensure_ascii=False),
        )
        session.add(quality)
        session.flush()

        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        decision = decide_chapter_production(item)
        package = build_human_decision_package(session, book_id=book.id, start=1, count=1)

        if item.next_action != "revise_chapter":
            failures.append(f"unexpected_next_action:{item.next_action}:{item.reason}")
        if decision.needs_author or decision.primary_intent != "continue":
            failures.append(f"unexpected_decision:{decision.to_dict()}")
        if package.approval_count != 0:
            failures.append(f"premature_human_approval_package:{package.to_dict() if hasattr(package, 'to_dict') else package.approval_count}")
        report_data = json.loads(quality.report)
        assessment_data = report_data.get("reading_assessment") or {}
        if quality.passed or version.status != "needs_revision" or assessment_data.get("action") == "author_review":
            failures.append(f"machine_gate_not_enforced:{quality.passed}:{version.status}:{assessment_data}")
        latest_brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id, status="revision_ready").order_by(ChapterBrief.id.desc()).first()
        if not latest_brief or "reading_assessment_auto_quality#" not in "\n".join([latest_brief.goal or "", latest_brief.required_beats or "", latest_brief.constraints or ""]):
            failures.append("machine_gate_revision_brief_missing")

    with session_scope() as session:
        book = Book(title="Reading Assessment Auto Revision", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="基础结构能用但作者意图和章节承诺明显不足的正文" * 1000,
            status="reviewed_pass",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=75,
            passed=True,
            report=json.dumps(_auto_revision_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        assessment = maybe_apply_reading_assessment(session, book_id=book.id, chapter_number=1, quality=quality)
        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        latest_brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id).order_by(ChapterBrief.id.desc()).first()
        if assessment.action != "auto_revise":
            failures.append(f"auto_revision_assessment_wrong:{assessment.to_dict()}")
        if version.status != "needs_revision":
            failures.append(f"auto_revision_did_not_reopen_version:{version.status}")
        report_data = json.loads(quality.report)
        if quality.passed or report_data.get("passed") is not False:
            failures.append(f"auto_revision_final_verdict_not_failed:{quality.passed}:{report_data.get('passed')}")
        if (report_data.get("final_verdict") or {}).get("status") != "needs_revision":
            failures.append(f"auto_revision_final_verdict_missing:{report_data.get('final_verdict')}")
        if report_data.get("base_quality_passed") is not True:
            failures.append(f"base_quality_result_not_preserved:{report_data.get('base_quality_passed')}")
        if item.next_action != "revise_chapter":
            failures.append(f"auto_revision_not_routed_to_revise:{item.next_action}:{item.reason}")
        if not latest_brief or "reading_assessment_auto_quality#" not in "\n".join([latest_brief.goal or "", latest_brief.required_beats or "", latest_brief.constraints or ""]):
            failures.append("auto_revision_brief_missing_marker")
        if not _revision_budget_guard_should_defer(version, latest_brief):
            failures.append("current_reading_assessment_brief_not_exempt_from_budget_recovery")

    with session_scope() as session:
        book = Book(title="Reading Assessment Trend Priority", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        previous = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="上一版失败但分数较高的正文" * 1000,
            status="needs_revision",
            source="revision:regression",
        )
        latest = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="第一章",
            content="最新阅读评估后需要重建的正文" * 1000,
            status="needs_revision",
            source="revision:regression",
        )
        session.add_all([previous, latest])
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=previous.id,
                score=82,
                passed=False,
                report=json.dumps(
                    {
                        "status": "NEEDS_REVISION",
                        "score": 82,
                        "passed": False,
                        "dimensions": {"hook_strength": 82, "writer_craft": 84},
                    },
                    ensure_ascii=False,
                ),
            )
        )
        session.add(
            QualityReport(
                chapter_version_id=latest.id,
                score=68,
                passed=False,
                report=json.dumps(
                    {
                        "status": "NEEDS_REVISION",
                        "score": 68,
                        "passed": False,
                        "reading_assessment": {"action": "auto_revise", "status": "needs_revision"},
                        "dimensions": {"hook_strength": 51, "writer_craft": 74},
                    },
                    ensure_ascii=False,
                ),
            )
        )
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估重建第1章：旧稿只保留可用素材。",
            required_beats="reading_assessment_auto_quality#999\n当前阅读层级：需重建\n失败结构不得沿用。",
            constraints="当前稿不是正式批准稿；系统必须按阅读评估合同继续修订。",
            status="revision_ready",
        )
        session.add(brief)
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        if item.next_action != "revise_chapter":
            failures.append(f"reading_assessment_trend_not_deferred:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="Reading Assessment Structural Rebuild", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="基础质检刚过但开头和段落审美已经结构性失败的正文" * 1000,
            status="reviewed_pass",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=71,
            passed=True,
            report=json.dumps(_structural_rebuild_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        assessment = maybe_apply_reading_assessment(session, book_id=book.id, chapter_number=1, quality=quality)
        latest_brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id).order_by(ChapterBrief.id.desc()).first()
        if assessment.action != "auto_rebuild" or assessment.revision_mode != "fresh":
            failures.append(f"structural_rebuild_assessment_wrong:{assessment.to_dict()}")
        if not latest_brief or "revision_mode:fresh" not in (latest_brief.constraints or ""):
            failures.append(f"structural_rebuild_brief_not_fresh:{latest_brief.constraints if latest_brief else None}")
        if latest_brief and not _revision_is_fresh_rewrite(latest_brief):
            failures.append("english_fresh_mode_not_detected_by_revision")
        if latest_brief and "不得换开场" in (latest_brief.required_beats or ""):
            failures.append("structural_rebuild_locked_bad_opening")
        if latest_brief and "第1章硬性交付" not in (latest_brief.required_beats or ""):
            failures.append("structural_rebuild_missing_chapter1_deliverables")
        if latest_brief and "第一句必须" not in (latest_brief.required_beats or ""):
            failures.append("structural_rebuild_missing_opening_ban")

    with session_scope() as session:
        book = Book(title="Reading Assessment Failed Rebuild Fresh", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="重建后仍然没有兑现核心卖点的正文" * 1000,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=45,
            passed=False,
            report=json.dumps(_failed_rebuild_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        assessment = maybe_apply_reading_assessment(session, book_id=book.id, chapter_number=1, quality=quality)
        latest_brief = session.query(ChapterBrief).filter_by(chapter_id=chapter.id).order_by(ChapterBrief.id.desc()).first()
        if assessment.action != "auto_rebuild" or assessment.revision_mode != "fresh":
            failures.append(f"failed_rebuild_not_fresh:{assessment.to_dict()}")
        if not latest_brief or "失败结构不得沿用" not in (latest_brief.required_beats or ""):
            failures.append(f"failed_rebuild_did_not_discard_old_draft:{latest_brief.required_beats if latest_brief else None}")
        if latest_brief and "第1章硬性交付" not in (latest_brief.required_beats or ""):
            failures.append("failed_rebuild_missing_chapter1_deliverables")
        if latest_brief and "任务刚触发收尾" not in (latest_brief.required_beats or ""):
            failures.append("failed_rebuild_missing_timing_contract")

    with session_scope() as session:
        book = Book(title="Reading Assessment Approve Ready", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第一章",
            content="准定稿正文" * 1200,
            status="needs_revision",
            source="revision:regression",
        )
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估结论：等待确认。",
            required_beats="reading_assessment_contract: 只剩最终阅读判断。",
            constraints="当前稿不是正式批准稿。",
            status="revision_ready",
        )
        session.add_all([version, brief])
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=88,
            passed=True,
            report=json.dumps(_approve_ready_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        assessment = maybe_apply_reading_assessment(session, book_id=book.id, chapter_number=1, quality=quality)
        item = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        if assessment.action != "approve_ready":
            failures.append(f"approve_ready_assessment_wrong:{assessment.to_dict()}")
        if version.status != "reviewed_pass":
            failures.append(f"approve_ready_did_not_restore_reviewed_pass:{version.status}")
        report_data = json.loads(quality.report)
        if not quality.passed or (report_data.get("final_verdict") or {}).get("status") != "pass":
            failures.append(f"approve_ready_final_verdict_wrong:{quality.passed}:{report_data.get('final_verdict')}")
        if brief.status != "superseded":
            failures.append(f"approve_ready_did_not_close_brief:{brief.status}")
        if item.next_action != "record_chapter_continuity":
            failures.append(f"approve_ready_wrong_next_action:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="Reading Assessment Hard Gate Reopen", genre="网游武侠", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=4, title="第四章", status="continuity_recorded")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第四章",
            content="表面通过但章节类型门禁仍失败的正文" * 1200,
            status="approved",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第4章：表面通过但仍需校验章节类型门禁。",
            required_beats="承接上一章；必须有外部压力、选择代价和章末后果。",
            constraints="3000-4500中文字符。",
            status="ready",
        )
        session.add(brief)
        session.flush()
        quality = QualityReport(
            chapter_version_id=version.id,
            score=73,
            passed=True,
            report=json.dumps(_hard_gate_reopen_report(), ensure_ascii=False),
        )
        session.add(quality)
        session.flush()
        try:
            approve_chapter(session, version_id=version.id, reviewer="regression")
            # Sprint 2 Phase E (2026-07-05, commit 2bbfc1a): approve_chapter is
            # a workflow-progression step, NOT a duplicate content review. All
            # content gates (hard_gate, chapter_type_gate, editorial) run
            # upstream in planning. So calling approve on an already-approved
            # version is either a re-approval (accepted) or a no-op — it must
            # NOT raise on hard_gate content signals. The pre-Phase-E behaviour
            # (reject with "门禁失败") was intentionally dropped.
        except ValueError as exc:
            # The only remaining rejection is the "approved -> approved"
            # transition guard from the state machine, which raises a
            # transition-invalid error. Anything else is a regression.
            if "invalid chapter_version transition" not in str(exc):
                failures.append(f"hard_gate_direct_approve_wrong_error:{exc}")
        item = plan_chapters(session, book_id=book.id, start=4, count=1)[0]
        report_data = json.loads(quality.report or "{}")
        # Phase E: approved chapters are terminal in the content-review sense.
        # Even if chapter_type_gate.passed=False in the historical QR, the
        # planner does NOT reopen an approved chapter — it proceeds to the
        # publish workflow (create_publish_job or mark_publish_job). Content
        # gates only fire on non-approved versions, so an already-approved
        # chapter is trusted as-is until an explicit human demote.
        if item.next_action not in {"create_publish_job", "mark_publish_job", "approve_chapter"}:
            failures.append(f"phase_e_approved_should_advance_publish:{item.next_action}:{item.reason}")
        if version.status != "approved":
            failures.append(f"phase_e_approved_should_stay_approved:{version.status}")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("reading-assessment-state-regression: PASS")
    return 0


def _auto_revision_report() -> dict:
    return {
        "status": "PASS",
        "score": 75,
        "passed": True,
        "issues": [],
        "warnings": [],
        "dimensions": {
            "author_intent": 20,
            "brief_coverage": 52,
            "readability": 66,
            "reader_momentum": 68,
            "hook_strength": 79,
            "scene_atmosphere": 51,
            "payoff_grounding": 64,
            "chapter_necessity": 58,
            "dialogue_fullness": 67,
            "character_voice": 88,
            "prose_voice": 80,
            "chapter_unit_flow": 68,
            "imageable_paragraphs": 58,
        },
        "llm_review": {
            "status": "completed",
            "verdict": "pass",
            "score": 78,
            "strengths": ["主事件可保留", "章末钩子成立"],
            "issues": ["开篇牵引偏慢", "章节承诺覆盖不足"],
            "revision_suggestions": ["更快进入冲突", "补齐奖励和代价落点"],
        },
    }


def _approve_ready_report() -> dict:
    dims = {
        "author_intent": 88,
        "brief_coverage": 86,
        "readability": 82,
        "reader_momentum": 84,
        "hook_strength": 86,
        "scene_atmosphere": 76,
        "payoff_grounding": 82,
        "chapter_necessity": 84,
        "dialogue_fullness": 78,
        "character_voice": 80,
        "prose_voice": 82,
        "chapter_unit_flow": 80,
        "imageable_paragraphs": 75,
    }
    return {
        "status": "PASS",
        "score": 88,
        "passed": True,
        "issues": [],
        "warnings": [],
        "dimensions": dims,
        "llm_review": {"status": "completed", "verdict": "pass", "score": 88, "strengths": ["整体稳定"]},
    }


def _hard_gate_reopen_report() -> dict:
    dims = {
        "author_intent": 80,
        "brief_coverage": 80,
        "readability": 80,
        "reader_momentum": 80,
        "hook_strength": 80,
        "scene_atmosphere": 80,
        "payoff_grounding": 80,
        "chapter_necessity": 80,
        "dialogue_fullness": 80,
        "character_voice": 80,
        "prose_voice": 80,
        "chapter_unit_flow": 80,
        "imageable_paragraphs": 80,
        "conflict_pressure": 50,
        "choice_and_cost": 50,
    }
    return {
        "status": "PASS",
        "score": 73,
        "passed": True,
        "base_quality_passed": True,
        "hard_gate": {"status": "PASS", "passed": True},
        "chapter_type_gate": {
            "schema": "chapter_type_gate_v1",
            "passed": False,
            "failures": ["conflict_pressure=50<68", "choice_and_cost=50<68"],
        },
        "issues": ["chapter_type_gate_failed:conflict_pressure=50<68,choice_and_cost=50<68"],
        "warnings": [],
        "dimensions": dims,
        "llm_review": {"status": "completed", "verdict": "pass", "score": 85},
    }


def _structural_rebuild_report() -> dict:
    dims = {
        "author_intent": 95,
        "brief_coverage": 51,
        "readability": 66,
        "reader_momentum": 66,
        "hook_strength": 69,
        "scene_atmosphere": 49,
        "payoff_grounding": 70,
        "chapter_necessity": 48,
        "dialogue_fullness": 50,
        "character_voice": 77,
        "prose_voice": 74,
        "chapter_unit_flow": 65,
        "imageable_paragraphs": 58,
        "paragraph_aesthetic": 45,
    }
    return {
        "status": "PASS",
        "score": 71,
        "passed": True,
        "base_quality_passed": True,
        "issues": [],
        "warnings": [],
        "dimensions": dims,
        "llm_review": {"status": "completed", "verdict": "pass", "score": 78, "strengths": []},
    }


def _failed_rebuild_report() -> dict:
    dims = {
        "author_intent": 35,
        "brief_coverage": 48,
        "readability": 69,
        "reader_momentum": 66,
        "hook_strength": 89,
        "scene_atmosphere": 37,
        "payoff_grounding": 64,
        "chapter_necessity": 71,
        "dialogue_fullness": 52,
        "character_voice": 88,
        "prose_voice": 74,
        "chapter_unit_flow": 62,
        "imageable_paragraphs": 40,
        "paragraph_aesthetic": 84,
    }
    return {
        "status": "NEEDS_REVISION",
        "score": 45,
        "passed": False,
        "base_quality_passed": False,
        "issues": ["imageable_underdeveloped: 40"],
        "warnings": [],
        "dimensions": dims,
        "editorial_stratification": {"tier": "E_contaminated"},
        "llm_review": {"status": "completed", "verdict": "fail", "score": 45, "strengths": []},
    }


if __name__ == "__main__":
    raise SystemExit(main())
