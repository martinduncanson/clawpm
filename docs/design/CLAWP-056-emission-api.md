# CLAWP-056 — clawpm-core emission API (SPEC / ADR)

| | |
|---|---|
| Status | **Proposed** — design gate, pre-build |
| Task | CLAWP-056 |
| Author | agent (design) |
| Date | 2026-06-11 |
| Depends on (assumed-extant) | CLAWP-054 (per-leaf contract fields), CLAWP-053 (won't-do ledger), CLAWP-057 (project constitution), CLAWP-055 (baseline-stamp) |
| Composes (extant) | CLAWP-037 `tasks decompose` / `add_subtask`, CLAWP-016/017 `emit-rubric` / `render_rubric_markdown`, `research.add_research`, `worklog.add_entry`, `concurrency.append_jsonl_line` |

---

## 1. Context and the seam

clawpm is gaining a goal→task-tree planning capability split along a **judgment / facts** seam:

- A **planner skill** (CLAWP-059, model-heavy) does recon, ideation, PRD drafting, and decomposition. It produces a fully-specified task-tree and a PRD/spec document. **All LLM work lives here.**
- **clawpm CORE** (this task) is a deterministic sink. It accepts the planner's output and persists it. **Zero LLM calls in this path.** It is "persist exactly what you are given, atomically, or persist nothing."

CLAWP-056 delivers the single CORE operation that ingests a planner-produced tree and writes it into clawpm's filesystem-first store by composing primitives that already exist (or are landing in sibling tasks). It introduces **no new storage architecture** — it is an orchestration layer over `add_subtask`, `emit-rubric`, `add_research`, and the validation hooks from 053/057.

### Why this fits clawpm's existing shape

Every clawpm write today is: parse → validate → `tmp.write_text(...)` → `tmp.replace(target)` (atomic rename); JSONL appends route through `concurrency.append_jsonl_line` under an exclusive lock (CLAWP-032). `tasks decompose` already accepts **per-child JSON** carrying `title` + `success_criteria` + `complexity` + `agent_profile` (`cli.py:1820`). The emission API is the same idea scaled from a flat child-list to a **multi-level tree with richer per-leaf contracts and emission-time validation gates** — and crucially, made **all-or-nothing**.

---

## 2. The emission operation

### 2.1 CLI surface — `clawpm tasks emit-tree`

**Recommendation: a new sibling command `tasks emit-tree`, NOT an extension of `decompose`.**

`decompose` is a thin, repeatable, human-typeable command (`--child` strings on argv). Stretching it to carry scope/stop_conditions/delegality/baseline/nested-children per leaf, plus a PRD blob, plus rollback semantics, would overload a command whose entire value is being terse. `emit-tree` is a **machine-to-machine** sink: one JSON document in, one structured result out. Keeping them separate keeps `decompose` ergonomic for hand use and lets `emit-tree` own the heavier atomic contract. Internally both call `add_subtask`, so the calibration/rollup behaviour is identical.

```
clawpm tasks emit-tree [--project ID] [--dry-run] [--format json] < tree.json
```

- **Input: a single JSON document on stdin.** Justification: clawpm is JSON-first (`--format json` is the default envelope; `decompose`/`add` already accept JSON via argv/`--stdin`). A task-tree is a nested structure with arbitrarily many leaves and multi-line PRD text — argv flags cannot express it without escaping hell, and a `--tree-file` path is strictly weaker than stdin (stdin accepts both a file redirect and a pipe from the planner skill, and avoids a temp-file lifecycle). Runner-up: `--tree-file PATH`. Rejected as the *primary* surface because the planner emits in-process; chosen as a convenience alias.
- `--dry-run`: run **all** validation + collision + constitution + reject-match checks and report what *would* be written, writing nothing. This is the planner's pre-flight. (See §5 — dry-run is also where the zero-LLM property is cheapest to assert.)
- Output: the standard `output_success` / `output_error` envelope, `data` carrying `{root_id, emitted: [...task dicts...], research_id, baseline_ref, rejected: [...], constitution_violations: [...]}`.

### 2.2 Tree schema (stdin document)

```jsonc
{
  "schema_version": 1,
  "project": "clawpm",                  // optional; --project overrides; else auto-detect
  "root": {
    "attach_to": "CLAWP-058",           // EITHER attach children under an existing task…
    // "title": "...", "predictions": {...}   // …OR create a new root task (exactly one of the two)
  },
  "prd": {                              // optional but recommended
    "title": "Goal X — PRD",
    "type": "spike",                    // ResearchType: investigation|spike|decision|reference
    "tags": ["prd", "plan"],
    "body_markdown": "## Problem\n…\n## Spec\n…"
  },
  "leaves": [ <Leaf>, … ]               // ≥1; order preserved; tree via parent_ref
}
```

```jsonc
// <Leaf>
{
  "ref": "L1",                          // tree-local handle, unique within the document
  "parent_ref": null,                   // null = child of root; else another leaf's ref → nesting
  "title": "Migrate auth to JWT",
  "success_criteria": [                 // → emit-rubric contract (CLAWP-016/017)
    {"criterion": "P95 <200ms", "gradeable_signal": "bench output", "comparator": "lt:200ms"}
  ],
  "scope": ["src/auth/**"],             // CLAWP-054 contract field
  "stop_conditions": ["tests green", "no new P95 regression"],  // CLAWP-054
  "delegability": "agent",              // CLAWP-054: agent|human|either
  "predictions": {                      // Predictions.from_dict shape (duration_min, confidence, …)
    "duration_min": 240, "complexity": "m", "confidence": 3,
    "approach": "drop-in JWT middleware", "pre_mortem": "mobile webview cookie edge case",
    "reference_tasks": ["CLAWP-042"]
  },
  "agent_profile": "backend",
  "parallel_group": 1                   // optional, CLAWP-021 batch ordinal
}
```

`schema_version` is mandatory and validated up front so the planner and CORE can evolve independently. Unknown top-level keys → hard reject (fail-closed; a typo'd `succes_criteria` must not silently drop a contract).

---

## 3. Atomicity / rollback on a filesystem-first store

This is the load-bearing design decision. A task-tree is **many files** (`PARENT/_task.md`, N× `PARENT-NNN.md`, the parent's `children:` frontmatter mutation, one research file, work_log appends). clawpm's per-file writes are individually atomic (`tmp.replace`) but the *aggregate* is not. We need all-or-nothing across the set.

**Recommendation: staging-directory build + atomic directory promotion, with a pre-write validation barrier.**

The emission is structured as four ordered phases. Phases 1–2 touch nothing under `.project/tasks/`; the first real mutation is phase 3.

1. **Parse + validate (pure, in-memory).** Deserialize the document; validate schema_version, leaf refs unique, parent_refs resolve, exactly-one-of `attach_to`/new-root, every comparator/criterion well-formed, predictions parse. Build the in-memory `Task`/`Research` objects. **No filesystem writes.** Any failure → exit non-zero, nothing touched.
2. **Validation barrier — all emission-time gates fire here (§4).** ID-collision pre-check, won't-do reject-match (053), constitution-check (057), baseline resolution (055). All are **read-only** against the store. If any gate fails (and isn't overridden), abort before the first write. This is the "fail before you write" guarantee — the expensive correctness checks sit at the barrier, not interleaved with mutations (mirrors the memory lesson: *place expensive verification at the terminal/irreversible transition*).
3. **Stage.** Render every task file (and the research file) to a **temp staging directory** on the **same filesystem** as `.project/tasks/` (e.g. `.project/tasks/.emit-<uuid>/`). Same-FS is required so the promotion is a rename, not a copy (CLAUDE.md `mv`-not-`cp+rm` discipline; also dodges the cross-volume copy window). Staging writes the full subtree layout: `PARENT/_task.md` with the complete `children:` list already populated, each `PARENT-NNN.md`, nested grandchildren dirs, and the research `.md`.
4. **Promote (the atomic step).** Move staged entries into place with `Path.replace` (atomic rename, same FS). Two cases:
   - **New-root tree:** the staged `PARENT/` directory is renamed wholesale into `.project/tasks/PARENT/` — **one** rename publishes the entire subtree atomically. This is the clean case and the reason new-root is preferred.
   - **`attach_to` an existing task:** the parent already exists on disk. We cannot rename a whole new dir over it. Instead: (a) if the target is a flat file, `split_task` it to a directory first (its own atomic rename); (b) rename each staged child file individually into the live parent dir; (c) **last**, atomically replace the parent `_task.md` with the version carrying the updated `children:` list. Ordering matters: children land before the parent's child-list references them, so a crash mid-promote leaves orphan child files (harmless — `parent_rollup_status` scans by `parent:` frontmatter and `list_tasks` recurses) but **never** a parent claiming a child that isn't there.

**Crash safety / cleanup.** The staging dir is named `.emit-<uuid>` and lives under `.project/tasks/`. A crash between phase 3 and completion leaves a `.emit-*` dir; `_scan_task_files` already skips dot-dirs (`item.name.startswith(".")`, `tasks.py:51`) so it is invisible to listings. `clawpm doctor` gains a sweep that removes stale `.emit-*` dirs older than N minutes. On any exception during phase 3/4, the handler `shutil.rmtree`s the staging dir in a `finally`.

**Why not a write-ahead journal / two-phase commit?** Overkill for a single-writer, no-daemon, filesystem store. The staging-dir + atomic-rename pattern gives all-or-nothing for the new-root case for free and a tightly-ordered, crash-safe partial for the attach case, with zero new infrastructure. **Runner-up: per-file write with a compensating-delete rollback list** (write each file, on failure delete what we wrote). Rejected: the rollback path is itself non-atomic and racy, and a crash *during* rollback leaves the store inconsistent — the exact failure shape staging avoids.

**JSONL side-effects (work_log) are appended LAST,** after promotion succeeds, via `worklog.add_entry` (locked append). They are intentionally outside the atomic set: a log line is advisory, append-only, and idempotent-enough; emitting it only on success means the log never claims a tree that didn't land.

---

## 4. Where each primitive composes and where each check fires

| Primitive | Role | Fires at | Mechanism |
|---|---|---|---|
| **ID-collision pre-check** | tree IDs don't clash with existing tasks | Phase 2 (barrier, read-only) | reuse `add_subtask`'s union-scan logic (parent dir glob + done/ + blocked/ + persisted `children:`) to *predict* every `PARENT-NNN` id before staging, so two leaves can't collide and the tree can't collide with a migrated child. |
| **won't-do reject-match (053)** | candidate leaves matched against the ledger | Phase 2, **before any write** | call `reject_ledger.match(project, leaf.title, leaf.scope)` per leaf. Hard matches → abort with `rejected: [...]` unless `--allow-rejected` (logged override). Deterministic string/scope match — **no LLM** (the planner already did the judgment; CORE only looks up). |
| **constitution-check (057)** | tree validated against project constitution | Phase 2, **before any write** | `constitution.validate(project, tree)` returns structured violations (e.g. scope outside allowed roots, missing required predictions, delegability vs constitution policy). Violations → abort with `constitution_violations: [...]` unless `--override-constitution` (logged). Rule-based, deterministic. |
| **baseline-stamp (055)** | every emitted task gets `baseline_ref` | Phase 2 resolve → Phase 3 stamp | resolve the baseline once at the barrier (`baseline.current_ref(project)` — e.g. git HEAD sha at emission time); write `baseline_ref: <ref>` into every staged task's frontmatter during rendering. One ref for the whole tree (the planning baseline), so the tree is internally consistent. |
| **`emit-rubric` (016/017)** | per-leaf success contract | Phase 3 (rendering) + post-emit echo | each leaf's `success_criteria` is written as `predictions.success_criteria` (the existing rubric source — `render_rubric_markdown` reads exactly that). No separate rubric file is written; the rubric is *derived* on demand by `tasks emit-rubric <leaf>`. `emit-tree --dry-run` can echo each leaf's rendered rubric for planner review. |
| **`add_subtask` / `split_task` (037)** | the actual subtask sink + rollup gating | Phase 3/4 | staging renders the same frontmatter `add_subtask` would; promotion reuses `split_task` for the attach case. Rollup gating (`parent_rollup_status`) is inherited unchanged — parent can't be `done` until leaves are `done`. |

**Validation barrier ordering** (cheapest/most-likely-to-fail first): schema → ID-collision → reject-match → constitution → baseline-resolve. All read-only, all before phase 3.

---

## 5. PRD / spec storage and linking

**Recommendation: store the PRD as a `research` entry, linked to the root task by a bidirectional frontmatter reference.**

- **Why research, not mission.** A `mission` is a *macro binary-outcome decomposition layer* (4–10 mini-goals, multi-week, deadline, YES/NO outcome) — that is a *planning* concept that may sit *above* a tree, not the PRD document itself. A `research` entry is exactly "a durable markdown document with frontmatter, retrievable by id" — which is what a PRD is. `add_research` already exists, writes atomically, and supports `type` (`spike`/`decision` fit a PRD) and `tags`. Using it means zero new entity.
- **Storage.** Phase 3 stages the research `.md` alongside the tasks via `add_research`-equivalent rendering; its `body_markdown` is the PRD. (Note: `add_research` today writes directly, not via staging — the emission path renders into the staging dir and promotes, to keep the PRD inside the atomic set. The rendering logic is shared with `add_research`.)
- **Linking surface (bidirectional, frontmatter-native):**
  - On the **root task**: `prd_ref: <research-id>` in frontmatter.
  - On the **research entry**: `linked_task_tree: <root-task-id>` in frontmatter.
  - Retrieval by a downstream executor: `clawpm research show <prd_ref>` from the task, or `clawpm tasks show <root>` surfaces `prd_ref` in its dict. A thin convenience `clawpm tasks prd <task-id>` (prints the linked PRD) can follow but isn't required for CLAWP-056.
- This mirrors the existing `research.openclaw` / `mission.mini_goals` linking idiom (frontmatter cross-reference, no DB), so it fits the established pattern and is greppable/diffable.

**Open question flagged** (see §7): whether a tree should *also* be attachable to a `mission` at emission time. Recommendation: out of scope for 056; the planner can call `mission add-goal` separately.

---

## 6. The "zero LLM calls" guarantee

CORE must make **no** model calls. The guarantee is structural and test-enforced:

1. **Structural:** `emit-tree`'s implementation imports only `tasks`, `research`, `worklog`, `concurrency`, and the 053/055/057 modules — none of which import the judge/model layer. The judge lives in `clawpm/judges/` and `dispatch.py`; `emit-tree` must not import from those packages. A module-boundary lint (import-graph assertion) is the cheapest guard.
2. **Test (the real teeth):** the zero-LLM test runs `emit-tree` against a fixture tree with the model-invocation seam **monkeypatched to raise**. clawpm's model entry points are `claude -p` (subprocess) and the local Ollama fallback (per MEMORY: judge = `claude -p` primary + Ollama fallback, CLAWP-041). The test patches `subprocess.run`/`subprocess.Popen` (and any `judges.*` callable) to `raise AssertionError("LLM invoked")`, then asserts a full `emit-tree` of a multi-leaf tree completes successfully. If any code path reached for a model, the subprocess patch trips. This is deterministic and CI-runnable on Windows/Linux.
3. **Belt-and-braces:** assert `emit-tree --dry-run` and a real emit produce **byte-identical** task frontmatter for the same input (a model call would introduce nondeterminism) — a cheap nondeterminism canary.

---

## 7. Test plan → success criteria

CLAWP-056's three success criteria (verifiable form):

| # | Criterion | Gradeable signal | Tests |
|---|---|---|---|
| **SC1** | A fully-specified tree persists **atomically** — all-or-nothing on failure | after a forced mid-emit failure, `list_tasks` shows **none** of the tree; after success, **all** leaves + PRD present and linked | `test_emit_tree_new_root_atomic_success` (full tree lands, parent gated, rubric derivable, `prd_ref`/`linked_task_tree` set); `test_emit_tree_aborts_before_write_on_constitution_violation` (store unchanged); `test_emit_tree_crash_during_stage_leaves_no_partial` (inject exception in phase 3 → staging dir cleaned, `list_tasks` empty); `test_emit_tree_attach_child_before_parent_childlist` (crash between child-rename and parent-rewrite → no parent claims a missing child) |
| **SC2** | Each leaf carries its full contract (rubric + scope + stop_conditions + delegability + baseline_ref) and the tree composes the existing primitives | per-leaf frontmatter contains all CLAWP-054 fields + `baseline_ref`; `emit-rubric <leaf>` renders the criteria; parent rollup gates on leaves | `test_emit_tree_leaf_contract_roundtrip`; `test_emit_tree_rubric_derivable_per_leaf`; `test_emit_tree_parent_rollup_gated`; `test_emit_tree_reject_match_aborts` (053); `test_emit_tree_baseline_stamped_uniform` (055) |
| **SC3** | **Zero LLM calls** in the emission path | subprocess/judge seam patched to raise; emit completes | `test_emit_tree_makes_no_model_calls` (§6.2); `test_emit_import_graph_excludes_judges` (§6.1); `test_emit_tree_dryrun_matches_real_frontmatter` (§6.3) |

Fixtures: a 1-root / 3-leaf flat tree and a 1-root / 2-leaf / 1-grandchild nested tree, both with a PRD block.

---

## 8. Open questions for the operator

1. **Reject-match / constitution overrides.** Should `--allow-rejected` / `--override-constitution` exist at all, or must a planner-emitted tree that hits the won't-do ledger / violates the constitution **always** hard-fail (forcing the planner to re-plan)? Recommendation: provide the flags but log every use to the override log (consistent with install-gate/destruct-gate doctrine). **Flagged — needs your call on whether CORE may ever be told to override a constitution violation.**
2. **`attach_to` vs new-root as the common case.** new-root gives the clean single-rename atomicity; attach is messier. Is the planner's normal output a *new* root task (CORE mints it) or attaching under a *pre-existing* tracking task (e.g. the operator filed `CLAWP-058 "Goal X"` and the planner decomposes it)? If the latter is dominant, the attach promotion path is the hot path and deserves extra crash-test coverage.
3. **PRD entity choice.** I recommend `research` (type `spike`/`decision`). Confirm you don't want a *new* first-class `prd`/`plan` entity. A new entity is more semantically honest but costs a model, CLI group, doctor checks, and discovery wiring — I judge `research` the right leverage trade unless PRDs need lifecycle distinct from research.
4. **Should a tree be linkable to a `mission` at emission time?** I scoped it out (planner calls `mission add-goal` separately). Confirm.
5. **Baseline granularity (055).** One `baseline_ref` for the whole tree (planning-time snapshot) vs per-leaf at each leaf's eventual dispatch. I recommend whole-tree-at-emission for internal consistency; per-leaf-at-dispatch is a 055 concern, not 056. Confirm the emission-time stamp is the planning baseline, not the execution baseline.
6. **Partial emission.** If 1 of 20 leaves fails validation, do we abort the whole tree (current design — all-or-nothing) or emit the valid N-1 and report the failure? Recommendation: **abort whole** — partial trees are a calibration and rollup hazard, and the planner should fix and re-emit. Confirm you want strict all-or-nothing rather than best-effort.
