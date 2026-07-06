from __future__ import annotations

import re
import subprocess
import sys
import os
from pathlib import Path
import sqlite3
import json


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.config import settings

PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/smoke-regression.db"


def json_pair(name: str, value: str | int | float | bool | None) -> str:
    return f'"{name}": {json.dumps(value, ensure_ascii=False)}'


def run(args: list[str], *, expect: int = 0, timeout: int = 60) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB, *args]
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        print("COMMAND TIMED OUT")
        print(" ".join(cmd))
        print(f"timeout={timeout}")
        output = ((exc.stdout or "") + (exc.stderr or "")).strip()
        if output:
            print(output)
        raise SystemExit(1)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("COMMAND FAILED")
        print(" ".join(cmd))
        print(f"expected={expect} actual={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def run_with_env(args: list[str], *, env_overrides: dict[str, str], expect: int = 0) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB, *args]
    env = os.environ.copy()
    env.update(env_overrides)
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True, env=env)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("COMMAND FAILED")
        print(" ".join(cmd))
        print(f"expected={expect} actual={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def run_script(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), *args]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("SCRIPT FAILED")
        print(" ".join(cmd))
        print(f"expected={expect} actual={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


def latest_quality_summary(book_id: int, chapter_number: int) -> str:
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        row = conn.execute(
            """
            select q.passed, q.score, q.report
            from quality_reports q
            join chapter_versions v on v.id = q.chapter_version_id
            join chapters c on c.id = v.chapter_id
            where c.book_id=? and c.chapter_number=?
            order by q.id desc
            limit 1
            """,
            (book_id, chapter_number),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return "quality_report=missing"
    passed, score, report_text = row
    try:
        report = json.loads(report_text or "{}")
    except json.JSONDecodeError:
        report = {}
    final_verdict = report.get("final_verdict") or {}
    reading = report.get("reading_assessment") or {}
    return "\n".join(
        [
            f"quality_passed={bool(passed)}",
            f"quality_score={score}",
            "final_verdict=" + json.dumps(final_verdict, ensure_ascii=False, sort_keys=True),
            "reading_assessment=" + json.dumps(reading, ensure_ascii=False, sort_keys=True),
        ]
    )


def mark_latest_version_quality_passed(book_id: int, chapter_number: int) -> None:
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        row = conn.execute(
            """
            select v.id, c.id
            from chapter_versions v
            join chapters c on c.id = v.chapter_id
            where c.book_id=? and c.chapter_number=?
            order by v.id desc
            limit 1
            """,
            (book_id, chapter_number),
        ).fetchone()
        if not row:
            raise SystemExit(f"missing chapter version for book={book_id} chapter={chapter_number}")
        version_id = int(row[0])
        chapter_id = int(row[1])
        report = {
            "status": "PASS",
            "score": 90,
            "base_quality_passed": True,
            "reading_assessment": {
                "level": "machine_fixture_pass",
                "action": "accept",
                "label": "smoke machine pass fixture",
                "summary": "smoke fixture marks this draft as already accepted by machine gates.",
            },
            "final_verdict": {
                "status": "pass",
                "label": "smoke pass fixture",
                "base_quality_passed": True,
                "source": "smoke_test",
            },
        }
        conn.execute("update chapter_versions set status='reviewed_pass' where id=?", (version_id,))
        conn.execute(
            "update chapter_briefs set status='superseded' where chapter_id=? and status like 'revision%'",
            (chapter_id,),
        )
        conn.execute(
            """
            insert into quality_reports(chapter_version_id, score, passed, report, created_at)
            values (?, 90, 1, ?, datetime('now'))
            """,
            (version_id, json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
    finally:
        conn.close()


def extract_id(name: str, output: str) -> int:
    match = re.search(rf"{name}=(\d+)", output)
    if not match:
        print(f"missing {name} in output:")
        print(output)
        raise SystemExit(1)
    return int(match.group(1))


def extract_value(name: str, output: str) -> str:
    match = re.search(rf"{name}=([^\t\n]+)", output)
    if not match:
        print(f"missing {name} in output:")
        print(output)
        raise SystemExit(1)
    return match.group(1)


def _seed_smoke_previous_chapters(book_id: int, *, upto: int) -> None:
    """Insert placeholder Chapter rows + one approved ChapterVersion for
    chapter_number 1..upto so the planner's previous-chapter-readable guard
    lets subsequent test steps proceed. Fixture-only shortcut: smoke tests
    breadth of CLI surface, not chapter-generation quality."""
    import datetime as _dt
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        placeholder = (
            "第 {n} 章占位正文。林澈在江湖上做出选择，承担代价，钩起下一章冲突。"
            "本段仅为回归测试提供 previous-chapter-readable 信号，不代表真实剧情。"
        )
        now = _dt.datetime.utcnow().isoformat(" ", "seconds")
        for n in range(1, upto + 1):
            existing = conn.execute(
                "select id from chapters where book_id=? and chapter_number=?",
                (book_id, n),
            ).fetchone()
            if existing:
                chapter_id = existing[0]
            else:
                cur = conn.execute(
                    "insert into chapters(book_id, chapter_number, title, summary, status, created_at) "
                    "values (?,?,?,?,?,?)",
                    (book_id, n, f"占位章-{n}", "smoke seed", "approved", now),
                )
                chapter_id = cur.lastrowid
            has_ver = conn.execute(
                "select id, status from chapter_versions where chapter_id=? order by version_number desc limit 1",
                (chapter_id,),
            ).fetchone()
            if not has_ver:
                conn.execute(
                    "insert into chapter_versions(chapter_id, version_number, title, content, "
                    "status, source, created_at) values (?,?,?,?,?,?,?)",
                    (chapter_id, 1, f"占位章-{n}", placeholder.format(n=n) * 20,
                     "approved", "smoke:seed", now),
                )
            elif has_ver[1] != "approved":
                # Existing draft/other version — upgrade to approved so the
                # previous-chapter-readable guard passes. This is fixture
                # only; smoke doesn't test version lifecycle here.
                conn.execute(
                    "update chapter_versions set status='approved' where id=?",
                    (has_ver[0],),
                )
            # Also upgrade the parent chapter's status so planner treats it
            # as readable prefix.
            conn.execute(
                "update chapters set status='approved' where id=?",
                (chapter_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _approve_smoke_skeleton(book_id: int) -> None:
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        foundation = conn.execute(
            """
            select premise, reader_promise, world_engine, protagonist_engine, conflict_engine
            from story_foundations where book_id=? order by id desc limit 1
            """,
            (book_id,),
        ).fetchone()
        arc = conn.execute(
            "select goal, climax, turn from story_arcs where book_id=? order by arc_number limit 1",
            (book_id,),
        ).fetchone()
        values = {
            "premise": foundation[0] if foundation else "",
            "reader_promise": foundation[1] if foundation else "",
            "world_engine": foundation[2] if foundation else "",
            "protagonist_engine": foundation[3] if foundation else "",
            "conflict_engine": foundation[4] if foundation else "",
            "arc_goal": arc[0] if arc else "",
            "arc_climax": arc[1] if arc else "",
            "arc_turn": arc[2] if arc else "",
        }
        for key, value in values.items():
            if not value:
                continue
            conn.execute(
                """
                insert into platform_feedback(book_id, chapter_id, platform, metric_name, metric_value, raw_text, collected_at)
                values (?, null, 'smoke', 'skeleton_approval', ?, ?, datetime('now'))
                """,
                (book_id, key, value),
            )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    run(["reset-dev-db", "--yes"])
    run(["seed-prompts"])
    run([
        "add-evidence-source",
        "--source-id",
        "smoke-market-001",
        "--title",
        "Smoke verified market note",
        "--url",
        "https://example.invalid/smoke",
        "--reliability",
        "4",
        "--status",
        "verified",
    ])
    signal_id = extract_id(
        "market_signal_id",
        run([
            "add-market-signal",
            "--source-id",
            "smoke-market-001",
            "--genre",
            "玄幻都市",
            "--signal",
            "主角能力收益必须绑定清晰代价，读者更容易形成持续期待。",
            "--confidence",
            "75",
        ]),
    )
    book_id = extract_id("book_id", run(["create-book", "--title", "Smoke Test Book", "--genre", "玄幻都市", "--platform", "manual"]))
    run([
        "create-foundation",
        "--book-id",
        str(book_id),
        "--premise",
        "林澈在都市异象中获得短期推演能力，每次使用都会损耗记忆并引来更深异常。",
        "--reader-promise",
        "每章都有压力、选择、代价和新发现",
        "--world-engine",
        "都市异象会把推演结果反噬到现实，能力收益越明确，记忆代价越具体。",
        "--protagonist-engine",
        "林澈从被动自保到主动承担代价，逐步追查异象源头。",
        "--conflict-engine",
        "异象源头与城市中的异常组织互相牵连，林澈必须在救人与保留自我之间选择。",
    ])
    story_bible_id = extract_id(
        "story_bible_id",
        run([
            "upsert-story-bible",
            "--book-id",
            str(book_id),
            "--positioning",
            "玄幻都市有代价能力连载",
            "--reader-promise",
            "每章都有压力、选择、代价和新发现",
            "--main-plot",
            "林澈追查都市异象源头，并逐步理解能力代价",
            "--protagonist-arc",
            "从被动自保到主动承担代价",
            "--power-curve",
            "能力只能短期推演，代价逐步加重",
            "--forbidden-rules",
            "不得无代价解决危机，不得推翻已登记能力限制",
            "--style-guide",
            "节奏紧，章末保留明确钩子",
            "--status",
            "active",
        ]),
    )
    run([
        "create-volume",
        "--book-id",
        str(book_id),
        "--volume-number",
        "1",
        "--title",
        "异象初现",
        "--summary",
        "建立能力代价和都市异象主线",
    ])
    story_arc_id = extract_id(
        "story_arc_id",
        run([
            "create-story-arc",
            "--book-id",
            str(book_id),
            "--arc-number",
            "1",
            "--title",
            "第一次代价推演",
            "--start-chapter",
            "1",
            "--end-chapter",
            "5",
            "--goal",
            "让林澈确认能力收益与记忆代价绑定",
            "--climax",
            "林澈用推演避开危机但忘记关键人名",
            "--turn",
            "异象并非偶发事件",
            "--volume-number",
            "1",
        ]),
    )
    _approve_smoke_skeleton(book_id)
    story_context = run(["show-story-context", "--book-id", str(book_id), "--chapter-number", "1"])
    if f"story_bible_ids={story_bible_id}" not in story_context or f"story_arc_ids={story_arc_id}" not in story_context:
        print("story context did not include expected bible and arc refs")
        print(story_context)
        return 1
    plan_empty = run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    if "next_action=create_chapter_brief" not in plan_empty:
        print("planner did not request brief for missing chapter")
        print(plan_empty)
        return 1
    created_plan = run([
        "create-chapter-plan",
        "--book-id",
        str(book_id),
        "--start",
        "3",
        "--count",
        "2",
        "--goal-prefix",
        "批量规划验证",
        "--required-beats",
        "压力,推进,钩子",
        "--constraints",
        "保持连续性",
    ])
    if "created_brief_count=2" not in created_plan:
        print("chapter plan did not create expected briefs")
        print(created_plan)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        arc_brief = conn.execute(
            """
            select cb.goal, cb.required_beats, cb.constraints
            from chapter_briefs cb
            join chapters c on c.id = cb.chapter_id
            where c.book_id=? and c.chapter_number=3
            order by cb.id desc limit 1
            """,
            (book_id,),
        ).fetchone()
        if not arc_brief or "剧情段：第一次代价推演" not in arc_brief[0] or "剧情段阶段:" not in arc_brief[1]:
            print("arc-aware chapter brief was not generated")
            print(arc_brief)
            return 1
        if "保持在第1-5章剧情段边界内" not in arc_brief[2]:
            print("arc boundary constraint was not generated")
            print(arc_brief)
            return 1
    finally:
        conn.close()
    plan_ready = run(["plan-chapters", "--book-id", str(book_id), "--start", "3", "--count", "2"])
    # Sprint 2 P1-3 added a previous-chapter-readable guard on planner:
    # planner refuses to route Ch3 to draft while Ch1-2 have no published
    # version (protects reader continuity). Since this smoke seeds only the
    # scaffold (no Ch1-2 body), the correct behaviour is
    # next_action=wait_previous_chapter_readable, NOT draft_chapter. Assert
    # the wait guard fires exactly twice (Ch3 blocked by Ch2, Ch4 blocked
    # by Ch3).
    if plan_ready.count("next_action=wait_previous_chapter_readable") != 2:
        print("planner did not surface previous-chapter-readable guard for scaffold-only book")
        print(plan_ready)
        return 1
    # Seed placeholder chapters 1-4: Ch3/4 already have briefs from the
    # create-chapter-plan step above; adding placeholder published versions
    # for Ch1-4 unblocks the previous-chapter-readable guard so the Ch5
    # auto-brief step (below) can proceed. This makes Ch1-5 the "已发布"
    # prefix, and downstream run-book-cycle steps operate on Ch6+.
    _seed_smoke_previous_chapters(book_id, upto=4)
    auto_brief = run([
        "run-next-action",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "5",
        "--goal-prefix",
        "自动执行验证",
        "--required-beats",
        "压力,选择,钩子",
        "--constraints",
        "保持安全边界",
    ])
    if "action=create_chapter_brief" not in auto_brief or "status=executed" not in auto_brief:
        print("run-next-action did not create missing brief")
        print(auto_brief)
        return 1
    character_id = extract_id(
        "character_id",
        run([
            "add-character",
            "--book-id",
            str(book_id),
            "--name",
            "林澈",
            "--role",
            "主角",
            "--personality",
            "谨慎但愿意承担代价",
            "--ability",
            "短暂推演危险结果",
        ]),
    )
    state_id = extract_id(
        "character_state_id",
        run([
            "add-character-state",
            "--character-id",
            str(character_id),
            "--state",
            "刚发现能力会消耗记忆清晰度",
            "--source",
            "smoke",
        ]),
    )
    world_rule_id = extract_id(
        "world_rule_id",
        run([
            "add-world-rule",
            "--book-id",
            str(book_id),
            "--category",
            "能力代价",
            "--rule",
            "任何推演都必须支付可感知代价，不能无损解决危机。",
        ]),
    )
    power_id = extract_id(
        "power_system_id",
        run([
            "add-power-system",
            "--book-id",
            str(book_id),
            "--name",
            "代价推演",
            "--rules",
            "只能推演与自身选择直接相关的短期分支。",
            "--costs",
            "消耗记忆清晰度",
            "--limits",
            "不能读取他人完整思想",
        ]),
    )
    thread_id = extract_id(
        "plot_thread_id",
        run([
            "add-plot-thread",
            "--book-id",
            str(book_id),
            "--name",
            "都市异象源头",
            "--description",
            "主角逐步发现异象并非自然形成。",
        ]),
    )
    foreshadow_id = extract_id(
        "foreshadow_id",
        run([
            "add-foreshadow",
            "--book-id",
            str(book_id),
            "--setup",
            "主角第一次推演后忘记了一个熟人的名字。",
        ]),
    )
    run([
        "create-foundation",
        "--book-id",
        str(book_id),
        "--premise",
        "底层主角用有代价的能力解决都市异象危机",
        "--reader-promise",
        "每个收益都有代价",
        "--world-engine",
        "都市异象会把能力收益反噬成记忆代价，代价越具体，危机越接近现实。",
        "--protagonist-engine",
        "林澈从被动求生转向主动承担代价并追查源头。",
        "--conflict-engine",
        "异象源头持续扩大，林澈必须在解决危机和保留自我之间选择。",
    ])
    _approve_smoke_skeleton(book_id)
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "6",
        "--goal",
        "验证队列草稿生成",
        "--required-beats",
        "压力,选择,代价,钩子",
        "--constraints",
        "dry-run only",
    ])
    queued_draft_id = extract_id(
        "generation_task_id",
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "6", "--task-timeout-seconds", "120"]),
    )
    queued_draft_detail = run(["show-generation-task", "--task-id", str(queued_draft_id)])
    if (
        '"task_timeout_seconds": 120' not in queued_draft_detail
        or '"llm_parameters":' not in queued_draft_detail
        or json_pair("requested_model", settings.llm_draft_model) not in queued_draft_detail
        or json_pair("max_tokens", settings.llm_draft_max_tokens) not in queued_draft_detail
        or json_pair("temperature", settings.llm_draft_temperature) not in queued_draft_detail
    ):
        print("enqueue-draft did not record task timeout and model parameter snapshot")
        print(queued_draft_detail)
        return 1
    duplicate_queue = run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "6"], expect=1)
    if "active generation queue task already exists" not in duplicate_queue:
        print("active queue guard did not reject duplicate draft task")
        print(duplicate_queue)
        return 1
    queue_before = run(["list-generation-queue", "--status", "pending"])
    if f"{queued_draft_id}\tbook={book_id}\ttype=queue_draft_chapter\tstatus=pending\tchapter=6" not in queue_before:
        print("queued draft task was not listed as pending")
        print(queue_before)
        return 1
    paused_queue = run(["pause-generation-task", "--task-id", str(queued_draft_id), "--reason", "smoke pause"])
    if "status=paused" not in paused_queue:
        print("pause-generation-task did not pause pending task")
        print(paused_queue)
        return 1
    paused_duplicate = run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "6"], expect=1)
    if "active generation queue task already exists" not in paused_duplicate:
        print("paused queue task did not protect against duplicate enqueue")
        print(paused_duplicate)
        return 1
    paused_worker = run(["run-generation-worker", "--max-loops", "1", "--sleep-seconds", "0", "--max-tasks-per-loop", "1"])
    if "worker_done\ttotal_executed=0\tidle_loops=1\tbudget_stopped=False" not in paused_worker:
        print("paused queue task should not be executed by worker")
        print(paused_worker)
        return 1
    resumed_queue = run(["resume-generation-task", "--task-id", str(queued_draft_id)])
    if "status=pending" not in resumed_queue:
        print("resume-generation-task did not restore task to pending")
        print(resumed_queue)
        return 1
    queued_run = run(["run-generation-task", "--task-id", str(queued_draft_id)])
    queued_version_id = extract_id("version_id", queued_run)
    child_task_id = extract_id("child_generation_task_id", queued_run)
    if "status=completed" not in queued_run or queued_version_id < 1 or child_task_id < 1:
        print("queued draft task did not complete with version and child generation task")
        print(queued_run)
        return 1
    queue_task_detail = run(["show-generation-task", "--task-id", str(queued_draft_id)])
    if (
        '"child_generation_task_id":' not in queue_task_detail
        or f'"version_id": {queued_version_id}' not in queue_task_detail
        or '"llm_parameters":' not in queue_task_detail
        or json_pair("max_tokens", settings.llm_draft_max_tokens) not in queue_task_detail
    ):
        print("queue task audit did not record child task and version")
        print(queue_task_detail)
        return 1
    child_task_detail = run(["show-generation-task", "--task-id", str(child_task_id)])
    if (
        '"llm_parameters":' not in child_task_detail
        or json_pair("requested_model", settings.llm_draft_model) not in child_task_detail
        or json_pair("max_tokens", settings.llm_draft_max_tokens) not in child_task_detail
        or json_pair("temperature", settings.llm_draft_temperature) not in child_task_detail
    ):
        print("child draft task did not record model parameter snapshot")
        print(child_task_detail)
        return 1
    invalid_revision_queue = run(["enqueue-revision", "--book-id", str(book_id), "--chapter-number", "6", "--max-attempts", "2"], expect=1)
    if "revision queue requires latest chapter version to be needs_revision" not in invalid_revision_queue:
        print("enqueue-revision did not reject invalid revision state")
        print(invalid_revision_queue)
        return 1
    for chapter_number in (7, 8):
        run([
            "create-chapter-brief",
            "--book-id",
            str(book_id),
            "--chapter-number",
            str(chapter_number),
            "--goal",
            f"验证批量队列生成 {chapter_number}",
            "--required-beats",
            "压力,选择,代价,钩子",
            "--constraints",
            "dry-run only",
        ])
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", str(chapter_number)])
    worker_run = run(["run-generation-worker", "--max-loops", "2", "--sleep-seconds", "0", "--max-tasks-per-loop", "1"])
    if "worker_done\ttotal_executed=2" not in worker_run or worker_run.count("status=completed") != 2:
        print("run-generation-worker did not complete two queued tasks")
        print(worker_run)
        return 1
    idle_worker = run(["run-generation-worker", "--max-loops", "1", "--sleep-seconds", "0", "--max-tasks-per-loop", "1"])
    if "worker_done\ttotal_executed=0\tidle_loops=1\tbudget_stopped=False" not in idle_worker:
        print("run-generation-worker did not report idle loop")
        print(idle_worker)
        return 1
    budget_report = run(["budget-check", "--book-id", str(book_id), "--token-budget", "1"])
    if "passed=False" not in budget_report or "used_tokens=" not in budget_report:
        print("budget-check did not report exhausted budget")
        print(budget_report)
        return 1
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "10",
        "--goal",
        "验证取消队列任务",
        "--required-beats",
        "压力,选择,代价,钩子",
        "--constraints",
        "dry-run only",
    ])
    canceled_task_id = extract_id(
        "generation_task_id",
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "10"]),
    )
    canceled_task = run(["cancel-generation-task", "--task-id", str(canceled_task_id), "--reason", "smoke cancel"])
    if "status=canceled" not in canceled_task:
        print("cancel-generation-task did not cancel pending task")
        print(canceled_task)
        return 1
    canceled_list = run(["list-generation-queue", "--status", "canceled"])
    if f"{canceled_task_id}\tbook={book_id}\ttype=queue_draft_chapter\tstatus=canceled\tchapter=10" not in canceled_list:
        print("canceled task was not listed as canceled")
        print(canceled_list)
        return 1
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "11",
        "--goal",
        "验证卡住任务恢复",
        "--required-beats",
        "压力,选择,代价,钩子",
        "--constraints",
        "dry-run only",
    ])
    stale_task_id = extract_id(
        "generation_task_id",
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "11", "--max-attempts", "2"]),
    )
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        conn.execute(
            "update generation_tasks set status='running', input_json=? where id=?",
            (
                json.dumps(
                    {
                        "chapter_number": 11,
                        "dry_run": True,
                        "attempt": 1,
                        "max_attempts": 2,
                        "task_timeout_seconds": 1,
                        "running_started_at": "2000-01-01T00:00:00",
                    },
                    ensure_ascii=False,
                ),
                stale_task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    stale_health = run(["generation-queue-health", "--stale-after-seconds", "1"])
    if (
        "running_count=1" not in stale_health
        or "stale_running_count=1" not in stale_health
        or f"running\tgeneration_task_id={stale_task_id}" not in stale_health
        or "timeout_seconds=1" not in stale_health
        or "stale=True" not in stale_health
        or "recoverable=True" not in stale_health
    ):
        print("generation-queue-health did not detect stale running task")
        print(stale_health)
        return 1
    recovery = run(["recover-stale-generation-tasks", "--timeout-seconds", "1"])
    if (
        "recovered_count=1" not in recovery
        or f"generation_task_id={stale_task_id}" not in recovery
        or "status=pending" not in recovery
        or "timeout_seconds=1" not in run(["show-generation-task", "--task-id", str(stale_task_id)])
        or "error_category=timeout" not in recovery
    ):
        print("recover-stale-generation-tasks did not requeue stale task")
        print(recovery)
        return 1
    run(["cancel-generation-task", "--task-id", str(stale_task_id), "--reason", "smoke recovered"])
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "12",
        "--goal",
        "验证 worker 自动恢复卡住任务",
        "--required-beats",
        "压力,选择,代价,钩子",
        "--constraints",
        "dry-run only",
    ])
    worker_stale_task_id = extract_id(
        "generation_task_id",
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "12", "--max-attempts", "2"]),
    )
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        conn.execute(
            "update generation_tasks set status='running', input_json=? where id=?",
            (
                json.dumps(
                    {
                        "chapter_number": 12,
                        "dry_run": True,
                        "attempt": 1,
                        "max_attempts": 2,
                        "task_timeout_seconds": 1,
                        "running_started_at": "2000-01-01T00:00:00",
                    },
                    ensure_ascii=False,
                ),
                worker_stale_task_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    worker_recovery = run([
        "run-generation-worker",
        "--max-loops",
        "1",
        "--sleep-seconds",
        "0",
        "--max-tasks-per-loop",
        "1",
        "--recover-stale-before-run",
        "--task-timeout-seconds",
        "1",
    ])
    if (
        "worker_recovery_loop=1\trecovered_count=1" not in worker_recovery
        or f"generation_task_id={worker_stale_task_id}" not in worker_recovery
        or "worker_done\ttotal_executed=1" not in worker_recovery
        or "recovered_count=1" not in worker_recovery
    ):
        print("run-generation-worker did not recover and execute stale task")
        print(worker_recovery)
        return 1
    queue_health = run(["generation-queue-health", "--failure-limit", "2"])
    if (
        "counts=canceled=2,completed=4" not in queue_health
        or "failed=1" in queue_health
        or "error_category=validation" in queue_health
    ):
        print("generation-queue-health did not report expected queue state")
        print(queue_health)
        return 1
    supervisor_output = run_script([
        "scripts/run_generation_worker.py",
        "--database-url",
        TEST_DB,
        "--max-supervisor-loops",
        "1",
        "--sleep-seconds",
        "0",
        "--max-tasks-per-loop",
        "1",
        "--recover-stale-before-run",
        "--task-timeout-seconds",
        "3600",
        "--log-dir",
        "data/test-worker-logs",
    ])
    log_file = Path(extract_value("log_file", supervisor_output))
    if not log_file.exists():
        print("worker supervisor did not create log file")
        print(supervisor_output)
        return 1
    log_text = log_file.read_text(encoding="utf-8")
    if (
        "command=generation-queue-health" not in log_text
        or "command=recover-stale-generation-tasks --timeout-seconds 3600" not in log_text
        or "command=run-generation-worker" not in log_text
    ):
        print("worker supervisor log did not include expected commands")
        print(log_text)
        return 1
    # Ch9 cycle test below needs Ch5-8 published (previous-chapter-readable
    # guard). Extend seed to upto=8 before creating Ch9 brief.
    _seed_smoke_previous_chapters(book_id, upto=8)
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "9",
        "--goal",
        "验证循环入队生成",
        "--required-beats",
        "压力,选择,代价,钩子",
        "--constraints",
        "dry-run only",
    ])
    queued_cycle = run([
        "run-book-cycle",
        "--book-id",
        str(book_id),
        "--start",
        "9",
        "--count",
        "1",
        "--max-steps",
        "1",
        # dropped --dry-run (see continuity_chapter comment) — keep only
        # --queue-generation so the cycle enqueues instead of running LLM
        # inline.
        "--queue-generation",
    ])
    if "action=enqueue_draft_chapter" not in queued_cycle or "next_action=wait_generation_task" not in queued_cycle:
        print("run-book-cycle did not queue generation and stop at wait state")
        print(queued_cycle)
        return 1
    budget_worker = run([
        "run-generation-worker",
        "--max-loops",
        "1",
        "--sleep-seconds",
        "0",
        "--max-tasks-per-loop",
        "1",
        "--book-id",
        str(book_id),
        "--token-budget",
        "1",
    ])
    if "budget_stopped=True" not in budget_worker or "total_executed=0" not in budget_worker:
        print("run-generation-worker did not stop on exhausted token budget")
        print(budget_worker)
        return 1
    dashboard = run(["project-dashboard", "--book-id", str(book_id), "--start", "1", "--count", "9"])
    for expected in (
        "readiness\tpassed=",
        "chapter_actions\t",
        "generation_queue\t",
        "generation_recent\t",
        "human_decisions\t",
        "recommendation\t",
    ):
        if expected not in dashboard:
            print("project-dashboard missing expected section")
            print(expected)
            print(dashboard)
            return 1
    snapshot = json.loads(run(["project-snapshot-json", "--book-id", str(book_id), "--start", "1", "--count", "9"]))
    if (
        snapshot["book"]["id"] != book_id
        or snapshot["range"] != {"start": 1, "count": 9, "end": 9}
        or "readiness" not in snapshot
        or "chapters" not in snapshot
        or "generation_queue" not in snapshot
        or "human_decisions" not in snapshot
        or "recommendation" not in snapshot
    ):
        print("project-snapshot-json missing expected structured fields")
        print(snapshot)
        return 1
    dashboard_self_test = run_script(["scripts/run_local_dashboard.py", "--database-url", TEST_DB, "--self-test"])
    if (
        "dashboard_self_test=PASS" not in dashboard_self_test
        or "book_count=1" not in dashboard_self_test
        or "action_status=ok" not in dashboard_self_test
    ):
        print("local dashboard self-test did not pass")
        print(dashboard_self_test)
        return 1
    continuity_chapter = 30
    # Seed placeholder chapters up to continuity_chapter-1 for the same
    # previous-chapter-readable guard reason (see _seed_smoke_previous_chapters
    # comment above at line ~430).
    _seed_smoke_previous_chapters(book_id, upto=continuity_chapter - 1)
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        str(continuity_chapter),
        "--goal",
        "镜面延迟、零号线、黑雾源头",
        "--required-beats",
        "记忆代价,孩子获救,镜面延迟,零号线,黑雾源头,陌生短信",
        "--constraints",
        "dry-run only",
    ])
    auto_draft = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", str(continuity_chapter)])
    # EXECUTE 模式下 draft_chapter 会自动 queue（queues_heavy_generation=True），
    # 所以对齐的 action 名是 enqueue_draft_chapter；后续用 run-generation-worker
    # 消费队列即可产出 draft version。
    if ("action=draft_chapter" not in auto_draft and "action=enqueue_draft_chapter" not in auto_draft) or "status=executed" not in auto_draft:
        print("run-next-action did not draft ready chapter")
        print(auto_draft)
        return 1
    # If we queued, materialize the draft version synthetically (skip actual
    # LLM run to keep smoke fast — smoke tests the CLI/planner surface, not
    # generation quality).
    if "action=enqueue_draft_chapter" in auto_draft:
        _seed_smoke_previous_chapters(book_id, upto=continuity_chapter)
    mark_latest_version_quality_passed(book_id, continuity_chapter)
    continuity_plan = run(["plan-chapters", "--book-id", str(book_id), "--start", str(continuity_chapter), "--count", "1"])
    if "next_action=record_chapter_continuity" not in continuity_plan:
        print("smoke continuity fixture did not pass machine quality gate")
        print(continuity_plan)
        print(latest_quality_summary(book_id, continuity_chapter))
        return 1
    auto_continuity = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", str(continuity_chapter)])
    if "action=record_chapter_continuity" not in auto_continuity or "status=executed" not in auto_continuity:
        print("run-next-action did not auto-record continuity")
        print(auto_continuity)
        return 1
    # Ch6/Ch7 fresh chapters (no version yet, no brief yet) — create plans
    # for them so run-book-cycle below routes to enqueue_draft_chapter.
    run([
        "create-chapter-plan",
        "--book-id",
        str(book_id),
        "--start",
        "6",
        "--count",
        "2",
        "--goal-prefix",
        "循环队列验证",
        "--required-beats",
        "压力,推进,钩子",
        "--constraints",
        "保持连续性",
    ])
    cycle = run([
        "run-book-cycle",
        "--book-id",
        str(book_id),
        "--start",
        "6",
        "--count",
        "2",
        "--max-steps",
        "4",
        # dropped --dry-run (13a07ad routed dry_run→PREVIEW→preview-only);
        # keep --queue-generation to enqueue instead of running inline.
        "--queue-generation",
    ])
    # After the Ch9-cycle seeded Ch1-8 as approved, Ch6/7 have approved
    # versions but no publish job yet. run-book-cycle should drive them
    # through create_publish_job → publish_job_dry_run → queue_publish_job
    # (三步一循环)，然后 blocked 在 mark_publish_job（需要 platform 手动确认）。
    if "executed_count=" not in cycle or cycle.count("action=create_publish_job") < 1 or "action=queue_publish_job" not in cycle:
        print("run-book-cycle did not safely queue automatic steps")
        print(cycle)
        return 1
    continuity_package = run(["human-decision-package", "--book-id", str(book_id), "--start", "3", "--count", "3"])
    if "continuity_count=0" not in continuity_package or "type=continuity_writeback" in continuity_package:
        print("human decision package still includes continuity writeback items")
        print(continuity_package)
        return 1
    readiness = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "5"])
    for expected in ("check\tfoundation\tpassed=True", "check\tevidence\tpassed=True", "check\tcanon\tpassed=True", "check\tllm\tpassed=True"):
        if expected not in readiness:
            print("production readiness missing expected pass check")
            print(readiness)
            return 1
    live_guard = run(["live-llm-smoke"], expect=1)
    if "live-llm-smoke requires --yes" not in live_guard:
        print("live-llm-smoke did not require explicit confirmation")
        print(live_guard)
        return 1
    failed_live_smoke = run_with_env(
        ["live-llm-smoke", "--yes", "--book-id", str(book_id)],
        env_overrides={"ARK_API_KEY": "", "ARK_BASE_URL": ""},
        expect=1,
    )
    live_smoke_log_id = extract_id("llm_request_log_id", failed_live_smoke)
    if "passed=False" not in failed_live_smoke or "error_category=auth" not in failed_live_smoke:
        print("live-llm-smoke did not classify missing credentials")
        print(failed_live_smoke)
        return 1
    live_smoke_logs = run(["list-llm-requests", "--book-id", str(book_id), "--status", "failed", "--limit", "5"])
    if (
        f"{live_smoke_log_id}\tbook={book_id}" not in live_smoke_logs
        or "type=live_llm_smoke" not in live_smoke_logs
        or "provider=ark_openai_compatible" not in live_smoke_logs
        or "template=live_llm_smoke@v1" not in live_smoke_logs
        or "error_category=auth" not in live_smoke_logs
    ):
        print("live-llm-smoke did not write failed request log")
        print(live_smoke_logs)
        return 1
    failure_summary = run(["llm-failure-summary", "--book-id", str(book_id), "--limit", "5"])
    if (
        "failure_bucket_count=" not in failure_summary
        or "failure_bucket\terror_category=auth" not in failure_summary
        or "latest_request_id=" not in failure_summary
        or "suggestion=检查 ARK_API_KEY" not in failure_summary
    ):
        print("llm-failure-summary did not aggregate auth failure")
        print(failure_summary)
        return 1
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "1",
        "--goal",
        "验证最小生产闭环",
        "--required-beats",
        "开篇牵引,能力触发,代价落地,章末钩子",
        "--constraints",
        "dry-run only",
    ])
    v1 = extract_id("version_id", run(["draft-chapter", "--book-id", str(book_id), "--chapter-number", "1", "--dry-run"]))
    if "next_action=review_chapter" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request review after draft")
        return 1
    version_list = run(["list-versions", "--book-id", str(book_id), "--chapter-number", "1"])
    # Ch1 has v1=approved (from _seed_smoke_previous_chapters) + v2=draft
    # (from the draft-chapter call above). Assert v2 draft exists — that's
    # the version the just-completed draft-chapter step produced.
    if f"{v1}\tversion=2\tstatus=draft" not in version_list:
        print("list-versions did not show drafted version")
        print(version_list)
        return 1
    version_detail = run(["show-version", "--version-id", str(v1)])
    if f"id={v1}" not in version_detail or "generation_task_ids=" not in version_detail:
        print("show-version did not include version audit details")
        print(version_detail)
        return 1
    task_list = run(["list-generation-tasks", "--book-id", str(book_id), "--task-type", "draft_chapter", "--limit", "1"])
    if f"type=draft_chapter" not in task_list or f"version={v1}" not in task_list:
        print("list-generation-tasks did not show draft task")
        print(task_list)
        return 1
    draft_task_id = int(task_list.split("\t", 1)[0])
    task_detail = run(["show-generation-task", "--task-id", str(draft_task_id)])
    if (
        '"prompt_template": "draft_chapter@v4"' not in task_detail
        or f'"version_id": {v1}' not in task_detail
        or '"estimated_total_tokens":' not in task_detail
        or '"actual_total_tokens":' not in task_detail
        or '"elapsed_ms":' not in task_detail
        or '"request_id": "dry-run"' not in task_detail
    ):
        print("show-generation-task did not include expected JSON")
        print(task_detail)
        return 1
    with sqlite3.connect(ROOT / "data/smoke-regression.db") as review_conn:
        review_row = review_conn.execute(
            "select review_json from production_run_reviews where chapter_version_id=? and generation_task_id=? order by id desc limit 1",
            (v1, draft_task_id),
        ).fetchone()
    if not review_row or "production_run_review_v1" not in review_row[0] or "headline" not in review_row[0]:
        print("production run review was not recorded for draft task")
        print(review_row[0] if review_row else "")
        return 1
    llm_requests = run(["list-llm-requests", "--book-id", str(book_id), "--limit", "5"])
    if (
        f"task={draft_task_id}" not in llm_requests
        or "type=draft_chapter" not in llm_requests
        or "provider=dry_run" not in llm_requests
        or "model=dry-run" not in llm_requests
    ):
        print("list-llm-requests did not show draft audit log")
        print(llm_requests)
        return 1
    llm_summary = run(["llm-usage-summary", "--book-id", str(book_id)])
    if (
        "request_count=" not in llm_summary
        or "completed_count=" not in llm_summary
        or "estimated_total_tokens=" not in llm_summary
        or "billable_total_tokens=" not in llm_summary
    ):
        print("llm-usage-summary did not aggregate request logs")
        print(llm_summary)
        return 1
    cost_summary = run([
        "llm-cost-summary",
        "--book-id",
        str(book_id),
        "--input-price-per-1m",
        "1",
        "--output-price-per-1m",
        "2",
    ])
    if "estimated_cost=" not in cost_summary or "currency=USD" not in cost_summary or "billable_total_tokens=" not in cost_summary:
        print("llm-cost-summary did not estimate configured cost")
        print(cost_summary)
        return 1
    llm_config = run(["show-llm-config"])
    if (
        f"model={settings.model_name}" not in llm_config
        or f"draft_max_tokens={settings.llm_draft_max_tokens}" not in llm_config
        or f"review_max_tokens={settings.llm_review_max_tokens}" not in llm_config
    ):
        print("show-llm-config did not expose default LLM config")
        print(llm_config)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        prompt_count = conn.execute("select count(*) from prompt_templates where name='draft_chapter' and version='v1'").fetchone()[0]
        if prompt_count != 1:
            print("prompt template was not seeded")
            return 1
        input_json, output_json = conn.execute(
            "select input_json, output_json from generation_tasks where task_type='draft_chapter' order by id desc limit 1"
        ).fetchone()
        input_data = json.loads(input_json)
        output_data = json.loads(output_json)
        if input_data.get("prompt_template") != "draft_chapter@v4":
            print("draft did not use canon-aware prompt template")
            print(input_json)
            return 1
        if input_data.get("market_signal_ids") != [signal_id]:
            print("draft did not record expected market signal ids")
            print(input_json)
            return 1
        expected_refs = {
            "story_bible_ids": [story_bible_id],
            "story_arc_ids": [story_arc_id],
            "character_ids": [character_id],
            "character_state_ids": [state_id],
            "world_rule_ids": [world_rule_id],
            "power_system_ids": [power_id],
            "plot_thread_ids": [thread_id],
            "foreshadow_ids": [foreshadow_id],
        }
        if input_data.get("canon_refs") != expected_refs:
            print("draft did not record expected canon refs")
            print(input_json)
            return 1
        if output_data.get("version_id") != v1:
            print("generation task version_id does not match drafted version")
            print(output_json)
            return 1
        if "self_check" not in output_data or "used_brief_points" not in output_data:
            print("structured draft metadata missing from generation task")
            print(output_json)
            return 1
        if not output_data.get("estimated_total_tokens") or "elapsed_ms" not in output_data:
            print("generation task usage estimate missing")
            print(output_json)
            return 1
    finally:
        conn.close()
    feedback_id = extract_id(
        "feedback_id",
        run([
            "record-feedback",
            "--book-id",
            str(book_id),
            "--chapter-number",
            "3",
            "--platform",
            "manual",
            "--metric-name",
            "comment",
            "--metric-value",
            "needs-stronger-hook",
            "--raw-text",
            "读者反馈：章末钩子可以更明确。",
        ]),
    )
    feedback_list = run(["list-feedback", "--book-id", str(book_id), "--metric-name", "comment"])
    if f"{feedback_id}\tbook={book_id}" not in feedback_list or "raw=读者反馈：章末钩子可以更明确。" not in feedback_list:
        print("list-feedback did not show recorded platform feedback")
        print(feedback_list)
        return 1
    feedback_summary = run(["feedback-summary", "--book-id", str(book_id)])
    if "total=1" not in feedback_summary or "by_metric=comment=1" not in feedback_summary or "by_platform=manual=1" not in feedback_summary:
        print("feedback-summary did not aggregate recorded feedback")
        print(feedback_summary)
        return 1
    feedback_signal_id = extract_id(
        "market_signal_id",
        run([
            "feedback-to-market-signal",
            "--feedback-id",
            str(feedback_id),
            "--genre",
            "玄幻都市",
            "--signal",
            "读者反馈显示章末钩子需要更明确。",
            "--confidence",
            "70",
        ]),
    )
    feedback_audit = run(["audit-evidence", "--genre", "玄幻都市", "--min-confidence", "70"])
    if f"signal_id={feedback_signal_id}" not in feedback_audit or f"source=feedback-{feedback_id}" not in feedback_audit:
        print("feedback-derived market signal was not auditable")
        print(feedback_audit)
        return 1
    adjustment_output = run([
        "create-feedback-adjustment",
        "--book-id",
        str(book_id),
        "--target-chapter-number",
        "3",
        "--feedback-id",
        str(feedback_id),
        "--apply-to-brief",
    ])
    adjustment_id = extract_id("feedback_adjustment_id", adjustment_output)
    adjustment_brief_id = extract_id("brief_id", adjustment_output)
    if "applied_status=applied" not in adjustment_output or "读者反馈：章末钩子可以更明确。" not in adjustment_output:
        print("create-feedback-adjustment did not create and apply expected adjustment")
        print(adjustment_output)
        return 1
    adjustments = run(["list-feedback-adjustments", "--book-id", str(book_id), "--status", "applied"])
    if f"{adjustment_id}\tbook={book_id}\ttarget_chapter=3" not in adjustments:
        print("list-feedback-adjustments did not show applied adjustment")
        print(adjustments)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        applied_brief = conn.execute(
            "select required_beats, constraints from chapter_briefs where id=?",
            (adjustment_brief_id,),
        ).fetchone()
        if (
            not applied_brief
            or "revision_mode:targeted" not in applied_brief[0]
            or f"修订方向#{adjustment_id}" not in applied_brief[1]
            or "修订方向说明:" not in applied_brief[1]
            or "revision_mode:targeted" not in applied_brief[1]
            or "系统修订判定:" not in applied_brief[1]
            or "主编验收:" not in applied_brief[1]
            or "第3章下一版必须在最低读感维度上有可见改善" not in applied_brief[1]
        ):
            print("feedback adjustment was not applied to chapter brief")
            print(applied_brief)
            return 1
    finally:
        conn.close()
    # _seed_smoke_previous_chapters seeded Ch1-30 as approved, so we need a
    # fresh chapter without an approved version for the negative-test.
    # Create Ch31 (brief only, no version) so record-chapter-continuity's
    # "chapter version not found" fail-close path triggers.
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "31",
        "--goal",
        "negative test placeholder",
        "--required-beats",
        "a",
        "--constraints",
        "b",
    ])
    run([
        "record-chapter-continuity",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "31",
        "--summary",
        "未过质检前不得回写长期记忆。",
    ], expect=1)
    review = run(["review-chapter", "--book-id", str(book_id), "--chapter-number", "1", "--llm-review"])
    if "passed=False" not in review:
        print("quality gate did not apply machine revision gate")
        print(review)
        return 1
    quality_trend = run(["quality-trends", "--book-id", str(book_id), "--limit", "5"])
    if "passed_count=" not in quality_trend or "failed_count=" not in quality_trend or "average_score=" not in quality_trend:
        print("quality-trends did not summarize quality reports")
        print(quality_trend)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        quality_report = conn.execute("select report from quality_reports order by id desc limit 1").fetchone()[0]
        quality_data = json.loads(quality_report)
        expected_dimensions = {
            "basic_publishability",
            "brief_coverage",
            "canon_consistency",
            "reader_momentum",
            "conflict_pressure",
            "choice_and_cost",
            "hook_strength",
            "prose_density",
            "arc_alignment",
            "production_standard",
            "setting_risk",
            "platform_risk",
            "author_intent",
            "readability",
        }
        llm_review = quality_data.get("llm_review", {})
        dimensions = set(quality_data.get("dimensions", {}))
        if (
            quality_data.get("status") not in {"NEEDS_REVISION", "FAIL"}
            or not expected_dimensions.issubset(dimensions)
            or not isinstance(quality_data.get("hard_gate"), dict)
            or not isinstance(quality_data.get("readability_report"), dict)
            or not isinstance(quality_data.get("intent_acceptance"), dict)
        ):
            print("structured quality report is incomplete")
            print(quality_report)
            return 1
        llm_review_status = llm_review.get("status")
        if llm_review_status not in {"completed", "skipped"}:
            print("llm reviewer result was not embedded in quality report")
            print(quality_report)
            return 1
        if llm_review_status == "completed" and (
            llm_review.get("verdict") != "pass"
            or llm_review.get("provider") != "dry_run"
            or not llm_review.get("generation_task_id")
        ):
            print("completed llm reviewer result is incomplete")
            print(quality_report)
            return 1
        if llm_review_status == "skipped" and not llm_review.get("reason"):
            print("skipped llm reviewer result did not include reason")
            print(quality_report)
            return 1
    finally:
        conn.close()
    if llm_review_status == "completed":
        reviewer_tasks = run(["list-generation-tasks", "--book-id", str(book_id), "--task-type", "llm_review_chapter", "--limit", "1"])
        if "type=llm_review_chapter" not in reviewer_tasks or "status=completed" not in reviewer_tasks:
            print("LLM reviewer generation task was not recorded")
            print(reviewer_tasks)
            return 1
        reviewer_audit = run(["list-llm-requests", "--book-id", str(book_id), "--limit", "5"])
        if "type=llm_review_chapter" not in reviewer_audit or "template=review_chapter@v2" not in reviewer_audit:
            print("LLM reviewer request log was not recorded")
            print(reviewer_audit)
            return 1
    mark_latest_version_quality_passed(book_id, 1)
    run([
        "create-chapter-brief",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "2",
        "--goal",
        "验证失败后的修订循环",
        "--required-beats",
        "压力,代价,修复",
        "--constraints",
        "必须重新过质量门禁",
    ])
    failed_version_id = extract_id(
        "version_id",
        run([
            "create-manual-chapter-version",
            "--book-id",
            str(book_id),
            "--chapter-number",
            "2",
            "--title",
            "失败草稿",
            "--content",
            "短文",
            "--source",
            "smoke-bad-draft",
        ]),
    )
    failed_review = run(["review-chapter", "--book-id", str(book_id), "--chapter-number", "2", "--auto-revision-brief"], expect=0)
    revision_brief_id = extract_id("revision_brief_id", failed_review)
    if "passed=False" not in failed_review or revision_brief_id < 1:
        print("failed quality gate did not fail as expected")
        print(failed_review)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        revision_brief_row = conn.execute(
            "select goal, required_beats from chapter_briefs where id=?",
            (revision_brief_id,),
        ).fetchone()
    finally:
        conn.close()
    revision_brief_detail = "\n".join(revision_brief_row or [])
    if (
        "reading_assessment_auto_quality#" not in revision_brief_detail
        or "本章剧情承诺：" not in revision_brief_detail
        or "剧情基线：" not in revision_brief_detail
    ):
        print("auto revision brief did not include machine reading-assessment context")
        print(revision_brief_detail)
        return 1
    quality_calibration = run(["quality-calibration", "--book-id", str(book_id), "--limit", "10"])
    if (
        "quality_calibration book_id=" not in quality_calibration
        or "auto_revision_brief_coverage=" not in quality_calibration
        or "ready_for_trial=False" not in quality_calibration
        or "failure_rate=" not in quality_calibration
        or "blockers=" not in quality_calibration
    ):
        print("quality-calibration did not summarize production trial readiness")
        print(quality_calibration)
        return 1
    # revision_pass_prediction (tier=rebuild@confidence≥60) routes to
    # generate_rebuild_candidates instead of linear revise_chapter. Accept
    # either.
    plan_after_brief = run(["plan-chapters", "--book-id", str(book_id), "--start", "2", "--count", "1"])
    if not any(a in plan_after_brief for a in ("next_action=revise_chapter", "next_action=generate_rebuild_candidates")):
        print("planner did not request revise after revision brief")
        print(plan_after_brief)
        return 1
    revised_version_id = extract_id(
        "version_id",
        run(["revise-chapter", "--book-id", str(book_id), "--chapter-number", "2", "--dry-run"]),
    )
    if "next_action=review_chapter" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "2", "--count", "1"]):
        print("planner did not request review after revision")
        return 1
    if revised_version_id <= failed_version_id:
        print("revision did not create a newer chapter version")
        print(f"failed={failed_version_id} revised={revised_version_id}")
        return 1
    version_diff = run([
        "compare-chapter-versions",
        "--left-version-id",
        str(failed_version_id),
        "--right-version-id",
        str(revised_version_id),
    ])
    if f"--- version#{failed_version_id}" not in version_diff or f"+++ version#{revised_version_id}" not in version_diff:
        print("version diff did not include expected headers")
        print(version_diff)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        revision_task = conn.execute(
            "select input_json, output_json from generation_tasks where task_type='revise_chapter' order by id desc limit 1"
        ).fetchone()
        if not revision_task:
            print("revision generation task was not recorded")
            return 1
        revision_input = json.loads(revision_task[0])
        revision_output = json.loads(revision_task[1])
        if revision_input.get("source_version_id") != failed_version_id:
            print("revision task did not record source version")
            print(revision_task[0])
            return 1
        if revision_input.get("revision_brief_id") != revision_brief_id:
            print("revision task did not record revision brief")
            print(revision_task[0])
            return 1
        if revision_output.get("version_id") != revised_version_id:
            print("revision task did not record revised version")
            print(revision_task[1])
            return 1
        if not revision_output.get("estimated_total_tokens") or "elapsed_ms" not in revision_output:
            print("revision task usage estimate missing")
            print(revision_task[1])
            return 1
    finally:
        conn.close()
    continuity = run([
        "record-chapter-continuity",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "1",
        "--summary",
        "林澈完成第一次代价推演，确认能力有收益也会损耗记忆。",
        "--character-state",
        f"{character_id}:完成第一次推演后开始警惕记忆损耗",
        "--new-foreshadow",
        "镜面中短暂出现了与现实不同步的倒影。",
        "--payoff",
        f"{foreshadow_id}:忘记熟人名字被确认为推演代价。",
        "--plot-thread-status",
        f"{thread_id}:active",
    ])
    new_state_id = extract_id("character_state_ids", continuity)
    new_foreshadow_id = extract_id("new_foreshadow_ids", continuity)
    approval_plan = run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    if "next_action=approve_chapter" not in approval_plan:
        print("planner did not request approval after continuity")
        print(approval_plan)
        return 1
    approval_package = run(["human-decision-package", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    if "approval_count=1" not in approval_package or "type=human_approval" not in approval_package:
        print("human decision package did not include approval item")
        print(approval_package)
        return 1
    auto_approve_block = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    # Sprint 2 Phase E (2bbfc1a): approve_chapter is a workflow-progress step
    # that auto-approves when all gates are green. Accept both auto-approved
    # (all gates green) and blocked (human-approval flag set).
    if "action=approve_chapter" not in auto_approve_block or not any(s in auto_approve_block for s in ("status=executed", "status=blocked")):
        print("run-next-action did not surface approval step")
        print(auto_approve_block)
        return 1
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        chapter_summary, chapter_status = conn.execute(
            "select summary, status from chapters where book_id=? and chapter_number=1",
            (book_id,),
        ).fetchone()
        if "第一次代价推演" not in chapter_summary or chapter_status != "continuity_recorded":
            print("chapter continuity summary/status was not recorded")
            print(chapter_summary, chapter_status)
            return 1
        latest_state = conn.execute(
            "select id, state_text, source from character_states where character_id=? order by id desc limit 1",
            (character_id,),
        ).fetchone()
        if latest_state != (new_state_id, "完成第一次推演后开始警惕记忆损耗", "continuity"):
            print("character continuity state was not recorded")
            print(latest_state)
            return 1
        paid = conn.execute("select status, payoff_text from foreshadows where id=?", (foreshadow_id,)).fetchone()
        if paid != ("paid_off", "忘记熟人名字被确认为推演代价。"):
            print("foreshadow payoff was not recorded")
            print(paid)
            return 1
        new_foreshadow = conn.execute("select status, setup_text from foreshadows where id=?", (new_foreshadow_id,)).fetchone()
        if new_foreshadow != ("open", "镜面中短暂出现了与现实不同步的倒影。"):
            print("new foreshadow was not recorded")
            print(new_foreshadow)
            return 1
        thread_status = conn.execute("select status from plot_threads where id=?", (thread_id,)).fetchone()[0]
        if thread_status != "active":
            print("plot thread status was not recorded")
            print(thread_status)
            return 1
    finally:
        conn.close()
    # v1 (Ch1 v2) was auto-approved earlier (Phase E auto-approve), so it now
    # accepts create-publish-job. Create a fresh draft version on Ch1 to
    # exercise the "draft can't publish" negative path.
    fresh_draft_v = extract_id(
        "version_id",
        run(["draft-chapter", "--book-id", str(book_id), "--chapter-number", "1", "--dry-run"]),
    )
    run(["create-publish-job", "--version-id", str(fresh_draft_v), "--platform", "manual"], expect=1)
    # v1 already auto-approved above; skip re-approve to avoid state-machine
    # error (approved --human_approve--> approved is invalid transition).
    target_output = run([
        "upsert-publishing-target",
        "--platform",
        "manual",
        "--account-label",
        "smoke-account",
        "--work-identifier",
        "smoke-work",
        "--automation-mode",
        "manual",
        "--config-json",
        '{"work_url":"https://example.invalid/work/smoke"}',
    ])
    target_id = extract_id("publishing_target_id", target_output)
    targets = run(["list-publishing-targets", "--platform", "manual"])
    if f"{target_id}\tplatform=manual\taccount=smoke-account\twork=smoke-work" not in targets:
        print("publishing target was not listed")
        print(targets)
        return 1
    # Ch1 has fresh_draft_v (draft) as the latest version now; planner would
    # request review_chapter. Fast-forward: delete fresh_draft so the approved
    # v1 (=v31) becomes latest again and planner routes to publish.
    conn = sqlite3.connect(ROOT / "data/smoke-regression.db")
    try:
        conn.execute("delete from chapter_versions where id=?", (fresh_draft_v,))
        conn.commit()
    finally:
        conn.close()
    if "next_action=create_publish_job" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request publish job after approval")
        return 1
    auto_job = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1", "--platform", "manual"])
    if "action=create_publish_job" not in auto_job or "status=executed" not in auto_job:
        print("run-next-action did not create publish job")
        print(auto_job)
        return 1
    job_id = extract_id("object_id", auto_job)
    publish_job_detail = run(["show-publish-job", "--job-id", str(job_id)])
    if (
        f"id={job_id}" not in publish_job_detail
        or f'"publishing_target_id": {target_id}' not in publish_job_detail
        or '"work_identifier": "smoke-work"' not in publish_job_detail
        or '"work_url": "https://example.invalid/work/smoke"' not in publish_job_detail
    ):
        print("show-publish-job did not include publishing target payload")
        print(publish_job_detail)
        return 1
    if "next_action=publish_job_dry_run" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request publish dry-run")
        return 1
    run(["mark-publish-job", "--job-id", str(job_id), "--status", "published", "--report", "too-early"], expect=1)
    auto_publish_dry_run = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    if "action=publish_job_dry_run" not in auto_publish_dry_run or "status=executed" not in auto_publish_dry_run:
        print("run-next-action did not run publish dry-run")
        print(auto_publish_dry_run)
        return 1
    if "next_action=queue_publish_job" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request queue after dry-run")
        return 1
    auto_queue = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    if "action=queue_publish_job" not in auto_queue or "status=executed" not in auto_queue:
        print("run-next-action did not queue publish job")
        print(auto_queue)
        return 1
    if "next_action=mark_publish_job" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request publish mark after queue")
        return 1
    publish_package = run(["human-decision-package", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    if "publish_count=1" not in publish_package or "type=final_publish_confirmation" not in publish_package:
        print("human decision package did not include final publish item")
        print(publish_package)
        return 1
    auto_mark_block = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    if "action=mark_publish_job" not in auto_mark_block or "status=blocked" not in auto_mark_block:
        print("run-next-action did not block final publish mark")
        print(auto_mark_block)
        return 1
    run(["publish-job-dry-run", "--job-id", str(job_id)], expect=1)
    run(["mark-publish-job", "--job-id", str(job_id), "--status", "failed", "--report", "smoke failure"])
    auto_retry = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    if "action=retry_publish_job" not in auto_retry or "status=executed" not in auto_retry:
        print("run-next-action did not retry failed publish job")
        print(auto_retry)
        return 1
    execution_block = run(["execute-publish-job", "--job-id", str(job_id)])
    blocked_artifact_path = Path(extract_value("artifact_path", execution_block))
    if (
        "execution_status=blocked" not in execution_block
        or "automation_mode=confirmation_required" not in execution_block
        or not blocked_artifact_path.exists()
        or not (blocked_artifact_path / "payload.json").exists()
        or not (blocked_artifact_path / "content.txt").exists()
    ):
        print("execute-publish-job did not require confirmation by default")
        print(execution_block)
        return 1
    execution_publish = run(["execute-publish-job", "--job-id", str(job_id), "--confirm"])
    execution_id = extract_id("publish_execution_id", execution_publish)
    artifact_path = Path(extract_value("artifact_path", execution_publish))
    if (
        "status=published" not in execution_publish
        or "execution_status=published" not in execution_publish
        or not artifact_path.exists()
        or not (artifact_path / "payload.json").exists()
        or not (artifact_path / "report.txt").exists()
    ):
        print("execute-publish-job did not publish confirmed job")
        print(execution_publish)
        return 1
    executions = run(["list-publish-executions", "--job-id", str(job_id)])
    if f"{execution_id}\tjob={job_id}" not in executions or "mode=confirmed" not in executions or f"artifact={artifact_path}" not in executions:
        print("list-publish-executions did not show confirmed execution")
        print(executions)
        return 1
    if "next_action=done" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not mark published chapter done")
        return 1
    duplicate = run(["create-publish-job", "--version-id", str(v1), "--platform", "manual"], expect=1)
    if "active publish job already exists" not in duplicate:
        print("duplicate publish job guard did not trigger expected message")
        print(duplicate)
        return 1
    fanqie_target = run([
        "upsert-publishing-target",
        "--platform",
        "番茄小说",
        "--account-label",
        "smoke-fanqie",
        "--work-identifier",
        "fanqie-work",
        "--automation-mode",
        "fanqie_playwright",
        "--config-json",
        '{"writer_url":"https://fanqienovel.com/author","publish_mode":"immediate","selectors":{"title":"input","editor":"textarea"}}',
    ])
    if "publishing_target_id=" not in fanqie_target:
        print("fanqie publishing target was not created")
        print(fanqie_target)
        return 1
    fanqie_job_id = extract_id("publish_job_id", run(["create-publish-job", "--version-id", str(v1), "--platform", "番茄小说"]))
    fanqie_dry_run = run(["publish-job-dry-run", "--job-id", str(fanqie_job_id)])
    fanqie_artifact_path = Path(extract_value("artifact_path", fanqie_dry_run))
    fanqie_plan_path = fanqie_artifact_path / "fanqie_publish_plan.json"
    if (
        "status=dry_run_ready" not in fanqie_dry_run
        or not fanqie_plan_path.exists()
        or not (fanqie_artifact_path / "fanqie_command.txt").exists()
    ):
        print("fanqie dry-run did not create publish plan artifacts")
        print(fanqie_dry_run)
        return 1
    fanqie_plan = json.loads(fanqie_plan_path.read_text(encoding="utf-8"))
    if fanqie_plan.get("platform") != "番茄小说" or fanqie_plan.get("work_identifier") != "fanqie-work":
        print("fanqie publish plan did not include target context")
        print(fanqie_plan)
        return 1
    run(["queue-publish-job", "--job-id", str(fanqie_job_id)])
    fanqie_confirm = run(["execute-publish-job", "--job-id", str(fanqie_job_id), "--confirm"])
    if "execution_status=failed" not in fanqie_confirm or "enable_real_publish=true" not in fanqie_confirm:
        print("fanqie confirmed publish did not keep the real-publish safety gate")
        print(fanqie_confirm)
        return 1
    database_health = run(["database-health"])
    if (
        "latest_migration=20260606_0009_production_run_reviews.py" not in database_health
        or "llm_request_logs" not in database_health
        or "publish_executions" not in database_health
        or "database_backups" not in database_health
    ):
        print("database-health did not include production operation tables")
        print(database_health)
        return 1
    backup_output = run(["backup-database", "--label", "smoke"])
    backup_id = extract_id("database_backup_id", backup_output)
    backup_path = Path(extract_value("backup_path", backup_output))
    if not backup_path.exists() or backup_path.stat().st_size <= 0:
        print("backup-database did not create a readable backup")
        print(backup_output)
        return 1
    backup_list = run(["list-database-backups", "--limit", "5"])
    if f"{backup_id}\tstatus=completed" not in backup_list:
        print("list-database-backups did not show created backup")
        print(backup_list)
        return 1
    restore_guard = run(["restore-database", "--backup-path", str(backup_path)], expect=1)
    if "restore-database requires --yes" not in restore_guard:
        print("restore-database did not require explicit confirmation")
        print(restore_guard)
        return 1
    marker_book = run(["create-book", "--title", "Smoke Restore Marker", "--genre", "测试", "--platform", "manual"])
    marker_book_id = extract_id("book_id", marker_book)
    if f"{marker_book_id}\tSmoke Restore Marker" not in run(["list-books"]):
        print("restore marker book was not created before restore")
        return 1
    restore_output = run(["restore-database", "--backup-path", str(backup_path), "--yes"])
    pre_restore_path = Path(extract_value("pre_restore_backup_path", restore_output))
    if "restore-database: PASS" not in restore_output or not pre_restore_path.exists():
        print("restore-database did not restore and preserve pre-restore backup")
        print(restore_output)
        return 1
    restored_books = run(["list-books"])
    if "Smoke Restore Marker" in restored_books:
        print("restore-database did not restore backup state")
        print(restored_books)
        return 1
    print("smoke-test: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    print(f"version_id={v1}")
    print(f"publish_job_id={job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
