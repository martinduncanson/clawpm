# ClawPM reference: predictions and reflection

Moved verbatim from `SKILL.md`. Covers: Reflection loop, prediction flags and units, completion flags, worked examples, reflect void and calibration queries, project-level reflection.

## Reflection — tasks as micro-experiments

Each task is a small applied-science loop:
- **BEFORE**: hypothesis, success contracts, predicted approach, known unknowns, pre-mortem, confidence
- **DURING**: work happens
- **AFTER**: actuals computed, deltas surfaced, process lesson extracted

The point isn't bureaucracy — it's calibration. Predictions you never check are
fantasy. Predictions you check and revise are skill.

ClawPM stores predictions at task creation and computes actuals at completion,
writing an append-only JSONL event to `~/clawpm/reflections/<task-id>.jsonl`.

### The two-layer reflection loop

**LAYER 1 — descriptive**: what happened vs what you predicted?
- predictions vs actuals, deltas computed automatically
- `--reflect-note "what surprised you"` for freeform capture

**LAYER 2 — prescriptive**: what update to your prediction PROCESS would have caught this?
- `--process-lesson "what rule of thumb does this teach for next time"`
- These accumulate. Over 20+ tasks they become your personal calibration manual.

> **The recursive loop matters more than any single prediction.** A single missed
> prediction teaches one thing. A `process_lesson` that says "I always underestimate X"
> teaches *future predictions*. Over time, the lessons compound into calibration.
> **Always fill in `--process-lesson` on done events — it's the rung that lets you climb.**
>
> **Confidence is data.** A confident wrong prediction is worse than an uncertain wrong
> prediction. Mark confidence honestly. 1 = "I made it up", 5 = "I have done this exact
> thing before and know the actuals". Most predictions are 2-3. Be honest.
>
> **Reference class > intuition.** Before predicting, look at 1-3 prior similar tasks.
> Pass them via `--reference-task`. Empty `reference_tasks` = "I used the inside view
> only" — that's signal too. Phase 2 will surface reference candidates automatically;
> for now do it manually.

**Claude's mandate (full text in `SKILL.md`): when Claude adds a task on the operator's behalf, ALWAYS include predictions.**

The operator has specifically requested this: Claude's time estimates are
systematically biased (often 10-100× over actual). Capturing predictions creates
the calibration corpus needed to correct that bias.

For high-stakes or complex tasks, also include the Phase 1.5 fields:
`--success-criteria`, `--predict-approach`, `--confidence`, `--reference-task`,
`--pre-mortem`, `--unknowns`.

### Setting predictions

Add `--predict-*` flags when creating or editing a task.

`--predict-duration` accepts human-friendly unit suffixes (wall-clock, not 8-hour
workday — calibration compares elapsed time, not scheduled hours):

| Input | Stored as |
|-------|-----------|
| `90` or `90m` | 90 minutes |
| `2h` | 120 minutes |
| `3d` | 4 320 minutes (3 × 24 h) |
| `1w` | 10 080 minutes (7 × 24 h) |
| `2h30m` | 150 minutes (combined units, CLAWP-096) |

**Phase 1 prediction flags** (on `tasks add` and `tasks edit`):
```
--predict-duration      Predicted duration: 90, 90m, 2h, 2h30m, 3d, 1w
--predict-complexity    s|m|l|xl
--predict-files-changed Number of files expected to change
--predict-scope         File glob scope (repeatable)
--predict-frameworks    Frameworks/libs to touch (repeatable)
--predict-pitfalls      Anticipated problem areas (free text)
--hypothesis            Causal hypothesis: "if X then Y"
```

**Phase 1.5 prediction flags** (on `tasks add` and `tasks edit`):
```
--success-criteria      Measurable performance contract (repeatable)
                        Example: "P95 latency <200ms"
--predict-approach      Architectural choice / solution pattern (1-2 sentences)
--unknowns              What you do NOT know going in (meta-curiosity)
--confidence            Operator confidence 1-5 (1=wild guess, 5=done this before)
--reference-task        Prior task ID used as reference class (repeatable)
--pre-mortem            "If this fails, the most likely cause is..."
```

**Phase 1.6 attribution flag** (on `tasks add`):
```
--predicted-by          agent | operator | operator-edited | retroactive
                        Default when predictions present: "operator"
                        Default when no predictions: null
                        Use "operator-edited" when agent proposed + human reviewed.
                        Use "retroactive" for back-filled historical tasks.
```
`filled_by` is stored in `predictions.filled_by` and carried through to the reflection event.
Phase 2 calibration weights: `operator-edited > operator > agent > retroactive`.

All flags are optional. Tasks without predictions still work normally.

### Completing with reflection notes

When marking a task done or blocked, optionally add:

**Phase 1 completion flags**:
- `--reflect-note` — what surprised you
- `--meta-reflect` — what could have been anticipated that wasn't, and why

**Phase 1.5 completion flags**:
- `--process-lesson` — what update to your prediction PROCESS would have caught this?
- `--surprise` — multi-pick taxonomy tag (repeatable): `unknown_unknown`, `scope_drift`,
  `dependency`, `tooling_friction`, `complexity_misread`, `assumption_broke`, `external_blocker`

```bash
clawpm done CLAWP-042 \
    --reflect-note "DB migration took 3x longer than expected" \
    --meta-reflect "should have checked existing schema constraints first" \
    --process-lesson "always run migration dry-run before estimating; add 2x for constraint work" \
    --surprise scope_drift --surprise tooling_friction

# Also works on tasks state and clawpm block:
clawpm block CLAWP-042 --reflect-note "hit API rate limit" --surprise external_blocker
```

### Worked example — full applied-science framing

