---
complexity: l
created: '2026-06-11'
depends:
- CLAWP-053
- CLAWP-054
id: CLAWP-056
predictions:
  complexity: l
  confidence: 3
  duration_min: 1440
  files_changed: 10
  pre_mortem: most likely failure = the emission API leaks judgment concerns back
    into core (e.g. core deciding leaf delegability) — keep core a pure persist-what-youre-given
    sink; all decisions arrive pre-made from the skill
  success_criteria:
  - 'a single core operation accepts a fully-specified task-tree (each leaf: title,
    rubric, scope, stop_conditions, delegability, predictions) and persists it via
    tasks decompose + emit-rubric atomically (all-or-nothing on failure), reject-matched
    against CLAWP-053 and constitution-checked against CLAWP-057 at emission, each
    task baseline-stamped (CLAWP-055)'
  - a PRD/spec artifact can be stored as a clawpm research/mission entry and linked
    to its emitted task-tree, retrievable by a downstream executor; demonstrated on
    one software AND one knowledge-work plan
  - clawpm core makes ZERO LLM calls in this path (deterministic; verified by test)
    — all judgment is supplied by the caller
priority: 5
---



# clawpm-core emission API: one-shot persist a fully-contracted task-tree + PRD/spec storage

RE-CARVED 2026-06-11 along the judgment/facts seam (deterministic-first: model for judgment, code for facts). This task is now clawpm-CORE ONLY — the deterministic emission API + artifact storage that the clawpm-planner SKILL (separate task) calls to persist a plan. The model-heavy judgment (recon/ideate/PRD/vet/decompose) lives in the skill, NOT here. clawpm core makes NO LLM calls for planning.

EXPLICIT NON-GOAL (operator directive): clawpm core must NOT become an audit/code-improvement tool, and must NOT couple the CLI to a model/provider for ideation. Core stays a deterministic sink any planner (improve-style, BMAD-style, custom) can emit into.

WHAT THIS DELIVERS — compose the EXISTING primitives into a clean one-shot emission surface so a skill can persist a fully-contracted task-tree atomically:
- `tasks decompose` (CLAWP-037) already records parent->child subtasks, rollup-gated, calibration-captured — the sink.
- emit-rubric (CLAWP-016/017) = per-leaf success contract.
- per-leaf contract fields: scope/stop_conditions/delegability (CLAWP-054).
- candidate match against the won't-do ledger (CLAWP-053) and validate against the project constitution (CLAWP-057), applied at emission time.
- baseline-stamp each emitted task (CLAWP-055).
The gap is that these are separate calls today; a planner emitting a 20-leaf tree needs ONE transactional operation that takes a fully-specified tree (each leaf: title, rubric, scope, stop_conditions, delegability, predictions) and persists it via decompose+emit-rubric, reject-matched + constitution-checked + baseline-stamped, all-or-nothing.

PRD/SPEC STORAGE: the planner skill DRAFTS a PRD/spec (judgment); core STORES it (facts) as a clawpm research/spec entry or mission attachment, LINKED to the emitted task-tree, so the cheap executor reads it durably. Add/confirm that storage+link surface.

DIRECTION candidates (separate from decomposition leaves) are stored as research entries, not interleaved into the tree (see the planner skill task).

OUT OF SCOPE (now in the planner SKILL task): recon, ideation/brainstorm, PRD authoring, vetting judgment, vertical-slice decomposition, scale-adaptive depth, personas, model-tier selection. This task does not know how the tree was produced — only how to persist it correctly.

DEPENDS on the contract/ledger/constitution primitives (053/054/055/057) being in place so the emission API has fields to write.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

