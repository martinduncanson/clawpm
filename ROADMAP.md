# clawpm Roadmap

Forward-looking notes. Captures ideas that came out of dogfooding but aren't on the immediate build list. Each entry has:
- **Trigger** — what conditions would justify building this
- **Sketch** — rough shape
- **Inspiration** — where to crib from

This is a living document. Items that get built move to CHANGELOG; items that get explicitly rejected stay here with a `❌ Rejected` tag and the reason.

---

## Reflection layer Phase 2 — calibration analytics

Already gated on data accumulation. Review fires **2026-06-05** (see `upskilling/skill-build-log.md`).

### `clawpm reflect lookup <title>`
**Trigger:** ≥20 task reflections in the corpus.
**Sketch:** find 3-5 most-similar past tasks via keyword/embedding match on title + scope; return their actuals as reference-class anchor for the new task. Closes the "I never know what to use as `--reference-task`" gap.
**Inspiration:** Bent Flyvbjerg's reference-class forecasting; DSPy's example retrieval.

### `clawpm reflect calibration`
**Trigger:** ≥20 reflections, 4+ weeks of data.
**Sketch:** compute bias factors per complexity bucket, per project, per filled_by attribution. Output: "complexity=m tasks: median 1.8x over"; "agent-filled predictions: 0.4 calibration"; "operator-edited: 0.85 calibration". Drives the "Claude estimated 3 days, historical correction 0.3x, so 1 day" UX.
**Inspiration:** Tetlock superforecasting; conformal prediction.

### `clawpm reflect process-lessons`
**Trigger:** ≥10 reflections with `process_lesson` filled.
**Sketch:** aggregate process_lesson entries, dedupe near-duplicates (cosine sim or fuzzy match), output operator's evolving top-10 prediction rules. Feeds back into agent prompt for next task ("before estimating, consider these prior lessons:").

### `clawpm reflect field-usage`
**Trigger:** **2026-06-05 review checkpoint** (mandatory).
**Sketch:** scan recent tasks + reflections, report per-field adoption rate. Auto-tag fields <20% as "candidate for removal". Drives the walk-back protocol committed to in skill-build-log.

### `clawpm project reflect`
**Trigger:** any project hits completion / pause / milestone.
**Sketch:** aggregate task reflections within the project + spec.md goal hypothesis. Output: goal-trace (which tasks served the goal, which were noise), aggregate prediction-vs-reality, next-project priors. Macro-experiment reflection.

---

## Inbox — built (Phase 1.7)

✅ `clawpm inbox send/read/ack/thread`. Filesystem-first, append-only, no daemons. ~140 LOC.

---

## Orchestration primitives — exploratory

Not on the build list. Codify the gap, don't fill it speculatively.

### Fan-in collection — `clawpm collect <parent-task> --from <subagent-id>...`
**Trigger:** Multi-subagent dispatches where the parent needs to merge results.
**Sketch:** after subagents complete, parent runs `clawpm collect` which reads each child's reflection events + final inbox messages, returns a structured JSON aggregate. Reduces ad-hoc result-aggregation logic in parent agents.
**Inspiration:** LangGraph's "send" node; Temporal's workflow fan-in.

### Retry / circuit-breaker for subagent calls
**Trigger:** Real signal that subagent calls fail flakily (>5 retries observed in any week).
**Sketch:** wrap `clawpm tasks add` for subagent dispatch with retry semantics. Track attempts in the task itself. Circuit-break after N consecutive failures.
**Inspiration:** Temporal's activity retry policy; Inngest step retries.

### Leader election for converging subagent decisions
**Trigger:** Real case where 3+ parallel subagents need to converge on a single decision (e.g. which approach wins).
**Sketch:** lightweight voting via inbox + a `clawpm decide --task <id> --quorum N` command that collects votes and posts the winner.
**Inspiration:** Raft (heavyweight); simpler: explicit operator-as-tiebreaker pattern.

### tmux integration
**Trigger:** When operator regularly drives 3+ parallel agent shells.
**Sketch:** `clawpm task start` opens `tmux new-window -n <task-id>`. `clawpm log tail --follow` in a side pane. `tmux capture-pane` on done saves terminal artifact.
**Inspiration:** byobu / tmux-resurrect persistence.
**❌ Not now.** Most agent dispatches are direct subagent calls within CC, not separate shell processes. Premature to optimize for the tmux case until the pattern emerges.

### DAG-based orchestration (full)
**❌ Rejected if scope.** If this ever feels needed, use Temporal or Prefect — don't reinvent. clawpm can be the backing store for task state, but should not be the orchestrator.

---

## Substrate hardening — small wins

Items observed in red-team but not yet built. Each is ~30-80 LOC.

