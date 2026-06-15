# Emission contract — the exact `clawpm tasks emit-tree` document

This is the **skill→core contract**. The skill builds this JSON and pipes it to
`clawpm tasks emit-tree` on stdin. Core (`src/clawpm/emit_tree.py`) validates it
**fail-closed** — an unknown key is a hard error, not a silent drop — so build it
exactly. Schema is `schema_version: 1`.

```bash
cat tree.json | clawpm tasks emit-tree --dry-run   # phases 1–2 only: validate + report, no writes
cat tree.json | clawpm tasks emit-tree             # + phases 3–4: stage + atomic promote
cat tree.json | clawpm tasks emit-tree --strict    # hard-fail on reject/constitution instead of report-back
```

`--project` is auto-detected from cwd; the `project` key in the document is
advisory. The project must be discoverable through the portfolio (`project_roots`
in `~/clawpm/portfolio.toml`) — a `.project/` dir outside those roots will fail
`No tasks directory for project`.

## Top-level keys (ONLY these — unknown keys are rejected)

`schema_version` · `project` · `root` · `prd` · `leaves`

```jsonc
{
  "schema_version": 1,                  // REQUIRED, must equal 1
  "project": "my-project",              // advisory; cwd auto-detect wins
  "root": { ... },                      // REQUIRED — see below
  "prd": { ... },                       // OPTIONAL — present for m/l/xl
  "leaves": [ ... ]                     // REQUIRED — non-empty list
}
```

## `root` — exactly one of `attach_to` XOR `title`

```jsonc
"root": {
  "title": "New root task",             // creates a NEW root task
  "predictions": { ... }                // OPTIONAL root-level predictions
}
// — OR —
"root": { "attach_to": "PLANN-001" }    // attach leaves UNDER an existing task id
```

- **New root** (`title`): mints a fresh parent task each call. Use for the
  **first** emit of an objective.
- **Attach** (`attach_to`): adds leaves under an existing task. Use for **every
  re-emit / top-up** — this is what makes the skill idempotent (see below).
- Supplying both, or neither, is a validation error.

## `prd` — optional spec/research artifact, linked to the tree root

```jsonc
"prd": {
  "title": "PRD: <objective>",          // REQUIRED
  "type": "spike",                      // one of: investigation | spike | decision | reference
  "tags": ["prd", "planner"],           // list of strings
  "body_markdown": "## Objective\n..."  // the plan-template.md body
}
```

Stored as a research entry under `.project/research/`, frontmatter
`linked_task_tree: <root-id>`, and `prd_ref` stamped on the root task. This is the
durable anchor a cheap executor reads. Use `references/plan-template.md` for the
body.

## `leaves[]` — the vertical slices (ONLY these keys per leaf)

`ref` · `parent_ref` · `title` · `success_criteria` · `scope` · `out_of_scope` ·
`stop_conditions` · `delegability` · `predictions` · `agent_profile` ·
`parallel_group` · `leaf_key`

```jsonc
{
  "ref": "L1",                          // REQUIRED, unique within the doc
  "parent_ref": null,                   // MUST be null in v1 (see nesting note)
  "leaf_key": "autosave::debounced-save", // stable idempotency key — SUPPLY ONE
  "title": "User edits autosave without pressing save",  // REQUIRED
  "success_criteria": [                 // the rubric (CLAWP-016/017)
    {
      "criterion": "Edits persist after 2s idle with no manual save",
      "gradeable_signal": "integration test: type, wait 2s, assert draft PUT fired",
      "comparator": "eq:1 draft row"
    }
  ],
  "scope": ["src/editor/autosave/**"],  // file globs (code) OR named deliverables (knowledge-work)
  "out_of_scope": ["src/sync/**"],      // CLAWP-054
  "stop_conditions": ["If the PUT endpoint is not idempotent, STOP and report."], // CLAWP-054
  "delegability": "agent",              // agent | human | either   (default "either")
  "predictions": {                      // clawpm Predictions schema (all optional)
    "duration_min": 120,
    "complexity": "m",                  // s | m | l | xl
    "confidence": 3,                    // 1–5
    "approach": "Debounce hook around the change event ...",
    "pre_mortem": "Most likely failure: debounce races with explicit save.",
    "reference_tasks": [],
    "filled_by": "agent"
  },
  "agent_profile": "frontend",          // OPTIONAL routing hint for dispatch
  "parallel_group": null                // OPTIONAL int — siblings sharing it run together
}
```

