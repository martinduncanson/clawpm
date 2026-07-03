---
complexity: xl
created: '2026-06-11'
depends:
- CLAWP-056
id: CLAWP-059
predictions:
  success_criteria:
  - 'DESIGN GATE: a SPEC/ADR defines the skill''s stage flow, the scale-adaptive depth
    dial, the ideation->decompose handoff, persona/stage optionality, and the exact
    clawpm emission-API contract it targets — reviewed before build'
  - running the skill on a free-text objective produces and EMITS a clawpm task-tree
    (>=1 parent, vertical-slice leaves each with rubric + scope/stop/delegability),
    with a linked PRD/spec artifact — demonstrated on one software AND one knowledge-work
    objective, with NO code-audit assumptions
  - 'depth is scale-adaptive: a trivial objective yields a flat 1-2 task result, a
    large one a multi-level tree (two fixtures); direction candidates are emitted
    separately as research entries, not interleaved as leaves'
  - the skill is model-tier-selectable (runnable on a capable model for planning)
    and emits leaves that clawpm dispatch can hand to a cheaper executor; a re-run
    does not duplicate already-emitted or rejected leaves
  - recon/decompose consult a repo graph when available (codegraph default; graphify
    for mixed/non-code corpora) to ground blast-radius and settle structural vetting;
    when NEITHER is available the skill surfaces the gap and proposes remediation
    to the operator rather than presenting vibe effort estimates as grounded
priority: 5
---

# clawpm-planner skill: in-harness judgment layer (objective -> vetted task-tree), model-tier-selectable, merges improve+BMAD+GSD+spec-kit+OpenSpec+task-master

The JUDGMENT layer split out of CLAWP-056 along the deterministic-first seam: an in-harness SKILL (not clawpm CLI code) that takes an objective and produces a vetted, scale-appropriate task-tree, EMITTING into clawpm via the core emission API (CLAWP-056). Runs IN the harness so it has the full toolbelt (Explore fan-out, codegraph, web, reading clawpm research entries) — a CLI subprocess shelling `claude -p` cannot do this; that is the decisive reason it is a skill, not a core command.

MODEL-TIERING IS THE POINT (operator intent): run THIS skill on the most capable model (Fable/Opus) — understanding + ideation + decomposition is where intelligence compounds. The emitted leaves are then handed to a CHEAP model via clawpm dispatch (sonnet/haiku), gated by the existing Stop-hook judge + blind-refuter + tournament + delegability. improve's economics (expensive advises, cheap executes), but with clawpm's superior back-half replacing improve's weak human-gated diff review.

PROJECT-AGNOSTIC (hard constraint): decomposes a software OR a knowledge-work objective. NOT a code auditor. The "exploration" input is whatever grounds the goal (codebase, brief, research entries, notes).

SYNTHESISES SIX SURVEYED SYSTEMS (skills compose prompt-judgment; this is the right home for the merge — a CLI cannot merge personas/stages):
- shadcn/improve — the spine: recon -> parallel Explore fan-out -> VET every candidate (re-read cited ground; reject by-design/mis-attributed/duplicate) -> prioritise by leverage -> self-contained plan-template. Import the vetting discipline above all (no slop).
- BMAD-METHOD — divergent IDEATION/brainstorm phase FIRST; SCALE-ADAPTIVE planning depth; optional composable PERSONAS (analyst/PM/architect lenses); elicitation that draws out the operator's thinking (propose-then-review).
- GSD-Pi — unit hierarchy (milestone->slice->unit); BIG-PICTURE traceability (every leaf traceable to the root objective across a long horizon); planning-vs-execution separation (this skill is planning-only).
- github/spec-kit — the phased command surface as COMPOSABLE STAGES: constitution -> specify -> plan -> tasks; the PRD/spec as an explicit intermediate artifact; optional clarify/analyze quality steps.
- OpenSpec — STAGES NOT GATES (update any artifact anytime, no ceremony — matches lean-core); change-as-delta + archive lifecycle maps onto clawpm done + reject-ledger.
- claude-task-master — parse-PRD -> tasks; analyze-complexity emitting a PER-NODE tailored expansion prompt + recommended subtask count; dependency-aware emission.
Plus mattpocock/tdd: leaves are VERTICAL SLICES (each independently verifiable end-to-end), never horizontal layers; mattpocock/triage: set per-leaf DELEGABILITY (agent|human|either).

