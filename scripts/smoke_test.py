from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
import sqlite3
import json


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / "venv/bin/python"
TEST_DB = "sqlite:///data/test-novel.db"


def run(args: list[str], *, expect: int = 0) -> str:
    cmd = [str(PYTHON), "-m", "app.cli", "--database-url", TEST_DB, *args]
    result = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != expect:
        print("COMMAND FAILED")
        print(" ".join(cmd))
        print(f"expected={expect} actual={result.returncode}")
        print(output)
        raise SystemExit(1)
    return output


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
    conn = sqlite3.connect(ROOT / "data/test-novel.db")
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
    if plan_ready.count("next_action=draft_chapter") != 2:
        print("planner did not mark planned chapters as draft-ready")
        print(plan_ready)
        return 1
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
    ])
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
        run(["enqueue-draft", "--book-id", str(book_id), "--chapter-number", "6"]),
    )
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
    queued_run = run(["run-generation-task", "--task-id", str(queued_draft_id)])
    queued_version_id = extract_id("version_id", queued_run)
    child_task_id = extract_id("child_generation_task_id", queued_run)
    if "status=completed" not in queued_run or queued_version_id < 1 or child_task_id < 1:
        print("queued draft task did not complete with version and child generation task")
        print(queued_run)
        return 1
    queue_task_detail = run(["show-generation-task", "--task-id", str(queued_draft_id)])
    if '"child_generation_task_id":' not in queue_task_detail or f'"version_id": {queued_version_id}' not in queue_task_detail:
        print("queue task audit did not record child task and version")
        print(queue_task_detail)
        return 1
    queued_revision_id = extract_id(
        "generation_task_id",
        run(["enqueue-revision", "--book-id", str(book_id), "--chapter-number", "6", "--max-attempts", "2"]),
    )
    retryable_revision = run(["run-generation-task", "--task-id", str(queued_revision_id)])
    if "status=pending" not in retryable_revision or '"retryable": true' not in retryable_revision:
        print("queued revision task did not remain pending after retryable failure")
        print(retryable_revision)
        return 1
    failed_revision = run(["run-generation-task", "--task-id", str(queued_revision_id)])
    if (
        "status=failed" not in failed_revision
        or '"error_category": "validation"' not in failed_revision
        or "latest chapter version must be needs_revision before revise" not in failed_revision
    ):
        print("queued revision task did not fail with expected final reason")
        print(failed_revision)
        return 1
    retry_revision = run(["retry-generation-task", "--task-id", str(queued_revision_id)])
    if "status=pending" not in retry_revision:
        print("retry-generation-task did not reset failed task")
        print(retry_revision)
        return 1
    run(["run-generation-task", "--task-id", str(queued_revision_id)])
    run(["run-generation-task", "--task-id", str(queued_revision_id)])
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
        "--dry-run",
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
    auto_draft = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "3", "--dry-run"])
    if "action=draft_chapter" not in auto_draft or "status=executed" not in auto_draft:
        print("run-next-action did not draft ready chapter")
        print(auto_draft)
        return 1
    auto_review = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "3"])
    if "action=review_chapter" not in auto_review or "status=executed" not in auto_review:
        print("run-next-action did not review draft chapter")
        print(auto_review)
        return 1
    auto_continuity_block = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "3"])
    if "action=record_chapter_continuity" not in auto_continuity_block or "status=blocked" not in auto_continuity_block:
        print("run-next-action did not block manual continuity writeback")
        print(auto_continuity_block)
        return 1
    cycle = run([
        "run-book-cycle",
        "--book-id",
        str(book_id),
        "--start",
        "4",
        "--count",
        "2",
        "--max-steps",
        "4",
        "--dry-run",
    ])
    if "executed_count=4" not in cycle or cycle.count("next_action=record_chapter_continuity") != 2:
        print("run-book-cycle did not advance safe steps and stop at manual continuity")
        print(cycle)
        return 1
    continuity_package = run(["human-decision-package", "--book-id", str(book_id), "--start", "3", "--count", "3"])
    if "continuity_count=3" not in continuity_package or "type=continuity_writeback" not in continuity_package:
        print("human decision package did not include continuity writeback items")
        print(continuity_package)
        return 1
    readiness = run(["production-readiness", "--book-id", str(book_id), "--start", "1", "--count", "5"])
    for expected in ("check\tfoundation\tpassed=True", "check\tevidence\tpassed=True", "check\tcanon\tpassed=True", "check\tllm\tpassed=True"):
        if expected not in readiness:
            print("production readiness missing expected pass check")
            print(readiness)
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
        "开场压力,能力触发,代价落地,章末钩子",
        "--constraints",
        "dry-run only",
    ])
    v1 = extract_id("version_id", run(["draft-chapter", "--book-id", str(book_id), "--chapter-number", "1", "--dry-run"]))
    if "next_action=review_chapter" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request review after draft")
        return 1
    version_list = run(["list-versions", "--book-id", str(book_id), "--chapter-number", "1"])
    if f"{v1}\tversion=1\tstatus=draft" not in version_list:
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
        '"prompt_template": "draft_chapter@v3"' not in task_detail
        or f'"version_id": {v1}' not in task_detail
        or '"estimated_total_tokens":' not in task_detail
        or '"elapsed_ms":' not in task_detail
    ):
        print("show-generation-task did not include expected JSON")
        print(task_detail)
        return 1
    conn = sqlite3.connect(ROOT / "data/test-novel.db")
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
        if input_data.get("prompt_template") != "draft_chapter@v3":
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
    run([
        "record-chapter-continuity",
        "--book-id",
        str(book_id),
        "--chapter-number",
        "1",
        "--summary",
        "未过质检前不得回写长期记忆。",
    ], expect=1)
    review = run(["review-chapter", "--book-id", str(book_id), "--chapter-number", "1"])
    if "passed=True" not in review:
        print("quality gate did not pass")
        print(review)
        return 1
    conn = sqlite3.connect(ROOT / "data/test-novel.db")
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
            "setting_risk",
            "platform_risk",
        }
        if quality_data.get("status") != "PASS" or set(quality_data.get("dimensions", {})) != expected_dimensions:
            print("structured quality report is incomplete")
            print(quality_report)
            return 1
    finally:
        conn.close()
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
    failed_review = run(["review-chapter", "--book-id", str(book_id), "--chapter-number", "2"], expect=0)
    if "passed=False" not in failed_review:
        print("failed quality gate did not fail as expected")
        print(failed_review)
        return 1
    revision_brief_id = extract_id(
        "revision_brief_id",
        run(["create-revision-brief", "--book-id", str(book_id), "--chapter-number", "2"]),
    )
    if "next_action=revise_chapter" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "2", "--count", "1"]):
        print("planner did not request revise after revision brief")
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
    conn = sqlite3.connect(ROOT / "data/test-novel.db")
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
    if "next_action=approve_chapter" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request approval after continuity")
        return 1
    approval_package = run(["human-decision-package", "--book-id", str(book_id), "--start", "1", "--count", "1"])
    if "approval_count=1" not in approval_package or "type=human_approval" not in approval_package:
        print("human decision package did not include approval item")
        print(approval_package)
        return 1
    auto_approve_block = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1"])
    if "action=approve_chapter" not in auto_approve_block or "status=blocked" not in auto_approve_block:
        print("run-next-action did not block manual approval")
        print(auto_approve_block)
        return 1
    conn = sqlite3.connect(ROOT / "data/test-novel.db")
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
    run(["create-publish-job", "--version-id", str(v1), "--platform", "manual"], expect=1)
    run(["approve-chapter", "--version-id", str(v1), "--reviewer", "smoke"])
    if "next_action=create_publish_job" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not request publish job after approval")
        return 1
    auto_job = run(["run-next-action", "--book-id", str(book_id), "--chapter-number", "1", "--platform", "manual"])
    if "action=create_publish_job" not in auto_job or "status=executed" not in auto_job:
        print("run-next-action did not create publish job")
        print(auto_job)
        return 1
    job_id = extract_id("object_id", auto_job)
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
    run(["mark-publish-job", "--job-id", str(job_id), "--status", "published", "--report", "smoke published"])
    if "next_action=done" not in run(["plan-chapters", "--book-id", str(book_id), "--start", "1", "--count", "1"]):
        print("planner did not mark published chapter done")
        return 1
    duplicate = run(["create-publish-job", "--version-id", str(v1), "--platform", "manual"], expect=1)
    if "active publish job already exists" not in duplicate:
        print("duplicate publish job guard did not trigger expected message")
        print(duplicate)
        return 1
    print("smoke-test: PASS")
    print(f"database={TEST_DB}")
    print(f"book_id={book_id}")
    print(f"version_id={v1}")
    print(f"publish_job_id={job_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