### Field rules that bite

- **`success_criteria`** — each item is either a bare string *or* a
  `{criterion, gradeable_signal, comparator}` object. Prefer the structured form
  so the Stop-hook judge can grade it. A leaf with no rubric trips a
  `require_success_criteria` constitution invariant (report-back).
- **`scope` is project-agnostic.** For software: file globs. For knowledge-work:
  **named deliverables** (`"deliverable: comparison-matrix.md"`). Both are just
  strings to core.
- **`delegability`** drives handoff: `agent`/`either` can dispatch to a cheap
  executor; `human` bounces to the operator and never auto-dispatches.
- **`leaf_key`** — **always supply one.** It is the idempotency unit. Make it
  stable across re-runs of the same objective (e.g. `<objective-slug>::<slice>`).
  If omitted, core falls back to `ref`, which is far less stable.
- **`predictions`** — embed `approach`, `pre_mortem`, `confidence`,
  `duration_min`, `complexity` here. When no graph grounded the effort, say so in
  `approach` ("UNGROUNDED — no graph consulted").

### v1 nesting note (fail-closed)

`parent_ref` **must be null**. In-document hierarchical nesting is **not
supported in v1** — a non-null `parent_ref` is a hard validation error (tracked
as CLAWP-064). To build depth (milestone → slice → unit), emit in **layers**:

1. Emit the milestone parents as a new-root tree (or attach under an objective root).
2. Then, for each milestone, emit its child slices with `root.attach_to: <milestone-id>`.

Each `attach_to` call adds one level. This is how l/xl multi-level trees are built.

## What core returns

```jsonc
{
  "root_id": "PLANN-001",
  "emitted": [ { task dicts } ],
  "research_id": "planner-demo-research-prd-...",   // null if no PRD
  "baseline_ref": "ts:2026-06-12T...",              // stamped on every task
  "rejected": [ { "leaf_ref", "leaf_title", "matched_rejected_id", "rationale" } ],
  "constitution_violations": [ { "invariant", "leaf_ref", "reason" } ],
  "dry_run": false
}
```

Surface `rejected` and `constitution_violations` to the operator. For
constitution violations, loop back to decompose/vet and re-emit. Advisory
invariants are reported but never block.

## Idempotency — the re-emit pattern

Core dedups by scanning `leaf_key`s **under the resolved parent**. Therefore:

- ✅ **Re-emit / top-up via `root.attach_to: <root-id>`.** Leaves whose
  `leaf_key` already exists under that root are silently skipped; new leaves emit.
  Rejected-ledger `leaf_key`s are also not re-emitted.
- ❌ **Do NOT re-run a new-root document.** Each `title` root mints a *fresh*
  parent (PLANN-001, then PLANN-002), so children never dedup → the tree
  duplicates. Verified in `examples/README.md` §4.

The handoff stage records the emitted `root_id`; any subsequent planning pass on
the same objective attaches to it.

## Gate behaviour summary

| Gate | When | Effect (default report-back) | `--strict` |
|---|---|---|---|
| Reject-match (CLAWP-053) | leaf title == a rejected task title (case-insensitive) | leaf dropped, surfaced in `rejected[]` | aborts |
| Constitution (CLAWP-057) | leaf violates a non-advisory invariant | surfaced in `constitution_violations[]`, still emits | aborts |
| ID collision | predicted id already on disk | **always** aborts emission | aborts |
| Idempotency | `leaf_key` already under parent | leaf silently skipped | skipped |
| Baseline (CLAWP-055) | always | stamps `baseline_ref` on every task | stamps |
