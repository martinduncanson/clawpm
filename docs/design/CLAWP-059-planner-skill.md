# CLAWP-059 — clawpm-planner skill: SPEC / ADR

**Status:** DESIGN — satisfies the CLAWP-059 "DESIGN GATE: SPEC/ADR reviewed before build" criterion.
**Author:** planner-design pass, 2026-06-11.
**Type:** in-harness Agent SKILL (Claude Code skill format), companion to `clawpm` core.
**Depends on:** CLAWP-056 (emission API — the facts half of the seam), CLAWP-053 (reject ledger), CLAWP-054 (contract fields), CLAWP-055 (baseline), CLAWP-057 (constitution).
**Scope of this doc:** the design only. No code, no SKILL.md is written here. One recommended design; alternatives noted inline.

---

## 0. Decision summary (the one recommended design)

Build `clawpm-planner` as a **lean-SKILL.md + `references/` reference-doc** skill (the shadcn/improve shape), shipped as a **companion skill inside the clawpm repo** (`skills/clawpm-planner/`) so it travels with the CLI while core stays deterministic. The skill is the **only** place model-judgment lives; it consumes a free-text objective, runs a **scale-adaptive, all-stages-optional** pipeline (constitution → recon → ideate → specify → decompose → vet → emit → handoff), and persists the result through **one transactional CLAWP-056 emission call**. Recon and decompose **consult a repo/corpus graph** (codegraph default, graphify for mixed corpora) for blast-radius and structural vetting; when neither graph exists the skill **surfaces the gap and proposes remediation** rather than presenting vibe estimates as grounded.

The deterministic-first seam is the spine of the whole design: **the skill decides; core persists.** Every judgment (what to build, how to slice, who executes, effort/risk) is made in the skill on a capable model; core receives a fully-contracted tree and writes it, making **zero LLM calls**.

---

## 1. Skill structure

### 1.1 Format & home

Match the existing `~/.claude/skills/clawpm/skills/clawpm/SKILL.md` frontmatter convention exactly (`name`, `description`, `user-invocable`, `metadata.openclaw`). Ship in the clawpm repo at `skills/clawpm-planner/SKILL.md` with `references/` alongside. This keeps the judgment layer versioned with the CLI it emits into — the contract between skill and core changes together.

```
skills/clawpm-planner/
  SKILL.md                      # lean spine: when-to-use, stage table, invocation, hard rules
  references/
    stage-playbook.md           # per-stage purpose / tools / IO / optionality / depth dial
    decompose-vet.md            # vertical-slice rules, per-node expansion prompt, vet checklist
    emission-contract.md        # the exact CLAWP-056 payload schema the skill builds (our half)
    graph-consultation.md       # codegraph/graphify usage, neither-available remediation, staleness caveat
    personas.md                 # OPTIONAL composable lenses (analyst/PM/architect) — opt-in only
    plan-template.md            # the self-contained PRD/spec artifact template (improve-style)
    fixtures/                   # software + knowledge-work demo objectives for the test plan
```

**Why split (improve discipline):** SKILL.md stays scannable — the model loads the spine every invocation, and pulls a reference doc only for the stage it is executing. The heavy vetting checklist, the emission schema, and the graph caveat-table are reference material, not spine.

### 1.2 Trigger / description

```
name: clawpm-planner
description: >
  Turn a free-text objective (software OR knowledge-work) into a vetted, scale-appropriate
  clawpm task-tree and emit it. Runs the planning judgment in-harness on a capable model:
  recon (graph-grounded) → divergent ideation → PRD/spec → vertical-slice decomposition →
  vetting against the reject ledger + constitution → transactional emission with per-leaf
  rubric/scope/stop/delegability/predictions/baseline. Hands delegable leaves to a cheaper
  executor via clawpm dispatch. Use when an objective needs planning, not when a single task
  is already well-formed. NOT a code auditor.
```

Triggers: "plan this objective", "break this goal into tasks", "decompose <objective>", "turn this brief/PRD into a clawpm tree", "what's the plan for <goal>". Anti-trigger (route to plain `clawpm add`): a single already-well-formed task; pure Q&A; "just add a task".

