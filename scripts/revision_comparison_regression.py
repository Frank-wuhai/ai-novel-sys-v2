from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, QualityReport
from app.services.revision_comparison import compare_and_restore_if_regressed
from regression_db import isolated_database


def main() -> int:
    isolated_database("revision-comparison-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="Revision Comparison Regression", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        source = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="源稿",
            content="源稿正文" * 900,
            status="reviewed_pass",
            source="draft:regression",
        )
        current = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="更差修订稿",
            content="更差正文" * 900,
            status="needs_revision",
            source="revision:regression",
        )
        session.add_all([source, current])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id,
            score=82,
            passed=True,
            report=json.dumps(
                {
                    "status": "PASS",
                    "score": 82,
                    "passed": True,
                    "dimensions": {
                        "readability": 75,
                        "author_intent": 80,
                        "prose_voice": 78,
                        "dialogue_fullness": 72,
                        "paragraph_aesthetic": 82,
                    },
                },
                ensure_ascii=False,
            ),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id,
            score=64,
            passed=False,
            report=json.dumps(
                {
                    "status": "FAIL",
                    "score": 64,
                    "passed": False,
                    "dimensions": {
                        "readability": 55,
                        "author_intent": 48,
                        "prose_voice": 50,
                        "dialogue_fullness": 42,
                        "paragraph_aesthetic": 52,
                    },
                },
                ensure_ascii=False,
            ),
        )
        task = GenerationTask(
            book_id=book.id,
            task_type="revise_chapter",
            status="completed",
            input_json=json.dumps({"chapter_number": 1, "source_version_id": source.id}, ensure_ascii=False),
            output_json=json.dumps({"version_id": current.id}, ensure_ascii=False),
        )
        session.add_all([source_quality, current_quality, task])
        session.flush()
        result = compare_and_restore_if_regressed(session, current_version=current, current_quality=current_quality)
        restored = session.get(ChapterVersion, result.restored_version_id) if result.restored_version_id else None
        restored_quality = (
            session.query(QualityReport)
            .filter(QualityReport.chapter_version_id == restored.id)
            .order_by(QualityReport.id.desc())
            .first()
            if restored
            else None
        )
        restored_status = restored.status if restored else ""
        restored_source = restored.source if restored else ""
        restored_quality_passed = bool(restored_quality.passed) if restored_quality else False
        restored_quality_report = restored_quality.report if restored_quality else ""
        current_report = json.loads(current_quality.report or "{}")

    with session_scope() as session:
        book = Book(title="Revision Comparison Protected Brief", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        source = ChapterVersion(
            chapter_id=chapter.id,
            version_number=1,
            title="源稿",
            content="源稿正文" * 900,
            status="reviewed_pass",
            source="draft:regression",
        )
        current = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="更差修订稿",
            content="更差正文" * 900,
            status="needs_revision",
            source="revision:regression",
        )
        protected_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估结论：当前稿不是正式批准稿，需要继续修。",
            required_beats="reading_assessment_contract: 修复开头承接、场景展开和人物动机。",
            constraints="人工意图: 保留当前最佳稿作为底稿，但不能直接批准。",
            status="superseded",
        )
        session.add_all([source, current, protected_brief])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id,
            score=82,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 82, "passed": True, "dimensions": {"readability": 75}}, ensure_ascii=False),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id,
            score=61,
            passed=False,
            report=json.dumps({"status": "FAIL", "score": 61, "passed": False, "dimensions": {"readability": 50}}, ensure_ascii=False),
        )
        task = GenerationTask(
            book_id=book.id,
            task_type="revise_chapter",
            status="completed",
            input_json=json.dumps({"chapter_number": 1, "source_version_id": source.id}, ensure_ascii=False),
            output_json=json.dumps({"version_id": current.id}, ensure_ascii=False),
        )
        session.add_all([source_quality, current_quality, task])
        session.flush()
        protected_result = compare_and_restore_if_regressed(session, current_version=current, current_quality=current_quality)
        protected_restored = session.get(ChapterVersion, protected_result.restored_version_id) if protected_result.restored_version_id else None
        protected_quality = (
            session.query(QualityReport)
            .filter(QualityReport.chapter_version_id == protected_restored.id)
            .order_by(QualityReport.id.desc())
            .first()
            if protected_restored
            else None
        )
        protected_report = json.loads(protected_quality.report or "{}") if protected_quality else {}
        protected_brief_status = protected_brief.status
        protected_restored_status = protected_restored.status if protected_restored else ""

    if result.status != "regressed":
        failures.append("comparison_did_not_detect_regression")
    if not restored or restored_status != "reviewed_pass" or not str(restored_source or "").startswith("revision_compare_restore:"):
        failures.append("source_not_restored_as_best_version")
    if not restored_quality or not restored_quality_passed or "revision_comparison_restore" not in restored_quality_report:
        failures.append("restored_quality_missing_restore_report")
    if current_report.get("revision_comparison", {}).get("status") != "regressed":
        failures.append("current_quality_missing_comparison_report")
    if protected_result.status != "regressed":
        failures.append("protected_comparison_did_not_detect_regression")
    if protected_restored_status != "needs_revision":
        failures.append("protected_restore_was_marked_pass")
    if protected_brief_status != "revision_ready":
        failures.append("protected_brief_not_reactivated")
    restore_meta = protected_report.get("revision_comparison_restore", {})
    if not restore_meta.get("protected_brief_id"):
        failures.append("protected_restore_missing_brief_id")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "result": result.to_dict(),
                "protected_result": protected_result.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
