from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from app.core.config import settings
from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, PlatformFeedback, QualityReport, StoryArc, StoryFoundation
from app.services.evidence import add_evidence_source, add_market_signal
from app.services.llm_queue import enqueue_draft_chapter, enqueue_revise_chapter
from app.services.planning import plan_chapters
from app.services.production import create_book, create_foundation
from app.services.readiness import check_production_readiness
from app.services.revision_supervisor import persistent_revision_budget
from regression_db import isolated_database


def main() -> int:
    isolated_database("production-hardening-regression")
    failures: list[str] = []

    with session_scope() as session:
        book = _create_gate_ready_book(session)
        chapter = Chapter(book_id=book.id, chapter_number=6, title="第6章", status="drafting")
        session.add(chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=chapter.id,
                goal="第6章推进主线。",
                required_beats="承接上一章后果，制造新的行动压力。",
                constraints="3000-4500中文字符。",
                status="ready",
            )
        )
        session.add(
            ChapterVersion(
                chapter_id=chapter.id,
                version_number=1,
                title="第6章",
                content="这是一版需要修订的正文。",
                status="needs_revision",
                source="regression",
            )
        )
        session.add(
            ChapterBrief(
                chapter_id=chapter.id,
                goal="修订第6章。",
                required_beats="保留主线，补足行动和后果。",
                constraints="不要推翻底稿。",
                status="revision_ready",
            )
        )
        session.flush()

        draft_task = enqueue_draft_chapter(session, book_id=book.id, chapter_number=6, dry_run=True)
        try:
            enqueue_revise_chapter(session, book_id=book.id, chapter_number=6, dry_run=True)
        except ValueError as exc:
            message = str(exc)
            if "active generation queue task already exists for chapter 6" not in message or "queue_draft_chapter" not in message:
                failures.append(f"wrong_same_chapter_queue_error:{message}")
        else:
            failures.append(f"same_chapter_cross_queue_not_blocked:{draft_task.id}")

        recovered_chapter = Chapter(book_id=book.id, chapter_number=7, title="第7章", status="continuity_recorded")
        session.add(recovered_chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=recovered_chapter.id,
                goal="第7章继续推进。",
                required_beats="承接上一章，完成一次选择。",
                constraints="3000-4500中文字符。",
                status="ready",
            )
        )
        readable = ChapterVersion(
            chapter_id=recovered_chapter.id,
            version_number=1,
            title="第7章",
            content="通过稿正文。" * 900,
            status="reviewed_pass",
            source="regression_passed",
        )
        session.add(readable)
        session.flush()
        recovered = ChapterVersion(
            chapter_id=recovered_chapter.id,
            version_number=2,
            title="第7章",
            content=readable.content,
            status="needs_revision",
            source=f"revision_budget_readable_restore:v{readable.id}",
        )
        session.add(recovered)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=recovered.id,
                score=86,
                passed=True,
                report=json.dumps(
                    {
                        "status": "PASS",
                        "score": 86,
                        "passed": True,
                        "hard_gate": {"status": "PASS", "passed": True},
                        "issues": [],
                        "dimensions": {
                            "author_intent": 86,
                            "brief_coverage": 86,
                            "readability": 86,
                            "reader_momentum": 86,
                            "hook_strength": 86,
                            "scene_atmosphere": 86,
                            "payoff_grounding": 86,
                            "chapter_necessity": 86,
                            "dialogue_fullness": 86,
                            "character_voice": 86,
                            "prose_voice": 86,
                            "chapter_unit_flow": 86,
                            "imageable_paragraphs": 86,
                        },
                        "llm_review": {"status": "completed", "verdict": "pass", "score": 86},
                    },
                    ensure_ascii=False,
                ),
            )
        )
        recovery_brief = ChapterBrief(
            chapter_id=recovered_chapter.id,
            goal="预算恢复后保留的修订合同。",
            required_beats="reading_assessment_auto_quality#1",
            constraints="persistent_revision_budget:3>=2；system_revision_budget_recovery",
            status="revision_ready",
        )
        session.add(recovery_brief)
        session.flush()
        recovered_item = plan_chapters(session, book_id=book.id, start=7, count=1, apply_state_repairs=True)[0]
        session.refresh(recovered)
        session.refresh(recovery_brief)
        if recovered.status != "reviewed_pass":
            failures.append(f"passed_budget_recovery_not_accepted:{recovered.status}:{recovered_item.next_action}")
        if recovery_brief.status != "superseded":
            failures.append(f"passed_budget_recovery_brief_not_closed:{recovery_brief.status}")
        if recovered_item.next_action != "approve_chapter":
            failures.append(f"passed_budget_recovery_wrong_next_action:{recovered_item.next_action}:{recovered_item.reason}")

        targeted_chapter = Chapter(book_id=book.id, chapter_number=8, title="第8章", status="continuity_recorded")
        session.add(targeted_chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=targeted_chapter.id,
                goal="第8章继续推进。",
                required_beats="承接上一章，完成一次定点修订。",
                constraints="3000-4500中文字符。",
                status="ready",
            )
        )
        targeted_version = ChapterVersion(
            chapter_id=targeted_chapter.id,
            version_number=1,
            title="第8章",
            content="预算恢复后的可读稿。" * 900,
            status="needs_revision",
            source="revision_budget_readable_restore:v100",
        )
        session.add(targeted_version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=targeted_version.id,
                score=76,
                passed=True,
                report='{"status":"PASS","score":76,"passed":true}',
            )
        )
        targeted_brief = ChapterBrief(
            chapter_id=targeted_chapter.id,
            goal="以恢复稿为底稿做定向首屏衔接修订。",
            required_beats=f"合同当前底稿：v{targeted_version.id}；revision_mode:targeted",
            constraints="只改开头和必要过渡，不推翻主线。",
            status="revision_ready",
        )
        session.add(targeted_brief)
        session.flush()
        targeted_item = plan_chapters(session, book_id=book.id, start=8, count=1, apply_state_repairs=True)[0]
        if targeted_item.next_action != "revise_chapter":
            failures.append(f"targeted_recovery_brief_budget_blocked:{targeted_item.next_action}:{targeted_item.reason}")

        stale_contract_chapter = Chapter(book_id=book.id, chapter_number=9, title="第9章", status="continuity_recorded")
        session.add(stale_contract_chapter)
        session.flush()
        old_version = ChapterVersion(
            chapter_id=stale_contract_chapter.id,
            version_number=1,
            title="第9章",
            content="旧底稿。" * 900,
            status="needs_revision",
            source="revision:old",
        )
        current_version = ChapterVersion(
            chapter_id=stale_contract_chapter.id,
            version_number=2,
            title="第9章",
            content="新通过稿。" * 900,
            status="needs_revision",
            source="revision:new",
        )
        session.add_all([old_version, current_version])
        session.flush()
        stale_brief = ChapterBrief(
            chapter_id=stale_contract_chapter.id,
            goal="旧阅读评估合同。",
            required_beats=f"reading_assessment_auto_quality#9；源版本锁定：v{old_version.id}",
            constraints="reading_assessment_contract: old",
            status="revision_ready",
        )
        session.add_all(
            [
                ChapterBrief(
                    chapter_id=stale_contract_chapter.id,
                    goal="第9章继续推进。",
                    required_beats="承接上一章。",
                    constraints="3000-4500中文字符。",
                    status="ready",
                ),
                QualityReport(
                    chapter_version_id=current_version.id,
                    score=86,
                    passed=True,
                    report=json.dumps(
                        {
                            "status": "PASS",
                            "score": 86,
                            "passed": True,
                            "hard_gate": {"status": "PASS", "passed": True},
                            "issues": [],
                            "dimensions": {
                                "author_intent": 86,
                                "brief_coverage": 86,
                                "readability": 86,
                                "reader_momentum": 86,
                                "hook_strength": 86,
                                "scene_atmosphere": 86,
                                "payoff_grounding": 86,
                                "chapter_necessity": 86,
                                "dialogue_fullness": 86,
                                "character_voice": 86,
                                "prose_voice": 86,
                                "chapter_unit_flow": 86,
                                "imageable_paragraphs": 86,
                            },
                            "llm_review": {"status": "completed", "verdict": "pass", "score": 86},
                        },
                        ensure_ascii=False,
                    ),
                ),
                stale_brief,
            ]
        )
        session.flush()
        stale_item = plan_chapters(session, book_id=book.id, start=9, count=1, apply_state_repairs=True)[0]
        session.refresh(current_version)
        session.refresh(stale_brief)
        if current_version.status != "reviewed_pass" or stale_brief.status != "superseded":
            failures.append(f"stale_contract_not_closed:{current_version.status}:{stale_brief.status}")
        if stale_item.next_action != "approve_chapter":
            failures.append(f"stale_contract_wrong_next_action:{stale_item.next_action}:{stale_item.reason}")

        current_contract_chapter = Chapter(book_id=book.id, chapter_number=10, title="第10章", status="continuity_recorded")
        session.add(current_contract_chapter)
        session.flush()
        current_contract_version = ChapterVersion(
            chapter_id=current_contract_chapter.id,
            version_number=1,
            title="第10章",
            content="当前底稿。" * 900,
            status="needs_revision",
            source="revision:current",
        )
        session.add(current_contract_version)
        session.flush()
        session.add_all(
            [
                ChapterBrief(
                    chapter_id=current_contract_chapter.id,
                    goal="第10章继续推进。",
                    required_beats="承接上一章。",
                    constraints="3000-4500中文字符。",
                    status="ready",
                ),
                QualityReport(
                    chapter_version_id=current_contract_version.id,
                    score=76,
                    passed=True,
                    report='{"status":"PASS","score":76,"passed":true}',
                ),
                ChapterBrief(
                    chapter_id=current_contract_chapter.id,
                    goal="当前底稿定向修订。",
                    required_beats=f"reading_assessment_auto_quality#10；合同当前底稿：v{current_contract_version.id}",
                    constraints="reading_assessment_contract: current；revision_mode:targeted",
                    status="revision_ready",
                ),
            ]
        )
        for index in range(3):
            session.add(
                GenerationTask(
                    book_id=book.id,
                    task_type="revise_chapter",
                    status="completed",
                    input_json=f'{{"chapter_number": 10, "revision_mode": "targeted", "index": {index}}}',
                    output_json='{"strategy":"full_revision","elapsed_ms":1000}',
                )
            )
        session.flush()
        current_contract_item = plan_chapters(session, book_id=book.id, start=10, count=1, apply_state_repairs=True)[0]
        if current_contract_item.next_action != "revise_chapter":
            failures.append(f"current_contract_budget_blocked:{current_contract_item.next_action}:{current_contract_item.reason}")

        restore_loop_chapter = Chapter(book_id=book.id, chapter_number=11, title="第11章", status="continuity_recorded")
        session.add(restore_loop_chapter)
        session.flush()
        session.add(
            ChapterBrief(
                chapter_id=restore_loop_chapter.id,
                goal="第11章继续推进。",
                required_beats="承接上一章。",
                constraints="3000-4500中文字符。",
                status="ready",
            )
        )
        restore_loop_versions: list[ChapterVersion] = []
        for index, (source, score, passed) in enumerate(
            [
                ("revision:bad_a", 72, False),
                ("revision_compare_restore:v1", 76, False),
                ("revision:bad_b", 45, False),
                ("revision_compare_restore:v2", 76, False),
            ],
            start=1,
        ):
            version = ChapterVersion(
                chapter_id=restore_loop_chapter.id,
                version_number=index,
                title=f"第11章 v{index}",
                content=f"正文{index}" * 900,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            restore_loop_versions.append(version)
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=passed,
                    report=f'{{"status":"NEEDS_REVISION","score":{score},"passed":false}}',
                )
            )
        session.add(
            ChapterBrief(
                chapter_id=restore_loop_chapter.id,
                goal="当前底稿定向修订。",
                required_beats=f"reading_assessment_auto_quality#11；合同当前底稿：v{restore_loop_versions[-1].id}",
                constraints="reading_assessment_contract: current；revision_mode:targeted",
                status="revision_ready",
            )
        )
        session.flush()
        restore_loop_item = plan_chapters(session, book_id=book.id, start=11, count=1, apply_state_repairs=True)[0]
        if restore_loop_item.next_action != "generate_rebuild_candidates":
            failures.append(f"comparison_restore_loop_not_escalated:{restore_loop_item.next_action}:{restore_loop_item.reason}")

        dry_run_budget_chapter = Chapter(book_id=book.id, chapter_number=12, title="第12章", status="draft")
        session.add(dry_run_budget_chapter)
        session.flush()
        dry_run_task = GenerationTask(
            book_id=book.id,
            task_type="revise_chapter",
            status="completed",
            input_json=json.dumps({"chapter_number": 12, "revision_mode": "rewrite", "dry_run": True}, ensure_ascii=False),
            output_json=json.dumps({"version_id": 999001, "provider": "regression", "elapsed_ms": 1000}, ensure_ascii=False),
        )
        real_task = GenerationTask(
            book_id=book.id,
            task_type="revise_chapter",
            status="completed",
            input_json=json.dumps({"chapter_number": 12, "revision_mode": "rewrite"}, ensure_ascii=False),
            output_json=json.dumps({"version_id": 999002, "provider": "regression", "elapsed_ms": 1000}, ensure_ascii=False),
        )
        session.add_all([dry_run_task, real_task])
        session.flush()
        dry_run_budget = persistent_revision_budget(session, book_id=book.id, chapter_number=12, max_full_revisions=2)
        if dry_run_budget.full_revision_count != 1 or dry_run_budget.exceeded:
            failures.append(f"persistent_budget_counted_dry_run_tasks:{dry_run_budget.to_dict()}")

        evidence_book = Book(title="Evidence Mode Regression", genre="硬化题材", target_platform="manual")
        session.add(evidence_book)
        session.flush()
        add_evidence_source(session, source_id="regression-source", title="regression", reliability=3, status="verified")
        add_market_signal(
            session,
            genre=evidence_book.genre,
            signal_text="近期高热作品强调强钩子和稳定兑现。",
            confidence=80,
            source_key="regression-source",
        )

        original_mode = settings.production_mode
        try:
            object.__setattr__(settings, "production_mode", "trial")
            trial_report = check_production_readiness(session, book_id=evidence_book.id)
            trial_evidence = _check_by_name(trial_report, "evidence")
            if not trial_evidence or not trial_evidence.passed or trial_evidence.severity != "warning":
                failures.append(f"trial_thin_evidence_not_warning:{trial_evidence}")

            object.__setattr__(settings, "production_mode", "production")
            production_report = check_production_readiness(session, book_id=evidence_book.id)
            production_evidence = _check_by_name(production_report, "evidence")
            if not production_evidence or production_evidence.passed or production_evidence.severity != "blocker":
                failures.append(f"production_thin_evidence_not_blocker:{production_evidence}")
            elif "insufficient_for_production" not in production_evidence.detail:
                failures.append(f"production_evidence_missing_reason:{production_evidence.detail}")
        finally:
            object.__setattr__(settings, "production_mode", original_mode)

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("production-hardening-regression: PASS")
    return 0


