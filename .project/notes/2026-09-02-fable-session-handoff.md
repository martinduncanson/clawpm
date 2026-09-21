# Fable planning session — handoff (2026-09-02)

Worktree: `F:/Git/clawpm-fable-merge-planning`, branch `session/fable-merge-planning-20260902`, based off `fork/main` @ 06a32b7 (the canonical martinduncanson/clawpm main — NOT `origin`, which is upstream malphas-gh, kept for reference only per repo CLAUDE.md).

Per global model-routing policy (`CLAUDE.md` → Model routing table, `decisions.md` 2026-07-14 model-routing entry): this session runs on **Fable 5, reasoning effort ≤ high**. Do not delegate the actual planning/comparison work to Sonnet subagents — Fable does the judgment work directly; Sonnet is for cheap execution once a plan exists.

Two queued Fable-class planning tasks live here, both filed L-complexity / ~4h estimate, both stale since filing (103: 2026-07-14, 104: 2026-07-31) because no session at the right model tier had picked them up. Operator (Martin) confirmed 2026-09-02 he wants a session started on this now. Work CLAWP-103 first — it's the one Martin explicitly asked about — then 104 if the session has room, or hand 104 to a fresh Fable session if 103 runs long.

## CLAWP-103 — merge plan: clawpm vs. wayfinder / OpenSpec / feature-dev / osmani agent-skills

Full task body: `.project/tasks/CLAWP-103.md` in this worktree.

**Goal:** gap analysis + adopt/adapt/skip verdict per mechanic, comparing clawpm against:
- mattpocock **wayfinder** — decision tickets, fog-of-war, one-ticket-per-session, charting/working phases
- **OpenSpec**
- **feature-dev** plugin (already installed locally — see its skills in the plugin listing: `feature-dev:code-architect`, `feature-dev:code-explorer`, `feature-dev:code-reviewer`)
- **addyosmani/agent-skills** — `/spec /plan /build` slash commands + specialist personas (code-reviewer / security-auditor style)

**Deliverable:** written merge plan (ADR-style) with adopt/adapt/skip verdict per mechanic, plus seeded follow-up clawpm tasks for whatever gets adopted. That's the task's own `success_criteria` — treat it as the actual acceptance bar, not just a suggestion.

**Guardrails — read before proposing anything:**
- `decisions.md` 2026-07-14 WONT-DO: **openspec as a parallel spec system is already rejected** — "clawpm .project/SPEC.md + clawpm-planner own spec-driven decomposition; revisit only if clawpm's spec story proves too thin." Don't re-litigate this without a specific reason clawpm's current spec story is failing.
- `decisions.md` 2026-07-14 WONT-DO: mattpocock **git-guardrails skill** already rejected (destruct-gate is a strict superset) — but note wayfinder is a *different* mattpocock project (ticket workflow, not git safety), not covered by that WONT-DO. Don't conflate the two.
- The task's own pre-mortem: *"Risk: producing a parallel planning system instead of extending clawpm — the WONT-DO ledger row exists precisely to prevent this."* Every adopt verdict should extend `clawpm`/`clawpm-planner`, not create a second planning system.
- Personas question (code-reviewer/security-auditor-style): the task body suggests these might map onto **code-quorum** instead of a persona system — evaluate that mapping explicitly rather than defaulting to "add personas."
- House rule (referenced in the task body, from a "3.1 drift complaint"): **every grilling/planning conversation must end by writing decisions to durable state** — the task body, `.project/SPEC.md`, `decisions.md`, or gbrain. Don't let this session's reasoning evaporate in the transcript. This is the same rule as the global CLAUDE.md "Durable-output house rule."

**Inputs referenced by the task that may need tracking down:**
- osmani-scout + superpowers-scout reports from session `d94ce3c4` (2026-07-14) — if these aren't findable (different machine, expired session, gbrain unreachable — it's been down 2+ sessions per this session's manifest), that's a real gap: flag it rather than reconstructing from memory, and consider a fresh scout pass instead of guessing.
- `specs/spec-conformance-lens.md` in `code-quorum` (separate repo, `F:/Git/code-quorum` locally) — the existing spec-conformance mechanism code-quorum already runs; relevant context for evaluating whether OpenSpec-style spec-conformance is already covered.

## CLAWP-104 — audit self-improvement spec against clawpm's calibration layer

Full task body: `.project/tasks/CLAWP-104.md` in this worktree.

**Source doc:** `F:/Git/.harness-configs/distilled/self-improvement-spec.md` (2026-04-18, operator-directed universal 8-step predict→measure→diverge→reflect→metareflect→patch loop).

**Goal:** audit clawpm's reflection layer (Phase 1.5/1.6 predictions, surprise taxonomy, reflect-void, Phase 2 stubs) against that spec, then propose/implement:
1. Miss-category taxonomy (`wrong_signal` / `wrong_weight` / `missed_variable` / `operator_override` / `noise` / `structural`) as a first-class field alongside or replacing surprise tags — forces root-cause classification. Includes the noise-is-valid rule and the repeat-category-triggers-metareflection rule.
2. Calibration metric set for `reflect summarize`: Brier score, MAE, overconfidence ratio (hit-rate of 0.9-confidence predictions / 0.9), systematic bias, learning velocity (Brier slope over 30/90/365d), miss-category distribution.
3. Two-line append-only ledger pattern (prediction line + outcome line sharing a `prediction_id`) vs. clawpm's current single-event reflections — assess migration cost/benefit, don't assume migration is right.
4. Pre-authorisation lanes concept (quantified auto-apply thresholds per domain) as a **future** clawpm-policy feature — spec only, not implementation, this round.

