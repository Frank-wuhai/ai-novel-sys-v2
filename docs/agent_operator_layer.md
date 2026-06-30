# Agent Operator Layer

This document defines how external agents such as OpenClaw or Hermes should work with AI Novel System v2.

## Role Split

- AI Novel System v2 owns story state, workflow gates, quality reports, chapter versions, approvals, publish jobs, and database safety.
- Agent Plan owns model calls, market evidence, semantic memory, and optional visual asset generation.
- OpenClaw should be used as an operator: run approved commands, watch status, collect reports, and notify the author.
- Hermes-style agents should be used as repeat-task reviewers: summarize failures, compare drafts, and produce suggestions against copied artifacts.

External agents must not become a second production pipeline.

## Safe Entry Point

Use this wrapper instead of giving an external agent unrestricted command access:

```bash
venv/bin/python scripts/agent_ops.py --help
```

The wrapper only exposes whitelisted operations and keeps high-cost or high-risk actions behind explicit flags.

## Recommended Daily Flow

### 1. Morning Status

Read-only. Safe for OpenClaw to run on a schedule.

```bash
venv/bin/python scripts/agent_ops.py status --book-id 1 --start 1 --count 5
```

This runs:

- `development-status`
- `production-readiness`
- `agent-plan-utilization`
- `generation-queue-health`

Use the output as the daily work report.

### 2. Prepare Context

Low-risk by default. It creates a database backup, refreshes Agent Plan context, previews scaffold repair, and prints readiness.

```bash
venv/bin/python scripts/agent_ops.py prepare --book-id 1 --chapter-number 1 --start 1 --count 5
```

Only add `--apply` after you inspect the scaffold repair preview:

```bash
venv/bin/python scripts/agent_ops.py prepare --book-id 1 --chapter-number 1 --start 1 --count 5 --apply
```

Only add `--live-embedding` when you intentionally want to spend Agent Plan embedding quota:

```bash
venv/bin/python scripts/agent_ops.py prepare --book-id 1 --chapter-number 1 --live-embedding
```

### 3. Preview The Next Chapter Action

Default is preview-only.

```bash
venv/bin/python scripts/agent_ops.py next-action --book-id 1 --chapter-number 1
```

Execute only after the preview is acceptable:

```bash
venv/bin/python scripts/agent_ops.py next-action --book-id 1 --chapter-number 1 --execute
```

Queue live generation only when you want the queue worker to spend model quota later:

```bash
venv/bin/python scripts/agent_ops.py next-action --book-id 1 --chapter-number 1 --execute --queue-generation
```

### 4. Run The Queue Worker

Default is inspect-only.

```bash
venv/bin/python scripts/agent_ops.py worker --book-id 1
```

Execute queued tasks with a hard budget:

```bash
venv/bin/python scripts/agent_ops.py worker --book-id 1 --execute --max-loops 1 --max-tasks-per-loop 1 --token-budget 100000
```

For a budget-limited setup, keep `--max-loops 1` and `--max-tasks-per-loop 1` until the chapter workflow is stable.

### 5. Publishing

External agents may prepare publish jobs and dry-run artifacts. They must not confirm real publishing.

```bash
venv/bin/python scripts/agent_ops.py publish-dry-run --version-id 1 --platform "番茄小说"
```

Real publishing still requires the existing gated command and explicit human confirmation:

```bash
venv/bin/python -m app.cli execute-publish-job --job-id 1 --confirm
```

## OpenClaw Use Cases

Use OpenClaw for:

- scheduled status reports
- running `agent_ops.py status`
- running `agent_ops.py prepare` without `--apply` on a daily schedule
- queue health checks
- notifying the author when readiness blocks production
- collecting publish dry-run artifacts

Do not give OpenClaw permission to:

- edit source code
- run arbitrary shell commands
- delete files
- restore databases
- confirm real publishing
- commit or push git changes

## Hermes Use Cases

Use Hermes-style agents for copied text artifacts, not direct database writes:

- compare latest chapter versions
- summarize repeated quality failures
- extract reusable revision lessons
- propose chapter-level repair notes
- turn review reports into author-friendly revision suggestions

Recommended pattern:

1. Export or copy the relevant chapter/report text.
2. Let Hermes produce a review artifact.
3. Submit the accepted feedback through normal AI Novel System v2 commands or the dashboard.

Hermes should not directly overwrite chapter text or Canon records.

## Minimum Permission Set

For OpenClaw, start with only these commands:

```bash
venv/bin/python scripts/agent_ops.py status ...
venv/bin/python scripts/agent_ops.py prepare ...
venv/bin/python scripts/agent_ops.py next-action ...
venv/bin/python scripts/agent_ops.py worker ...
venv/bin/python scripts/agent_ops.py publish-dry-run ...
```

For Hermes, start with read-only file access to exported reports and no database write access.

## Failure Handling

If a command fails:

1. Run `venv/bin/python scripts/agent_ops.py status --book-id 1`.
2. Check `generation-queue-health`.
3. Do not retry live generation repeatedly without reading `llm-failure-summary`.
4. Back up before applying scaffold repair or restoring data.

The system should spend Agent Plan quota only after readiness and queue health are understandable.
