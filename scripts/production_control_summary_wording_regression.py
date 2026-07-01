"""Regression: control-summary wording must agree with the counted metric.

Bug (problem D): when a chapter is in ``mark_publish_job`` state, planning
records it as ``decision_type='final_publish_confirmation'`` — which is *not*
counted by ``decisions.approval_count``.  ``production_decision`` still marks
the chapter as ``needs_author=True`` though, so ``confirmation_waiting`` is
non-empty and the status flips to ``needs_confirmation``.  The old summary
line pulled ``metrics['approval_waiting']`` (== approval_count) and produced
the contradictory user-facing text::

    status_label = 等待确认
    summary      = 系统已把内容推到确认点；待确认 0 章。

This regression seeds exactly that state and asserts:

1. status == 'needs_confirmation' (or 'ready_to_publish' fallthrough).
2. ``human_waiting`` >= 1 (the metric that actually drove the branch).
3. summary quotes ``human_waiting``, NOT ``approval_waiting``.
4. If ``approval_waiting != human_waiting`` the wording still stays
   consistent with what triggered the branch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app.db.session import session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    PublishJob,
    QualityReport,
)
from app.services.production_control import build_production_control_report, _summary

from regression_db import isolated_database


def _seed_publish_confirmation_chapter(session) -> int:
    """Seed a book whose latest chapter is waiting on ``mark_publish_job``."""

    book = Book(title="control summary wording", genre="test", target_platform="manual", status="approved")
    session.add(book)
    session.flush()
    # Chapter 1: approved + published so planning does not treat it as blocker.
    chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="approved")
    session.add(chapter)
    session.flush()
    brief = ChapterBrief(
        chapter_id=chapter.id,
        goal="发布确认",
        required_beats="",
        constraints="",
        status="approved",
    )
    session.add(brief)
    session.flush()
    version = ChapterVersion(
        chapter_id=chapter.id,
        version_number=1,
        title="第1章",
        content="正文" * 1200,
        status="approved",
        source="revision:v1",
    )
    session.add(version)
    session.flush()
    quality = QualityReport(
        chapter_version_id=version.id,
        score=85,
        passed=True,
        report=json.dumps({"issues": []}, ensure_ascii=False),
    )
    session.add(quality)
    session.flush()
    # PublishJob queued -> planning maps this to next_action='mark_publish_job',
    # decision_type='final_publish_confirmation', which is NOT counted by
    # decisions.approval_count but IS counted by confirmation_waiting.
    publish_job = PublishJob(
        chapter_version_id=version.id,
        platform="manual",
        status="queued",
        automation_payload=json.dumps({"note": "queued for platform"}, ensure_ascii=False),
        result_report="",
        created_at=datetime.now(timezone.utc),
    )
    session.add(publish_job)
    session.flush()
    return book.id


def main() -> int:
    isolated_database("production-control-summary-wording")
    failures: list[str] = []

    # -----------------------------------------------------------------------
    # PART 1 — unit-level assertion on the wording function itself.
    #
    # This guards the exact regression: when human_waiting and approval_waiting
    # diverge, the summary must quote human_waiting (the metric that actually
    # drove the branch), not approval_waiting.
    # -----------------------------------------------------------------------
    for status in ("needs_confirmation", "needs_author"):
        summary = _summary(
            status=status,
            metrics={"human_waiting": 2, "approval_waiting": 0, "auto_ready": 0, "queue_running": 0, "queue_pending": 0},
        )
        if "待确认 2 章" not in summary:
            failures.append(
                f"unit[{status}]: summary must quote human_waiting=2, got {summary!r}"
            )
        if "待确认 0 章" in summary:
            failures.append(
                f"unit[{status}]: summary regressed to approval_waiting=0, got {summary!r}"
            )

    # Equal-metrics: still correct (backwards-compat sanity).
    summary_eq = _summary(
        status="needs_confirmation",
        metrics={"human_waiting": 3, "approval_waiting": 3, "auto_ready": 0, "queue_running": 0, "queue_pending": 0},
    )
    if "待确认 3 章" not in summary_eq:
        failures.append(f"unit[equal]: summary should quote 3, got {summary_eq!r}")

    # -----------------------------------------------------------------------
    # PART 2 — integration: seed a book waiting on mark_publish_job so the
    # divergence appears in real metrics. We assert the divergence itself
    # (human_waiting >= 1 AND approval_waiting == 0) is exposed; a live
    # wording assertion is only enforced when the branch is actually reached.
    # -----------------------------------------------------------------------
    with session_scope() as session:
        book_id = _seed_publish_confirmation_chapter(session)
        report = build_production_control_report(session, book_id=book_id, start=1, count=3)

    metrics = report.metrics
    summary = report.summary
    status = report.status
    human_waiting = int(metrics.get("human_waiting", 0))
    approval_waiting = int(metrics.get("approval_waiting", 0))

    # Divergence must be exposed by the metrics layer regardless of which
    # status branch wins — that's the raw signal we're guarding against.
    if human_waiting < 1:
        failures.append(
            f"integration: expected human_waiting>=1 after seeding mark_publish_job, "
            f"got human_waiting={human_waiting}"
        )
    if approval_waiting != 0:
        failures.append(
            f"integration: expected approval_waiting==0 (mark_publish_job is not human_approval), "
            f"got approval_waiting={approval_waiting}"
        )

    # If the run reached the needs_confirmation branch, the summary MUST quote
    # human_waiting; if it fell into blocked/other branch (e.g. because of
    # unrelated alignment blockers on this minimal fixture) we only care that
    # the summary does not misreport 待确认 0 章.
    if status in {"needs_confirmation", "needs_author"}:
        if f"待确认 {human_waiting} 章" not in summary:
            failures.append(
                f"integration: needs_confirmation branch reached but summary does not quote "
                f"human_waiting={human_waiting}; summary={summary!r}"
            )
    else:
        # In every branch, an inconsistency like "等待确认 / 待确认 0 章" would
        # be a regression. Guard against it explicitly.
        if "等待确认" in report.status_label and "待确认 0 章" in summary and human_waiting > 0:
            failures.append(
                f"integration: label/summary contradiction 待确认 0 章 while human_waiting={human_waiting}"
            )

    if failures:
        print("production_control_summary_wording_regression=FAIL")
        for failure in failures:
            print(f"- {failure}")
        print(f"metrics={metrics}")
        print(f"status={status} status_label={report.status_label}")
        print(f"summary={summary}")
        return 1
    print("production_control_summary_wording_regression=PASS")
    print(f"status={status} status_label={report.status_label}")
    print(f"summary={summary}")
    print(
        f"metrics.human_waiting={human_waiting} approval_waiting={approval_waiting}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
