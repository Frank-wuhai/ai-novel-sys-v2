# AI Novel System v2

AI Novel System v2 is a Python-owned novel production system. It keeps story state, quality gates, revision contracts, generation queues, publishing jobs, and database safety inside the application layer. LLM providers generate controlled drafts; OpenClaw/browser tooling is reserved for approved platform automation, screenshots, publishing, and feedback collection.

## Quick Start

```bash
python -m app.cli init-db
python -m app.cli seed-prompts
python -m app.cli create-book --title "Demo" --genre "真实武侠" --platform "manual"
python -m app.cli repair-production-scaffold --book-id 1 --apply
python -m app.cli author-command-center --book-id 1 --chapter-number 1 --start 1 --count 20
python scripts/run_local_dashboard.py --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765` and use the writing desk as the main flow. The default author-facing action should be continue, repair, revise, approve, or prepare publishing; queue controls, database tools, model diagnostics, and publishing internals stay in advanced panels.

## Author Flow

The intended day-to-day flow is documented in [docs/author_main_flow.md](docs/author_main_flow.md).

In short:

- Create or select a book.
- Let the system repair missing production scaffold.
- Continue writing from the author command center or dashboard.
- Submit natural-language revision feedback when a chapter feels wrong.
- Let the system infer revision strength automatically.
- Review, approve, and prepare publishing only after quality gates pass.

## Operator Manual

The full command reference has moved to [docs/operator_manual.md](docs/operator_manual.md). Use it for CLI recovery, database operations, queue supervision, publishing targets, inspection commands, and regression details.

## Core Boundaries

- Python owns database state, workflow gates, chapter versions, reviews, approvals, and publish jobs.
- LLM providers only produce draft or revision text through controlled service calls.
- OpenClaw/browser automation only operates on approved publish jobs and returns execution reports.
- Dry-run publishing may update publish job status and reports, but must not post to a real platform.
- Confirmed publishing requires explicit confirmation and platform automation configuration.

## Quality And Revision

The project uses a humanized writing pipeline instead of one-shot chapter generation. See [docs/humanized_production.md](docs/humanized_production.md).

Revision feedback is author-friendly: users write natural-language feedback, while the system infers whether the next pass should be a local patch, polish, targeted scene repair, structural rewrite, or fresh restart. The inferred decision is recorded in the revision contract for auditability.

## Regression

Run the local regression suite before changing prompts, workflow gates, dashboard actions, or database behavior:

```bash
venv/bin/python scripts/run_regressions.py
```

Strict quality mode can turn quality attention into failures:

```bash
venv/bin/python scripts/run_regressions.py --strict-quality
```

Regression and smoke scripts use isolated `data/*-regression.db` databases so they do not contend with the local production/dashboard database.

## Development Guardrail

Development follows the DNA spiral rule in [docs/dna_spiral_development.md](docs/dna_spiral_development.md): every capability increment should pair a feature change with a stability anchor, CLI recovery path, Web surface, and regression check.

When a book drifts away from the author's intended direction, run:

```bash
python -m app.cli story-alignment-audit --book-id 1 --chapter-limit 5
```

For a single chapter, separate system-context drift from model-default drift with:

```bash
python -m app.cli chapter-bias-audit --book-id 1 --chapter-number 1
```
