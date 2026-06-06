# AI Novel System v2 Documentation

This directory is the project memory for development decisions, session outputs, and architecture rules. It is organized so older session documents can be kept without becoming competing instructions.

## Current Canonical Documents

| Document | Role | Status |
| --- | --- | --- |
| [`dna_spiral_development.md`](dna_spiral_development.md) | Development method: grow capability and stability together like a double helix. | Active rule |
| [`production_roadmap.md`](production_roadmap.md) | Product direction and staged roadmap toward an author-friendly main writing assistant. | Active roadmap |
| [`one_button_production_flow.md`](one_button_production_flow.md) | Frontend contract: every daily page shows diagnosis summary plus one primary action, with advanced tools folded away. | Active UI rule |
| [`humanized_production.md`](humanized_production.md) | Writing workflow model: how a human author/editor process maps into system production. | Active production spec |
| [`root_cause_alignment.md`](root_cause_alignment.md) | Diagnosis method for story direction drift, stale context, and model-default bias. | Active troubleshooting spec |
| [`current_development_status.md`](current_development_status.md) | Summary of active documents mapped to current system maturity and next DNA spiral. | Active status |
| [`development_archive.md`](development_archive.md) | Consolidated archive of session-era development decisions and how they relate to current architecture. | Living archive |

## How To Add Old Session Documents

When a previous conversation produced a useful plan, analysis, or design note:

1. Keep the original text if possible in `docs/archive/`.
2. Add an entry to [`development_archive.md`](development_archive.md).
3. Mark whether the document is `active`, `absorbed`, `historical`, or `superseded`.
4. Link it to the current canonical document that now owns the rule.

If an old document conflicts with an active rule, do not delete it. Mark it as `superseded` and explain which newer rule replaces it.

## Status Labels

- `active`: still governs implementation.
- `absorbed`: useful ideas have been integrated into an active document.
- `historical`: kept for context, not a current instruction.
- `superseded`: replaced by a newer rule or implementation.

## Current Priority Stack

1. Keep the original production state machine stable.
2. Improve author-facing production through the roadmap.
3. Use Agent Plan as an enhancement layer for research, semantic memory, and visual assets.
4. Apply DNA spiral development to every future change.
5. Preserve older session knowledge through archive entries instead of scattered one-off notes.
