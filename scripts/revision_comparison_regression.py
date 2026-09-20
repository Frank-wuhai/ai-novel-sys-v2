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
            constraints="修订方向: 保留当前最佳稿作为底稿，但不能直接批准。\nrevision_mode:fresh",
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
        protected_brief_text = "\n".join([protected_brief.goal or "", protected_brief.required_beats or "", protected_brief.constraints or ""])
        protected_restored_status = protected_restored.status if protected_restored else ""

    with session_scope() as session:
        book = Book(title="Revision Comparison Unit Flow Protected Brief", genre="玄幻", target_platform="manual")
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
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        current = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="失败U1补丁",
            content="失败正文" * 900,
            status="needs_revision",
            source="revision:unit_flow_patch",
        )
        unit_flow_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="U1局部补丁：共7个单元只重写第1个 L1-L10,其他6个单元保持不动",
            required_beats="补场景描绘、心理链、动作反应链。",
            constraints="revision_mode:local_patch；unit_flow；只替换第一个单元。max_chars=2500",
            status="superseded",
        )
        later_reading_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估定点修订第1章：以当前最佳稿为底稿，禁止整章重写。",
            required_beats="reading_assessment_contract: 系统自动阅读评估生成。",
            constraints="revision_mode:targeted",
            status="superseded",
        )
        session.add_all([source, current, unit_flow_brief, later_reading_brief])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id,
            score=74,
            passed=True,
            report=json.dumps({"status": "PASS", "score": 74, "passed": True, "base_quality_passed": True, "dimensions": {"brief_coverage": 66}}, ensure_ascii=False),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id,
            score=74,
            passed=False,
            report=json.dumps({"status": "NEEDS_REVISION", "score": 74, "passed": False, "base_quality_passed": False, "dimensions": {"brief_coverage": 45}}, ensure_ascii=False),
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
        unit_flow_result = compare_and_restore_if_regressed(session, current_version=current, current_quality=current_quality)
        unit_flow_restored = session.get(ChapterVersion, unit_flow_result.restored_version_id) if unit_flow_result.restored_version_id else None
        unit_flow_restored_status = unit_flow_restored.status if unit_flow_restored else ""
        unit_flow_brief_status = unit_flow_brief.status
        later_reading_brief_status = later_reading_brief.status

    with session_scope() as session:
        book = Book(title="Revision Comparison Base Pass Regression", genre="玄幻", target_platform="manual")
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
            status="needs_revision",
            source="revision:regression",
        )
        current = ChapterVersion(
            chapter_id=chapter.id,
            version_number=2,
            title="小降但基础失败稿",
            content="小降正文" * 900,
            status="needs_revision",
            source="revision:regression",
        )
        session.add_all([source, current])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id,
            score=72,
            passed=False,
            report=json.dumps(
                {"status": "NEEDS_REVISION", "score": 72, "passed": False, "base_quality_passed": True, "dimensions": {"readability": 70}},
                ensure_ascii=False,
            ),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id,
            score=70,
            passed=False,
            report=json.dumps(
                {"status": "NEEDS_REVISION", "score": 70, "passed": False, "base_quality_passed": False, "dimensions": {"readability": 69}},
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
        base_pass_result = compare_and_restore_if_regressed(session, current_version=current, current_quality=current_quality)

    # 2026-09-20 第 4.5 步场景 A: QC 只读路径(allow_restore=False)——检出回退但不得改版本状态
    with session_scope() as session:
        book = Book(title="Revision Comparison Readonly QC", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        source = ChapterVersion(
            chapter_id=chapter.id, version_number=1, title="源稿", content="源稿正文" * 900,
            status="reviewed_pass", source="draft:regression",
        )
        current = ChapterVersion(
            chapter_id=chapter.id, version_number=2, title="回退修订稿", content="回退正文" * 900,
            status="needs_revision", source="revision:regression",
        )
        session.add_all([source, current])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id, score=80, passed=True,
            report=json.dumps({"status": "PASS", "score": 80, "passed": True, "dimensions": {"readability": 75}}, ensure_ascii=False),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id, score=60, passed=False,
            report=json.dumps({"status": "FAIL", "score": 60, "passed": False, "dimensions": {"readability": 50}}, ensure_ascii=False),
        )
        task = GenerationTask(
            book_id=book.id, task_type="revise_chapter", status="completed",
            input_json=json.dumps({"chapter_number": 1, "source_version_id": source.id}, ensure_ascii=False),
            output_json=json.dumps({"version_id": current.id}, ensure_ascii=False),
        )
        session.add_all([source_quality, current_quality, task])
        session.flush()
        version_count_before = session.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter.id).count()
        readonly_result = compare_and_restore_if_regressed(
            session, current_version=current, current_quality=current_quality, allow_restore=False
        )
        version_count_after = session.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter.id).count()
        readonly_current_report = json.loads(current_quality.report or "{}")

    # 2026-09-20 第 4.5 步场景 B: 用户裁决保护——简报含「用户裁决」指令的修订稿禁止自动恢复
    with session_scope() as session:
        book = Book(title="Revision Comparison User Adjudication Guard", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第一章", status="draft")
        session.add(chapter)
        session.flush()
        source = ChapterVersion(
            chapter_id=chapter.id, version_number=1, title="源稿", content="源稿正文" * 900,
            status="reviewed_pass", source="draft:regression",
        )
        current = ChapterVersion(
            chapter_id=chapter.id, version_number=2, title="用户裁决修订稿", content="裁决正文" * 900,
            status="needs_revision", source="revision:unit_flow_patch",
        )
        adjudicated_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="【用户裁决修订指令·最高优先级】删去老道问句，其余保留。",
            required_beats="只做最小必要删改。",
            constraints="revision_mode:local_patch",
            status="superseded",
        )
        session.add_all([source, current, adjudicated_brief])
        session.flush()
        source_quality = QualityReport(
            chapter_version_id=source.id, score=80, passed=True,
            report=json.dumps({"status": "PASS", "score": 80, "passed": True, "dimensions": {"readability": 75}}, ensure_ascii=False),
        )
        current_quality = QualityReport(
            chapter_version_id=current.id, score=60, passed=False,
            report=json.dumps({"status": "FAIL", "score": 60, "passed": False, "dimensions": {"readability": 50}}, ensure_ascii=False),
        )
        task = GenerationTask(
            book_id=book.id, task_type="revise_chapter", status="completed",
            input_json=json.dumps(
                {"chapter_number": 1, "source_version_id": source.id, "revision_brief_id": adjudicated_brief.id},
                ensure_ascii=False,
            ),
            output_json=json.dumps({"version_id": current.id}, ensure_ascii=False),
        )
        session.add_all([source_quality, current_quality, task])
        session.flush()
        adjudicated_version_count_before = session.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter.id).count()
        adjudicated_result = compare_and_restore_if_regressed(session, current_version=current, current_quality=current_quality)
        adjudicated_version_count_after = session.query(ChapterVersion).filter(ChapterVersion.chapter_id == chapter.id).count()

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
    if (
        "revision_mode:targeted" not in protected_brief_text
        or "revision_mode:fresh" in protected_brief_text
        or "修订模式:fresh" in protected_brief_text
    ):
        failures.append(f"protected_brief_not_downgraded_to_targeted:{protected_brief_text}")
    if "禁止整章重写" not in protected_brief_text:
        failures.append("protected_brief_missing_no_fresh_guard")
    restore_meta = protected_report.get("revision_comparison_restore", {})
    if not restore_meta.get("protected_brief_id"):
        failures.append("protected_restore_missing_brief_id")
    if unit_flow_result.status != "regressed":
        failures.append("unit_flow_protected_comparison_did_not_detect_regression")
    if unit_flow_restored_status != "needs_revision":
        failures.append(f"unit_flow_protected_restore_was_marked_pass:{unit_flow_restored_status}")
    if unit_flow_brief_status != "revision_ready":
        failures.append(f"unit_flow_protected_brief_not_reactivated:{unit_flow_brief_status}")
    if base_pass_result.status != "regressed" or not base_pass_result.restored_version_id:
        failures.append("base_quality_pass_regression_not_restored")
    if readonly_result.status != "regressed_readonly" or readonly_result.restored_version_id is not None:
        failures.append(f"readonly_qc_unexpected:{readonly_result.to_dict()}")
    if version_count_after != version_count_before:
        failures.append("readonly_qc_mutated_version_state")
    if readonly_current_report.get("revision_comparison", {}).get("status") != "regressed_readonly":
        failures.append("readonly_qc_missing_comparison_report")
    if adjudicated_result.status != "regressed_protected" or adjudicated_result.restored_version_id is not None:
        failures.append(f"user_adjudication_unexpected:{adjudicated_result.to_dict()}")
    if adjudicated_version_count_after != adjudicated_version_count_before:
        failures.append("user_adjudication_guard_mutated_version_state")
    print(
        json.dumps(
            {
                "status": "fail" if failures else "pass",
                "failures": failures,
                "result": result.to_dict(),
                "protected_result": protected_result.to_dict(),
                "unit_flow_result": unit_flow_result.to_dict(),
                "base_pass_result": base_pass_result.to_dict(),
                "readonly_result": readonly_result.to_dict(),
                "adjudicated_result": adjudicated_result.to_dict(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
