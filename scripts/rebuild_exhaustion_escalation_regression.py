"""Regression: linear-revise + rebuild-batches exhaustion escalation.

Bug (Book2 Ch4 Phase E.3): a chapter that fails to converge through both
linear revisions and multiple rebuild_candidate batches has no terminal
state. The orchestrator returns to `generate_rebuild_candidates` every
round, burning tokens forever. Ch4 accumulated 6+ rebuild batches, 31+
versions, 645 revision briefs before manual intervention.

Fix: when compute_exhaustion_signals detects ≥2 completed rebuild_candidates
tasks AND no gate-passing version exists, the orchestrator escalates to
accept_early_stop with the exhaustion-picked best version, sets
chapter_type_gate.soft_pass=True on its QualityReport, and writes a
`qa_backlog_soft_pass` PlatformFeedback for human follow-up.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from regression_db import isolated_database  # type: ignore

from app.db.session import session_scope
from app.models.entities import (
    Book,
    Chapter,
    ChapterBrief,
    ChapterVersion,
    GenerationTask,
    PlatformFeedback,
    QualityReport,
)
from app.services.llm_queue import QUEUE_REBUILD_CANDIDATES
from app.services.planning import (
    _compute_exhaustion_signals,
    _execute_accept_early_stop,
    ChapterPlanItem,
)
from app.services.production_orchestrator import (
    ProductionSituation,
    decide_production_route,
)
from sqlalchemy import select


def _seed_book(session, *, chapter_count: int = 1):
    import uuid
    book = Book(title=f"EXH-{uuid.uuid4().hex[:6]}", genre="net", status="drafting")
    session.add(book)
    session.flush()
    chapters = []
    for n in range(1, chapter_count + 1):
        ch = Chapter(book_id=book.id, chapter_number=n, title=f"Ch{n}", status="drafting")
        session.add(ch)
        session.flush()
        chapters.append(ch)
    return book, chapters


def _seed_version(session, chapter, *, vnum, status, score):
    v = ChapterVersion(
        chapter_id=chapter.id, version_number=vnum, content="x" * 300,
        status=status, source="revision:ark_openai_compatible",
    )
    session.add(v)
    session.flush()
    qr = QualityReport(chapter_version_id=v.id, score=score, passed=(status == "reviewed_pass"), report="{}")
    session.add(qr)
    session.flush()
    return v, qr


def _seed_revision_brief(session, chapter):
    b = ChapterBrief(
        chapter_id=chapter.id,
        goal="test revise", required_beats="beat", constraints="",
        status="revision_ready",
    )
    session.add(b)
    session.flush()
    return b


def _seed_rebuild_task(session, book_id, chapter_number, status="completed"):
    t = GenerationTask(
        book_id=book_id, task_type=QUEUE_REBUILD_CANDIDATES, status=status,
        input_json=json.dumps({"chapter_number": chapter_number, "attempt": 1}),
        output_json="{}",
    )
    session.add(t)
    session.flush()
    return t


def test_zero_batches_not_exhausted():
    isolated_database("exh-zero-batches")
    with session_scope() as s:
        _, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="needs_revision", score=60)
        b = _seed_revision_brief(s, ch)
        exh, best_num, best_score, count = _compute_exhaustion_signals(s, chapter_id=ch.id, revision_brief=b)
        assert exh is False, f"0 batches should not be exhausted (got exh={exh})"
        assert count == 0
    print("test_zero_batches_not_exhausted PASS")


def test_one_batch_not_exhausted():
    isolated_database("exh-one-batch")
    with session_scope() as s:
        book, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="needs_revision", score=60)
        b = _seed_revision_brief(s, ch)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        exh, _, _, count = _compute_exhaustion_signals(s, chapter_id=ch.id, revision_brief=b)
        assert exh is False
        assert count == 1
    print("test_one_batch_not_exhausted PASS")


def test_two_batches_no_brief_not_exhausted():
    isolated_database("exh-two-no-brief")
    with session_scope() as s:
        book, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="needs_revision", score=60)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        exh, _, _, _ = _compute_exhaustion_signals(s, chapter_id=ch.id, revision_brief=None)
        assert exh is False
    print("test_two_batches_no_brief_not_exhausted PASS")


def test_two_batches_but_has_passing_version_not_exhausted():
    isolated_database("exh-two-with-pass")
    with session_scope() as s:
        book, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="reviewed_pass", score=80)
        b = _seed_revision_brief(s, ch)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        exh, _, _, _ = _compute_exhaustion_signals(s, chapter_id=ch.id, revision_brief=b)
        assert exh is False, "should not exhaust when passing version exists"
    print("test_two_batches_but_has_passing_version_not_exhausted PASS")


def test_two_batches_no_passing_version_IS_exhausted():
    isolated_database("exh-two-no-pass")
    with session_scope() as s:
        book, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="needs_revision", score=60)
        _seed_version(s, ch, vnum=2, status="needs_revision", score=73)
        _seed_version(s, ch, vnum=3, status="needs_revision", score=45)
        b = _seed_revision_brief(s, ch)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        _seed_rebuild_task(s, book.id, ch.chapter_number)
        exh, best_num, best_score, count = _compute_exhaustion_signals(s, chapter_id=ch.id, revision_brief=b)
        assert exh is True, f"should be exhausted (got exh={exh})"
        assert best_num == 2, f"best_version_number should be 2 (highest score 73), got {best_num}"
        assert best_score == 73
        assert count == 2
    print(f"test_two_batches_no_passing_version_IS_exhausted PASS (best v{best_num}@{best_score})")


def test_orchestrator_routes_exhausted_to_soft_pass():
    situation = ProductionSituation(
        chapter_number=4,
        chapter_status="drafting",
        has_brief=True,
        latest_version_status="needs_revision",
        latest_quality_passed=False,
        has_revision_brief=True,
        early_stop_should_stop=True,
        early_stop_best_version=2,
        early_stop_best_score=73,
        rebuild_and_revision_exhausted=True,
        exhausted_best_version_number=2,
        exhausted_best_score=73,
        exhausted_rebuild_batch_count=2,
    )
    decision = decide_production_route(situation)
    assert decision.action == "accept_early_stop", f"expected accept_early_stop, got {decision.action}"
    assert decision.intent == "accept_early_stop_soft_pass", f"expected soft_pass intent, got {decision.intent}"
    assert "soft-pass best v2 score=73" in decision.reason
    print(f"test_orchestrator_routes_exhausted_to_soft_pass PASS ({decision.intent})")


def test_accept_early_stop_soft_pass_execution():
    """When _execute_accept_early_stop sees an exhaustion-escalation item:
       - promotes exhaustion-picked best version to reviewed_pass
       - sets QR.passed=True
       - sets chapter_type_gate.soft_pass=True in QR.report JSON
       - writes a qa_backlog_soft_pass PlatformFeedback row
    """
    isolated_database("exh-execution")
    with session_scope() as s:
        book, chapters = _seed_book(s)
        ch = chapters[0]
        _seed_version(s, ch, vnum=1, status="needs_revision", score=60)
        v2, qr2 = _seed_version(s, ch, vnum=2, status="needs_revision", score=73)
        _seed_version(s, ch, vnum=3, status="needs_revision", score=45)
        reason = (
            "exhaustion escalation: revise+rebuild exhausted after 2 rebuild batches; "
            "soft-pass best v2 score=73 (QA backlog logged)"
        )
        item = ChapterPlanItem(
            chapter_number=ch.chapter_number,
            chapter_id=ch.id,
            brief_id=None,
            latest_version_id=v2.id,
            latest_version_status="needs_revision",
            latest_quality_passed=False,
            publish_job_id=None,
            publish_job_status="",
            next_action="accept_early_stop",
            reason=reason,
        )
        result = _execute_accept_early_stop(s, book_id=book.id, chapter_number=ch.chapter_number, item=item)
        assert result.status == "executed", f"expected executed, got {result.status}"
        s.flush()
        v2_after = s.get(ChapterVersion, v2.id)
        assert v2_after.status == "reviewed_pass", f"v2 status should be reviewed_pass, got {v2_after.status}"
        qr2_after = s.get(QualityReport, qr2.id)
        assert qr2_after.passed is True
        report_data = json.loads(qr2_after.report or "{}")
        assert report_data.get("chapter_type_gate", {}).get("soft_pass") is True, f"soft_pass flag missing: {report_data}"
        assert report_data["chapter_type_gate"]["soft_pass_reason"] == "exhaustion_escalation"
        pf = s.scalars(
            select(PlatformFeedback).where(
                PlatformFeedback.chapter_id == ch.id,
                PlatformFeedback.metric_name == "qa_backlog_soft_pass",
            )
        ).first()
        assert pf is not None, "qa_backlog_soft_pass PlatformFeedback row must exist"
        assert pf.metric_value == str(v2.id)
    print("test_accept_early_stop_soft_pass_execution PASS (backlog + soft_pass gate + QR.passed all set)")


if __name__ == "__main__":
    test_zero_batches_not_exhausted()
    test_one_batch_not_exhausted()
    test_two_batches_no_brief_not_exhausted()
    test_two_batches_but_has_passing_version_not_exhausted()
    test_two_batches_no_passing_version_IS_exhausted()
    test_orchestrator_routes_exhausted_to_soft_pass()
    test_accept_early_stop_soft_pass_execution()
    print("\nrebuild-exhaustion-escalation-regression: PASS")