```bash
# Adding a task with full applied-science framing:
clawpm tasks add -t "Migrate auth from sessions to JWT" \
  --predict-duration 4h \
  --predict-complexity m \
  --hypothesis "JWT cuts session-table contention by >=50% under 100 RPS load" \
  --success-criteria "P95 login latency <200ms" \
  --success-criteria "Session table writes drop >=50% in prod logs over 7 days" \
  --predict-approach "Drop-in JWT middleware, keep session table for refresh tokens" \
  --predict-frameworks pyjwt fastapi \
  --predict-pitfalls "Constraint conflict on existing session_id column" \
  --pre-mortem "If this fails, most likely cause: cookie domain edge case in mobile webview" \
  --unknowns "Whether refresh-token rotation gives audit-grade traceability or we still need sessions" \
  --confidence 3 \
  --reference-task CLAWP-042 --reference-task ARB-P-013

# Completing it — the meta-loop:
clawpm done CLAWP-099 \
  --note "Shipped; PR #128 merged" \
  --reflect-note "constraint conflict didn't materialize but mobile webview cookie issue did" \
  --meta-reflect "I should have read the mobile auth tests before predicting" \
  --process-lesson "When auth touches mobile, ALWAYS read the mobile test suite before predicting duration" \
  --surprise tooling_friction --surprise assumption_broke
```

### Lifecycle example (minimal)

```bash
# 1. Add task with predictions
clawpm tasks add -t "Refactor auth layer" \
    --predict-duration 90 --predict-complexity m \
    --predict-scope "src/auth/**" \
    --confidence 2

# 2. Start working (auto-logged)
clawpm start CLAWP-042

# ... do work, git commits, etc. ...

# 3. Complete with reflection
clawpm done CLAWP-042 \
    --reflect-note "JWT validation added 30 extra min" \
    --meta-reflect "should have checked middleware chain first" \
    --process-lesson "JWT tasks always run 1.5x; use that as default multiplier"

# 4. Reflection event appears at:
#    ~/clawpm/reflections/CLAWP-042.jsonl
```

The reflection JSONL schema (Phase 1.5):

```json
{
  "event": "task_done",
  "task_id": "CLAWP-042",
  "project_id": "clawpm",
  "occurred_at": "2026-05-05T18:00:00Z",
  "predictions": {
    "duration_min": 90, "complexity": "m",
    "success_criteria": ["P95 latency <200ms"],
    "approach": "Drop-in JWT middleware",
    "unknowns": "Whether rotation gives audit traceability",
    "confidence": 3,
    "reference_tasks": ["CLAWP-042"],
    "pre_mortem": "cookie domain edge case in mobile webview"
  },
  "actuals":     { "duration_min": 167, "files_touched": [...] },
  "deltas": {
    "duration_ratio": 1.85,
    "complexity_match": false
  },
  "note": "...",
  "meta_reflection": "...",
  "process_lesson": "JWT tasks always run 1.5x; use that as default multiplier",
  "surprise_taxonomy": ["tooling_friction", "assumption_broke"]
}
```

### reflect void (Phase 1.6)

Marks a reflection event as bad data without deleting it (event-source discipline — append-only).
A `"void"` entry is appended to the task's `.jsonl`; Phase 2 calibration will skip voided events.

```bash
clawpm reflect void <task-id> --reason "<why this reflection is bad data>"
clawpm reflect void --all-empty-actuals --reason "corpus cleanup — no actuals recorded"
```

- `--all-empty-actuals` bulk-voids any reflection where `actuals.duration_min` is null.
- Original events are never modified; the void record is a separate appended line.
- `clawpm tasks show <id>` includes `"reflections_voided": true` when any void entry exists.

### Calibration queries (shipped — CLAWP-040)

The calibration loop is live. These read the reflection corpus and calibrate
estimates deterministically (no model call):

```bash
# Measurement half — aggregate actual/predicted duration ratios, bucketed by
# complexity / confidence / agent_profile. Rows with no usable actual are
# flagged dirty and excluded. Omit --project to span all projects.
clawpm reflect summarize [-p <project>]

# Application half — deflate an estimate by the learned ratio. Two modes:
clawpm reflect suggest <task-id>                       # derive complexity/confidence/
                                                       #   predicted-duration from the task
clawpm reflect suggest --complexity m --predicted-duration 6h
                                                       # calibrate a bare gut estimate
#   Optional: --confidence N --agent-profile <p> --min-bucket N
#   Falls back to the global ratio when the complexity bucket has < --min-bucket samples.

# Scan session transcripts / agent logs for task mentions and return an
# aggregate report. Source: --source <dir> or CLAWPM_HISTORY_SOURCE.
clawpm reflect history-import [--source <dir>]
#   Currently reports mentions only; writing reflection events back to the
#   corpus is Phase 3 (the operator decides what to do with the report).
```

`reflect void` (documented above) completes the group — mark bad-data events
void without deleting them.

## Project-level reflection (Phase 2 stub — planned)

Tasks are micro-experiments. **Projects are macro-experiments.** Each project has a goal hypothesis in `.project/spec.md` at init time; tasks within the project are sub-predictions about HOW to achieve that goal.

At project completion, pause, or major-milestone review, run a project-level reflection:
- Did the goal hypothesis hold?
- Which tasks served the goal? Which were noise?
- Which sub-hypotheses turned out to be load-bearing?
- What would I predict differently for the NEXT project of similar shape?

**Phase 2 command (not yet implemented):**
```bash
clawpm project reflect                    # Aggregate task reflections, surface goal-trace
clawpm project reflect --milestone "M1"   # Reflect on a milestone within a project
```

**For now (manual):** add a `## Reflection` section to `spec.md` at completion. Capture the four questions above. The aggregated reflection becomes the prior for the next similar project.
