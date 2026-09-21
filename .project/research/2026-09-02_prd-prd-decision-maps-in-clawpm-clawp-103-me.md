---
created: '2026-09-02'
id: clawpm-research-prd-prd-decision-maps-in-clawpm-clawp-103-me
linked_task_tree: CLAWP-111
status: open
tags:
- prd
- planner
- wayfinder
- clawp-103
type: decision
---
# PRD: Decision maps in clawpm (CLAWP-103 merge plan)

## Objective
clawpm gains wayfinder's decide-before-build shape as a task kind: decision tickets with recorded resolutions, a root map (destination / decisions so far / not yet specified / out of scope), a frontier query, blocking edges at emission, and a planner chart mode. No second tracker, no second spec store.

## Why
CLAWP-103 gap analysis (ADR docs/design/ADR-2026-09-02-planning-merge-plan.md). clawpm already has predictions, rubric-gated dispatch, leases, won't-do ledger and an emission API; the missing shape is the one wayfinder has: decide first, one decision per session, fog kept coarse until sharp.

## Constraints
- Extend clawpm core / clawpm-planner only (WONT-DO 2026-07-14: no parallel planning system).
- New frontmatter keys omitted when default so existing task files are byte-identical.
- Zero LLM calls in core; all judgment stays in the planner skill.
- Round-trip tests for every new field through tasks add, tasks edit, emit-tree.

## Out of scope
- OpenSpec-style spec deltas folded into .project/SPEC.md on close (fog).
- GitHub-issue distribution of maps (CLAWP-058).
- Persona system (routed to code-quorum as lens checklists).

## Success definition
A planner run in chart mode emits a decision map that `clawpm next --frontier <root>` can walk; resolving a decision with `clawpm done --resolution` updates the map's Decisions-so-far; fog graduates via `tasks add --graduates`; the plugin exposes /clawpm:plan, /clawpm:chart, /clawpm:next.

## Chosen approach
See ADR sections D1-D6. Leaves are vertical slices per user-visible guarantee.

## Open questions
- Whether `resolution` should also be mirrored into a research entry of type decision for cross-project recall (deferred; the task file is the record).

## Traceability
Ground: clawpm source at fork/main 06a32b7 read directly (models.py, emit_tree.py, cli/shortcuts.py next, tasks.py get_next_task, leases). No graph consulted; effort estimates are read-based, tagged UNGROUNDED-GRAPH.

