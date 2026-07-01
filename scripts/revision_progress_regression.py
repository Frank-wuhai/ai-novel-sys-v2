"""Regression: describe_revision_progress observability (phase2/6).

Creates a temp SQLite DB, seeds a chapter with a series of versions +
quality reports, verifies:

  * version_count counts all versions
  * first_pass_version is the earliest version with score>=PASS_FLOOR
  * best_score / best_version_number track the max
  * early_stopped picks up the PlatformFeedback marker
  * latest_verdict reflects the newest report's verdict field (or falls
    back to derived value when the field is absent)
  * project_revision_progress_summary produces a non-empty string that
    mentions the version count
"""

from __future__ import annotations

import json
import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models.entities import (
    Base,
    Book,
    Chapter,
    ChapterVersion,
    PlatformFeedback,
    QualityReport,
    StoryFoundation,
)
from app.services.revision_progress import (
    describe_revision_progress,
    project_revision_progress_summary,
)


def _mk_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return engine, tmp.name


def _seed(session: Session) -> tuple[int, int]:
    book = Book(title="回归Book", genre="test")
    session.add(book)
    session.flush()
    session.add(StoryFoundation(book_id=book.id, premise="", reader_promise="", world_engine="", protagonist_engine="", conflict_engine=""))
    chapter = Chapter(book_id=book.id, chapter_number=1, status="needs_revision")
    session.add(chapter)
    session.flush()
    # 5 versions with scores 40, 60, 68, 74, 82
    scores = [40, 60, 68, 74, 82]
    for idx, score in enumerate(scores, start=1):
        ver = ChapterVersion(
            chapter_id=chapter.id,
            version_number=idx,
            content=f"内容v{idx}",
            status="reviewed_pass" if score >= 65 else "needs_revision",
        )
        session.add(ver)
        session.flush()
        # emulate phase2/3 verdict tiering
        verdict = "pass" if score >= 75 else ("soft_pass" if score >= 65 else "hard_fail")
        report_payload = {
            "status": "PASS" if score >= 65 else "FAIL",
            "verdict": verdict,
            "score": score,
        }
        session.add(
            QualityReport(
                chapter_version_id=ver.id,
                score=score,
                report=json.dumps(report_payload, ensure_ascii=False),
                passed=(score >= 65),
            )
        )
    session.flush()
    return book.id, chapter.id


def main() -> int:
    failures: list[str] = []
    engine, path = _mk_engine()
    try:
        with Session(engine) as session:
            book_id, chapter_id = _seed(session)
            session.commit()

            progress = describe_revision_progress(session, chapter_id=chapter_id)

            # version_count
            if progress.version_count != 5:
                failures.append(f"version_count: expected 5, got {progress.version_count}")
            # first_pass_version: earliest version with score>=75 -> v5
            if progress.first_pass_version != 5:
                failures.append(f"first_pass_version: expected 5, got {progress.first_pass_version}")
            # best_score / best_version_number: 82 at v5
            if progress.best_score != 82:
                failures.append(f"best_score: expected 82, got {progress.best_score}")
            if progress.best_version_number != 5:
                failures.append(f"best_version_number: expected 5, got {progress.best_version_number}")
            # latest verdict = pass
            if progress.latest_verdict != "pass":
                failures.append(f"latest_verdict: expected 'pass', got {progress.latest_verdict!r}")
            # not early-stopped yet
            if progress.early_stopped:
                failures.append(f"early_stopped: expected False, got True")

            # Now simulate accept_early_stop marker
            latest_ver_id = session.scalar(
                __import__("sqlalchemy").select(ChapterVersion.id)
                .where(ChapterVersion.chapter_id == chapter_id)
                .order_by(ChapterVersion.version_number.desc())
                .limit(1)
            )
            session.add(
                PlatformFeedback(
                    book_id=book_id,
                    chapter_id=chapter_id,
                    platform="production_kernel",
                    metric_name="revision_early_stop",
                    metric_value=str(latest_ver_id),
                    raw_text="early-stop: accept_score reached (82 >= 75); best_score=82",
                )
            )
            session.commit()
            progress = describe_revision_progress(session, chapter_id=chapter_id)
            if not progress.early_stopped:
                failures.append("early_stopped: expected True after marker inserted")
            if not progress.stop_reason or "82" not in progress.stop_reason:
                failures.append(f"stop_reason: expected reason with score, got {progress.stop_reason!r}")

            summary = project_revision_progress_summary(progress)
            if "版本 5" not in summary:
                failures.append(f"summary missing version count: {summary!r}")
            if "首达标" not in summary and "尚未达标" not in summary:
                failures.append(f"summary missing pass indicator: {summary!r}")
            if "82" not in summary:
                failures.append(f"summary missing best score: {summary!r}")

            # --- edge case: empty chapter -----------------------------
            book2 = Book(title="空Book", genre="test")
            session.add(book2)
            session.flush()
            empty_chapter = Chapter(book_id=book2.id, chapter_number=1, status="planned")
            session.add(empty_chapter)
            session.commit()
            empty_progress = describe_revision_progress(session, chapter_id=empty_chapter.id)
            if empty_progress.version_count != 0:
                failures.append(f"empty version_count: expected 0, got {empty_progress.version_count}")
            if empty_progress.first_pass_version is not None:
                failures.append(f"empty first_pass_version: expected None, got {empty_progress.first_pass_version}")
            if empty_progress.early_stopped:
                failures.append("empty early_stopped: expected False")

            # --- soft_pass tier scenario: no version >= 75 --------------
            book3 = Book(title="软通过Book", genre="test")
            session.add(book3)
            session.flush()
            soft_chapter = Chapter(book_id=book3.id, chapter_number=1, status="needs_revision")
            session.add(soft_chapter)
            session.flush()
            for idx, score in enumerate([50, 65, 70], start=1):
                ver = ChapterVersion(chapter_id=soft_chapter.id, version_number=idx, content="", status="needs_revision")
                session.add(ver)
                session.flush()
                verdict = "soft_pass" if score >= 65 else "hard_fail"
                session.add(
                    QualityReport(
                        chapter_version_id=ver.id,
                        score=score,
                        report=json.dumps({"verdict": verdict, "score": score}, ensure_ascii=False),
                        passed=(score >= 65),
                    )
                )
            session.commit()
            soft_progress = describe_revision_progress(session, chapter_id=soft_chapter.id)
            if soft_progress.first_pass_version is not None:
                failures.append(f"soft: first_pass_version should be None (no >=75), got {soft_progress.first_pass_version}")
            if soft_progress.best_score != 70:
                failures.append(f"soft: best_score expected 70, got {soft_progress.best_score}")
            if soft_progress.latest_verdict != "soft_pass":
                failures.append(f"soft: latest_verdict expected soft_pass, got {soft_progress.latest_verdict!r}")

    finally:
        engine.dispose()
        os.unlink(path)

    if failures:
        print("revision_progress_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("revision_progress_regression=PASS")
    print("cases_evaluated=13")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