STAGE FLOW (all stages optional/composable per OpenSpec; depth scale-adaptive per BMAD/task-master):
1. constitution — load/confirm project invariants (clawpm CLAWP-057).
2. recon — map the domain/ground (improve); CONSULT A REPO GRAPH for structure/topology when available (see GRAPH CONSULTATION).
3. ideate — divergent brainstorm of approaches AND direction-candidates (BMAD); direction candidates presented SEPARATELY, stored as research entries, not as leaves.
4. specify — draft the PRD/spec (what+why); stored via clawpm as a research/mission artifact linked to the tree (CLAWP-056).
5. decompose — scale-adaptive depth, vertical-slice leaves, unit-traceable to root, per-node expansion prompt; GROUND effort/risk per leaf with the graph's impact/blast-radius (deterministic), not a vibe estimate.
6. vet — re-read/confirm each leaf, reject dup/by-design, diff against the won't-do ledger (CLAWP-053). Use the graph to settle STRUCTURAL claims (dead code = callers 0, "duplicated in N places" = exact count) and correct mis-attributed evidence.
7. emit — into clawpm via the CLAWP-056 emission API: each leaf carries rubric + scope/stop/delegability (CLAWP-054) + predictions + baseline; constitution-checked.
8. handoff — clawpm dispatch hands delegable leaves to the cheap executor; human leaves bounce to the operator.

GRAPH CONSULTATION (deterministic-first AGAIN: graph = FACTS "what calls what / what breaks", Explore fan-out = JUDGMENT "is this wrong / worth doing" — complementary, the graph never replaces the semantic fan-out):
- A repo/corpus graph multiplies the STRUCTURAL spine only — recon orientation (god-objects/high-fan-in, layering violations, cycles), effort/risk prioritisation (blast-radius), tech-debt/dead-code, and structural vetting. It adds little to the SEMANTIC categories (correctness, security, by-design judgment, test quality, direction) — those stay model work.
- DEFAULT: codegraph (MIT, Windows-native, already wired in clawpm's own repo). Use `codegraph_impact` for blast-radius, `codegraph_trace`/callers for reachability, `codegraph_context` for orientation.
- MULTIMODAL / KNOWLEDGE-WORK: graphify — graphs code AND docs/PDF/SQL-schema/infra in one graph, Leiden community detection (god-nodes / surprising connections), edge provenance. Prefer it when the objective's ground is a mixed/non-code corpus (the project-agnostic case codegraph can't serve). Note: heavier (LLM index layer spends host-session tokens), non-code edges are model-dependent; pending the UPSKI-012 bake-off — judge on merit, run install-gate before adoption.
- IF NEITHER GRAPH IS AVAILABLE for the target: CONSULT THE OPERATOR TO REMEDIATE (init codegraph on a code repo / install graphify for a mixed corpus) before relying on blast-radius grounding. Do NOT silently fall back to vibe effort/risk estimates and present them as grounded — surface the gap, propose the remediation, let the operator choose.
- CAVEAT (no silent caps): a graph indexes only what its parser saw — dynamic dispatch, reflection, DI, config-driven wiring, cross-language boundaries are missed or guessed. Topology-only findings (dead code, "unreachable") carry a reachability caveat; never rest a security claim on the graph alone — read the site.

PACKAGING: build via the skill-creator workflow; ship as a COMPANION skill in the clawpm repo (alongside ~/.claude/skills/clawpm) so it travels with the CLI while keeping core lean. Codex/Gemini-review the skill before packaging per skill-creation discipline.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

