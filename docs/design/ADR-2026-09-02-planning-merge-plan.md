# ADR — Merge plan: clawpm vs wayfinder / OpenSpec / feature-dev / osmani agent-skills

**Status:** Accepted 2026-09-02 (Fable planning session, CLAWP-103) · **Decides for:** clawpm core, clawpm-planner, clawpm plugin, code-quorum (one handoff) · **Implements via:** task tree emitted from `.project/plans/2026-09-02-clawp103.emit.json`

## Context

CLAWP-103 (filed 2026-07-14) asked for a gap analysis of clawpm against four planning/execution systems and an adopt/adapt/skip verdict per mechanic. Inputs used:

- The 2026-07-14 scout reports (session d94ce3c4): pocock-scout (mattpocock/skills), osmani-scout (addyosmani/agent-skills), superpowers-scout. Recovered from the subagent transcripts, not reconstructed.
- wayfinder `SKILL.md` fetched verbatim from `mattpocock/skills` (`skills/engineering/wayfinder/SKILL.md`, main, 2026-09-02).
- OpenSpec README (Fission-AI/OpenSpec, 2026-09-02).
- feature-dev plugin 1.0.0 as installed locally (`commands/feature-dev.md`, three agents).
- code-quorum `specs/spec-conformance-lens.md` (draft v0.1, 2026-07-14) and its open task CODE--021.
- `~/.claude/decisions.md` WONT-DO rows of 2026-07-14 (openspec; git-guardrails).
- clawpm source at fork/main 06a32b7: `models.py`, `emit_tree.py`, `cli/shortcuts.py` (`next`), `tasks.py` (`get_next_task`), leases, research types, clawpm-planner skill and references.

**Guardrail honoured:** every adopt verdict below extends clawpm core or clawpm-planner. Nothing here creates a second planning system, a second tracker, or a second spec store.

## The one-line verdict

