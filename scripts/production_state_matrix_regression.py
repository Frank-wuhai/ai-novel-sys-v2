from __future__ import annotations

import json
from dataclasses import dataclass

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.planning import plan_chapters
from app.services.production_decision import decide_chapter_production
from regression_db import isolated_database


@dataclass(frozen=True)
class Case:
    name: str
    version_status: str
    source: str
    brief_required: str
    brief_constraints: str
    quality_score: int | None
    quality_passed: bool | None
    quality_report: dict
    expected_action: str
    expected_intent: str
    forbidden_label: str = "刷新状态"
    chapter_status: str = "briefing"


def main() -> int:
    isolated_database("production-state-matrix-regression")
    failures: list[str] = []
    cases = [
        Case(
            name="normal_revision_contract",
            version_status="needs_revision",
            source="revision:ark_openai_compatible",
            brief_required="质检报告 #1\n修订模式:rewrite",
            brief_constraints="必须修复场景、对白、后果。",
            quality_score=72,
            quality_passed=False,
            quality_report={"score": 72, "status": "NEEDS_REVISION"},
            expected_action="revise_chapter",
            expected_intent="continue",
        ),
        Case(
            name="active_budget_recovery",
            version_status="needs_revision",
            source="revision_budget_recovery:v1",
            brief_required="system_revision_budget_recovery: detected\n修订模式:rewrite",
            brief_constraints="system_revision_budget_recovery: 系统自行换策略。\nrevision_mode:targeted",
            quality_score=None,
            quality_passed=None,
            quality_report={},
            expected_action="revise_chapter",
            expected_intent="continue",
        ),
        Case(
            name="selected_rebuild_candidate",
            version_status="needs_revision",
            source="rebuild_candidate_selected:v2",
            brief_required="reading_assessment_auto_quality#9\n当前阅读层级：需重建\n失败结构不得沿用。",
            brief_constraints="revision_mode:rewrite",
            quality_score=69,
            quality_passed=False,
            quality_report={"score": 69, "reading_assessment": {"action": "auto_rebuild", "status": "needs_revision"}},
            expected_action="revise_chapter",
            expected_intent="continue",
        ),
        Case(
            name="reviewed_pass_needs_continuity",
            version_status="reviewed_pass",
            source="review:system",
            brief_required="",
            brief_constraints="",
            quality_score=82,
            quality_passed=True,
            quality_report={"score": 82, "passed": True},
            expected_action="record_chapter_continuity",
            expected_intent="continue",
            chapter_status="draft",
        ),
        Case(
            name="continuity_recorded_needs_approval",
            version_status="reviewed_pass",
            source="review:system",
            brief_required="",
            brief_constraints="",
            quality_score=82,
            quality_passed=True,
            quality_report={"score": 82, "passed": True},
            expected_action="approve_chapter",
            expected_intent="approve",
            chapter_status="continuity_recorded",
            forbidden_label="",
        ),
    ]
    with session_scope() as session:
        book = Book(title="state matrix", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        for index, case in enumerate(cases, start=1):
            chapter = Chapter(book_id=book.id, chapter_number=index, title=f"第{index}章", status=case.chapter_status)
            session.add(chapter)
            session.flush()
            session.add(ChapterBrief(chapter_id=chapter.id, goal=f"{case.name} base brief", required_beats="基础章纲", constraints="", status="ready"))
            if case.brief_required or case.brief_constraints:
                session.add(
                    ChapterBrief(
                        chapter_id=chapter.id,
                        goal=f"{case.name} revision brief",
                        required_beats=case.brief_required,
                        constraints=case.brief_constraints,
                        status="revision_ready",
                    )
                )
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=1,
                title=f"第{index}章",
                content="正文" * 1200,
                status=case.version_status,
                source=case.source,
            )
            session.add(version)
            session.flush()
            if case.quality_score is not None:
                session.add(
                    QualityReport(
                        chapter_version_id=version.id,
                        score=case.quality_score,
                        passed=bool(case.quality_passed),
                        report=json.dumps(case.quality_report, ensure_ascii=False),
                    )
                )
            session.flush()

            item = plan_chapters(session, book_id=book.id, start=index, count=1)[0]
            decision = decide_chapter_production(item)
            if item.next_action != case.expected_action:
                failures.append(f"{case.name}:action:{item.next_action}!={case.expected_action}:{item.reason}")
            if decision.primary_intent != case.expected_intent:
                failures.append(f"{case.name}:intent:{decision.primary_intent}!={case.expected_intent}:{decision.to_dict()}")
            if case.forbidden_label and decision.primary_label == case.forbidden_label:
                failures.append(f"{case.name}:forbidden_label:{decision.to_dict()}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-state-matrix-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
