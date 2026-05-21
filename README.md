# AI Novel System v2

Python owns the production system. LLM providers generate structured drafts. OpenClaw and browser tooling are reserved for platform automation, publishing, screenshots, and feedback collection.

## First Loop

```bash
python -m app.cli init-db
python -m app.cli seed-prompts
python -m app.cli add-evidence-source --source-id "source-001" --title "..." --url "..." --reliability 4 --status verified
python -m app.cli add-market-signal --source-id "source-001" --genre "玄幻都市" --signal "..." --confidence 75
python -m app.cli create-book --title "Demo" --genre "玄幻都市" --platform "番茄小说"
python -m app.cli add-character --book-id 1 --name "主角" --role "protagonist"
python -m app.cli add-world-rule --book-id 1 --category "能力代价" --rule "..."
python -m app.cli add-power-system --book-id 1 --name "..." --rules "..." --costs "..." --limits "..."
python -m app.cli create-foundation --book-id 1 --premise "..."
python -m app.cli upsert-story-bible --book-id 1 --positioning "..." --reader-promise "..." --main-plot "..." --forbidden-rules "..."
python -m app.cli create-volume --book-id 1 --volume-number 1 --title "第一卷"
python -m app.cli create-story-arc --book-id 1 --arc-number 1 --title "开局剧情段" --start-chapter 1 --end-chapter 10 --goal "..."
python -m app.cli create-arc-chapter-plan --book-id 1 --arc-number 1
python -m app.cli create-chapter-plan --book-id 1 --start 1 --count 5 --goal-prefix "第一卷推进"
python -m app.cli plan-chapters --book-id 1 --start 1 --count 5
python -m app.cli run-next-action --book-id 1 --chapter-number 1 --dry-run
python -m app.cli run-book-cycle --book-id 1 --start 1 --count 5 --max-steps 10 --dry-run
python -m app.cli run-book-cycle --book-id 1 --start 1 --count 5 --max-steps 10 --dry-run --queue-generation
python -m app.cli enqueue-draft --book-id 1 --chapter-number 1 --max-attempts 3
python -m app.cli list-generation-queue --status pending
python -m app.cli run-generation-task --task-id 1
python -m app.cli run-generation-queue --max-tasks 3
python -m app.cli run-generation-worker --max-loops 5 --sleep-seconds 2 --max-tasks-per-loop 2
python -m app.cli project-dashboard --book-id 1 --start 1 --count 20
python -m app.cli human-decision-package --book-id 1 --start 1 --count 5
python -m app.cli production-readiness --book-id 1 --start 1 --count 5
python -m app.cli create-chapter-brief --book-id 1 --chapter-number 1 --goal "..."
python -m app.cli draft-chapter --book-id 1 --chapter-number 1 --dry-run
python -m app.cli enqueue-revision --book-id 1 --chapter-number 1 --max-attempts 3
python -m app.cli retry-generation-task --task-id 1
python -m app.cli review-chapter --book-id 1 --chapter-number 1
python -m app.cli create-revision-brief --book-id 1 --chapter-number 1
python -m app.cli revise-chapter --book-id 1 --chapter-number 1 --dry-run
python -m app.cli record-chapter-continuity --book-id 1 --chapter-number 1 --summary "..."
python -m app.cli approve-chapter --version-id 1 --reviewer human
python -m app.cli create-publish-job --version-id 1 --platform "manual"
python -m app.cli publish-job-dry-run --job-id 1
python -m app.cli queue-publish-job --job-id 1
python -m app.cli mark-publish-job --job-id 1 --status published --report "manual test"
python -m app.cli retry-publish-job --job-id 1
```

OpenClaw should not decide story canon or quality gates. It can execute approved `publish_jobs`.

## Inspection Commands

