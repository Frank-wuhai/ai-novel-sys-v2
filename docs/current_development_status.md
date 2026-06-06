# Current Development Status

This document summarizes the active development documents and maps them to the current system state. Use it with:

```bash
python -m app.cli development-status --book-id 1
```

## Document Summary

### DNA Spiral Development

Current rule: every capability increment must be paired with stability anchors.

In practice, a change should not stop at a service function. It should also have enough of the surrounding rings to be maintainable:

- service interface
- CLI recovery path
- Web surface when user-facing
- dry-run or preview mode
- readiness, preflight, audit, or status visibility
- regression or self-test
- documentation

### Development Archive

Current rule: previous session decisions should be preserved as project memory, but active instructions should remain centralized.

Old notes belong in `docs/archive/` and should be registered in `docs/development_archive.md` as active, absorbed, historical, or superseded.

### Production Roadmap

Current direction: the system should become an author-friendly main writing assistant instead of a raw workflow console.

The author should mainly decide:

- story direction
- whether a draft is acceptable
- whether to approve, locally modify, or rewrite

The system should manage internal workflow state, queue behavior, review, revision, continuity, and publishing preflight.

### Humanized Production

Current writing model: chapters are not one-shot text completions.

The system should emulate a human author/editor loop:

- confirm story promise and core rules
- build arcs and chapter intent
- draft through small causal units
- review reader experience
- translate feedback into visible revision requirements

### Root Cause Alignment

Current troubleshooting rule: when the story goes off direction, diagnose before spending more model tokens.

The system must distinguish:

- foundation or Story Bible mismatch
- stale or polluted chapter brief
- old draft or old review pollution
- model-default drift
- ordinary prose quality failure

## Current System Maturity

### Stable Core

- Python owns database state and workflow gates.
- Alembic migrations and migration regression exist.
- Generation queue, task status, budget, LLM audit, and cost summaries exist.
- Dashboard self-test exists.
- Publishing has dry-run, queue, retry, execution report, and explicit confirmation.

### Active Production Intelligence

- Story foundation, Story Bible, Canon, evidence, chapter brief, draft, review, revision, continuity, approval, and publish jobs are represented in the system.
- Humanized production and chapter standards have service-level support.
- Direction alignment, bias audit, failure attribution, and data governance are present.
- Author-mode production is present but still evolving toward a simpler front-end experience.

### Agent Plan Enhancement Strand

Agent Plan is integrated as an enhancement layer:

- language model configuration uses Agent Plan provider settings
- web research creates importable market research packs
- embeddings build semantic memory
- semantic memory can enter production packet context as recall
- visual assets create Canon-bound cover and chapter illustration prompt artifacts

It is not a separate production pipeline.

### Current Gap Pattern

For an individual book, production readiness may still fail when the book lacks:

- Story Bible or approved production skeleton
- usable market evidence for its genre
- Character, WorldRule, or PowerSystem Canon records

This is expected. Agent Plan can strengthen the system, but it should not bypass the core production contract.

## Recommended Next DNA Spiral

For the next development ring:

1. Capability strand: make market research import and Canon completion easier from the dashboard.
2. Stability strand: keep scaffold repair preview-only by default; require explicit apply before writing production data.
3. CLI ring: preserve every Web action as a CLI command.
4. Web ring: expose fewer, clearer author-facing actions.
5. Regression ring: run compile, migration regression, readiness regression, and dashboard self-test after changes.

The immediate practical goal is to turn a book with missing Story Bible, evidence, and Canon into a production-ready book without breaking the original chapter state machine.
