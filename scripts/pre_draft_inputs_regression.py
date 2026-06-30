from __future__ import annotations

import json

from sqlalchemy import select

from app.db.session import session_scope
from app.models.entities import Book, Chapter, ChapterBrief, ChapterVersion, GenerationTask, PlatformFeedback, QualityReport, StoryArc, StoryFoundation
from app.services.chapter_samples import TASK_TYPE_CHAPTER_SAMPLE
from app.services.planning import plan_chapters, run_next_action
from app.services.pre_draft_inputs import evaluate_pre_draft_inputs
from regression_db import isolated_database


def main() -> int:
    isolated_database("pre-draft-inputs-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = Book(title="pre draft inputs", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        skeleton = {
            "premise": "主角进入江湖",
            "reader_promise": "每章有目标、阻碍和章末钩子",
            "world_engine": "江湖规则有代价",
            "protagonist_engine": "主角靠观察和交易推进",
            "conflict_engine": "玩家竞争持续升级",
            "arc_goal": "前五章建立玩家竞争",
            "arc_climax": "捕快介入改变局面",
            "arc_turn": "同行玩家暴露信息差",
        }
        session.add(
            StoryFoundation(
                book_id=book.id,
                premise=skeleton["premise"],
                reader_promise=skeleton["reader_promise"],
                world_engine=skeleton["world_engine"],
                protagonist_engine=skeleton["protagonist_engine"],
                conflict_engine=skeleton["conflict_engine"],
            )
        )
        session.add(StoryArc(book_id=book.id, arc_number=1, start_chapter=1, end_chapter=5, goal=skeleton["arc_goal"], climax=skeleton["arc_climax"], turn=skeleton["arc_turn"]))
        for key, value in skeleton.items():
            session.add(PlatformFeedback(book_id=book.id, platform="system", metric_name="skeleton_approval", metric_value=key, raw_text=value))
        previous = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="reviewed")
        session.add(previous)
        session.flush()
        previous_version = ChapterVersion(
            chapter_id=previous.id,
            version_number=1,
            title="第2章",
            content="上一章稳定正文。" * 200,
            status="reviewed_pass",
            source="regression",
        )
        session.add(previous_version)
        session.flush()
        session.add(QualityReport(chapter_version_id=previous_version.id, score=82, passed=True, report="{}"))

        chapter = Chapter(book_id=book.id, chapter_number=3, title="第3章", status="briefing")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第3章：玩家竞争继续升级",
            required_beats="承接上一章，主角遇到新的同行玩家。",
            constraints="3000-4500中文字符",
            status="ready",
        )
        session.add(brief)
        session.flush()

        item = plan_chapters(session, book_id=book.id, start=3, count=1)[0]
        if item.next_action != "generate_chapter_samples":
            failures.append(f"missing_sample_did_not_route_to_samples:{item.next_action}:{item.reason}")

        sample_task = GenerationTask(
            book_id=book.id,
            task_type=TASK_TYPE_CHAPTER_SAMPLE,
            status="completed",
            input_json=json.dumps({"chapter_number": 3}, ensure_ascii=False),
            output_json=json.dumps(
                {
                    "samples": [
                        {
                            "index": 2,
                            "title": "茶棚试探",
                            "direction": "用玩家试探制造竞争压力",
                            "opening": "林北按住茶碗，先看对方手背旧伤。",
                            "scene_plan": "茶棚交易、试探、捕快出现。",
                            "exploration_axis": "玩家竞争规则误判",
                            "experiment_hypothesis": "同行不会直接暴露身份",
                            "difference_from_existing": "从交易切入",
                            "anti_ai_flavor_strategy": "用动作替代说明",
                            "pov_strategy": "贴近主角误判",
                            "precision_strategy": "先证据后判断",
                        }
                    ],
                    "diversity_report": {
                        "score": 80,
                        "status": "pass",
                        "recommended_sample_index": 2,
                        "usable_sample_indices": [2],
                    },
                },
                ensure_ascii=False,
            ),
        )
        session.add(sample_task)
        session.flush()

        permit = evaluate_pre_draft_inputs(session, book_id=book.id, chapter_number=3, brief=brief)
        if permit.action != "adopt_recommended_chapter_sample" or permit.recommended_sample_index != 2:
            failures.append(f"recommended_sample_not_detected:{permit}")
        item = plan_chapters(session, book_id=book.id, start=3, count=1)[0]
        if item.next_action != "adopt_recommended_chapter_sample":
            failures.append(f"recommended_sample_not_routed:{item.next_action}:{item.reason}")

        result = run_next_action(session, book_id=book.id, chapter_number=3, dry_run=False)
        if result.action != "adopt_recommended_chapter_sample" or result.status != "executed":
            failures.append(f"adopt_action_failed:{result}")
        item = plan_chapters(session, book_id=book.id, start=3, count=1)[0]
        if item.next_action != "draft_chapter":
            failures.append(f"adopted_sample_did_not_unlock_draft:{item.next_action}:{item.reason}")
        if "production_optimization@v1" not in (brief.required_beats or ""):
            failures.append("skeleton_preflight_not_written")

    with session_scope() as session:
        book = Book(title="pre draft previous stability", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        previous = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="drafting")
        current = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add_all([previous, current])
        session.flush()
        unstable = ChapterVersion(
            chapter_id=previous.id,
            version_number=7,
            title="第1章",
            content="上一章仍在修订。" * 200,
            status="needs_revision",
            source="regression",
        )
        session.add(unstable)
        session.flush()
        session.add(QualityReport(chapter_version_id=unstable.id, score=74, passed=False, report="{}"))
        current_brief = ChapterBrief(
            chapter_id=current.id,
            goal="第2章：必须承接上一章结尾",
            required_beats="上一章后果必须落到开头。",
            constraints="3000-4500中文字符",
            status="ready",
        )
        session.add(current_brief)
        session.flush()

        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "wait_previous_chapter_readable":
            failures.append(f"unstable_previous_chapter_not_blocked:{item.next_action}:{item.reason}")
        result = run_next_action(session, book_id=book.id, chapter_number=2, dry_run=True)
        if result.action != "wait_previous_chapter_readable" or result.status != "preview":
            failures.append(f"unstable_previous_preview_wrong:{result}")
        result = run_next_action(session, book_id=book.id, chapter_number=2, dry_run=False)
        if result.action != "wait_previous_chapter_readable" or result.status != "blocked":
            failures.append(f"unstable_previous_action_not_blocked:{result}")

        stable = ChapterVersion(
            chapter_id=previous.id,
            version_number=8,
            title="第1章",
            content="上一章已稳定定稿。" * 200,
            status="reviewed_pass",
            source="regression",
        )
        session.add(stable)
        session.flush()
        session.add(QualityReport(chapter_version_id=stable.id, score=82, passed=True, report="{}"))
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "generate_chapter_samples":
            failures.append(f"stable_previous_chapter_did_not_unlock_samples:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="pre draft previous deferred", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        previous = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="continuity_deferred")
        current = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add_all([previous, current])
        session.flush()
        deferred = ChapterVersion(
            chapter_id=previous.id,
            version_number=1,
            title="第1章",
            content="上一章暂存稿。" * 300,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(deferred)
        session.flush()
        session.add(QualityReport(chapter_version_id=deferred.id, score=72, passed=False, report="{}"))
        session.add(
            ChapterBrief(
                chapter_id=current.id,
                goal="第2章：承接暂存上一章继续推进。",
                required_beats="上一章后果必须落到开头。",
                constraints="3000-4500中文字符",
                status="ready",
            )
        )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "wait_previous_chapter_readable":
            failures.append(f"deferred_previous_chapter_should_block_samples:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="pre create missing chapter blocked", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        previous = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(previous)
        session.flush()
        version = ChapterVersion(
            chapter_id=previous.id,
            version_number=1,
            title="第1章",
            content="未通过正文。" * 300,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(version)
        session.flush()
        session.add(QualityReport(chapter_version_id=version.id, score=74, passed=False, report="{}"))
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=2, count=1)[0]
        if item.next_action != "wait_previous_chapter_readable":
            failures.append(f"missing_next_chapter_should_wait_previous:{item.next_action}:{item.reason}")

    with session_scope() as session:
        book = Book(title="pre draft deferred segment gate", genre="test", target_platform="manual")
        session.add(book)
        session.flush()
        deferred_chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="continuity_deferred")
        ch5 = Chapter(book_id=book.id, chapter_number=5, title="第5章", status="reviewed")
        ch6 = Chapter(book_id=book.id, chapter_number=6, title="第6章", status="briefing")
        session.add_all([deferred_chapter, ch5, ch6])
        session.flush()
        deferred_version = ChapterVersion(
            chapter_id=deferred_chapter.id,
            version_number=1,
            title="第2章",
            content="第2章暂存稿。" * 300,
            status="needs_revision",
            source="revision:regression",
        )
        stable_v5 = ChapterVersion(
            chapter_id=ch5.id,
            version_number=1,
            title="第5章",
            content="第5章稳定稿。" * 300,
            status="reviewed_pass",
            source="revision:regression",
        )
        session.add_all([deferred_version, stable_v5])
        session.flush()
        session.add(QualityReport(chapter_version_id=deferred_version.id, score=72, passed=False, report="{}"))
        session.add(QualityReport(chapter_version_id=stable_v5.id, score=82, passed=True, report="{}"))
        session.add(
            ChapterBrief(
                chapter_id=ch6.id,
                goal="第6章：进入下一生产段。",
                required_beats="承接第五章。",
                constraints="3000-4500中文字符",
                status="ready",
            )
        )
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=6, count=1)[0]
        if item.next_action != "resolve_deferred_backlog":
            failures.append(f"deferred_segment_gate_not_blocked:{item.next_action}:{item.reason}")
        result = run_next_action(session, book_id=book.id, chapter_number=6, dry_run=True)
        if result.action != "resolve_deferred_backlog" or result.status != "preview":
            failures.append(f"deferred_segment_gate_preview_wrong:{result}")
        result = run_next_action(session, book_id=book.id, chapter_number=6, dry_run=False)
        if result.action != "resolve_deferred_backlog" or result.status != "blocked":
            failures.append(f"deferred_segment_gate_action_not_blocked:{result}")
        deferred_chapter.status = "continuity_recorded"
        deferred_version.status = "reviewed_pass"
        latest_q = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == deferred_version.id)
            .order_by(QualityReport.id.desc())
        )
        latest_q.passed = True
        session.flush()
        item = plan_chapters(session, book_id=book.id, start=6, count=1)[0]
        if item.next_action != "draft_chapter":
            failures.append(f"deferred_segment_gate_did_not_unlock_after_clear:{item.next_action}:{item.reason}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("pre-draft-inputs-regression: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