def _create_gate_ready_book(session):
    book = create_book(session, title="Production Hardening Regression", genre="网游武侠", platform="manual")
    foundation = create_foundation(
        session,
        book_id=book.id,
        premise="主角获得虚拟现实武侠网游内测资格。",
        reader_promise="每章都有明确行动、阻碍和兑现。",
        world_engine="游戏收益同步现实，但必须付出代价。",
        protagonist_engine="主角靠观察、交易和试错推进。",
        conflict_engine="玩家竞争与现实异常持续升级。",
    )
    arc = StoryArc(
        book_id=book.id,
        arc_number=1,
        title="开局破局",
        start_chapter=1,
        end_chapter=8,
        goal="建立能力和玩家竞争。",
        climax="主角用信息差完成救场。",
        turn="他发现游戏规则正在反噬现实。",
        status="planning",
    )
    session.add(arc)
    session.flush()
    approvals = {
        "premise": foundation.premise,
        "reader_promise": foundation.reader_promise,
        "world_engine": foundation.world_engine,
        "protagonist_engine": foundation.protagonist_engine,
        "conflict_engine": foundation.conflict_engine,
        "arc_goal": arc.goal,
        "arc_climax": arc.climax,
        "arc_turn": arc.turn,
    }
    for key, value in approvals.items():
        session.add(PlatformFeedback(book_id=book.id, platform="system", metric_name="skeleton_approval", metric_value=key, raw_text=value))
    session.flush()
    return book


def _check_by_name(report, name: str):
    return next((check for check in report.checks if check.name == name), None)


if __name__ == "__main__":
    raise SystemExit(main())
