# Stage playbook — per-stage purpose, tools, IO, optionality

All stages are **optional and composable** (stages, not gates). The model selects
the stages the objective warrants and may re-enter any stage. The scale dial
(`scale-dial.md`) governs which run and how deep. Stages 5 (decompose) and 7
(emit) are the only ones never fully skipped — they are the deliverable.

---

## 1. constitution

- **Purpose:** load the project's named invariants (CLAWP-057) so they constrain
  every leaf. Examples: `require_success_criteria` (every leaf needs a rubric),
  `max_complexity` (no leaf above `l`), `require_scope`.
- **Tool:** `clawpm constitution list` (auto-detects project from cwd).
- **In → Out:** project context → invariant checklist applied at vet + emit.
- **Optional when:** no constitution declared — the stage runs identically and
  yields an empty set. Core's constitution gate is a graceful no-op then.
- **Note:** the skill should *pre-check* candidate leaves against the invariants
  during vet, so emit's report-back has nothing to flag. A `require_success_criteria`
  invariant on a rubric-less leaf is reported back by core (proven in
  `examples/README.md` §5).

## 2. recon

- **Purpose:** orient on the ground before deciding anything. Map structure,
  blast-radius, hotspots, what already exists.
- **Tools:** the **graph** (codegraph default / graphify for mixed corpora) for
  structural facts; parallel **Explore** subagents for semantic judgment; `Read`
  for specific sites. See `graph-consultation.md`.
- **In → Out:** objective + ground (codebase OR brief/research/notes) →
  orientation notes (god-objects, layering, fan-in, fragile areas, prior art).
- **Optional when:** a trivial objective with self-evident ground (s-scale runs a
  light pass only).
- **Project-agnostic:** for knowledge-work the "ground" is the brief, existing
  research entries (`clawpm research list`), and notes — not a codebase.

## 3. ideate

- **Purpose:** diverge before converging. Generate multiple **approaches** (how to
  achieve the objective) AND **direction candidates** (alternative objectives /
  adjacent opportunities surfaced during recon).
- **Tools:** model judgment; web search where the objective needs external facts;
  `clawpm research add` to file direction candidates.
- **In → Out:** orientation → ranked approaches + a direction-candidate set.
- **Hard rule:** **direction candidates become `clawpm research` entries, never
  leaves.** Only the chosen approach crosses into specify/decompose. This is what
  stops the tree filling with speculative "maybe also do X" work.
- **Elicitation:** present approaches **propose-then-review** so the operator's
  thinking is drawn out before a PRD is committed.
- **Optional when:** s-scale objective with one obvious approach.

## 4. specify

- **Purpose:** draft the PRD/spec — the durable "what + why" a cheap executor
  reads in isolation.
- **Tool:** `references/plan-template.md`.
- **In → Out:** chosen approach → PRD artifact, emitted as the `prd` block of the
  emit-tree document (stored as a research entry, linked to the root).
- **Optional when:** s-scale, or the operator already supplied a spec.

## 5. decompose

- **Purpose:** the heart. Produce **vertical slices** — each leaf independently
  verifiable end-to-end — and draft-contract each (rubric + scope/out_of_scope/
  stop/delegability + predictions). See `decompose-vet.md`.
- **Tools:** model judgment; **graph `impact`/`trace`** for blast-radius that
  grounds per-leaf effort/risk.
- **In → Out:** PRD → candidate leaf tree.
- **Depth:** scale-adaptive (`scale-dial.md`) — flat for s, multi-level (via
  layered `attach_to` emits) for l/xl.
- **Never fully skipped** — it IS the deliverable.

## 6. vet — the no-slop gate

- **Purpose:** reject slop before it reaches the store. For each candidate leaf:
  1. **Re-read the cited ground** to confirm the leaf is real, not mis-attributed.
  2. **Reject by-design behaviour and duplicates.**
  3. **Diff against the won't-do ledger** — `clawpm tasks list --state rejected`
     — including **fuzzy/resembling** matches, and remind the operator *why* the
     near-duplicate was rejected.
  4. **Settle structural claims with the graph** deterministically ("dead code" =
     callers 0; "duplicated in N places" = exact count) — never a vibe.
  5. **Check each leaf's rubric ladders up to the PRD's success definition** — a
     leaf whose criteria don't serve the objective is noise; cut it.
- **Tools:** graph (`callers`/`impact`); `Read`; `clawpm tasks list --state rejected`.
- **In → Out:** candidate tree → vetted tree (+ rejections noted).
- **Never skipped above s-scale.**

## 7. emit

- **Purpose:** one transactional persist of the fully-contracted tree + PRD.
- **Tool:** `clawpm tasks emit-tree` (stdin JSON). See `emission-contract.md`.
- **In → Out:** vetted tree → emitted clawpm task-tree (parent + leaves + PRD).
- **Always dry-run first** (`--dry-run`) to confirm gates pass, then emit for real.
- **Never skipped** — it's the whole point.

## 8. handoff

- **Purpose:** dispatch delegable leaves to the cheap executor; bounce human
  leaves to the operator.
- **Tool:** `clawpm tasks dispatch <id>` (worktree/separate-process mode) or
  `success_criteria` + `subagent-judge` (in-harness mode — see the `clawpm`
  skill's "Two dispatch modes").
- **In → Out:** emitted tree → in-flight dispatches / operator queue.
- **Optional and DEFERRED:** **emit then PAUSE.** Do not auto-dispatch. Run
  handoff only when the operator says "and run them". `human`-delegability leaves
  never auto-dispatch.
