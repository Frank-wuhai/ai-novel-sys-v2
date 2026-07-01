"""Regression: manual override for early-stop (phase2/7).

Covers app/services/revision_manual_override.py:

  1. request_manual_revision_continuation refuses non-'needs_confirmation'
     chapters and returns performed=False with an explanatory reason.
  2. On a chapter sitting in needs_confirmation (post accept_early_stop):
     - chapter.status flips back to needs_revision
     - a PlatformFeedback row with metric_name='revision_manual_override'
       is appended, metric_value = current highest version_number.
     - performed=True, baseline_version_number is set.
  3. find_active_override_baseline returns the override's baseline when
     no later stop marker exists, and returns None once a fresh
     accept_early_stop marker is appended (spent-window semantics).
  4. The pruning behaviour that planning.py implements: after an
     override with baseline=5, only versions with version_number>5
     should feed the early-stop engine. We verify by calling
     collect_version_scores + the same filter directly.
"""

from __future__ import annotations

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
from app.services.production_state import collect_version_scores
from app.services.revision_manual_override import (
    OVERRIDE_METRIC,
    STOP_METRIC,
    find_active_override_baseline,
    request_manual_revision_continuation,
)


def _mk_engine():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    engine = create_engine(f"sqlite:///{tmp.name}")
    Base.metadata.create_all(engine)
    return engine, tmp.name


def _seed_chapter_with_versions(session: Session, *, status: str, num_versions: int, title: str = "ov书") -> tuple[int, int]:
    book = Book(title=title, genre="test")
    session.add(book)
    session.flush()
    session.add(StoryFoundation(book_id=book.id, premise="", reader_promise="", world_engine="", protagonist_engine="", conflict_engine=""))
    chapter = Chapter(book_id=book.id, chapter_number=1, status=status)
    session.add(chapter)
    session.flush()
    for idx in range(1, num_versions + 1):
        ver = ChapterVersion(chapter_id=chapter.id, version_number=idx, content=f"v{idx}", status="needs_revision")
        session.add(ver)
        session.flush()
        session.add(QualityReport(chapter_version_id=ver.id, score=60 + idx, report="{}", passed=(60 + idx) >= 65))
    session.flush()
    return book.id, chapter.id


def main() -> int:
    failures: list[str] = []
    engine, path = _mk_engine()
    try:
        with Session(engine) as session:
            # -------- case 1: wrong status ------------------------------
            _, ch_id = _seed_chapter_with_versions(session, status="needs_revision", num_versions=3, title="ov书1")
            session.commit()
            r1 = request_manual_revision_continuation(session, chapter_id=ch_id, note="try")
            if r1.performed:
                failures.append(f"case1: needs_revision chapter should not be reopened, got performed=True")
            if "not eligible" not in r1.reason:
                failures.append(f"case1: reason should mention 'not eligible', got {r1.reason!r}")

            # -------- case 2: unknown chapter --------------------------
            r2 = request_manual_revision_continuation(session, chapter_id=99999, note="")
            if r2.performed:
                failures.append("case2: unknown chapter should return performed=False")
            if "not found" not in r2.reason:
                failures.append(f"case2: reason should mention 'not found', got {r2.reason!r}")

            # -------- case 3: valid override ----------------------------
            _, ch2_id = _seed_chapter_with_versions(session, status="needs_confirmation", num_versions=5, title="ov书2")
            session.commit()
            r3 = request_manual_revision_continuation(session, chapter_id=ch2_id, note="继续修")
            if not r3.performed:
                failures.append(f"case3: valid override should return performed=True, got {r3.reason!r}")
            if r3.baseline_version_number != 5:
                failures.append(f"case3: baseline should be 5, got {r3.baseline_version_number}")
            ch2 = session.get(Chapter, ch2_id)
            if ch2.status != "needs_revision":
                failures.append(f"case3: chapter status should be needs_revision, got {ch2.status!r}")
            marker = session.execute(
                __import__("sqlalchemy").select(PlatformFeedback)
                .where(PlatformFeedback.chapter_id == ch2_id, PlatformFeedback.metric_name == OVERRIDE_METRIC)
            ).scalar_one_or_none()
            if marker is None:
                failures.append("case3: override marker not written")
            elif marker.metric_value != "5":
                failures.append(f"case3: marker metric_value should be '5', got {marker.metric_value!r}")
            elif "继续修" not in marker.raw_text:
                failures.append(f"case3: marker raw_text should include note, got {marker.raw_text!r}")

            session.commit()

            # -------- case 4: find_active_override_baseline returns 5 --
            baseline = find_active_override_baseline(session, chapter_id=ch2_id)
            if baseline != 5:
                failures.append(f"case4: baseline should be 5, got {baseline}")

            # -------- case 5: after stop marker, baseline goes None ----
            session.add(
                PlatformFeedback(
                    book_id=ch2.book_id,
                    chapter_id=ch2_id,
                    platform="production_kernel",
                    metric_name=STOP_METRIC,
                    metric_value="7",
                    raw_text="fresh stop after override",
                )
            )
            session.commit()
            baseline = find_active_override_baseline(session, chapter_id=ch2_id)
            if baseline is not None:
                failures.append(f"case5: after a fresh stop marker, baseline should be None, got {baseline}")

            # -------- case 6: another override reopens the window ------
            ch2.status = "needs_confirmation"
            session.add(ch2)
            session.commit()
            r6 = request_manual_revision_continuation(session, chapter_id=ch2_id, note="again")
            if not r6.performed:
                failures.append("case6: second override should perform")
            baseline = find_active_override_baseline(session, chapter_id=ch2_id)
            if baseline is None:
                failures.append("case6: after second override, baseline should be reactivated (not None)")

            # -------- case 7: pruning drops old versions ---------------
            # add 2 more versions (v6, v7) to the chapter, then verify that
            # after the (still-active) override_baseline=5, only v6,v7 pass
            # through the phase2/7 pruning filter that planning.py applies.
            ver6 = ChapterVersion(chapter_id=ch2_id, version_number=6, content="v6", status="needs_revision")
            session.add(ver6)
            session.flush()
            session.add(QualityReport(chapter_version_id=ver6.id, score=72, report="{}", passed=True))
            ver7 = ChapterVersion(chapter_id=ch2_id, version_number=7, content="v7", status="needs_revision")
            session.add(ver7)
            session.flush()
            session.add(QualityReport(chapter_version_id=ver7.id, score=76, report="{}", passed=True))
            session.commit()

            all_scores = collect_version_scores(session, ch2_id)
            baseline = find_active_override_baseline(session, chapter_id=ch2_id)
            pruned = [vs for vs in all_scores if vs.version_number > (baseline or 0)]
            if [vs.version_number for vs in pruned] != [6, 7]:
                failures.append(f"case7: pruning failed, expected [6,7] got {[vs.version_number for vs in pruned]}")
            # early-stop shouldn't even fire on 2 versions -- min warm-up
            from app.services.revision_early_stop import evaluate_early_stop
            decision = evaluate_early_stop(pruned)
            if decision.should_stop:
                failures.append(f"case7: policy should NOT stop on 2 fresh versions, got should_stop=True")

    finally:
        engine.dispose()
        os.unlink(path)

    if failures:
        print("revision_manual_override_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("revision_manual_override_regression=PASS")
    print("cases_evaluated=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