---

## 2. Stage flow

All stages are **optional and composable** (OpenSpec: stages-not-gates). The pipeline never forces ceremony; the model selects the stages the objective warrants and can re-enter any stage. The **scale dial** (§2.10) governs how many stages run and how deep decompose goes.

| # | Stage | Purpose | Tools | In → Out | Optional when |
|---|-------|---------|-------|----------|---------------|
| 1 | constitution | Load/confirm project invariants that constrain every leaf | `clawpm` constitution read (CLAWP-057) | project ctx → invariant set | no constitution declared (runs identically) |
| 2 | recon | Orient on the ground; map structure/topology | **codegraph** / graphify; Read; parallel **Explore** subagents | objective + ground → orientation notes (god-objects, layering, fan-in, hotspots) | trivial objective with self-evident ground |
| 3 | ideate | Divergent brainstorm of approaches AND **direction** candidates | model judgment; web; `clawpm research` | orientation → ranked approaches + direction-candidate set | s-scale objective with one obvious approach |
| 4 | specify | Draft the PRD/spec (what + why) | model; `plan-template.md` | chosen approach → PRD artifact (stored via CLAWP-056) | s-scale; or operator supplied a spec already |
| 5 | decompose | Scale-adaptive, vertical-slice, unit-traceable leaves + per-node expansion prompt | model; **graph `impact`/`trace`** for blast-radius | PRD → candidate leaf tree (each leaf draft-contracted) | never fully skipped (it IS the deliverable) but depth scales to flat for s |
| 6 | vet | Re-read cited ground; reject dup/by-design; diff vs reject ledger; settle structural claims with the graph | graph (`callers`/`impact`); Read; `clawpm` reject-ledger query (CLAWP-053) | candidate tree → vetted tree (+ rejections logged) | never skipped above s-scale — this is the no-slop gate |
| 7 | emit | One transactional persist of the fully-contracted tree + PRD link | **CLAWP-056 emission API** | vetted tree → emitted clawpm task-tree (parent + leaves) | never skipped (it's the whole point) |
| 8 | handoff | Dispatch delegable leaves to the cheap executor; bounce human leaves | `clawpm tasks dispatch`; operator surface | emitted tree → in-flight dispatches / operator queue | optional — operator may want to review before dispatch |

### 2.1–2.8 Per-stage detail

**Constitution** — pulls the project's named invariants (CLAWP-057). These become a checklist the **emit** stage validates each leaf against (e.g. "all code work is test-first" rejects a no-test leaf). Pure facts-lookup; no judgment beyond "which invariants apply".

**Recon** — the improve "map the domain" step. Fans out parallel **Explore** subagents for semantic orientation ("where does X live, what's fragile, what's already solved") AND consults the **graph** for structural orientation (high-fan-in nodes, cycles, layering violations). The two are complementary: graph = facts ("what calls what"), Explore = judgment ("is this the right place / is this wrong"). The graph never replaces the fan-out. Output is orientation notes, not leaves.

**Ideate** — BMAD's divergent-first step. Generates multiple *approaches* (how to achieve the objective) and *direction candidates* (alternative objectives / adjacent opportunities surfaced during recon). **Direction candidates are stored as `clawpm research` entries, NEVER interleaved as leaves** — they are "things to consider", not "things to do". Elicitation: present approaches propose-then-review so the operator's thinking is drawn out before a PRD is committed.

**Specify** — drafts the PRD/spec using `plan-template.md` (self-contained: objective, why, constraints, out-of-scope, success definition, chosen approach, open questions). Stored via the CLAWP-056 PRD-storage surface as a research/mission artifact, **linked to the tree** so the cheap executor reads it durably.

**Decompose** — the heart. Produces **vertical slices** (mattpocock/tdd): each leaf is independently verifiable end-to-end, never a horizontal layer ("write the DB schema" is wrong; "user can save a draft, persisted and retrievable" is right). Each leaf gets a **per-node expansion prompt + recommended subtask count** (claude-task-master) tailored to that leaf. Effort/risk per leaf is **grounded by the graph's blast-radius** (`codegraph_impact`), not a vibe estimate. Depth is scale-adaptive (§2.10). Every leaf carries a **traceability link to the root objective** (§3).

**Vet** — the no-slop gate, imported from improve above all else. For each candidate leaf: (a) re-read the cited ground to confirm the leaf is real and not mis-attributed; (b) reject by-design behaviour and duplicates; (c) diff against the **reject ledger** (CLAWP-053) including fuzzy/resembling matches, so a near-duplicate of an already-rejected idea is caught and the operator reminded *why*; (d) use the **graph to settle structural claims** deterministically — "dead code" = callers 0, "duplicated in N places" = exact count. Rejections are written back to the ledger with rationale.

**Emit** — single transactional CLAWP-056 call (§4). Each leaf arrives fully-contracted; core persists all-or-nothing, reject-matches, constitution-checks, baseline-stamps. The skill makes the decisions; core writes the facts.

**Handoff** — `clawpm tasks dispatch` hands **delegable** leaves (`delegability: agent|either`) to the cheap executor (sonnet/haiku), gated by clawpm's existing Stop-hook judge + blind-refuter + tournament. **Human leaves bounce to the operator** — the dispatch path refuses to auto-dispatch a `human` leaf (CLAWP-054). This is the model-tier handoff: capable model planned, cheap model executes under the rubric gate.

### 2.10 The scale-adaptive dial (maps to clawpm s/m/l/xl)

The model classifies the objective's complexity up front and again after recon (recon can revise it). The classification selects stage set and decompose depth:

| Scale | Stages run | Decompose depth | Shape |
|-------|-----------|-----------------|-------|
| **s** | recon (light) → decompose → emit | flat | 1–2 leaves, no PRD, no ideation |
| **m** | recon → (ideate) → specify → decompose → vet → emit | 1 level | parent + several leaves, short PRD |
| **l** | full pipeline | 2 levels | milestone → slice → unit (GSD-Pi hierarchy) |
| **xl** | full pipeline + personas (opt-in) + explicit direction-candidate review | 2–3 levels | multi-milestone tree, PRD + ADR, direction candidates surfaced separately |

The dial is the BMAD/task-master scale-adaptive principle made concrete against clawpm's existing `s/m/l/xl` complexity vocabulary, so the emitted leaves' `complexity` predictions and the planning depth share one scale. **Default to one tier lower when uncertain** — over-planning a small objective is the regression to avoid.

---

## 3. Ideation→decompose handoff & big-picture traceability

Two distinct outputs leave ideation: **approaches** (feed forward into specify→decompose) and **direction candidates** (sidelined into `clawpm research`). The handoff rule: *only the chosen approach crosses into decompose; everything else is a research entry, not a leaf.* This is what stops the tree filling with speculative "maybe also do X" work.

**Big-picture traceability (GSD-Pi):** every leaf carries a back-reference to the root objective and its parent milestone/slice, established at decompose time and persisted at emit time. The mechanism:

- The **PRD artifact** is the root anchor (stored + linked via CLAWP-056).
- Each emitted task's parent chain (`tasks decompose` parent→child, already in core) gives milestone→slice→unit hierarchy.
- Each leaf body carries an explicit "traces to: <objective> / <milestone>" line so a cheap executor reading one leaf in isolation still sees the why.
- The success-criteria of every leaf must serve the PRD's success definition — vet checks this (a leaf whose rubric doesn't ladder up to the objective is noise and gets cut).

