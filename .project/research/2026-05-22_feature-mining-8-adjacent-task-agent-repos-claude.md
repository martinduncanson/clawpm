---
created: '2026-05-22'
id: clawpm-research-feature-mining-8-adjacent-task-agent-repos-claude
status: open
tags:
- feature-mining
- adjacent-repos
- goal-primitive
type: investigation
---
# Feature mining: 8 adjacent task/agent repos + Claude /goal docs

## Question

Which patterns from swarma, ralph-orchestrator, malphas/ralph, ralphy, guild, ClawTeam, task-magic, gsd-redux + Claude /goal docs are worth adopting into clawpm?

## Summary

(To be filled in as research progresses)

## Findings

...

## Conclusion

...
# Research: Feature mining from 8 adjacent repos + Claude /goal docs

**Date:** 2026-05-22
**Task:** CLAWP-015
**Sources:** swarma, ralph-orchestrator, malphas/ralph, ralphy, guild, ClawTeam, task-magic, gsd-redux, + goals-short.txt, goals-long.txt, code.claude.com/docs/en/goal

## Per-repo verdict matrix

| Repo | Stars | Storage | Standout mechanism | Verdict |
|---|---|---|---|---|
| glitch-rabin/swarma | 174 | SQLite + TSV + md | Hypothesis→experiment→verdict→ratchet loop with 20%-threshold | STEAL-FROM |
| mikeyobrien/ralph-orchestrator | 2881 | JSONL event bus | Wave correlation + thrashing/abandonment heuristic | READ-DEEPER |
| malphas-gh/ralph | 1 | Filesystem (`.progress.md` suffix) | STOP file protocol + crash recovery via filename | STEAL-FROM |
| michaelshimeles/ralphy | 2869 | YAML/MD + worktrees | Parallel groups + git-worktree auto-merge with AI conflict resolution | READ-DEEPER |
| mathomhaus/guild | 265 | SQLite (`~/.guild/`) | Atomic quest claim + dependency cascade auto-unblock + typed lore (BM25+vector) | STEAL-FROM |
| HKUDS/ClawTeam | 5237 | FS inbox w/ fcntl locks | Leader-spawn pattern + pluggable transport + DLQ | READ-DEEPER |
| iannuttall/task-magic | 240 | `.ai/tasks/*.md` + master TASKS.md | Dual sync (master checklist ↔ task YAML) + dot-notation subtasks | STEAL-FROM |
| gsd-build/gsd-redux | 31 (fork; orig abandoned) | `.planning/` w/ phase-keyed dirs | Verification gates (Confirm/Quality/Safety/Transition) + fresh-context dispatch | READ-DEEPER |

## Ranked adoption candidates (impact × fit)

### A. Dependency cascade auto-unblock — HIGH
**Source:** guild (`cascade.go` `findNewlyUnblocked()`), malphas/ralph (`task next` respects depends DAG), task-magic (deps as gate).

**clawpm gap:** Tasks already have `depends_on` in frontmatter (see CLAWP-007 → CLAWP-006), but no machinery auto-promotes a `blocked`-on-dep task back to `open` when its dep clears. Today the operator has to know.

**Adoption sketch:** On any task `done` event, scan sibling tasks where `depends_on` includes the completed ID; if all deps now done, transition `blocked` → `open` and emit a `cascade_unblock` work_log event. Cheap, deterministic. Pair with `clawpm doctor` check "stale-blocked": task is in `blocked/` with no remaining open deps for >24h.

