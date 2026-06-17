from __future__ import annotations

import json
from datetime import datetime

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterVersion, GenerationTask, QualityReport
from app.services.chapter_samples import TASK_TYPE_CHAPTER_SAMPLE, latest_chapter_samples
from app.services.writer_loop import build_writer_loop_plan
from regression_db import isolated_database


def main() -> int:
    isolated_database("writer-loop-regression")
    with session_scope() as session:
        book_id = _seed_fixture(session)
        chapter = session.query(Chapter).filter_by(book_id=book_id, chapter_number=3).one_or_none()
        version = (
            session.query(ChapterVersion)
            .filter_by(chapter_id=chapter.id)
            .order_by(ChapterVersion.id.desc())
            .first()
        )
        quality = (
            session.query(QualityReport)
            .filter_by(chapter_version_id=version.id)
            .order_by(QualityReport.id.desc())
            .first()
            if version
            else None
        )
        samples = latest_chapter_samples(session, book_id=book_id, chapter_number=1)
        quality_report = json.loads(quality.report) if quality else {}
        version_content = version.content if version else ""
    chapter_plan = build_writer_loop_plan(
        chapter_number=3,
        quality_report=quality_report,
        previous_content=version_content,
        mode="regression",
    )
    sample_plan = build_writer_loop_plan(
        chapter_number=1,
        sample_report=samples.get("diversity_report") or {},
        mode="sample_regression",
    )
    failures = []
    local = chapter_plan.local_revision
    if not local.get("recommended") or local.get("target_dimension") != "visual_staging":
        failures.append("chapter3_visual_local_revision_missing")
    if not chapter_plan.pov_card.get("感官入口"):
        failures.append("pov_card_missing")
    if "小样方向重置" not in sample_plan.focus:
        failures.append("sample_failure_focus_missing")
    if not sample_plan.rewrite_directives:
        failures.append("sample_rewrite_directives_missing")
    result = {
        "status": "pass" if not failures else "attention",
        "failures": failures,
        "chapter3": chapter_plan.to_dict(),
        "chapter1_samples": {
            "gate_passed": samples.get("gate_passed"),
            "score": (samples.get("diversity_report") or {}).get("score"),
            "writer_loop": sample_plan.to_dict(),
        },
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


def _seed_fixture(session) -> int:
    book = Book(title=f"writer-loop-regression-{datetime.utcnow().timestamp()}", genre="真实武侠", target_platform="manual")
    session.add(book)
    session.flush()
    chapter = Chapter(book_id=book.id, chapter_number=3, title="第三章", status="draft")
    session.add(chapter)
    session.flush()
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=1,
        title="第三章",
        content="陈默走进药铺，看见账纸和血痕，却只用一句话概括了所有画面。" * 80,
        status="needs_revision",
        source="manual",
    )
    session.add(version)
    session.flush()
    session.add(
        QualityReport(
            chapter_version_id=version.id,
            score=52,
            passed=False,
            report=json.dumps(
                {
                    "dimensions": {"visual_staging": 42, "brief_coverage": 65},
                    "issues": ["visual_underdeveloped:42"],
                    "warnings": ["weak_design_dimension: visual_staging=42"],
                },
                ensure_ascii=False,
            ),
        )
    )
    sample_task = GenerationTask(
        book_id=book.id,
        task_type=TASK_TYPE_CHAPTER_SAMPLE,
        status="completed",
        input_json=json.dumps({"chapter_number": 1}, ensure_ascii=False),
        output_json=json.dumps(
            {
                "gate_passed": False,
                "diversity_report": {
                    "score": 42,
                    "status": "attention",
                    "issues": ["sample1_uses_banned_old_entry"],
                    "repeated_motifs": ["现实片场"],
                },
                "samples": [],
            },
            ensure_ascii=False,
        ),
    )
    session.add(sample_task)
    session.flush()
    return book.id


if __name__ == "__main__":
    raise SystemExit(main())
