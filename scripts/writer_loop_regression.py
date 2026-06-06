from __future__ import annotations

import json

from app.db.session import session_scope
from app.models.entities import Chapter, ChapterVersion, QualityReport
from app.services.chapter_samples import latest_chapter_samples
from app.services.writer_loop import build_writer_loop_plan


def main() -> int:
    with session_scope() as session:
        chapter = session.query(Chapter).filter_by(book_id=2, chapter_number=3).one_or_none()
        if not chapter:
            print(json.dumps({"status": "attention", "error": "chapter3_missing"}, ensure_ascii=False, indent=2))
            return 1
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
        samples = latest_chapter_samples(session, book_id=2, chapter_number=1)
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


if __name__ == "__main__":
    raise SystemExit(main())