clawpm already has the substrate every one of these systems lacks (predictions, calibration, rubric-gated dispatch, won't-do ledger, leases, emission API). What it lacks is wayfinder's **decide-before-build** shape: decision tickets, a fog-of-war frontier, and one-decision-per-session working. Adopt that shape as a task *kind* inside clawpm. Everything else is either already covered, routed to code-quorum, or skipped.

## Verdict table

| # | System · mechanic | Verdict | Lands in | Why |
|---|---|---|---|---|
| W1 | wayfinder · decision tickets (question → decision, not a build slice) | **Adapt** | core: `kind: decision` on Task; `done --resolution` | clawpm tasks already have depends, leases (claim), states, dispatch, delegability; only the "resolves a decision" semantics and the resolution record are missing. Research entries lack depends/leases so they are the wrong host. |
| W2 | wayfinder · the map (Destination / Decisions so far / Not yet specified / Out of scope) | **Adapt** | core: root task fields `destination`, `not_yet_specified`; body sections auto-maintained | Root task + PRD research entry already play the map role in planner output. Add the two missing sections; `out_of_scope` exists at leaf level, promote to root. |
| W3 | wayfinder · fog of war (don't ticket what you can't phrase sharply yet; graduate on resolution) | **Adopt** | core: `not_yet_specified` list + `tasks add --parent <root> --graduates "<fog text>"` | Directly fixes the planner's known regression risk (over-planning). Test is wayfinder's own: "can you state the question precisely now?" |
| W4 | wayfinder · frontier = open + unblocked + unclaimed children | **Adopt** | core: `clawpm next --frontier <root>` / `tasks list --frontier <root>` | `get_next_task` already honours depends; leases already exist. One query away. |
| W5 | wayfinder · one ticket per session (research excepted) | **Adapt** | clawpm SKILL.md working-the-map rule; leases enforce claim | Session-scope rule; the claim IS the lease. No new state. |
| W6 | wayfinder · charting vs working phases | **Adapt** | clawpm-planner: **chart mode** (emit a decision map, then stop) | Planner rule 5 (emit then PAUSE) is already charting's "stop". Chart mode = when ideate surfaces open decisions that block decomposition, emit decision leaves instead of build leaves. |
| W7 | wayfinder · ticket types research/prototype/grilling/task | **Adapt** | `kind: decision` + existing `delegability` (`agent` = AFK research, `human` = HITL grilling/prototype, `either` = task) | No new enum. HITL rule ("the agent never answers its own grilling questions") goes in SKILL.md. |
| W8 | wayfinder · refer by name, never bare ids | **Adopt** | clawpm SKILL.md output rule | Cheap, real legibility win in narration. |
| W9 | wayfinder · tracker-native blocking edges rendered in the tracker UI | **Skip** | — | clawpm `depends` is the edge; GitHub-issue distribution is CLAWP-058 (deferred). |
| W10 | wayfinder · blocking edges wired at charting time | **Adopt** | emit-tree: leaf `depends_refs` | Gap found: `ALLOWED_LEAF_KEYS` has no dependency field; decision maps need it. Resolve refs → minted ids in the same transaction. |
| O1 | OpenSpec · as a parallel spec system (specs/ changes/ archive/, /opsx:* commands) | **Skip — WONT-DO upheld** | — | Re-examined against the 2026-07-14 revisit clause ("only if clawpm's spec story proves too thin"). Spec story today = PRD research entry + structured `success_criteria` + code-quorum spec-conformance lens (spec'd, CODE--021 open). Not thin enough to justify a second store. Revisit trigger sharpened below. |
| O2 | OpenSpec · requirement phrasing "WHEN … THEN …" | **Adapt** | clawpm-planner `decompose-vet.md`: recommended `criterion` phrasing | Makes `success_criteria` gradeable by the Stop-hook judge and by the spec lens. Zero code. |
| O3 | OpenSpec · spec deltas (ADDED/MODIFIED/REMOVED) folded into a canonical spec on archive | **Skip (fog)** | — | Real gap: `.project/SPEC.md` is static and nothing updates it on task close. Not sharp enough to ticket; recorded as fog on the CLAWP-103 root. |
| F1 | feature-dev · 7-phase HITL command (discover → explore → questions → architect → approve → implement → review) | **Skip** | — | Three mandatory approval stops; no predictions, no rubric, no ledger. clawpm-planner + dispatch + code-quorum cover the lifecycle. |
| F2 | feature-dev · parallel `code-explorer` fan-out with "return 5–10 key files, then read them" | **Adapt** | clawpm-planner `stage-playbook.md` recon stage | Name the installed `feature-dev:code-explorer` agent type as the recon fan-out when present; keep Explore as fallback. Docs only. |
| F3 | feature-dev · three architect lenses (minimal / clean / pragmatic) compared with a recommendation | **Adapt** | clawpm-planner `personas.md` → "approach lenses" for l/xl specify | Sharper than the current analyst/PM/architect lenses for software objectives. Docs only. |
| F4 | feature-dev · `code-reviewer` agents | **Skip** | — | code-quorum owns review. |
| A1 | osmani · /spec → /plan → /build → /test → /review → /ship chain | **Skip** | — | Strictly shallower than clawpm (no predictions, confidence, reflection, won't-do ledger). Confirmed by their own comparison doc. |
| A2 | osmani · personas (code-reviewer, security-auditor, test-engineer, web-performance-auditor) | **Adapt → code-quorum, not clawpm** | code-quorum: lens checklists in the PRE-REVIEW local pass | Answers the task's persona question: **no persona system.** The personas are checklists; code-quorum's local Sonnet pass is where a checklist earns its keep. security-auditor's STRIDE + OWASP-LLM section and test-engineer's scenario table are the material. Also closes the open question in `spec-conformance-lens.md`. Handoff leaf emitted to the code-quorum project. |
| A3 | osmani · /ship mechanical skip threshold (≤2 files ∧ <50 lines ∧ no auth/payments/data/config touch) | **Adapt → code-quorum** | same leaf as A2 | Replaces prose skip conditions with a testable rule. |
| A4 | osmani · /build auto stop-list (auth, migrations, deletions, secrets) | **Adapt** | clawpm-planner `decompose-vet.md`: default `stop_conditions` seeded when a leaf's scope touches those categories | Operationalises the L4 rung inside dispatch. Docs + example. |
| A5 | osmani · interview-me (one question at a time; ~95% confidence gate; "want vs should-want"; false-yes anti-patterns) | **Adapt** | clawpm-planner `stage-playbook.md` specify stage; chart mode's destination-naming step | Replaces importing Pocock's grilling separately (superpowers-scout verdict stands: brainstorming keeps the durable-spec gate). |
| A6 | osmani · Prove-It bug pattern | **Skip (have it)** | — | Already the house rule in this repo's CLAUDE.md task-definition discipline. |
| A7 | osmani · doubt-driven-development (in-flight skeptic) | **Skip** | — | clawpm has the blind refuter (CLAWP-043) and tournament (CLAWP-044) for the post-hoc case; in-flight doubt is a code-quorum PRE-REVIEW concern, not a PM-layer one. |
| A8 | osmani · source-driven-development, context-engineering trust levels, evals TF-IDF trigger-collision | **Out of scope for clawpm** | routed: claude-config / skill-creator | Real steals, wrong repo. Listed under "Routed elsewhere" so they don't evaporate. |
| S1 | all four · slash-command ergonomics (`/spec`, `/opsx:propose`, `/wayfinder`, `/feature-dev`) | **Adapt** | clawpm plugin `commands/`: `/clawpm:plan`, `/clawpm:chart`, `/clawpm:next` | CLAWP-107 packaged the plugin with skills only. Commands are the missing user-facing verb; each is a thin router into the existing skills, not new logic. |

## Design of the adopted mechanics

### D1. `kind: decision` tasks (W1, W7)

- `Task.kind: Literal["build","decision"]`, default `build`, frontmatter key `kind`, omitted when `build` (zero diff for existing files).
- A decision task's body is the **question**. `success_criteria` default to one entry: `{"criterion": "Decision recorded with rationale", "gradeable_signal": "resolution field non-empty", "comparator": "nonempty"}` when the emitter supplies none.
- `clawpm done <id> --resolution "<text>"`: required for `kind: decision` (error `decision_needs_resolution` without it; `--force` is not offered — a decision without a resolution is not done). Stored as frontmatter `resolution:` and `resolved_at:`. For `kind: build`, `--resolution` is accepted and stored but optional.
- On done of a decision leaf with a parent: append `- [<title>](<id>): <first line of resolution>` under the parent's `## Decisions so far` section (create the section if absent). The parent is the map; the ticket holds the detail (wayfinder's "index, not store").
- Reflection: decision tasks write `task_done` events like any task. Their duration signal is HITL-contaminated; tag the event `kind: decision` so calibration can bucket or exclude them (see calibration spec §2.4).
- `delegability` carries wayfinder's labour mode: `agent` → dispatchable research (AFK), `human` → grilling/prototype (HITL, never auto-dispatched — already the rule), `either` → task.

### D2. Root map sections (W2, W3)

- Root/parent task frontmatter gains `destination: str | None` and `not_yet_specified: list[str]`. Rendered into the body as `## Destination`, `## Decisions so far`, `## Not yet specified`, `## Out of scope` (the last from existing `out_of_scope`).
- `clawpm tasks add --parent <root> --graduates "<fog text>"`: creates the child and removes the matching fog entry (exact or case-insensitive prefix match; ambiguity → error listing matches). Multiple `--graduates` allowed; one fog patch may graduate into several tickets, or none.
- `clawpm tasks fog <root> --add "<text>"` / `--drop "<text>"` for edits outside `add`. Keep the surface minimal; no fog "status".
- Wayfinder rule enforced in docs, not code: fog is coarser than a ticket; ticket when the question is sharp even if blocked.

### D3. Frontier query (W4, W5)

- `clawpm next --frontier <root-id>`: children of root (any depth) that are `open`, whose `depends` are all done, and that hold no active lease. Ordered by priority then id. JSON: `{root, frontier: [task...], blocked: [{id, waiting_on: [...]}], claimed: [{id, lease_holder}]}`. Text: names wrap ids (W8).
- `clawpm tasks list --frontier <root-id>` is the same view without the "pick one" framing.
- Claim = `clawpm start <id>` (existing lease). SKILL.md rule: **one decision leaf per session**; `agent`-delegable research leaves may be dispatched in parallel from the same session.

### D4. Emission contract additions (W10, D1, D2)

- Leaf: `kind` (`build`|`decision`), `depends_refs: list[str]` (refs of sibling leaves in the same document; resolved to minted ids before promote; unknown ref → validation error; cycles → validation error).
- Root: `destination`, `not_yet_specified`.
- `attach_to` re-emits may reference existing task ids in `depends_refs` as `"id:CLAWP-123"`.
- Contract doc + a new validated example `examples/decision-map.emit.json` (chart-mode output).

### D5. clawpm-planner chart mode (W6, F2, F3, A4, A5, O2)

Docs-only changes in `skills/clawpm-planner/`:

- `SKILL.md`: add **chart mode**. Trigger: after ideate, if ≥2 open decisions block decompose (or the operator says "chart this"), run specify on the *destination* only (A5 interview discipline: one question at a time; facts looked up, decisions asked; reject "sounds good"/silence as confirmation), then emit a **decision map**: root with `destination` + fog, `kind: decision` leaves with `depends_refs`, `agent` research leaves dispatchable immediately (wayfinder step 5), and stop. If charting surfaces no fog, say so and offer plain decompose (wayfinder's own exit).
- `stage-playbook.md`: recon names `feature-dev:code-explorer` as the fan-out agent when installed, with the "return 5–10 key files, then read them" rule; specify gets the interview discipline; ideate/specify on l/xl compare three approach lenses (minimal / clean / pragmatic) and recommend one.
- `decompose-vet.md`: recommend `WHEN <condition> THEN <observable>` phrasing for `criterion`; add the **default stop-list** — any leaf whose scope touches auth/session, schema migrations, bulk deletion, secrets/credentials, payment, or prod config gets a seeded `stop_conditions` entry ("STOP and surface to the operator before <category> change") unless the operator has waived it in the PRD.
- `scale-dial.md`: chart mode sits between m and l; s never charts.

### D6. Plugin commands (S1)

`commands/` in the plugin: `/clawpm:plan <objective>` → invoke clawpm-planner; `/clawpm:chart <idea>` → clawpm-planner chart mode; `/clawpm:next [root]` → `clawpm next --frontier` when a root is given, else `clawpm next`. Each command is ≤15 lines of routing prose. `marketplace.json` lists `commands`.

## Routed elsewhere (not clawpm; recorded so they don't evaporate)

| Item | Route | Note |
|---|---|---|
| osmani personas as code-quorum lens checklists + /ship skip threshold (A2, A3) | code-quorum project, leaf emitted from `.project/plans/2026-09-02-codequorum-handoff.emit.json` | Resolves the open question in `specs/spec-conformance-lens.md`. |
| source-driven-development (fetch the version-specific official doc before implementing) | claude-config skills | Standalone skill; nothing in the stack does this. |
| context-engineering trust levels + Confusion Management block | claude-config CLAUDE.md | Trust tiers already exist in CLAUDE.md security digest; the lettered-options confusion block is the addition. |
| evals TF-IDF trigger-collision detection | skill-creator | skill-creator has no collision check today. |
| OpenSpec-style spec deltas folded into `.project/SPEC.md` on close (O3) | fog on CLAWP-103 root | Not sharp enough to ticket. |

## Revisit triggers

- **OpenSpec WONT-DO** (2026-07-14, upheld 2026-09-02): revisit only if, once the spec-conformance lens (CODE--021) is live, it reports `NO_SPEC` on more than half of reviewed PRs in a calendar month, or if planner-emitted PRDs are found to diverge from `.project/SPEC.md` with no mechanism to reconcile. Either is evidence the spec story is too thin; neither exists today.
- **Persona system**: revisit only if code-quorum's lens checklists prove insufficient because a lens needs *state* across reviews (a persona with memory), which a checklist cannot carry.
- **Decision maps as GitHub issues** (W9): revisit with CLAWP-058.

## Consequences

- Positive: planning gains a decide-first shape without a second tracker; the won't-do ledger, predictions and leases apply to decisions for free; the frontier query makes multi-session efforts legible.
- Negative: two new task fields and one new task kind widen the schema surface that `tasks edit` and `emit-tree` must keep in sync (CLAWP-108 shows that seam is already fragile — the edit path must be fixed or covered by tests in the same PR that adds fields).
- Neutral: decision tasks add HITL-contaminated duration data; the calibration spec buckets them out.

## Pre-mortem

If this fails in 12 months, the likely cause is that chart mode was never used because `/clawpm:chart` and the frontier view were shipped but the planner kept defaulting to build leaves. Mitigation: the chart-mode trigger is written as a rule in SKILL.md, not an option, and the decision-map example is CLI-validated so the shape is copyable.
