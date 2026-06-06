# DNA Spiral Development

AI Novel System v2 should evolve like a DNA double helix: one strand grows capability, the other strand preserves stability. Every new feature must attach through explicit interfaces, checks, and rollback-friendly data boundaries.

## Double Helix

Capability strand:

- production intelligence: planning, drafting, reviewing, revision, publishing
- Agent Plan extensions: web research, semantic memory, visual assets
- author experience: dashboard actions, background workers, one-click production

Stability strand:

- database migrations and schema regression
- readiness gates and preflight checks
- deterministic dry-runs before live model calls
- audit logs, status transitions, and human approval points
- dashboard self-test and targeted regression scripts

The two strands must move together. A feature is not complete when it only works from one command; it is complete when its state, checks, UI surface, and regression path are clear.

## Base Pairs

Each iteration should pair a capability change with a stability anchor:

| Capability change | Stability anchor |
| --- | --- |
| New table or stored artifact | Alembic migration, schema regression, graceful missing-table behavior |
| New LLM or Agent Plan call | dry-run mode, config visibility, error categorization |
| New production action | readiness/preflight effect, queue or status audit |
| New dashboard button | CLI equivalent and dashboard self-test |
| New prompt context | task audit field and token/cost visibility |
| New automated workflow | manual override and idempotent retry behavior |

## Spiral Levels

Build in small rings. Each ring may touch multiple modules, but it should keep the blast radius bounded.

1. Data ring: schema, persisted artifacts, status fields, indexes.
2. Service ring: pure orchestration functions with explicit inputs and outputs.
3. CLI ring: repeatable command for local verification and recovery.
4. Web ring: dashboard action or view that calls the same service path as CLI.
5. Gate ring: readiness, preflight, audit, cost, and failure behavior.
6. Regression ring: compile, migration regression, targeted workflow tests, dashboard self-test.
7. Documentation ring: README command path and architectural note.

Do not skip directly from service code to Web-only behavior. The CLI ring is the system's pressure valve for debugging and future repair.

## Stability Rules

- Preserve the original production flow unless the change explicitly migrates it.
- Treat Agent Plan features as enhancement layers unless they become a proven hard dependency.
- Default expensive or irreversible operations to dry-run, preview, or explicit confirmation.
- Keep Canon, market evidence, semantic memory, and visual assets separate. Semantic memory may recall; it must not silently rewrite Canon.
- A readiness warning can be non-blocking when it describes an enhancement. A readiness failure should block only when the core production contract is unsafe.
- Every generated or imported external artifact must be traceable by path, source ID, task ID, or table ID.
- Web actions should be idempotent where possible. Re-running them should update or add traceable records, not corrupt workflow state.
- If a migration introduces a new table, dashboard payloads should degrade gracefully before the migration is applied.

## Completion Definition

A development ring is complete only when:

- CLI and Web use the same service function or clearly documented sibling functions.
- The feature can be inspected without calling a live model.
- Existing production commands still run.
- The main database schema can be upgraded with Alembic.
- Relevant regression checks pass.
- README or docs explain when to use the feature and how it relates to the main production flow.

## Current Agent Plan Position

Agent Plan is currently an enhancement strand, not a parallel production system.

- Web search strengthens market evidence.
- Embeddings strengthen semantic memory and context recall.
- Visual models support cover and chapter illustration planning.
- Language models continue to serve the existing production pipeline through provider configuration.

The main production helix remains:

```text
Story foundation / Bible / Canon / Evidence
-> Chapter brief
-> Draft
-> Review
-> Revision
-> Continuity
-> Approval / Publishing
```

Agent Plan should wrap and strengthen that helix without replacing its state machine until the replacement has equal gates, audits, fallbacks, and tests.
