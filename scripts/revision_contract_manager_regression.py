from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from regression_db import isolated_database
from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, QualityReport
from app.services.revision_contract_manager import latest_revision_mode, normalize_active_revision_contract


def main() -> int:
    isolated_database("revision-contract-manager-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="合同回归", genre="玄幻", target_platform="manual")
        session.add(book)
        session.flush()
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="drafting")
        session.add(chapter)
        session.flush()
        version = ChapterVersion(chapter_id=chapter.id, version_number=1, title="第1章", content="正文", status="needs_revision")
        session.add(version)
        session.flush()
        old = ChapterBrief(chapter_id=chapter.id, goal="旧合同", required_beats="", constraints="revision_mode:targeted", status="revision_ready")
        active = ChapterBrief(
            chapter_id=chapter.id,
            goal="新合同\n修订模式:targeted",
            required_beats="旧残留",
            constraints="禁止整章重写\nrevision_mode:rewrite",
            status="revision_ready",
        )
        session.add_all([old, active])
        quality = QualityReport(
            chapter_version_id=version.id,
            score=45,
            passed=False,
            report=json.dumps(
                {
                    "production_failure_classification": {
                        "category": "structure_rewrite",
                        "structural_reasons": ["length_out_of_range"],
                    }
                },
                ensure_ascii=False,
            ),
        )
        session.add(quality)
        session.flush()
        audit = normalize_active_revision_contract(session, chapter_id=chapter.id, quality=quality)
        if not audit or audit.revision_mode != "rewrite":
            failures.append(f"rewrite_not_preserved:{audit}")
        if old.status != "superseded":
            failures.append("older_contract_not_superseded")
        if latest_revision_mode("\n".join([active.goal, active.required_beats, active.constraints])) != "rewrite":
            failures.append("final_mode_not_rewrite")
        if "修订模式:targeted" in active.goal or "revision_mode:targeted" in active.constraints:
            failures.append("old_mode_line_not_removed")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("revision-contract-manager-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
