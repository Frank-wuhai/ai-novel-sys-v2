from __future__ import annotations

import json

from sqlalchemy import func, select, text

from app.db.session import session_scope
from app.llm.providers import LLMResponse
from app.llm.schemas import StructuredOutputError
from app.models.entities import Chapter, ChapterBrief, ChapterVersion, GenerationTask, PromptTemplate, QualityReport, StoryArc
from app.services.feedback import record_platform_feedback
from app.services.planning import plan_chapters, run_next_action
from app.services.production import create_book, create_foundation
from app.services.prompts import seed_prompt_templates
from app.services.rebuild_candidates import (
    TASK_TYPE_REBUILD_CANDIDATES,
    IncumbentDraft,
    _author_sample_anchor_prompt_block,
    _author_sample_anchor_rejection,
    _best_incumbent_draft,
    _prepare_candidate_prompt,
    _run_candidate_llm,
    _should_restore_incumbent_over_candidate,
    generate_rebuild_candidates,
)
from regression_db import isolated_database


def main() -> int:
    isolated_database("rebuild-candidates-regression")
    failures: list[str] = []
    with session_scope() as session:
        book = create_book(session, title="多候选回归书", genre="网游武侠", platform="manual")
        foundation = create_foundation(
            session,
            book_id=book.id,
            premise="主角进入江湖游戏，通过桥段演绎改变现实和游戏双线局面。",
            reader_promise="热闹江湖、主动破局、收益代价同场落地。",
            world_engine="游戏江湖逐步升维。",
            protagonist_engine="主角靠观察、试探、交易和行动破局。",
            conflict_engine="冲突来自桥段误判、现实同步和江湖规矩。",
        )
        session.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS chapter_exit_states (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chapter_id INTEGER,
                    chapter_version_id INTEGER,
                    main_character_state TEXT,
                    relationship_delta TEXT,
                    plot_hook TEXT,
                    new_facts TEXT,
                    physical_location TEXT,
                    time_marker TEXT,
                    raw_summary TEXT,
                    hook_keywords TEXT
                )
                """
            )
        )
        seed_prompt_templates(session)
        revise_v4 = session.scalar(
            select(PromptTemplate).where(PromptTemplate.name == "revise_chapter", PromptTemplate.version == "v4")
        )
        revise_v5 = session.scalar(
            select(PromptTemplate).where(PromptTemplate.name == "revise_chapter", PromptTemplate.version == "v5")
        )
        if revise_v5:
            revise_v5.template = revise_v4.template if revise_v4 else revise_v5.template
            revise_v5.status = "active"
        else:
            session.add(
                PromptTemplate(
                    name="revise_chapter",
                    version="v5",
                    template=revise_v4.template if revise_v4 else "{revision_goal}\n{revision_required_beats}\n{revision_constraints}",
                    status="active",
                )
            )
        session.flush()
        arc = StoryArc(
            book_id=book.id,
            arc_number=1,
            title="初入江湖",
            start_chapter=1,
            end_chapter=12,
            goal="主角确认桥段演绎能力的真实规则，并拿到第一条升维线索。",
            climax="主角用有代价的桥段复刻破掉第一个江湖死局。",
            turn="现实副作用证明游戏和现实正在同步。",
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
            record_platform_feedback(
                session,
                book_id=book.id,
                platform="regression",
                metric_name="skeleton_approval",
                metric_value=key,
                raw_text=value,
            )
        chapter = Chapter(book_id=book.id, chapter_number=1, title="第1章", status="draft")
        session.add(chapter)
        session.flush()
        brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="阅读评估要求重建第1章，旧稿结构不得沿用。",
            required_beats="reading_assessment_auto_quality#1\nrevision_mode:fresh\n主角主动破局；章末现实副作用。",
            constraints="clean_rebuild_contract@1\n失败结构不得沿用；需重建；3000字以上。",
            status="revision_ready",
        )
        session.add(brief)
        session.flush()
        for index, score in enumerate([20, 21, 22], start=1):
            version = ChapterVersion(
                chapter_id=chapter.id,
                version_number=index,
                title=f"失败稿{index}",
                content="旧稿内容。清虚观门口，道士盘问，顾晚差点说内测和NPC。" * 700,
                status="needs_revision",
                source="revision:regression",
            )
            session.add(version)
            session.flush()
            session.add(
                QualityReport(
                    chapter_version_id=version.id,
                    score=score,
                    passed=False,
                    report=json.dumps(
                        {
                            "status": "FAIL",
                            "score": score,
                            "reading_assessment": {"action": "auto_rebuild"},
                            "issues": ["单稿重建连续失败"],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            session.flush()

        latest_source = session.scalar(select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc()))
        latest_quality = session.scalar(
            select(QualityReport).where(QualityReport.chapter_version_id == latest_source.id).order_by(QualityReport.id.desc())
        )
        prepared_prompt = _prepare_candidate_prompt(
            session,
            book=book,
            chapter=chapter,
            chapter_number=1,
            source_version=latest_source,
            brief=brief,
            quality=latest_quality,
            foundation_premise=foundation.premise,
            reader_promise=foundation.reader_promise,
            template=revise_v5,
            strategy={"name": "盘问破局", "opening": "山门盘问", "middle": "江湖话套规矩", "ending": "现实副作用"},
        )
        prompt_text = prepared_prompt["prompt"]
        for marker in ("游戏内世界现场零元概念泄漏", "不得出现：内测、论坛、玩家、NPC", "系统分配我来的", "山门规矩、旧木牌/拜帖/衣着误判"):
            if marker not in prompt_text:
                failures.append(f"rebuild_prompt_missing_world_logic_hard_constraint:{marker}")

        plan = plan_chapters(session, book_id=book.id, start=1, count=1)[0]
        if plan.next_action != "generate_rebuild_candidates":
            failures.append(f"plan_not_candidate_rebuild:{plan.next_action}")
        preview = run_next_action(session, book_id=book.id, chapter_number=1, dry_run=True)
        if preview.action != "generate_rebuild_candidates" or preview.status != "preview":
            failures.append(f"preview_not_candidate_rebuild:{preview.action}:{preview.status}:{preview.message}")
        author_anchor_brief = ChapterBrief(
            chapter_id=chapter.id,
            goal="第1章《旧盔》返修",
            required_beats="标题：旧盔；章末保留“第二个”压力；敲门求伤药。",
            constraints="用户作者样稿约束：旧盔、雪花、药味、伤药、第二个。",
            status="revision_ready",
        )
        anchor_prompt = _author_sample_anchor_prompt_block(author_anchor_brief)
        if "用户样稿不可漂移锚点" not in anchor_prompt or "不得出现：道观、道童" not in anchor_prompt:
            failures.append("author_sample_anchor_prompt_missing")
        prompt_with_anchor = "用户样稿不可漂移锚点\n" + anchor_prompt
        valid_anchor_content = (
            "沈渡抱着旧盔接单，屏幕里雪花乱跳，下一刻整个人摔进山里。"
            "他抬头看见星星，胳膊上全是血，顺着药味摸到窝棚门口，"
            "哑声求一碗伤药，说拿活抵。门后老人说：这个月你是第二个。"
        )
        if _author_sample_anchor_rejection(prompt_with_anchor, valid_anchor_content):
            failures.append("author_sample_anchor_rejected_valid_content")
        drift_content = (
            "沈渡抱着旧盔来到清虚观，屏幕雪花之后摔进山里。"
            "道童捡起铁牌，说这是外门执事身份牌，问他是不是来投师。"
        )
        drift_rejection = _author_sample_anchor_rejection(prompt_with_anchor, drift_content)
        if "forbidden_storyline" not in drift_rejection:
            failures.append(f"author_sample_anchor_failed_to_reject_drift:{drift_rejection}")
        repairable_provider = _RepairingCandidateProvider()
        try:
            repaired_out = _run_candidate_llm(
                repairable_provider,
                prompt="生成清虚观入门候选",
                min_chars=300,
                max_tokens=2000,
                temperature=0.5,
                model="fake",
                candidate_index=1,
                dry_run=False,
            )
            repaired_content = repaired_out["draft"].content or ""
            forbidden_markers = ("系统分配", "内测", "论坛", "玩家", "NPC", "任务栏", "任务面板", "界面", "系统提示", "新手村")
            if any(marker in repaired_content for marker in forbidden_markers):
                failures.append("rebuild_candidate_meta_leak_repair_kept_forbidden_marker")
            repair_meta = (repaired_out["length_repair"] or {}).get("meta_leak_repair") or {}
            if not repair_meta.get("accepted"):
                failures.append("rebuild_candidate_meta_leak_repair_missing_trace")
        except StructuredOutputError as exc:
            failures.append(f"rebuild_candidate_meta_leak_repair_rejected:{exc}")

        leaking_provider = _LeakingCandidateProvider()
        try:
            _run_candidate_llm(
                leaking_provider,
                prompt="生成清虚观入门候选",
                min_chars=300,
                max_tokens=2000,
                temperature=0.5,
                model="fake",
                candidate_index=1,
                dry_run=False,
            )
            failures.append("rebuild_candidate_meta_leak_not_rejected_before_persist")
        except StructuredOutputError as exc:
            if "game-world meta leakage" not in str(exc):
                failures.append(f"rebuild_candidate_meta_leak_wrong_error:{exc}")

        result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=1, dry_run=True)
        selected = session.get(ChapterVersion, int(result.selected_version_id or 0))
        candidate_count = session.scalar(
            select(func.count())
            .select_from(ChapterVersion)
            .where(ChapterVersion.chapter_id == chapter.id, ChapterVersion.source.like("rebuild_candidate:%"))
        )
        if candidate_count != 1:
            failures.append(f"candidate_count_wrong:{candidate_count}")
        rebuild_task = session.scalar(
            select(GenerationTask)
            .where(GenerationTask.id == result.task_id, GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES)
        )
        task_output = json.loads(rebuild_task.output_json or "{}") if rebuild_task else {}
        if not rebuild_task or rebuild_task.status != "completed":
            failures.append(f"rebuild_task_not_completed:{rebuild_task.status if rebuild_task else None}")
        selection_reason = task_output.get("selection_reason")
        if selection_reason == "best_failed_candidate_retained":
            if not selected or selected.status != "needs_revision" or not selected.source.startswith("rebuild_candidate:"):
                failures.append(f"failed_candidate_not_retained:{result.selected_version_id}")
        elif selection_reason == "incumbent_ranked_higher_than_candidates":
            if not selected or not selected.source.startswith("rebuild_candidate_incumbent_restore:"):
                failures.append(f"incumbent_restore_missing:{result.selected_version_id}")
        else:
            if selection_reason != "best_ranked_candidate":
                failures.append("unexpected_selection_reason:" + str(selection_reason))
            if not selected or not selected.source.startswith("rebuild_candidate_selected:v"):
                failures.append(f"selected_version_missing:{result.selected_version_id}")
            latest = session.scalar(
                select(ChapterVersion).where(ChapterVersion.chapter_id == chapter.id).order_by(ChapterVersion.id.desc())
            )
            if selected and (not latest or latest.id != selected.id):
                failures.append("latest_version_not_selected_copy")
            if selected:
                selected_quality = session.scalar(
                    select(QualityReport).where(QualityReport.chapter_version_id == selected.id).order_by(QualityReport.id.desc())
                )
                if not selected_quality:
                    failures.append("selected_quality_missing")
                elif "selected_from_candidate_version_id" not in json.loads(selected_quality.report or "{}"):
                    failures.append("selected_quality_missing_candidate_trace")

        budget_chapter = Chapter(book_id=book.id, chapter_number=2, title="第2章", status="briefing")
        session.add(budget_chapter)
        session.flush()
        budget_brief = ChapterBrief(
            chapter_id=budget_chapter.id,
            goal="自动重建第2章修订目标",
            required_beats="system_revision_budget_recovery: detected\n修订模式:rewrite；预算恢复后重建章节承诺。",
            constraints="system_revision_budget_recovery: 系统自行换策略。\ncoverage_rebuild: brief_coverage",
            status="revision_ready",
        )
        session.add(budget_brief)
        session.flush()
        for index, source in enumerate(
            [
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
                "revision:ark_openai_compatible",
                "revision_budget_recovery:v1",
            ],
            start=1,
        ):
            version = ChapterVersion(
                chapter_id=budget_chapter.id,
                version_number=index,
                title="第2章",
                content=("失败稿" if source.startswith("revision:") else "恢复稿") * 1200,
                status="needs_revision",
                source=source,
            )
            session.add(version)
            session.flush()
            if source.startswith("revision:"):
                session.add(
                    QualityReport(
                        chapter_version_id=version.id,
                        score=45,
                        passed=False,
                        report=json.dumps({"score": 45, "passed": False, "dimensions": {"brief_coverage": 52}}, ensure_ascii=False),
                    )
                )
        session.flush()
        budget_plan = plan_chapters(session, book_id=book.id, start=2, count=1, apply_state_repairs=False)[0]
        if budget_plan.next_action != "generate_rebuild_candidates":
            failures.append(f"budget_recovery_pingpong_not_candidates:{budget_plan.next_action}:{budget_plan.reason}")

        protected_chapter = Chapter(book_id=book.id, chapter_number=3, title="第3章", status="briefing")
        session.add(protected_chapter)
        session.flush()
        protected_brief = ChapterBrief(
            chapter_id=protected_chapter.id,
            goal="根据用户意见重建候选，但不能丢掉首屏衔接。",
            required_beats=(
                "reading_assessment_auto_quality#3\n"
                "修订方向: 只做定向首屏衔接修订，不推翻第3章茶棚遇同行主线。\n"
                "必须在开头300-600字补齐上一章结尾到本章茶棚的过渡。\n"
                "保留第3章既有茶棚遇赵乾、青字纸、玩家试探、捕快压力主线。"
            ),
            constraints="不新增追杀、官方机构、昏迷、系统面板解题。",
            status="revision_ready",
        )
        session.add(protected_brief)
        session.flush()
        protected_version = ChapterVersion(
            chapter_id=protected_chapter.id,
            version_number=1,
            title="第3章",
            content="旧稿" * 1200,
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        session.add(protected_version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=protected_version.id,
                score=76,
                passed=False,
                report=json.dumps({"status": "NEEDS_REVISION", "score": 76, "passed": False}, ensure_ascii=False),
            )
        )
        session.flush()
        protected_result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=3, dry_run=True)
        protected_task = session.scalar(
            select(GenerationTask)
            .where(GenerationTask.id == protected_result.task_id, GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES)
        )
        task_input = json.loads(protected_task.input_json or "{}") if protected_task else {}
        protected_text = task_input.get("protected_rebuild_constraints") or ""
        if "只做定向首屏衔接修订" not in protected_text:
            failures.append("protected_rebuild_missing_user_direction")
        if "茶棚遇同行" not in protected_text:
            failures.append("protected_rebuild_missing_retained_mainline")
        if "不新增追杀、官方机构、昏迷、系统面板解题" not in protected_text:
            failures.append("protected_rebuild_missing_user_forbidden_rules")

        floor_chapter = Chapter(book_id=book.id, chapter_number=4, title="第4章", status="briefing")
        session.add(floor_chapter)
        session.flush()
        high_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=1,
            title="第4章",
            content="历史最佳稿。" * 1200,
            status="needs_revision",
            source="revision_compare_restore:v1",
        )
        session.add(high_version)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=high_version.id,
                score=99,
                passed=True,
                report=json.dumps({"status": "PASS", "score": 99, "passed": True}, ensure_ascii=False),
            )
        )
        low_latest = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=2,
            title="第4章",
            content="当前低分稿。" * 1200,
            status="needs_revision",
            source="revision:regression",
        )
        session.add(low_latest)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=low_latest.id,
                score=45,
                passed=False,
                report=json.dumps({"status": "FAIL", "score": 45, "passed": False}, ensure_ascii=False),
            )
        )
        floor_brief = ChapterBrief(
            chapter_id=floor_chapter.id,
            goal="重建第4章，但不得低于历史最佳稿。",
            required_beats="reading_assessment_auto_quality#4\n需重建失败结构。",
            constraints="3000-4500中文字符。",
            status="revision_ready",
        )
        session.add(floor_brief)
        session.flush()
        floor_result = generate_rebuild_candidates(session, book_id=book.id, chapter_number=4, dry_run=True)
        floor_selected = session.get(ChapterVersion, floor_result.selected_version_id)
        floor_quality = session.scalar(
            select(QualityReport)
            .where(QualityReport.chapter_version_id == floor_result.selected_version_id)
            .order_by(QualityReport.id.desc())
        )
        if not floor_selected or not str(floor_selected.source or "").startswith("rebuild_candidate_incumbent_restore:"):
            failures.append(f"rebuild_floor_did_not_restore_incumbent:{floor_selected.source if floor_selected else None}")
        if not floor_quality or int(floor_quality.score or 0) != 99:
            failures.append(f"rebuild_floor_score_regressed:{floor_quality.score if floor_quality else None}")

        blocker_incumbent_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=99,
            title="第4章",
            content="高分但阻断稿。" * 1200,
            status="needs_revision",
            source="regression:blocking_incumbent",
        )
        clean_candidate_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=100,
            title="第4章",
            content="低分但已关闭阻断候选。" * 1200,
            status="candidate",
            source="regression:clean_candidate",
        )
        session.add_all([blocker_incumbent_version, clean_candidate_version])
        session.flush()
        blocker_quality = QualityReport(
            chapter_version_id=blocker_incumbent_version.id,
            score=81,
            passed=False,
            report=json.dumps(
                {
                    "score": 81,
                    "passed": False,
                    "issues": ["chapter_type_gate_failed:conflict_pressure=58<68"],
                },
                ensure_ascii=False,
            ),
        )
        clean_quality = QualityReport(
            chapter_version_id=clean_candidate_version.id,
            score=71,
            passed=True,
            report=json.dumps({"score": 71, "passed": True, "issues": []}, ensure_ascii=False),
        )
        session.add_all([blocker_quality, clean_quality])
        session.flush()
        should_restore = _should_restore_incumbent_over_candidate(
            incumbent=IncumbentDraft(blocker_incumbent_version, blocker_quality, 81, False),
            candidate={"version_id": clean_candidate_version.id, "score": 71, "passed": True},
            candidate_quality=clean_quality,
        )
        if should_restore:
            failures.append("rebuild_selection_preferred_blocking_incumbent_over_clean_candidate")

        stale_world_logic_incumbent = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=101,
            title="第4章",
            content=(
                "瘦高道士问：谁让你来的？顾晚把我是来参加内测的咽回去。"
                "游戏里 NPC 不吃这套，得按规矩来。"
            ) * 400,
            status="needs_revision",
            source="regression:stale_world_logic_incumbent",
        )
        stale_candidate_version = ChapterVersion(
            chapter_id=floor_chapter.id,
            version_number=102,
            title="第4章",
            content="低分新候选但无玩家层泄漏。" * 1200,
            status="candidate",
            source="regression:world_logic_clean_candidate",
        )
        session.add_all([stale_world_logic_incumbent, stale_candidate_version])
        session.flush()
        stale_quality = QualityReport(
            chapter_version_id=stale_world_logic_incumbent.id,
            score=90,
            passed=False,
            report=json.dumps({"score": 90, "passed": False, "issues": ["old_report_before_world_logic_fix"]}, ensure_ascii=False),
        )
        stale_candidate_quality = QualityReport(
            chapter_version_id=stale_candidate_version.id,
            score=42,
            passed=False,
            report=json.dumps({"score": 42, "passed": False, "issues": ["new_candidate_low_score"]}, ensure_ascii=False),
        )
        session.add_all([stale_quality, stale_candidate_quality])
        session.flush()
        stale_should_restore = _should_restore_incumbent_over_candidate(
            incumbent=IncumbentDraft(stale_world_logic_incumbent, stale_quality, 90, False),
            candidate={"version_id": stale_candidate_version.id, "score": 42, "passed": False},
            candidate_quality=stale_candidate_quality,
        )
        if stale_should_restore:
            failures.append("rebuild_selection_restored_stale_world_logic_incumbent")

        source_only_chapter = Chapter(book_id=book.id, chapter_number=5, title="第5章", status="briefing")
        session.add(source_only_chapter)
        session.flush()
        source_only = ChapterVersion(
            chapter_id=source_only_chapter.id,
            version_number=1,
            title="第5章",
            content="当前失败源稿。" * 1200,
            status="needs_revision",
            source="revision:regression_failed_source",
        )
        session.add(source_only)
        session.flush()
        session.add(
            QualityReport(
                chapter_version_id=source_only.id,
                score=91,
                passed=False,
                report=json.dumps({"status": "FAIL", "score": 91, "passed": False}, ensure_ascii=False),
            )
        )
        session.flush()
        incumbent = _best_incumbent_draft(session, chapter_id=source_only_chapter.id, exclude_version_id=source_only.id)
        if incumbent is not None:
            failures.append(f"rebuild_incumbent_included_current_failed_source:{incumbent.version.id}")

        broken_chapter = Chapter(book_id=book.id, chapter_number=6, title="第6章", status="briefing")
        session.add(broken_chapter)
        session.flush()
        broken_brief = ChapterBrief(
            chapter_id=broken_chapter.id,
            goal="重建第6章。",
            required_beats="reading_assessment_auto_quality#6\n需重建失败结构。",
            constraints="3000-4500中文字符。",
            status="revision_ready",
        )
        broken_source = ChapterVersion(
            chapter_id=broken_chapter.id,
            version_number=1,
            title="第6章",
            content="坏源稿。" * 1200,
            status="needs_revision",
            source="revision:regression",
        )
        historical_incumbent = ChapterVersion(
            chapter_id=broken_chapter.id,
            version_number=0,
            title="第6章",
            content="历史可用稿。" * 1200,
            status="reviewed_pass",
            source="regression:historical_incumbent",
        )
        session.add_all([broken_brief, historical_incumbent])
        session.flush()
        session.add(broken_source)
        session.flush()
        session.add_all(
            [
                QualityReport(
                    chapter_version_id=broken_source.id,
                    score=10,
                    passed=False,
                    report=json.dumps({"status": "FAIL", "score": 10, "passed": False}, ensure_ascii=False),
                ),
                QualityReport(
                    chapter_version_id=historical_incumbent.id,
                    score=99,
                    passed=True,
                    report="{not valid json",
                ),
            ]
        )
        session.flush()
        try:
            generate_rebuild_candidates(session, book_id=book.id, chapter_number=6, dry_run=True)
            failures.append("rebuild_invalid_quality_report_did_not_fail")
        except json.JSONDecodeError:
            failed_task = session.scalar(
                select(GenerationTask)
                .where(GenerationTask.book_id == book.id, GenerationTask.task_type == TASK_TYPE_REBUILD_CANDIDATES)
                .order_by(GenerationTask.id.desc())
            )
            if not failed_task or failed_task.status != "failed":
                failures.append(f"rebuild_post_generation_exception_left_task_running:{failed_task.status if failed_task else None}")

    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("rebuild-candidates-regression: PASS")
    return 0


class _RepairingCandidateProvider:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, prompt: str, *, max_tokens: int = 2000, temperature=None, response_format=None, model: str | None = None) -> LLMResponse:
        self.calls += 1
        if self.calls == 1:
            content = (
                "道士问：哪来的木牌？顾晚想说系统分配的，话到嘴边咽回去。"
                "他记得进游戏前签过协议，内测期间必须完成一次门派入门任务。"
                "他从怀里掏出拜帖，这是刚才在界面里唯一能点开的东西。"
            ) * 8
            title = "待修复候选"
        else:
            content = (
                "瘦高道士把拂尘横在顾晚肩上，问他腰间木牌从哪来。"
                "顾晚按住那块枣木牌，把半截实话吞回去，只说山门口有人塞给他，清虚观认牌不认人。"
                "道士眯眼去看牌背，火漆边缘还软，像是刚从拜帖上揭下来的。"
                "顾晚顺势递上那封皱巴巴的拜帖，说三日内拜入一门，过期银钱作废，旁的规矩没人肯明说。"
                "道士没有接话，只捏了捏他的腕骨，又让他去井边挑两担水。"
                "顾晚听出这是试探，便先认错后讨价，说若挑完水还不收人，至少把送牌的人名告诉他。"
                "院里几个小道童停下扫帚看热闹，道士脸色沉了沉，终于让开半步。"
            ) * 5
            title = "木牌入山门"
        text = json.dumps(
            {
                "title": title,
                "content": content,
                "self_check": ["已把来历解释改成木牌、拜帖和山门规矩。"],
                "used_brief_points": ["清虚观盘问", "主角主动试探"],
            },
            ensure_ascii=False,
        )
        return LLMResponse(text=text, provider=self.name, model=model or "fake", response_chars=len(text), prompt_chars=len(prompt))


class _LeakingCandidateProvider:
    name = "fake"

    def generate(self, prompt: str, *, max_tokens: int = 2000, temperature=None, response_format=None, model: str | None = None) -> LLMResponse:
        content = (
            "道士问：哪来的木牌？顾晚想说系统分配的，话到嘴边咽回去。"
            "他记得进游戏前签过协议，内测期间必须完成一次门派入门任务。"
            "他从怀里掏出拜帖，这是刚才在界面里唯一能点开的东西。"
        ) * 4
        text = json.dumps(
            {
                "title": "泄漏候选",
                "content": content,
                "self_check": [],
                "used_brief_points": [],
            },
            ensure_ascii=False,
        )
        return LLMResponse(text=text, provider=self.name, model=model or "fake", response_chars=len(text), prompt_chars=len(prompt))


if __name__ == "__main__":
    raise SystemExit(main())
