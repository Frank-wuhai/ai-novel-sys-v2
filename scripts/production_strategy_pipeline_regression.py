from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.production_strategy import assess_production_strategy, get_production_strategy_rules
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-strategy-pipeline-regression")
    failures: list[str] = []
    rule_names = [rule.name for rule in get_production_strategy_rules()]
    required_order = [
        "active_budget_recovery",
        "active_trend_recovery",
        "pending_trend_recovery_contract",
        "narrow_repairable_gate",
        "regressed_rebuild_candidate",
        "active_rebuild_candidate",
        "blocked_chapter_rebuild",
        "comparison_restore_loop",
        "budget_recovery_pingpong",
        "near_gate_plateau",
        "linear_revision_exhaustion",
        "contract_conflict",
        "quality_rebuild_signal",
        "pass_prediction_rebuild",
        "protected_context",
    ]
    if rule_names != required_order:
        failures.append(f"wrong_rule_pipeline_order:{rule_names}")

    with session_scope() as session:
        book = Book(title="strategy pipeline", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="draft")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="修订第2章",
            required_beats="system_revision_budget_recovery: detected\nrevision_mode:rewrite",
            constraints="revision_mode:targeted\nreading_assessment_auto_quality#1",
            status="revision_ready",
        )
        version = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="第2章",
            content="正文" * 1000,
            status="needs_revision",
            source="revision_budget_recovery:v1",
        )
        session.add_all([brief, version])
        session.flush()
        quality = QualityReport(chapter_version_id=version.id, score=55, passed=False, report=json.dumps({"issues": ["needs_rebuild"]}, ensure_ascii=False))
        session.add(quality)
        session.flush()
        assessment = assess_production_strategy(
            session,
            chapter_id=chapter.id,
            latest_version=version,
            latest_quality=quality,
            revision_brief=brief,
        )
        if assessment.intent != "continue_active_budget_recovery" or assessment.action:
            failures.append(f"pipeline_did_not_short_circuit_active_recovery:{assessment}")

    if failures:
        print(json.dumps({"status": "fail", "failures": failures}, ensure_ascii=False, indent=2))
        return 1
    print("production-strategy-pipeline-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