**Predicted value:** Removes a manual step that operators forget. Quantifiable via reflection-events: count of tasks restarted within 1h of cascade. **Confidence: 4** (mechanism is small and bounded; guild's Go impl is ~40 LOC to port to Python).

---

### B. Verification gates as a state-machine extension — HIGH
**Source:** gsd-redux (Confirm / Quality / Safety / Transition gates per phase).

**clawpm gap:** State machine is `open | progress | done | blocked`. No way to express "implementation complete but blocked on review gate" without conflating it with `blocked` (which currently means external dependency). The PR-review and codex-review workflows are a poor fit for plain `progress`.

**Adoption sketch:** Add `gate` field to task frontmatter — values `pre-review | pre-commit | pre-pr | pre-merge | none`. Independent of state. Surface in `clawpm next` so the operator sees "CLAWP-099 is `progress + gate=pre-review` — pending Codex". Hook the existing skill suggestions table (commit-commands, codex-review, pr-review-toolkit) onto specific gate transitions. Reflection-events capture gate-fail rate per task type.

**Predicted value:** Closes the loop with the existing reviewer triangle doctrine (CLAUDE.md "After commit, before PR"). Today that doctrine is prose; this turns it into queryable state. **Confidence: 3** (design choice on whether `gate` is orthogonal axis or sub-state needs operator call).

---

### C. Parallel-group YAML for subagent dispatch — HIGH
**Source:** ralphy (`parallel_group: N` semantics — group N runs together, N+1 only after N completes).

**clawpm gap:** Scope-aware dispatch (`clawpm conflicts`) catches collisions but doesn't *order* batches. Subtasks under a parent have no inter-subtask ordering primitive beyond `depends_on` (which is binary, not group-batched). Today launching 4 parallel subagents requires the operator to hand-pick non-conflicting subtasks.

**Adoption sketch:** Add `parallel_group: N` to subtask YAML. `clawpm next --batch` returns the next group whose scope set is pairwise-non-overlapping (assertion, not heuristic) and emits a dispatch manifest. `clawpm conflicts --batch <group>` validates pre-dispatch. Works with the existing inter-agent inbox: parent agent dispatches a group, each subagent receives its subtask-ID via inbox.

**Predicted value:** Operator goes from "manually picking 3 non-conflicting subtasks" to "dispatch group 1". Directly extends the existing scope-conflict pre-flight from N=2 (pre-flight one task) to N=k (pre-flight a whole group). **Confidence: 4** (mechanism is a topological-sort + scope-intersect; ~80 LOC).

---

### D. Hypothesis-verdict threshold (measurable success-criteria) — MEDIUM-HIGH
**Source:** swarma (20% improvement → keep, 20% decline → discard, between = inconclusive; auto-ratchet validated patterns into `strategy.md`).

**clawpm gap:** `success_criteria` today is freeform strings. They aren't evaluated. Reflection-events compare predicted vs actual duration / complexity, not whether the *contract* held. CLAWP-007's success-criteria (e.g. "Generator runs in <60s") sits in frontmatter unmeasured.

**Adoption sketch:** Extend success-criteria to a structured form: `{metric, threshold, comparator}` (e.g., `{metric: "p95_latency_ms", threshold: 200, comparator: "lt"}`). On `done`, prompt for measured value (or accept `--measured-value` flag). Reflection-event records `met | unmet | inconclusive`. Pair with `process_lesson` accumulation: surface "your <200ms latency contracts hit 60% of the time" in `clawpm reflect summarize` (Phase 2 stub becomes load-bearing here).

**Predicted value:** Turns success-criteria from doc-prose into a calibration corpus. The 20%-threshold trick is the contribution — it gives "inconclusive" a first-class slot, which honest reflection needs. **Confidence: 3** (schema is straightforward; the friction is operator habit of typing measured values at done-time).

---

### E. Mission Control / mixed-actor decomposition — MEDIUM-HIGH
**Source:** Claude `/goal` long-form (goals-long.txt) — 4-week binary outcomes, 4–10 mini-goals each tagged `agent|human`, POSTed to a dashboard.

**clawpm gap:** clawpm tasks are agent-or-operator-implicit. There's no `actor` axis. Personal-life-adjacent goals (the kind the long-form is built for) don't fit cleanly today — clawpm wants code-bearing work. Also: clawpm has a Phase-2 stub `clawpm project reflect` but no `project mission` primitive for 4-week macro-experiments.

**Adoption sketch:** Add a `mission` layer above `project`:
- `clawpm mission add --title "..." --binary-outcome "..." --deadline-days 28`
- Mini-goals are subtasks of the mission with `actor: agent | human` tag
- `clawpm mission status` shows progress against binary outcome
- Optional `actor: human` tasks don't show up in `clawpm next` for agent dispatch — they show in a separate `clawpm mission inbox` view for the operator

This dovetails with the existing project-level reflection (macro-experiment) stub. The `/goal` single-session primitive maps cleanly to a clawpm task with `predict-duration <2h, confidence>=3, scope set, success-criteria binary`. Could ship a `clawpm task emit-goal-prompt <id>` helper that produces a `/goal` slash-command string from a task.

**Predicted value:** Brings clawpm into the personal-life-goal-tracking space without compromising its code-work core. Mixed actors are the bridge. **Confidence: 2** (mission layer is a meaningful scope expansion; needs operator validation that this is wanted before building).

---

### F. STOP file protocol + retryable/fatal error classification — MEDIUM
**Source:** malphas/ralph (STOP file with `DONE:` / `BLOCKED:` / `CLARIFY:`), ralphy (`isFatalError` vs `isRetryableError`).

**clawpm gap:** When a subagent finishes a subtask, clawpm has no canonical contract for *how* the subagent signals completion vs needs-input. Today it's prose in the inbox. The reflection-events `surprise` taxonomy classifies the *kind* of surprise but not whether the failure was retryable.

**Adoption sketch:** Add `subagent_exit` field to inbox messages: `done | blocked-retryable | blocked-fatal | clarify-needed`. Subagent SDK helper writes this on exit. Parent agent consumes via `clawpm inbox read --unacked` and routes accordingly (retry the same subagent vs reassign vs surface to operator). Abandonment heuristic: 3 retryable-blocks on the same subtask → auto-mark `blocked + tag=thrashing`.

**Predicted value:** Closes a workflow gap that today requires prose-parsing of inbox messages. Pairs with the existing `--surprise external_blocker` reflection tag — completes the loop. **Confidence: 3**.

---

### G. Dual-sync master checklist — LOW-MEDIUM
**Source:** task-magic (`.ai/TASKS.md` as human-readable index kept in lockstep with per-task YAML).

**clawpm gap:** No project-level human-scannable index. `clawpm tasks list -f text` is one-shot; there's no file you can open in an editor and scan-edit. Operators ask "what's open in this project" by running the CLI.

**Adoption sketch:** `clawpm tasks index` regenerates a `.project/TASKS.md` from current YAML state. Read-only — edits go through the CLI. Useful as a git-tracked artifact for PR context (Codex / PR-Agent already read the diff; an updated TASKS.md gives reviewers project context for free).

**Predicted value:** Marginal — solves a "discoverability" problem more elegantly via a markdown index than via CLI commands. **Confidence: 4** (mechanism is trivial). Lower priority because clawpm context / clawpm status already serve this for agents; the gain is for human-editor browsing.

---

## NOT recommended (with reason)

- **Pluggable inbox transport** (ClawTeam) — clawpm's "filesystem-first, no daemon" is a load-bearing design principle. Adopting Redis/network transport breaks the survives-reboot-without-config promise. Skip.
- **SQLite for everything** (guild, swarma) — clawpm's JSONL + markdown is queryable enough and stays diff-friendly for git. The atomic-claim guarantees SQLite offers are nice but the scope-conflict heuristic is honest about its limits — operator gets a clear false-positive bias rather than a silent race. Skip unless conflicts become routine.
- **Wave correlation IDs** (ralph-orchestrator) — premature for clawpm's current scale. Add when N>10 parallel subagents per task becomes routine.
- **Cost/model-profile routing in task YAML** (swarma, gsd-redux) — interesting but cross-cuts skill-routing, which already lives in CLAUDE.md doctrine + skill descriptors. Better to keep the cost decision in the skill layer than push it into task metadata.

## Recommended next moves (clawpm task candidates)

If the operator wants to act on this, propose these tasks in priority order:

1. **CLAWP-NEW-A: Dependency cascade auto-unblock** (s, ~2h) — small, deterministic, immediately useful.
2. **CLAWP-NEW-B: `parallel_group: N` for subtask dispatch** (m, ~4h) — extends the existing scope-conflict pre-flight, high subagent-workflow leverage.
3. **CLAWP-NEW-C: Structured success-criteria with measured value at done** (m, ~3h) — turns the success-criteria field from prose into calibration data.
4. **CLAWP-NEW-D: Subagent exit contract via inbox** (s-m, ~3h) — closes the retryable/fatal classification gap.
5. **CLAWP-NEW-E: `clawpm mission` layer with mixed-actor mini-goals** (l, ~8h) — requires operator confirmation that the macro-experiment scope is wanted before building.
6. **CLAWP-NEW-F: Verification gates as orthogonal state axis** (m, ~4h) — design call needed first on orthogonal-field vs sub-state.

Lower priority / on-deck:
- TASKS.md regeneration (xs, ~1h)
- STOP-file convention for skill-side workflows (xs, ~1h documentation)

## Notes on /goal docs specifically

The short-form `/goal` primitive (binary outcome, 20-turn cap, no leading whitespace, agent-agnostic) is **already substantially expressed** by a well-formed clawpm task with `predict-duration <2h`, `confidence>=3`, populated `success_criteria` and `scope`. The literal-prefix rule (`/goal ` first 6 chars) is a slash-command artifact of Claude Code 2.1.139+, not a generalisable mechanism.

The valuable adaptation is the **emit-prompt helper**: `clawpm tasks emit-goal-prompt <id>` could deterministically produce a `/goal …` string from a task's frontmatter. Tiny feature, high operator delight. Pair with the mission layer (mini-goals → `/goal` prompts per agent-tagged mini-goal).

The long-form Mission Control is the genuine novelty over what clawpm has today — see candidate E.

---

## L3 self-select decisions made during this work

None — task was straightforwardly scoped by the operator's prompt.

---

## ADDENDUM — Piebald /goal system-prompts analysis (same day)

Operator surfaced the Piebald-AI reverse-engineering of Claude Code 2.1.143 `/goal`. Material updates to the analysis above:

### What `/goal` actually is

NOT a slash-command prompt template (that's the *user-facing skill* the goals-long.txt encoded). The actual primitive is:
- `user.define_outcome` event (Managed Agents Outcomes API, `managed-agents-2026-04-01` beta) carrying `description` + `rubric` (REQUIRED, markdown with explicit gradeable criteria) + `max_iterations` (default 3, max 20).
- Independent grader in a separate context window, emitting `span.outcome_evaluation_end` with `satisfied | needs_revision | max_iterations_reached | failed | interrupted`.
- Stop-hook condition evaluator (small LLM judge) returning `{ok, reason}` / `{ok: false, impossible: true, reason}`. Critical doctrine: *"the assistant claiming the goal is impossible is evidence, not proof; independently confirm"*.
- Hooks system: PreToolUse | PostToolUse | Stop | PreCompact | SessionStart, with `prompt` and `agent` hook types.

### Local emulation vs paid Managed Agents

clawpm targets LOCAL emulation. The Managed Agents API is paid per-token; Claude Code's hooks + sub-sessions give us the same iterate→grade→revise primitive locally for free (already covered by Claude Code subscription). Same rubric format works on both — clean upgrade path without lock-in.

### Revised top 6 candidates (replaces ranking above)

1. **Rubric-shaped success-criteria + `clawpm tasks emit-rubric <id>` helper** (subsumes old D) — Schema `{criterion, gradeable_signal, comparator}`. Emit as markdown rubric compatible with `user.define_outcome` OR a local Stop-hook evaluator. ~3h, confidence 4.
2. **Stop-hook condition evaluator for clawpm subagents (NEW, killer feature)** — Adopt official `{ok, reason, impossible}` JSON shape. Subagent literally cannot terminate until criteria met or impossibility independently confirmed. Hard enforcement of success-criteria contract. ~4h, confidence 3.
3. **Subagent dispatch via hooks (NEW)** — clawpm emits `.claude/settings.local.json` on dispatch, pre-loading the subtask's rubric as a Stop-condition evaluator + state-update PostToolUse hooks. Subagent doesn't need to know about clawpm; integration by construction. ~5h, confidence 3.
4. **Outcome iteration log → reflection events (NEW)** — Capture every grader cycle as a reflection event. Iteration count becomes a predict-vs-actual axis. ~3h, confidence 3.
5. **Dep cascade auto-unblock** (unchanged from old A) — ~2h, confidence 4.
6. **`parallel_group: N` for subagent dispatch** (unchanged from old C, enforcement-via-hooks adds polish) — ~4h, confidence 4.

### Folded / dropped

- Old B (verification gates as orthogonal state axis) → FOLDED into #3. Gates better expressed as hook configurations than as new frontmatter axis.
- Old F (STOP-file exit contract + retryable/fatal) → FOLDED into #2. Adopt Piebald's official JSON shape instead of inventing STOP-file. `{ok: true}` = done; `{ok: false, impossible: true}` = blocked-fatal; `{ok: false}` = blocked-retryable.
- Old E (mission layer with mixed-actor mini-goals) → STILL ON-DECK, unchanged. Distinct concern; not affected by Stop-hook analysis.

### Net positioning shift

clawpm becomes the persistence + dispatch layer over Anthropic's Managed Agents Outcomes pattern, locally emulated via Claude Code hooks. Emits rubrics, captures iteration events, enforces success-criteria via hooks at the subagent boundary. Sharper than "yet another task manager with reflection".