### Operator-vs-agent attribution beyond predictions
**Trigger:** Phase 2 calibration shows agent-filled predictions are much worse than operator predictions.
**Sketch:** extend `filled_by` to work_log entries, reflections, issues — track provenance everywhere.

### `clawpm reflect retroactive-predict <task-id>`
**Trigger:** Want to back-fill predictions on the 6 known-empty OPENW reflections + similar.
**Sketch:** prompt agent (or operator) to retroactively estimate what the prediction WOULD have been, store with `filled_by: "retroactive"`. Weighted lower than real-time predictions but still better than null.

### SessionStart drift report
**Trigger:** Have a `repo-pipeline` skill that fires on session start. **OR third observation of stale-worktree leakage** (see `feedback-agent-worktree-leakage.md` — already 2 occurrences).
**Sketch:** at session start in a project dir, automatically run `clawpm doctor` + `clawpm inbox read --unacked` + `clawpm review-check --all` + `git fetch && check-HEAD-recency`, surface a single 3-5 line summary. The "what changed since I last looked at this" digest. The stale-worktree check is the highest-value piece — code subagents have lost wall-clock time twice now starting on weeks-old commits.

### "Already done?" check before task add
**Trigger:** Repeated instances of tasks describing work that's already partially or fully implemented (FT-22-1 / FT-22-13 was the canonical example: "implement run_sweep_loop" was described as new work but was prior-implementation + just needed wiring).
**Sketch:** before `clawpm tasks add` for substantive code work, grep the codebase for named functions/systems in the title. If hits, narrow the task description to the actually-missing piece (wiring, flag, doc, test) rather than "implement". Could be enforced via SKILL.md guidance OR a `clawpm add --check-prior` flag that surfaces matches.
**Inspiration:** discovery before delivery — the Phase 1.5 predictions ask "what do you know going in?"; this would extend to "what already exists in the repo?".

---

## Frameworks worth studying (not adopting)

Reference points for ideas, NOT candidates for direct adoption.

| Framework | Studied for |
|---|---|
| **LangGraph** | State-machine DAG modeling; fan-out/fan-in primitives |
| **CrewAI** | Role/goal/process abstraction for specialist agents |
| **AutoGen** | Agent-to-agent conversation semantics; group chat |
| **MetaGPT** | Role-decomposition for software teams |
| **Temporal** | Durable execution; retry/fan-in/leader-election as first-class |
| **Inngest** | Lighter durable workflow alternative |
| **DSPy** | Modular LLM-program composition; example retrieval |

**clawpm is not trying to be any of these.** It's a persistent state substrate. If/when orchestration tooling becomes genuinely necessary, use one of these as the orchestrator with clawpm as backing store.

---

## Doctrine notes

These aren't features — they're constraints that should hold across all future builds.

1. **Event-source discipline.** Never rewrite history. Acks, voids, deletes are all new events.
2. **Filesystem-first, no daemons.** A CLI tool a human can `cat` is a CLI tool that survives reboots, compaction, server moves.
3. **Default values match existing conventions.** New fields default to None or appropriate empty; old data loads.
4. **Backward compat indefinitely.** Reflection events from Phase 1 must read in Phase 9.
5. **The 4-week review checkpoint is non-negotiable.** Anything shipped is candidate for removal if it fails to earn its keep.
6. **Adoption rate is the truth.** A field nobody fills in is worse than a field that doesn't exist. The honest cull mechanism is what makes the ship-fast-and-iterate pattern viable.

---

## Phase summary (chronological)

| Phase | Shipped | Notes |
|---|---|---|
| 0 (upstream baseline) | Original clawpm by malphas-gh | 2026-02-20 |
| 1 (fork init + bug fixes) | TOML/backslash fix, test fixture fix, AGENTS.md template, Cowork skill | 2026-05-05 |
| 1a | scope field + `clawpm conflicts` | 2026-05-05 |
| 1b | Reflection layer Phase 1 (predictions/actuals/deltas/notes) | 2026-05-05 |
| 1c | Unicode/cp1252 + ID-collision fixes | 2026-05-08 |
| 1d (v1) | Calibration loop fixes: duration units, subtask isolation, files_changed filter, unblock action, re-start warning | 2026-05-08 |
| 1.5 | Applied-science fields: success_criteria, approach, unknowns, confidence, reference_tasks, pre_mortem, process_lesson, surprise_taxonomy | 2026-05-08 |
| Defacto-default | Global CLAUDE.md + SKILL.md doctrine | 2026-05-08 |
| 1.6 (in flight) | `clawpm doctor` checks, `clawpm reflect void`, `filled_by` field | 2026-05-08 |
| 1.7 (in flight) | `clawpm inbox` — inter-agent messaging | 2026-05-08 |
| 2 (gated) | Calibration analytics (lookup/calibration/process-lessons/field-usage/project reflect) | After 2026-06-05 review |
