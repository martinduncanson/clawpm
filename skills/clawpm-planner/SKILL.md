---
name: clawpm-planner
description: >
  Turn a free-text OBJECTIVE (software OR knowledge-work) into a vetted,
  scale-appropriate clawpm task-tree and EMIT it via `clawpm tasks emit-tree`.
  Runs the planning judgment in-harness on a capable model: constitution →
  recon (graph-grounded) → divergent ideation → PRD/spec → vertical-slice
  decomposition → vetting against the won't-do ledger + constitution →
  transactional emission with per-leaf rubric/scope/stop/delegability/
  predictions/baseline. Direction candidates are filed as research entries,
  not leaves. Delegable leaves hand off to a cheaper executor via clawpm
  dispatch. Triggers: "plan this objective", "break this goal into tasks",
  "decompose <objective>", "turn this brief/PRD into a clawpm tree", "what's
  the plan for <goal>". NOT a code auditor; NOT for a single already-well-formed
  task (use `clawpm tasks add`).
license: MIT
user-invocable: true
metadata: { "openclaw": { "homepage": "https://github.com/martinduncanson/clawpm", "upstream": "https://github.com/malphas-gh/clawpm", "requires": { "bins": ["clawpm"] }, "emoji": "🗺️" } }
---

# clawpm-planner

Plan a free-text objective into a clawpm task-tree, vet it for slop, and **emit
it through the deterministic core** (`clawpm tasks emit-tree`). The skill is the
**only** place model judgment lives; core makes **zero LLM calls** — it receives
a fully-contracted tree and persists it atomically.

**The seam: the skill decides, core persists.** Every judgment — what to build,
how to slice, who executes, effort/risk — is made here. Core only writes facts.

## When to use

Use when an **objective needs planning** — it spans multiple steps, the slicing
isn't obvious, or it will resume across sessions. Works for **software**
(objective + codebase) *and* **knowledge-work** (objective + brief / research /
notes). Not a code auditor — it plans toward a goal, it doesn't hunt for bugs.

**Skip** (route to `clawpm tasks add`): a single already-well-formed task; pure
Q&A; "just add a task".

## Stage flow — stages, not gates

All stages are **optional and composable**. The model selects the stages the
objective warrants and may re-enter any stage. The **scale dial** (s/m/l/xl)
governs how many run and how deep decompose goes — see
`references/scale-dial.md`. Full per-stage detail in
`references/stage-playbook.md`.

| # | Stage | Does | Key tool |
|---|-------|------|----------|
| 1 | **constitution** | Load project invariants that constrain every leaf | `clawpm constitution list` |
| 2 | **recon** | Orient on the ground; map structure/blast-radius | **graph** (codegraph/graphify) + parallel **Explore** |
| 3 | **ideate** | Diverge: approaches **and** direction candidates | model judgment; web; `clawpm research` |
| 4 | **specify** | Draft the PRD/spec (what + why) | `references/plan-template.md` |
| 5 | **decompose** | Vertical-slice leaves, each draft-contracted | model + graph `impact`/`trace` |
| 6 | **vet** | Re-read ground; reject dup/by-design; diff vs won't-do ledger | graph + `clawpm tasks list --state rejected` |
| 7 | **emit** | One transactional persist of the contracted tree + PRD | **`clawpm tasks emit-tree`** (stdin JSON) |
| 8 | **handoff** | Dispatch delegable leaves; bounce human leaves | `clawpm tasks dispatch` |

Scale shortcut: **s** = recon(light) → decompose → emit (1–2 flat leaves, no PRD).
**xl** = full pipeline + opt-in personas + explicit direction-candidate review.
Default one tier **lower** when unsure — over-planning a small objective is the
regression to avoid.

## Hard rules

1. **Leaves are vertical slices.** Each leaf is independently verifiable
   end-to-end (a user-visible or deliverable-visible guarantee), never a
   horizontal layer. "Write the DB schema" is wrong; "user can save a draft,
   persisted and retrievable" is right. Each carries a **rubric**
   (`success_criteria`) plus `scope` / `out_of_scope` / `stop_conditions` /
   `delegability`. See `references/decompose-vet.md`.

2. **Direction candidates are research, not leaves.** Alternative objectives /
   adjacent opportunities surfaced in ideate go to `clawpm research add` —
   **never** interleaved as leaves. Only the *chosen approach* crosses into
   decompose.

3. **Graph = facts, fan-out = judgment.** In recon/decompose, consult a graph
   (codegraph default; graphify for mixed corpora) for blast-radius and
   structural claims. If **neither is available**, **surface the gap and propose
   remediation** (`codegraph init -i` / install graphify) and **tag every
   effort/risk estimate UNGROUNDED** — never present a vibe number as grounded.
   See `references/graph-consultation.md`.

4. **Vet is the no-slop gate.** Before emit: re-read the cited ground, reject
   by-design behaviour and duplicates, and **diff candidates against the won't-do
   ledger** (`clawpm tasks list --state rejected`) including fuzzy matches —
   reminding the operator *why* a near-duplicate was rejected.

5. **Emit then PAUSE.** The emit stage runs **one** `clawpm tasks emit-tree`
   call and stops. Do **not** auto-dispatch. Hand off only when the operator says
   "and run them" (handoff stage).

6. **Idempotent re-emit via `attach_to`.** Each leaf carries a stable `leaf_key`.
   To add to / re-run a tree, re-emit with `root.attach_to: <root-id>` — core
   skips leaves whose `leaf_key` already exists under that parent. A fresh
   *new-root* emit does **not** dedup (it mints a new parent), so never re-run a
   new-root document to "top up" a tree.

## The emit call

The emit stage builds one JSON document and pipes it to the CLI on stdin:

```bash
cat tree.json | clawpm tasks emit-tree --dry-run    # validate all gates, no writes
cat tree.json | clawpm tasks emit-tree              # promote atomically
```

Core validates (schema, reject-match, constitution, ID-collision, baseline),
then stages and promotes the whole subtree all-or-nothing. It returns
`{ emitted, rejected, constitution_violations, research_id, root_id,
baseline_ref }`. Surface `rejected` and `constitution_violations` to the operator;
for constitution violations, loop back to decompose/vet.

**The exact document schema** the skill must build is in
`references/emission-contract.md`. Worked, CLI-validated examples (software +
knowledge-work) are in `examples/` — read them before constructing a tree.

## Model tiering

This skill runs on a **capable** model (the planning judgment). Emitted
`agent`/`either` leaves are **dispatchable to a cheaper executor** (sonnet/haiku)
via `clawpm tasks dispatch`, gated by clawpm's existing Stop-hook judge +
delegability gate. `human` leaves never auto-dispatch — they bounce to the
operator. Capable model plans; cheap model executes under the rubric.

## Personas (opt-in, off by default)

On **l/xl** objectives or when the operator asks ("plan this as a PM"), the model
may adopt composable elicitation lenses (analyst / PM / architect) during
ideate/specify. They are prompt lenses, **not** extra subagents or stages. See
`references/personas.md`. On s/m they stay off.