```bash
python -m app.cli list-books
python -m app.cli show-book --book-id 1
python -m app.cli list-chapters --book-id 1
python -m app.cli show-chapter --book-id 1 --chapter-number 1
python -m app.cli list-versions --book-id 1 --chapter-number 1
python -m app.cli show-version --version-id 1
python -m app.cli list-generation-tasks --book-id 1
python -m app.cli show-generation-task --task-id 1
python -m app.cli compare-chapter-versions --left-version-id 1 --right-version-id 2
python -m app.cli list-publish-jobs
python -m app.cli list-prompts
python -m app.cli show-prompt --name draft_chapter --version v1
python -m app.cli list-evidence-sources
python -m app.cli list-market-signals --genre "玄幻都市" --usable-only
python -m app.cli show-evidence-context --genre "玄幻都市"
python -m app.cli show-canon-context --book-id 1
python -m app.cli show-story-bible --book-id 1
python -m app.cli show-outline --book-id 1
python -m app.cli show-story-context --book-id 1 --chapter-number 1
python -m app.cli plan-chapters --book-id 1 --start 1 --count 10
```

## Architecture Boundary

- Python owns database state, workflow gates, chapter versions, reviews, approvals, and publish jobs.
- LLM providers only produce draft text through controlled service calls.
- OpenClaw/browser automation only operates on approved publish jobs and returns execution reports.
- A publish dry-run may update `publish_jobs.status` and `publish_jobs.result_report`; it must not post to a real platform.

## Workflow Gates

Chapter version status transitions:

- `draft --quality_pass--> reviewed_pass`
- `draft --quality_fail--> needs_revision`
- `needs_revision --quality_pass--> reviewed_pass`
- `reviewed_pass --human_approve--> approved`

Publish job status transitions:

- `pending --dry_run--> dry_run_ready`
- `dry_run_ready --queue_for_platform--> queued`
- `queued --mark_published--> published`
- `queued --mark_failed--> failed`
- `failed --retry--> queued`

Commands cannot skip these transitions.

## Quality Gate

`review-chapter` writes a structured JSON report to `quality_reports.report`.

Current deterministic dimensions:

- `basic_publishability`: length and forbidden production markers
- `brief_coverage`: chapter goal, required beats, and constraints coverage
- `canon_consistency`: visible use of registered Canon context
- `reader_momentum`: pressure, choice, cost, discovery, and hook markers
- `conflict_pressure`: visible conflict, danger, obstruction, or escalation
- `choice_and_cost`: meaningful choice with cost, consequence, or tradeoff
- `hook_strength`: end-of-chapter mystery, turn, discovery, or unresolved pressure
- `prose_density`: guards against thin, repetitive, or filler-heavy prose
- `arc_alignment`: visible alignment with Story Arc phase, goal, climax/turn, and boundaries
- `setting_risk`: obvious rule-breaking phrases such as no-cost/infinite-use power
- `platform_risk`: system/meta leakage markers

The chapter passes only when there are no issues, total score is at least `70`, and every dimension is at least `50`.

## Revision Loop

When a chapter version fails `review-chapter`, its status becomes `needs_revision`.

Use the failure report to create a structured revision brief:

```bash
python -m app.cli create-revision-brief --book-id 1 --chapter-number 2
```

Then create a revised draft version:

```bash
python -m app.cli revise-chapter --book-id 1 --chapter-number 2 --dry-run
```

The revised version returns to `draft`; it must run through `review-chapter` again before continuity writeback, approval, or publishing. Revision tasks record:

- source failed version
- quality report
- revision brief
- market evidence IDs
- Canon refs

Manual/imported chapter versions can enter the same loop:

```bash
python -m app.cli create-manual-chapter-version --book-id 1 --chapter-number 2 --title "..." --content "..."
```

## Chapter Planner

Use the planner to move from single-chapter operation to a chapter queue:

```bash
python -m app.cli create-chapter-plan \
  --book-id 1 \
  --start 1 \
  --count 10 \
  --goal-prefix "第一卷主线推进" \
  --required-beats "压力,选择,代价,钩子" \
  --constraints "保持 Canon 连续性"

python -m app.cli plan-chapters --book-id 1 --start 1 --count 10
```

When chapters fall inside a Story Arc, `create-chapter-plan` automatically enriches each brief with the arc title, phase, goal, climax/turn direction, and arc boundary constraints.

To plan directly from an existing arc:

```bash
python -m app.cli create-arc-chapter-plan --book-id 1 --arc-number 1
```

`plan-chapters` does not generate prose. It inspects each chapter and reports the next action, such as:

- `create_chapter_brief`
- `draft_chapter`
- `review_chapter`
- `create_revision_brief`
- `revise_chapter`
- `record_chapter_continuity`
- `approve_chapter`
- `create_publish_job`
- `publish_job_dry_run`
- `queue_publish_job`
- `mark_publish_job`
- `done`

