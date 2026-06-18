from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import build_human_decision_package, plan_chapters
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

        if item.next_action != "reading_assessment_review":
            failures.append(f"unexpected_next_action:{item.next_action}:{item.reason}")
        if not decision.needs_author or decision.primary_intent != "approve":
            failures.append(f"unexpected_decision:{decision.to_dict()}")
        if package.approval_count != 1:
            failures.append(f"missing_human_approval_package:{package.to_dict() if hasattr(package, 'to_dict') else package.approval_count}")

        approve_chapter(session, version_id=version.id, reviewer="regression")
        session.flush()
        if brief.status != "superseded":
            failures.append(f"approval_did_not_close_revision_brief:{brief.status}")

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
        if item.next_action != "revise_chapter":
            failures.append(f"auto_revision_not_routed_to_revise:{item.next_action}:{item.reason}")
        if not latest_brief or "reading_assessment_auto_quality#" not in "\n".join([latest_brief.goal or "", latest_brief.required_beats or "", latest_brief.constraints or ""]):
            failures.append("auto_revision_brief_missing_marker")

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
        if brief.status != "superseded":
            failures.append(f"approve_ready_did_not_close_brief:{brief.status}")
        if item.next_action != "record_chapter_continuity":
            failures.append(f"approve_ready_wrong_next_action:{item.next_action}:{item.reason}")

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


if __name__ == "__main__":
    raise SystemExit(main())
