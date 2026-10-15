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

**The seam: the skill decides, core persists.** Every judgment (what, how to
slice, who executes, effort/risk) is made here. Core only writes facts.

## When to use

Use when an **objective needs planning** — multiple steps, non-obvious slicing, or
cross-session. Works for **software** *and* **knowledge-work** (brief / research /
notes). Not a code auditor — it plans toward a goal, it doesn't hunt for bugs.

**Skip** (use `clawpm tasks add`): a single well-formed task; pure Q&A.

## Stage flow — stages, not gates

All stages are **optional and composable**; select what the objective warrants and
re-enter freely. The **scale dial** (s/m/l/xl) governs how many run and how deep
decompose goes — `references/scale-dial.md`.

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
**xl** = full pipeline + opt-in personas + direction-candidate review. Default one
tier **lower** when unsure. **Chart mode** (hard rule 7) sits between m and l.

`references/stage-playbook.md`: recon fan-out (`feature-dev:code-explorer`),
specify interview discipline, l/xl approach lenses (minimal / clean / pragmatic).

## Hard rules

1. **Leaves are vertical slices.** Each leaf is independently verifiable
   end-to-end, never a horizontal layer ("user can save a draft", not "write the
   DB schema"). Each carries a **rubric** (`success_criteria`, WHEN/THEN phrased)
   plus `scope` / `out_of_scope` / `stop_conditions` (seed the default stop-list) /
   `delegability`. See `references/decompose-vet.md`.

2. **Direction candidates are research, not leaves.** Alternative objectives /
   adjacent opportunities surfaced in ideate go to `clawpm research add` —
   **never** interleaved as leaves. Only the *chosen approach* crosses into
   decompose.

3. **Graph = facts, fan-out = judgment.** In recon/decompose, consult a graph
   (codegraph default; graphify for mixed corpora) for blast-radius and structural
   claims. If **neither is available**, surface the gap, propose remediation
   (`codegraph init -i`), and **tag every estimate UNGROUNDED**. See
   `references/graph-consultation.md`.

4. **Vet is the no-slop gate.** Before emit: re-read the cited ground, reject
   by-design behaviour and duplicates, and **diff against the won't-do ledger**
   (`clawpm tasks list --state rejected`), fuzzy matches included; say *why* a
   near-duplicate was rejected.

5. **Emit then PAUSE.** The emit stage runs **one** `clawpm tasks emit-tree`
   call and stops. Do **not** auto-dispatch. Hand off only when the operator says
   "and run them" (handoff stage).

6. **Idempotent re-emit via `attach_to`.** Each leaf carries a stable `leaf_key`.
   To top up a tree, re-emit with `root.attach_to: <root-id>` — core skips leaves
   whose `leaf_key` exists under that parent. A *new-root* emit mints a new parent
   and does **not** dedup, so never re-run one to "top up".

7. **Chart when decisions block (rule, not option).** Trigger: after ideate, **≥2
   open decisions block decompose**, or the operator says "chart this". Then:
   specify the *destination only* (interview discipline: `stage-playbook.md`),
   and emit **one decision map** — new root with `destination` + `not_yet_specified`
   (the fog), `kind: decision` leaves (`human` = operator answers, `agent` =
   research), build leaves with `depends_refs` on the decisions they wait on —
   then **STOP**: no build decomposition, no dispatch. Fog test: name a question
   only if you can state it precisely now. **No-fog exit:** if charting finds <2
   open decisions, say so and offer plain decompose. s never charts. Procedure and
   shape: `references/chart-mode.md`; copy `examples/decision-map.emit.json`.

## The emit call

The emit stage builds one JSON document and pipes it to the CLI on stdin:

```bash
cat tree.json | clawpm tasks emit-tree --dry-run    # validate all gates, no writes
cat tree.json | clawpm tasks emit-tree              # promote atomically
```

Core validates (schema, reject-match, constitution, ID-collision, baseline), then
promotes the whole subtree all-or-nothing and returns `{ emitted, rejected,
constitution_violations, research_id, root_id, baseline_ref }`. Surface `rejected`
and `constitution_violations`; for violations, loop back to decompose/vet.

Exact schema: `references/emission-contract.md`. CLI-validated examples are in
`examples/` — read them before constructing a tree.

## Model tiering

A **capable** model plans. Emitted `agent`/`either` leaves dispatch to a cheaper
executor via `clawpm tasks dispatch`, gated by the Stop-hook judge + delegability
gate. `human` leaves bounce to the operator and never auto-dispatch.

## Personas (opt-in, off by default)

On **l/xl** or on request, adopt analyst / PM / architect prompt lenses in
ideate/specify (not subagents or stages): `references/personas.md`. Off at s/m.