So traceability is enforced at three layers: structural (parent chain), documentary (PRD link + per-leaf trace line), and semantic (vet rejects non-laddering leaves).

---

## 4. The emission-API contract the skill targets (our half of CLAWP-056)

This is the **skill→core contract**. CLAWP-056 is designing the core half in parallel; this section states exactly what the skill needs to send and get back. **Flag any mismatch to CLAWP-056.**

### 4.1 What the skill SENDS — one transactional call

A single operation (CLI subcommand or JSON-on-stdin; recommend `clawpm tasks emit-tree --json -` reading the payload from stdin, JSON-out) accepting a fully-specified tree:

```jsonc
{
  "project": "clawpm",
  "objective": "free-text root objective",
  "prd": {                          // optional; present for m/l/xl
    "title": "...", "body_md": "...", "kind": "spec",
    "link_as": "research"           // stored as research/mission entry, linked to tree root
  },
  "tree": {
    "parent": { "title": "...", "body": "...", "complexity": "l" },
    "leaves": [
      {
        "title": "...",
        "body": "...",                       // includes the per-node expansion prompt + "traces to" line
        "rubric": [                           // → emit-rubric (CLAWP-016/017)
          { "criterion": "...", "gradeable_signal": "...", "comparator": "..." }
        ],
        "scope": ["src/x/**"],                // file globs OR named deliverables
        "out_of_scope": ["..."],              // CLAWP-054
        "stop_conditions": ["if X false, STOP and report"],  // CLAWP-054
        "delegability": "agent|human|either", // CLAWP-054, default either
        "predictions": {                      // clawpm prediction schema
          "duration_min": 90, "complexity": "m", "confidence": 3,
          "approach": "...", "pre_mortem": "...", "reference_tasks": []
        },
        "recommended_subtasks": 0,            // task-master per-node count; 0 = leaf is atomic
        "traces_to": "milestone-id|objective"
      }
    ]
  },
  "idempotency": {
    "run_key": "planner-<objective-hash>",    // stable across re-runs of same objective
    "leaf_keys": ["<stable per-leaf key>"]     // for dedup against already-emitted/rejected
  }
}
```