Use `run-next-action` to execute one safe planner step for a single chapter:

```bash
python -m app.cli run-next-action --book-id 1 --chapter-number 3 --dry-run
```

Safe automated actions:

- create chapter brief
- draft dry-run
- review chapter
- create revision brief
- revise dry-run
- create publish job
- publish dry-run
- queue publish job
- retry failed publish job

Manual-only actions remain blocked:

- continuity writeback
- human approval
- final platform publish mark

Use `run-book-cycle` to repeatedly execute safe steps across a chapter range:

```bash
python -m app.cli run-book-cycle \
  --book-id 1 \
  --start 1 \
  --count 10 \
  --max-steps 20 \
  --dry-run
```

The cycle re-plans after every executed step. It stops when there are no safe actions left in range or when `--max-steps` is reached, then prints executed, blocked, and done chapters.

Use `--queue-generation` when you want planner cycles to enqueue draft/revision tasks instead of calling the LLM synchronously:

```bash
python -m app.cli run-book-cycle \
  --book-id 1 \
  --start 1 \
  --count 10 \
  --max-steps 20 \
  --dry-run \
  --queue-generation
```

Queued generation tasks track attempts and retryable failures:

```bash
python -m app.cli enqueue-draft --book-id 1 --chapter-number 1 --max-attempts 3
python -m app.cli run-generation-queue --max-tasks 3
python -m app.cli run-generation-worker --max-loops 20 --sleep-seconds 5 --max-tasks-per-loop 2
python -m app.cli retry-generation-task --task-id 1
```

`run-generation-worker` is a bounded long-running queue consumer. It exits after `--max-loops`, so it can be supervised by shell scripts, systemd, or another process manager.

## Operator Dashboard

Use the dashboard to see the current operating state of a book range without mutating data:

```bash
python -m app.cli project-dashboard --book-id 1 --start 1 --count 20
```

It reports readiness checks, chapter next-action counts, per-chapter state, generation queue state, recent generation usage estimates, human decision counts, and one recommended next command.

## Human Decision Package

Use the decision package after a book cycle to see exactly what requires human judgment:

```bash
python -m app.cli human-decision-package --book-id 1 --start 1 --count 10
```

It groups blocked work into:

- `continuity_writeback`: chapter passed quality and needs Canon/continuity updates
- `human_approval`: continuity is recorded and the chapter needs human approval
- `final_publish_confirmation`: platform automation is queued and needs final confirmation
- `manual_inspection`: unusual state that needs direct inspection

Each item includes a command hint, so the package answers what must be approved and how to do it.

## Production Readiness

Before disabling `--dry-run`, run the readiness gate:

```bash
python -m app.cli production-readiness --book-id 1 --start 1 --count 10
```

It checks:

- story foundation
- story bible and arc coverage
- usable evidence
- Canon coverage
- chapter queue state
- human decision package state
- LLM configuration

To verify the live Volcano Ark/Coding Plan model path, opt in explicitly:

```bash
python -m app.cli production-readiness --book-id 1 --start 1 --count 10 --live-llm
```

`--live-llm` sends a tiny health-check request. It does not generate novel prose.

## Development Database Safety

Use `reset-dev-db` only for local development data:

```bash
python -m app.cli reset-dev-db --yes
```

Use a separate test database for repeatable checks:

```bash
python -m app.cli --database-url sqlite:///data/test-novel.db reset-dev-db --yes
python -m app.cli --database-url sqlite:///data/test-novel.db list-books
```

Run the smoke test without touching `data/novel.db`:

```bash
python scripts/smoke_test.py
```

The smoke test uses `data/test-novel.db` and verifies both successful workflow transitions and blocked invalid transitions.

Run the targeted production-readiness regression test when changing readiness or evidence logic:

```bash
python scripts/readiness_regression_test.py
```

It uses `data/readiness-regression.db` and verifies that market evidence must match the current book genre before production readiness can pass.

## Prompt And Structured Draft Output

Draft generation uses versioned prompt templates stored in `prompt_templates`.

The default drafting template is:

- `draft_chapter@v3`

`draft_chapter@v3` injects usable market evidence and Canon context into the prompt. A market signal is usable only when:

