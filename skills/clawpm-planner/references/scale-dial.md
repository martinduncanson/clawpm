# Scale-adaptive depth dial

The single most important calibration in the skill: **match planning depth to
objective size.** Over-planning a small objective is the regression to avoid. The
dial reuses clawpm's existing `s/m/l/xl` complexity vocabulary, so the planning
depth and the emitted leaves' `complexity` predictions share one scale.

## Classify twice

1. **Up front**, from the objective text alone.
2. **Again after recon** — recon can revise the classification up or down (a
   "small" objective touching a god-object is really `l`; a "big" objective that's
   one well-bounded change is `m`).

**Default one tier LOWER when uncertain.**

## The dial

| Scale | Stages run | Decompose depth | Shape | PRD? |
|-------|-----------|-----------------|-------|------|
| **s** | recon(light) → decompose → emit | flat | 1–2 leaves, no ideation, no vet ceremony | no |
| **m** | recon → (ideate) → specify → decompose → vet → emit | 1 level | parent + several leaves | short PRD |
| **l** | full pipeline | 2 levels | milestone → slice → unit | PRD |
| **xl** | full pipeline + personas (opt-in) + explicit direction-candidate review | 2–3 levels | multi-milestone tree, PRD + lightweight ADR, directions surfaced separately | PRD (+ ADR) |

## How depth maps to emit-tree calls (v1 has no in-document nesting)

`parent_ref` is null in v1 — you build depth by **emitting in layers**, each
`attach_to` adding one level:

- **s / m (flat / 1 level):** one emit-tree call. `root.title` = objective, leaves
  are the slices. *(See `examples/software-autosave.emit.json` — an m-scale tree.)*

- **l (2 levels):**
  1. Emit milestones as leaves under a new-root objective → milestone task ids.
  2. For each milestone, emit its slices with `root.attach_to: <milestone-id>`.

- **xl (2–3 levels):** as `l`, plus a third `attach_to` layer (slice → unit) where
  a slice is itself large; plus opt-in personas (`personas.md`) and an explicit
  direction-candidate review beat.

## What changes per scale

- **Ideation:** skipped at s; light at m; full divergent set at l/xl.
- **PRD:** absent at s; short at m; full at l; PRD + ADR at xl.
- **Vet:** minimal at s (just a ledger diff); full no-slop gate at m+.
- **Direction candidates:** only meaningfully surfaced at l/xl (filed as
  `clawpm research`, never leaves).
- **Personas:** off at s/m; opt-in at l/xl.

## Worked anchors

- **s** — "add a `--json` flag to the export command": recon(light) → 1 leaf
  ("export emits valid JSON, schema-checked") → emit. No PRD.
- **m** — "add draft autosave to the editor": recon → specify → 3 vertical slices
  → vet → emit. Short PRD. *(the software example.)*
- **m (knowledge-work)** — "produce a competitor-pricing brief": recon(brief) →
  specify → 3 deliverable slices → vet → emit. *(the knowledge example.)*
- **l** — "migrate auth to JWT across web + mobile + API": milestones (web, mobile,
  api, cutover) → per-milestone slices via `attach_to`. PRD.
- **xl** — "re-platform billing onto the new ledger": multi-milestone, PRD + ADR,
  personas (PM scoping, architect risk), direction candidates (e.g. "also
  consolidate the two invoice tables?") filed as research, not leaves.
