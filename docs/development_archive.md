# Development Archive

This archive consolidates development documents and session decisions that shaped AI Novel System v2. It is not a replacement for the active specs; it explains where each idea came from, what still applies, and where future work should attach.

## Archive Index

| Area | Source document | Current status | Current owner |
| --- | --- | --- | --- |
| DNA-style iterative development | [`dna_spiral_development.md`](dna_spiral_development.md) | active | Stability and iteration method |
| Author-friendly production roadmap | [`production_roadmap.md`](production_roadmap.md) | active | Product direction |
| Humanized chapter production | [`humanized_production.md`](humanized_production.md) | active | Writing production contract |
| Direction drift diagnosis | [`root_cause_alignment.md`](root_cause_alignment.md) | active | Preflight, bias audit, and troubleshooting |
| Agent Plan migration and enhancement layer | README Agent Plan section plus current code | active | Provider config, research, semantic memory, visual assets |

## Consolidated Decisions

### 1. Python Owns Production State

Status: active

The core production flow stays in Python services and database state:

```text
Story foundation / Bible / Canon / Evidence
-> Chapter brief
-> Draft
-> Review
-> Revision
-> Continuity
-> Approval / Publishing
```

LLM providers generate structured content through controlled service calls. Browser automation and OpenClaw-style tooling can execute approved platform operations, but they do not decide story Canon, quality gates, or workflow state.

Current owner:

- README architecture boundary
- `app/services/production*.py`
- `app/workflows/state_machine.py`

### 2. Humanized Production Is The Writing Model

Status: active

The system should not treat chapters as one-shot generation. It should emulate a human author/editor loop:

- confirm story promise and core rules
- turn a volume into arcs
- turn each chapter into a short director sheet
- write in small causal units
- review against reader experience
- translate feedback into visible revision requirements

Current owner:

- [`humanized_production.md`](humanized_production.md)
- `app/services/chapter_standards.py`
- `app/services/production_packet.py`
- `app/services/quality.py`

### 3. Author Experience Should Hide Internal Machinery

Status: active

The roadmap direction is to move from a workflow console toward an author workbench. The user should mostly make author decisions:

- set or revise story direction
- read the current draft
- approve, request a local change, or ask for a rewrite

The system should handle queue state, revision loops, continuity writeback, failure attribution, and publishing preflight behind clearer front-end actions.

Current owner:

- [`production_roadmap.md`](production_roadmap.md)
- `scripts/run_local_dashboard.py`
- `app/dashboard_assets.py`
- `app/services/author_runner.py`

### 4. Direction Drift Must Be Diagnosed Before Spending More Tokens

Status: active

When a draft goes in the wrong direction, the system should distinguish:

- story foundation or Story Bible mismatch
- stale or polluted chapter brief
- old draft or old review pollution
- model-default drift
- actual writing quality problem

Do not keep adding prompt text blindly. Run alignment and bias audits, then choose whether to change foundation, regenerate brief, use local patch, targeted revision, rewrite, or fresh restart.

Current owner:

- [`root_cause_alignment.md`](root_cause_alignment.md)
- `app/services/story_alignment.py`
- `app/services/bias.py`
- `app/services/failure_attribution.py`

### 5. Agent Plan Is An Enhancement Strand

Status: active

Agent Plan is not a parallel production system. It strengthens the existing flow:

- language models serve the existing provider abstraction
- web search strengthens market evidence
- embeddings strengthen semantic memory and context recall
- visual models support cover and chapter illustration planning

Current owner:

- README Agent Plan section
- `app/core/config.py`
- `app/llm/providers.py`
- `app/services/agent_plan_intelligence.py`
- `migrations/versions/20260605_0007_agent_plan_intelligence.py`

### 6. DNA Spiral Development Is The Iteration Contract

Status: active

Every future feature should advance two strands at once:

- capability: the new behavior or intelligence
- stability: dry-run, migration, CLI, Web, readiness/preflight, audit, regression, and docs

This keeps future sessions from creating disconnected features that work once but are hard to modify later.

Current owner:

- [`dna_spiral_development.md`](dna_spiral_development.md)

## Supersession Rules

When old session notes disagree with current docs:

- Keep the old note as historical context.
- Mark it `superseded`.
- Link to the active owner.
- Do not implement from superseded notes unless the user explicitly reopens the decision.

## Future Archive Template

Use this block when adding a previous-session document:

```text
Title:
Source/session:
Date:
Status: active | absorbed | historical | superseded
Summary:
Current owner:
Follow-up:
```

## Open Integration Gaps

- If there are previous conversation exports outside this repository, place them in `docs/archive/` and register them here.
- Agent Plan live image generation still needs an official endpoint adapter before it should become a live Web action.
- Market research import remains evidence-first: search output must be imported and audited before it influences production.
