# Decompose & vet — vertical slices and the no-slop gate

## Vertical slices, not horizontal layers

A leaf must be an **independently verifiable end-to-end guarantee** — something a
user or a reader can observe — not an internal building block.

| ❌ Horizontal layer (wrong) | ✅ Vertical slice (right) |
|---|---|
| "Write the drafts DB schema" | "User edits autosave and survive a reload" |
| "Add the API endpoint" | "Draft saves are idempotent on retry" |
| "Build the comparison spreadsheet" | "Normalised like-for-like comparison matrix, every cell sourced" |
| "Gather the data" | "Sourced pricing dataset, every row cited to a primary source" |

The test: **can this leaf be graded on its own?** If grading it requires another
unshipped leaf, it's a layer — re-slice so each leaf delivers observable value and
its rubric resolves independently. (Dependencies between slices are fine; *partial
invisibility* is not.)

This holds for knowledge-work too: each leaf is a **self-contained deliverable
section** of the final artifact, verifiable by "can a reviewer check this section
against its rubric without the others?"

## Per-leaf contract (every leaf carries all of these)

- **`title`** — the user/deliverable-visible guarantee, phrased as an outcome.
- **`success_criteria` (rubric)** — structured `{criterion, gradeable_signal,
  comparator}`. The gradeable signal is *what evidence proves it* (a test, a
  count, a cited cell). Weak criteria ("make it work") defeat the Stop-hook judge —
  spend the calories here.
- **`scope`** — file globs (software) or named deliverables (knowledge-work).
- **`out_of_scope`** — what this leaf explicitly does NOT do (prevents drift).
- **`stop_conditions`** — "if X is false, STOP and report" — the executor's
  circuit-breaker. Especially: a precondition another leaf must satisfy first.
- **`delegability`** — `agent` (cheap executor), `human` (judgment/approval), or
  `either`. Drives handoff routing.
- **`predictions`** — `duration_min`, `complexity` (s/m/l/xl), `confidence` (1–5),
  `approach`, `pre_mortem`. Effort grounded by graph blast-radius where available;
  tagged UNGROUNDED in `approach` where not.
- **`leaf_key`** — stable idempotency key, `<objective-slug>::<slice>`.
- **`traces_to` (in the title/notes)** — a "traces to: \<objective\> / \<milestone\>"
  line so an executor reading one leaf in isolation still sees the why.

## Big-picture traceability (three layers)

1. **Structural** — the parent chain (root → milestone → slice via layered
   `attach_to`).
2. **Documentary** — the PRD link (`prd_ref` on the root, `linked_task_tree` on the
   research entry) + the per-leaf "traces to" line.
3. **Semantic** — vet rejects any leaf whose rubric doesn't ladder up to the PRD's
   success definition.

## The vet checklist (run before emit)

For **each** candidate leaf:

1. **Re-read the cited ground.** Confirm the leaf describes something real and
   correctly attributed — not a hallucinated file/function/fact. (This is the
   improve no-slop discipline: a planner that cites ground it never read is the
   failure mode.)
2. **Reject by-design + duplicates.** Is this already handled deliberately
   elsewhere? Is it a restatement of another leaf?
3. **Diff against the won't-do ledger.** `clawpm tasks list --state rejected`.
   Match **fuzzy/resembling**, not just exact — a near-duplicate of an
   already-rejected idea should be caught *here*, and the operator reminded
   **why** (cite the rejected task's rationale). Core does a final **exact**
   case-insensitive match as a backstop and reports it in `rejected[]`
   (see `examples/README.md` §6) — but the *fuzzy* judgment is the skill's job.
4. **Settle structural claims with the graph.** "Dead code" → `codegraph_callers`
   == 0. "Duplicated in N places" → exact count. Never assert topology from vibe.
   Carry the reachability caveat into the leaf (graphs miss dynamic dispatch — see
   `graph-consultation.md`).
5. **Ladder check.** Does the leaf's rubric serve the PRD success definition? If
   not, cut it.

Rejected candidates: either drop them, or if they're genuine "won't-do" decisions,
file them so the ledger catches them next time (`clawpm tasks add` then
`clawpm tasks state <id> rejected --rationale "..."`).

## Per-node expansion (task-master pattern)

For a leaf that is itself large, attach a **per-node expansion prompt** in its
notes — "to expand this slice, decompose into: …" — and a recommended subtask
count. At l/xl this becomes the next `attach_to` layer (slice → units). At s/m a
leaf is atomic (expansion count 0).