- it has `confidence >= 60`
- it is linked to an evidence source
- the source is `verified`
- the source has `reliability >= 3`

LLM draft output must be valid JSON:

```json
{
  "title": "章节标题",
  "content": "章节草稿正文",
  "self_check": ["如何遵守约束"],
  "used_brief_points": ["使用了哪些 brief 点"]
}
```

The system stores `content` in `chapter_versions.content` and stores structured metadata in `generation_tasks.output_json`.

## Evidence Layer

Evidence and market signals are stored separately from prose:

```bash
python -m app.cli add-evidence-source --source-id "fanqie-rank-20260520" --title "..." --url "..." --reliability 4 --status verified
python -m app.cli add-market-signal --source-id "fanqie-rank-20260520" --genre "玄幻都市" --signal "..." --confidence 75
python -m app.cli show-evidence-context --genre "玄幻都市"
python -m app.cli audit-evidence --genre "玄幻都市"
```

Draft generation records the selected `market_signal_ids` and `canon_refs` in `generation_tasks.input_json`, so every generated draft can be traced back to the evidence and Canon available at generation time.

Generation task output records lightweight usage telemetry:

- `prompt_chars`
- `response_chars`
- `estimated_prompt_tokens`
- `estimated_response_tokens`
- `estimated_total_tokens`
- `elapsed_ms`
- provider `usage` when available

`audit-evidence` explains why each market signal is or is not usable, including low confidence, missing source, unverified source, and low source reliability.

## Story Bible And Outline

Story Bible stores the long-range control layer above chapter briefs:

```bash
python -m app.cli upsert-story-bible \
  --book-id 1 \
  --positioning "玄幻都市有代价能力连载" \
  --reader-promise "每章都有压力、选择、代价和新发现" \
  --main-plot "主角追查异象源头并理解能力代价" \
  --protagonist-arc "从被动自保到主动承担代价" \
  --power-curve "能力收益逐步变强，代价同步加重" \
  --forbidden-rules "不得无代价解决危机" \
  --style-guide "节奏紧，章末保留明确钩子"

python -m app.cli create-volume --book-id 1 --volume-number 1 --title "异象初现"

python -m app.cli create-story-arc \
  --book-id 1 \
  --arc-number 1 \
  --title "第一次代价推演" \
  --start-chapter 1 \
  --end-chapter 10 \
  --goal "建立能力收益与代价绑定" \
  --climax "主角赢下危机但付出记忆代价" \
  --turn "异象并非偶发事件" \
  --volume-number 1
```

Use these inspection commands before drafting:

```bash
python -m app.cli show-story-bible --book-id 1
python -m app.cli show-outline --book-id 1
python -m app.cli show-story-context --book-id 1 --chapter-number 1
```

Draft generation injects Story Bible and the matching Story Arc through Canon context, and records their IDs in `generation_tasks.input_json.canon_refs`.

## Canon Layer

Canon is the long-term story memory used before drafting:

```bash
python -m app.cli add-character --book-id 1 --name "林澈" --role "主角" --personality "..." --ability "..."
python -m app.cli add-character-state --character-id 1 --state "..."
python -m app.cli add-world-rule --book-id 1 --category "能力代价" --rule "..."
python -m app.cli add-power-system --book-id 1 --name "代价推演" --rules "..." --costs "..." --limits "..."
python -m app.cli add-plot-thread --book-id 1 --name "..." --description "..."
python -m app.cli add-foreshadow --book-id 1 --setup "..."
python -m app.cli show-canon-context --book-id 1
```

The drafting service does not invent long-term setting when Canon is empty. It injects a warning instead, so missing story memory is visible in the generation audit trail.

## Continuity Writeback

After a chapter passes quality review, use continuity writeback to update long-term memory:

```bash
python -m app.cli record-chapter-continuity \
  --book-id 1 \
  --chapter-number 1 \
  --summary "本章摘要" \
  --character-state "1:人物在本章结束时的新状态" \
  --new-foreshadow "新增伏笔" \
  --payoff "2:已回收伏笔的兑现说明" \
  --plot-thread-status "1:active"
```

Continuity writeback is gated: the latest chapter version must be `reviewed_pass` or `approved`. Drafts that have not passed quality review cannot update Canon.
