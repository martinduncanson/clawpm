# Graph consultation — codegraph / graphify, and the neither-available behaviour

**Deterministic-first, restated:** the **graph = facts** ("what calls what / what
breaks"); the **Explore fan-out = judgment** ("is this wrong / worth doing"). They
are complementary — the graph **never** replaces the semantic fan-out. Recon and
decompose consult the graph; vet settles structural claims with it.

## Default — codegraph

MIT, Windows-native, already live in the clawpm repo. Structural facts in
sub-millisecond reads. Uses:

- `codegraph_context` → **recon** orientation (what's here, what's central).
- `codegraph_impact` → **decompose** blast-radius → grounds per-leaf effort/risk
  ("this slice touches 14 callers" is a grounded `l`, not a vibe `m`).
- `codegraph_callers` / `codegraph_trace` → **vet** reachability ("dead code" =
  callers 0; "is X reachable from Y" in one trace call).

Check health with `codegraph_status`. **Watcher lag:** the index debounces ~500ms
behind writes — don't query immediately after an edit in the same turn.

## Mixed / knowledge-work corpora — graphify

Graphs code **and** docs/PDF/SQL-schema/infra (Leiden community detection, edge
provenance). Prefer when the objective's ground is a **non-code corpus** — the
project-agnostic case codegraph can't serve (e.g. a research-brief objective).
Heavier (an LLM index layer spends host tokens); non-code edges are
model-dependent. **Run `install-gate` before adopting it**; bake-off pending
(UPSKI-012). Until then, graphify is a *suggested* path, not a hard requirement.

## Neither available — the required remediation behaviour

This is a **CLAWP-059 success criterion**, not optional. When no graph covers the
objective's ground:

1. **Surface the gap and propose remediation** — name it explicitly to the
   operator: *"No structural graph on this corpus. Remediation: `codegraph init -i`
   on the repo (code), or install graphify (mixed corpus). Proceed ungrounded?"*
2. **Do NOT silently fall back to vibe estimates presented as grounded.** Decompose
   may still proceed, but **every effort/risk number is explicitly tagged
   `UNGROUNDED — no graph consulted`** in the leaf's `predictions.approach`, so the
   operator sees the confidence drop. *(Both `examples/` PRDs do exactly this — the
   demo corpus had no graph, so the effort fields carry the UNGROUNDED tag.)*
3. **Lower the confidence** on ungrounded leaves' `predictions.confidence`
   accordingly.

The failure this prevents: a plan whose effort/risk numbers *look* grounded but
were guessed — the operator trusts them, they're wrong, and the calibration loop
is poisoned.

## Staleness / coverage caveat (no silent caps)

A graph indexes only what its parser saw. **Dynamic dispatch, reflection,
dependency injection, config-driven wiring, and cross-language boundaries are
missed or guessed.** Therefore:

- Topology-only findings (dead-code, "unreachable") carry a **reachability caveat**
  in the leaf — confirm at the site for anything load-bearing.
- **Never rest a security or correctness claim on the graph alone — read the
  site.** The graph narrows where to look; it doesn't close the question.
- Treat a graph "0 callers" as "0 *static* callers" — a DI container or a string-keyed
  dispatch can still reach it.