**Cross-links to check before designing metrics from scratch:**
- `cognition-layer` project, task `COGNI-007` — "calibration engine resurrection"; the self-improvement spec is described as *its* design doc. Read COGNI-007's current state before treating this as greenfield.
- gbrain `takes_calibration` — dedupe metric definitions so clawpm and gbrain compute calibration identically. If gbrain is still unreachable (it has been for 2+ sessions as of 2026-09-02 — CONNECT_TIMEOUT), flag the dependency rather than guessing gbrain's existing formulas.

**Known unknown (from the task's own `unknowns` field):** whether existing reflection JSONLs have enough resolved predictions to compute meaningful baselines. Check this empirically early — `clawpm reflect summarize` on the real portfolio — before designing metrics that assume a data volume that may not exist yet. This directly informs the pre-mortem: *"If clawpm Phase 2 architecture diverges enough from the spec's ledger model that retrofitting costs more than the calibration signal is worth, ship the metrics on the existing event shape instead."*

## Housekeeping when this session wraps

- Update this worktree's own manifest/notes with decisions made, per the Durable-output house rule above.
- Seed follow-up clawpm tasks via `clawpm tasks add` (or `clawpm tasks emit-tree` if the merge plan naturally decomposes into a tree) rather than leaving the plan as prose only.
- Commit + push the branch (`session/fable-merge-planning-20260902`) early and often per git-discipline — don't let a long Fable session sit unpushed.
- Mark CLAWP-103/104 done (or split into follow-ups + mark the parent done) in the **main** checkout (`F:/Git/clawpm`), never from inside this worktree — that's the exact CLAWP-098 worktree-isolation bug this same session fixed today (PR #55).

## Outcome — Fable planning session, 2026-09-02 (planning only; implementation is for Sonnet sessions)

Both tasks planned and specced in one session. Nothing implemented here by design.

**Deliverables in this branch**
- `docs/design/ADR-2026-09-02-planning-merge-plan.md` — CLAWP-103 merge plan: 27-row adopt/adapt/skip table, design D1–D6 of the adopted mechanics, routed-elsewhere table, revisit triggers, pre-mortem.
- `docs/design/calibration-metrics-spec.md` — CLAWP-104 audit (measured corpus facts) + the single definition of every calibration formula, miss-category taxonomy, forward-only two-line ledger, honest actuals, pre-auth lanes (spec only), gbrain dedupe rules.
- `.project/plans/*.emit.json` — the three emit-tree documents, CLI-validated (`--dry-run` clean) and emitted.

**Trees emitted (live in the main checkout backlog)**
- `CLAWP-111` decision maps — 6 leaves, parallel_group 1→3: 111-001 kind:decision + resolution · 111-002 root map sections + fog · 111-003 emit-tree depends_refs/kind · 111-004 frontier query · 111-005 planner chart mode docs · 111-006 SKILL rules + plugin commands.
- `CLAWP-112` calibration — 6 leaves, parallel_group 1→4: 112-001 pre-registered predictions ledger · 112-002 honest actuals · 112-003 miss_category · 112-004 metrics + scorecard · 112-005 nudges + closure alarm · 112-006 docs + COGNI-007 cross-link.
- `CODE--026` (code-quorum) — osmani personas as PRE-REVIEW lens checklists + mechanical skip threshold.

**Inputs that were found, not reconstructed**: the d94ce3c4 scout reports (osmani, pocock, superpowers) and the Fable meta-learning design review were recovered from the subagent transcripts under `~/.claude/projects/C--Users-Martin-Workspace/d94ce3c4-*/subagents/`. wayfinder `SKILL.md` fetched verbatim. gbrain formulas read from `garrytan/gbrain` source (server unreachable).

**Decisions recorded** in `~/.claude/decisions.md` (four rows dated 2026-09-02): wayfinder adopted into clawpm as a task kind; openspec WONT-DO upheld with a sharpened revisit trigger; no persona system (code-quorum lenses instead); calibration formulas defined once in clawpm and shared with the gbrain bridge.

**Execution order for the Sonnet session**: `clawpm next --project clawpm` will surface parallel_group 1 leaves first. 111-001/002 and 112-001/002 are independent and can run as two parallel pairs. Each leaf is one PR with tests through code-quorum. Read the ADR (for 111) or the spec (for 112) before starting a leaf — the task bodies carry the rubric, the docs carry the design.

**Open items not ticketed**: OpenSpec-style spec deltas folded into `.project/SPEC.md` on close (fog on CLAWP-111); retire/redefine `confidence` if it still shows no discrimination against `held` after 112-004 (research entry, not a leaf); gbrain reconciliation once reachable (cognition-layer COGNI-007, not clawpm).