### 4.2 What core MUST do (the asks on CLAWP-056)

1. **Atomic all-or-nothing persist** of parent + all leaves via `tasks decompose` + `emit-rubric` in one transaction; partial failure rolls back the whole tree.
2. **Reject-match** every leaf against the CLAWP-053 ledger at emission; a matched leaf is dropped (not emitted) and reported back, not silently swallowed.
3. **Constitution-check** every leaf against CLAWP-057 invariants pre-emission; a violation is reported back (so the skill can split/fix/route-to-reject), not auto-emitted.
4. **Baseline-stamp** each emitted task (CLAWP-055) at creation.
5. **PRD storage + link**: persist the PRD artifact and link it to the tree root, retrievable by a downstream executor.
6. **Idempotent re-emit**: given the same `run_key`/`leaf_keys`, core must NOT duplicate already-emitted leaves and must NOT re-emit a leaf whose key is in the reject ledger. (This is what satisfies CLAWP-059's "a re-run does not duplicate already-emitted or rejected leaves" criterion — and it is a **shared** responsibility: skill supplies stable keys, core enforces dedup.)
7. **Zero LLM calls** in this path (core stays deterministic, verified by test on the 056 side).

### 4.3 What core MUST return

A structured result the skill can act on without re-deriving: `{ emitted: [task_ids], rejected: [{leaf_key, reason: dup|constitution|ledger}], prd_id, tree_root_id }`. The skill surfaces rejections to the operator and, for constitution violations, may loop back to decompose/vet.

### 4.4 Mismatch risks to raise with CLAWP-056

- **Idempotency key ownership.** The skill proposes deriving stable `leaf_key`s (objective-hash + normalised title + scope). If 056 instead wants to own key derivation, the dedup contract breaks — agree the key scheme explicitly. *This is the highest mismatch risk.*
- **Constitution-violation handling.** Skill assumes core *reports* violations for the skill to resolve; if 056 instead routes violations straight to the reject ledger, the skill loses the split/fix loop. Confirm: report-back, not auto-reject.
- **PRD link shape.** Skill assumes a research/mission entry id it can reference; confirm 056 exposes the link both directions (tree→PRD and PRD→tree).
- **Transaction granularity.** Confirm the rollback unit is the whole tree, not per-leaf — a half-emitted tree is worse than none.

---

## 5. Graph consultation (codegraph / graphify)

Deterministic-first, restated: **graph = facts ("what calls what / what breaks"), Explore fan-out = judgment ("is this wrong / worth doing")**. Complementary; the graph never replaces the semantic fan-out.

**Default — codegraph** (MIT, Windows-native, already live in this repo: 67 files / 2146 nodes confirmed at design time). Uses:
- `codegraph_context` → recon orientation.
- `codegraph_impact` → decompose blast-radius → grounds per-leaf effort/risk.
- `codegraph_callers` / `codegraph_trace` → vet reachability ("dead code" = callers 0).

**Mixed / knowledge-work — graphify** (graphs code AND docs/PDF/SQL-schema/infra; Leiden community detection; edge provenance). Prefer when the objective's ground is a non-code corpus — the project-agnostic case codegraph can't serve. Heavier (LLM index layer spends host tokens), non-code edges model-dependent; **run install-gate before adoption**; pending UPSKI-012 bake-off.

**Neither available (the remediation behaviour — a CLAWP-059 success criterion):** the skill must **surface the gap and propose remediation** — "init codegraph on this repo" / "install graphify for this corpus" — and let the operator choose. It must **NOT** silently fall back to vibe effort/risk estimates presented as grounded. Decompose may still proceed, but every effort/risk number is explicitly tagged *ungrounded — no graph consulted* so the operator sees the confidence drop. This is a hard behavioural requirement, tested (§8).

**Staleness / coverage caveat (no silent caps):** a graph indexes only what its parser saw — dynamic dispatch, reflection, DI, config-driven wiring, cross-language boundaries are missed or guessed. Topology-only findings (dead-code, "unreachable") carry a reachability caveat in the leaf; **never rest a security or correctness claim on the graph alone — read the site.** Also: the codegraph watcher lags writes ~500ms; the skill must not query immediately after an edit in the same turn.

---

## 6. Persona / stage optionality — do personas earn their keep?

**Recommendation: personas are OPT-IN and OFF by default. They are *lenses on the prompt*, not separate agents or mandatory roles.**

BMAD's analyst/PM/architect personas add real value as *elicitation lenses* — "what would a PM ask about this objective; what would an architect flag" — but as **mandatory composed roles they are ceremony** that bloats every run. The lean-core ethos and OpenSpec's stages-not-gates both argue against forcing them.

So: ship a `personas.md` reference with three composable lenses the model *may* adopt during ideate/specify on **l/xl** objectives or when the operator asks ("plan this as a PM" / "give me the architect's view"). On s/m they stay off. They are lens-prompts applied within existing stages — **not** extra subagents, **not** extra stages. This keeps the value (multi-perspective elicitation when warranted) without the ceremony (a 3-persona round-trip on a 2-leaf objective). Personas that don't earn their keep on a given objective simply aren't invoked.

**Alternative considered & rejected:** persona-as-subagent (each persona a separate Explore agent). Rejected — multiplies cost and depth, risks the depth>2 nesting smell, and the lens value is captured far cheaper as a prompt section.

---

## 7. Packaging

1. **Companion skill in the clawpm repo** (`skills/clawpm-planner/`) — travels with the CLI, versioned with the emission contract, keeps core lean.
2. **Build via the `skill-creator` workflow** — canonical; don't improvise the skill scaffold.
3. **Codex/Gemini review before packaging** (skill-creation discipline): the skill carries substantive prompt logic + reference docs >100 LOC-equivalent, so the review gate fires. Commit on a feature branch in the clawpm fork, open a PR, tag `@codex` with Goal/Approach/Concerns (concerns: the stage-selection heuristics, the vet checklist, the emission payload schema). Wait via `wait-for-codex.py`. Only `package_skill.py` after clean. Gemini parallel-primary if the skill lands ≥~300 LOC-equivalent or the operator flags it.
4. **Coordinate the merge with CLAWP-056** — the skill cannot emit until the core API exists. Land 056 first (or behind a feature flag), then the skill; or land both in a stacked PR set. The emission-contract reference doc (§4) is the shared artifact both PRs must agree on.

---

## 8. Test / validation plan — mapped to the 5 CLAWP-059 success criteria

| # | Criterion | Validation |
|---|-----------|------------|
| 1 | **DESIGN GATE** — SPEC/ADR defines stage flow, scale dial, ideation→decompose handoff, persona optionality, emission contract, reviewed before build | **This document.** Reviewed (Codex/Gemini or operator) before any code. |
| 2 | Run on a free-text objective → EMITS a clawpm tree (≥1 parent, vertical-slice leaves each with rubric+scope+stop+delegability) + linked PRD; demonstrated on one **software** AND one **knowledge-work** objective, NO code-audit assumptions | Two fixtures in `references/fixtures/`: (a) software — e.g. "add draft autosave to the editor"; (b) knowledge-work — e.g. "produce a competitor-pricing brief". Run skill end-to-end against each; assert a parent + vertical-slice leaves emitted, each leaf fully-contracted, PRD stored + linked. Knowledge-work fixture grounds on graphify-or-remediation, proving project-agnosticism. |
| 3 | Depth is scale-adaptive (trivial → flat 1–2 tasks; large → multi-level tree, two fixtures); direction candidates emitted as research entries, not interleaved leaves | Two depth fixtures: an **s** objective asserts ≤2 flat leaves, no PRD; an **xl** objective asserts ≥2-level tree. Assert the ideate stage's direction candidates land as `clawpm research` entries and **zero** appear in `tree.leaves`. |
| 4 | Model-tier-selectable (runs on capable model; emits leaves clawpm dispatch hands to a cheaper executor); a re-run does not duplicate already-emitted or rejected leaves | Skill documents the capable-model assumption; emitted `agent|either` leaves dispatch to sonnet/haiku under the Stop-hook gate. **Idempotency test:** run the skill twice on the same objective; assert no duplicate task_ids and that a leaf rejected on run 1 is not re-emitted on run 2 (relies on the §4.2.6 shared dedup contract — co-tested with CLAWP-056). |
| 5 | recon/decompose consult a graph when available (codegraph/graphify) to ground blast-radius + structural vetting; when **neither** available, surface the gap + propose remediation rather than vibe estimates as grounded | Test A (graph present): assert decompose calls `codegraph_impact` and per-leaf effort cites blast-radius. Test B (**graph absent**): point the skill at a non-indexed corpus; assert it **surfaces the remediation prompt** and tags effort estimates *ungrounded*, and does NOT present them as grounded. This is the explicit graph-remediation behaviour gate. |

Validation harness: the two end-to-end fixtures double as the demo for criteria 2–3; criteria 4–5 are unit-style assertions on the skill's stage outputs. Criteria 4's dedup and the whole emit path **block on CLAWP-056** — sequence the skill's integration tests after 056 lands.

---

## 9. Open questions for the operator

1. **Idempotency key ownership** — skill-derived `leaf_key`s vs core-derived. Recommend skill-derived (objective-hash + normalised title + scope); needs CLAWP-056 agreement. *Highest-risk open item.*
2. **graphify adoption timing** — wait for the UPSKI-012 bake-off before wiring graphify, or wire it now behind install-gate with codegraph as the proven default? Recommend: ship codegraph-only first; graphify as a documented "preferred for mixed corpora, pending bake-off" path the skill *suggests* but doesn't hard-require.
3. **Handoff autonomy** — should the skill auto-`dispatch` delegable leaves at the end of a run (L2), or always stop at "emitted, awaiting your go" for operator review? Recommend: emit-then-pause by default; auto-dispatch only when the operator says "and run them".
4. **Personas surface** — confirm opt-in-off-by-default is right, or does the operator want a persona always-on for xl?
5. **PRD vs ADR** — for xl objectives, store a PRD only, or PRD + a lightweight ADR for the chosen-approach rationale? (This doc is itself the pattern for the latter.)
6. **Constitution authoring** — does CLAWP-057 ship before this skill, or does the skill need to degrade gracefully (run with no constitution) in the interim? Design assumes graceful degradation; confirm.
