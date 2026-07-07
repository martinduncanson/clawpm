---
name: clawpm
description: Multi-project task and research management (JSON-first CLI)
user-invocable: true
metadata: { "openclaw": { "homepage": "https://github.com/martinduncanson/clawpm", "upstream": "https://github.com/malphas-gh/clawpm", "requires": { "bins": ["clawpm"] }, "emoji": "📋", "install": [{ "id": "uv", "kind": "uv", "package": "git+https://github.com/martinduncanson/clawpm", "bins": ["clawpm"], "label": "Install clawpm (uv)" }] } }
---

# ClawPM Skill

Multi-project task management. All commands emit JSON by default; use `-f text` for human-readable output.

## When to use clawpm — defacto default

clawpm is the defacto-default PM layer for every meaningful work session. Use it for any work unit that meets ANY of these:

- Produces file changes
- Takes more than ~5 minutes of focused effort
- Spans more than one logical step
- Will likely be resumed across sessions
- Has a hypothesis worth testing or a goal worth tracking
- Is dispatched to a subagent (subagent gets a subtask under the parent)

**Skip clawpm only for:** pure Q&A / explanation / brainstorming with no deliverable; single-action lookups (`git status`, "show me X"); session orientation reads; or when the operator says "just do this quickly".

If unsure, use clawpm. Under-used task entries cost nothing; lost calibration data and forgotten cross-session work compound.

**Granularity — track the work unit, not the records it processes.** clawpm tracks phases, batches, milestones, and blockers — never the individual transactions, decisions, rows, or files a task operates on. The task is "reconcile period 2026-05" or "apply approved corrections", not one task per invoice. In high-volume-record domains (accounting, data pipelines, migrations) the >5-minute default means *phases* — otherwise you file hundreds of micro-tasks.

## Capability map — which command for which situation

clawpm is meant to be *agentic*: the acting agent should reach for the right primitive, not just `add`/`done`. Match the situation, not the noun.

| Situation | Reach for | Why |
|---|---|---|
| New multi-step work unit | `tasks add` with `--predict-*` + `--success-criteria` | The success_criteria become the gradeable contract for everything below. |
| One task is really several independent sub-pieces | `tasks decompose <id>` | Splits into child subtasks under the parent; each gets its own rubric. |
| Several queued tasks can run in parallel | set `--parallel-group N`, then `clawpm next --batch` | Returns the next dispatchable group; `tasks add` siblings sharing a group run together. |
| About to spawn a parallel agent on shared files | `clawpm conflicts --scope ...` (or `--task <id>`) | Pre-flight file-claim check; empty `conflicts` array = safe to dispatch. |
| Subagent doing >5min file work toward a verifiable goal | **see "Two dispatch modes" below** | The mode depends on *how* the subagent runs (separate process vs in-harness). |
| Verify a subagent's deliverable against its rubric | `subagent-judge` skill | Independent criterion-by-criterion grade; catches "said done, missed criterion N". |
| Render a task's rubric for a grader / outcome payload | `tasks emit-rubric <id>` | Markdown rubric, or `--rubric-format outcome-payload` for the Anthropic Outcomes shape. |
| Long/crash-prone dispatch that must survive a dead holder | `lease grant/heartbeat` (+ `tasks dispatch --lease-ttl`) | TTL + heartbeat; expiry → fallback policy (requeue/route/escalate). No daemon. |
| High-confidence "done" you don't fully trust | `eval-stop --confirm-close` (or `CLAWPM_CONFIRM_CLOSE`) | Spends an adversarial refutation pass only on the ok=true→close transition. |
| Calibrate future estimates from history | `reflect summarize` / `reflect suggest` | Actual/predicted ratios bucketed by complexity/confidence; deflates a gut estimate. |
| Macro binary outcome above the task layer | `mission` commands | The mission layer; tasks are its mini-goals. |
| After a merged PR | `commit-commands /clean_gone` + `tasks done` | Reaps gone branches; closing the task cascades unblocks. |

### Two dispatch modes — pick by *how* the subagent runs

This is the steering gap most agents miss. There are two distinct ways to hand work to a subagent, and **`tasks dispatch` only fits one of them**:

- **Separate spawned `claude` process (worktree workflow):** `clawpm tasks dispatch <id> [--worktree]` writes `.claude/settings.local.json` Stop/PostToolUse/SessionStart hooks into the target dir. You then `cd` into that dir and run a **fresh `claude`** — *that* process picks up the hooks and is literally hook-gated from terminating until the rubric is satisfied (or impossibility is independently confirmed). This is the rubric-enforced contract. Use it when you genuinely spawn a separate Claude Code process per task.
- **In-harness subagent (same session, Task/Agent tool):** a subagent you dispatch from *within* your current session does **not** pick up another dir's `settings.local.json` hooks — so `tasks dispatch`'s Stop-hook enforcement does **not** apply. Instead: put structured `--success-criteria` on the task, dispatch the in-harness subagent, then verify its deliverable with the **`subagent-judge`** skill before marking done. That's the in-harness equivalent of the hook-gated rubric.

If you're driving a single Claude Code session and delegating via the Agent tool (the common case), you're in the **second** mode — reach for `success_criteria` + `subagent-judge`, not `tasks dispatch`. Reserve `tasks dispatch` for the worktree/separate-process workflow. (Rubric *scoping* discipline for either mode — never scope a dispatched rubric across a human approval gate — is in **Dispatch discipline** below.)

## How Claude fills predictions — "agent proposes, human reviews"

When adding a task on the operator's behalf, **propose all predictions in a single block, then ask for confirmation/edits.** Don't silently file with bare flags; don't ask for every field individually. Single proposal, single review beat:

> "Adding **CLAWP-099 Migrate auth to JWT** with: duration 4h (confidence 3), complexity m, approach 'drop-in JWT middleware', success criteria 'P95 <200ms; session writes drop ≥50%', pre-mortem 'mobile webview cookie edge case', reference task CLAWP-042. **Confirm or edit?**"

The operator overrides only the fields where their gut conflicts with Claude's guess. The gut-vs-Claude delta is itself calibration signal. Always include `--confidence` honestly (1 = wild guess, 5 = done-this-exact-thing-before).

## First-Time Setup

```bash
clawpm setup               # Creates ~/clawpm/ with portfolio.toml, projects/, work_log.jsonl
clawpm setup --check       # Verify installation
```

## Creating Projects

Projects are directories with a `.project/` folder. They don't need to be git repos.

### Initialize in any directory

```bash
cd /path/to/my-project
clawpm project init                    # Auto-detects ID/name from directory
clawpm project init --id myproj        # Custom ID
```

### From a git clone (auto-init)

Git repos under `~/clawpm/projects/` auto-initialize on first use:

```bash
git clone git@github.com:user/repo.git ~/clawpm/projects/repo
cd ~/clawpm/projects/repo
clawpm add "First task"    # Auto-initializes .project/, then adds task
```

### Discover untracked repos

```bash
clawpm projects list --all   # Shows tracked + untracked git repos
```

## Quick Start

```bash
# From a project directory (auto-detected):
clawpm status              # See project status
clawpm next                # Get next task
clawpm start 42            # Start task (short ID works)
clawpm done 42             # Mark done

# Or set a project context:
clawpm use my-project
clawpm status              # Now uses my-project
```

## Top-Level Commands (Shortcuts)

| Command | Equivalent | Description |
|---------|------------|-------------|
| `clawpm add "Title"` | `clawpm tasks add -t "Title"` | Quick add a task |
| `clawpm add "Title" -b "desc"` | `clawpm tasks add -t "Title" -b "desc"` | Add with body |
| `clawpm add "Title" --parent 25` | - | Add subtask |
| `clawpm done 42` | `clawpm tasks state 42 done` | Mark task done |
| `clawpm start 42` | `clawpm tasks state 42 progress` | Start working |
| `clawpm block 42` | `clawpm tasks state 42 blocked` | Mark blocked |
| `clawpm unblock 42` | `clawpm tasks state 42 open` | Unblock a task |
| `clawpm next` | `clawpm projects next` | Get next task |
| `clawpm status` | - | Project overview |
| `clawpm context` | - | Full agent context |
| `clawpm use <id>` | - | Set project context |

## Project Auto-Detection

ClawPM automatically detects your project from (in priority order):
1. **Subcommand flag**: `clawpm tasks list --project clawpm`
2. **Global flag**: `clawpm --project clawpm status`
3. **Current directory**: Walks up looking for `.project/settings.toml`
4. **Auto-init**: If in untracked git repo under project_roots, auto-initializes
5. **Context**: Previously set with `clawpm use <project>`

## Short Task IDs

You can use just the numeric part of a task ID:
- `42` → `CLAWP-042` (prefix derived from project ID)
- `CLAWP-042` → `CLAWP-042` (full ID works too)

## Subtasks

```bash
clawpm add "Subtask" --parent 25   # Creates subtask (auto-splits parent if needed)
clawpm tasks split 25              # Manually convert task to parent directory

clawpm done 25             # Fails if subtasks not done
clawpm done 25 --force     # Override and complete anyway
```

Subtasks move with parent on state change (done/blocked moves entire directory).

## Agent Context (Resuming Work)

Get everything needed to resume work in one command:

```bash
clawpm context             # Full context for current project
clawpm context -p myproj   # Specific project
```

Returns JSON with: project info + spec, in-progress/next task, blockers, recent work log, git status, open issues.

## Workflow Example

```bash
clawpm context             # Get full context
clawpm start 42            # Mark in progress (auto-logs)
# ... do work ...
git add . && git commit -m "feat: ..."
clawpm done 42 --note "Completed"       # Auto-logs with files_changed
clawpm log commit                        # Also log the git commits themselves
```

Hit a blocker:
```bash
clawpm block 42 --note "Need API credentials"
# Later, when the blocker is resolved:
clawpm unblock 42 --note "Credentials obtained"        # → open
clawpm unblock 42 --note "Good to go" --start          # → in-progress immediately
```

### Don't re-`start` an in-progress task to log midway updates

**Re-starting a task that's already in-progress corrupts the duration anchor.**
Actuals are computed from the *first* start event — a re-start makes elapsed time
look shorter than it actually is, breaking the calibration signal.

Instead, use `log add --action progress` for mid-task updates:

```bash
# WRONG — resets the duration anchor
clawpm start 42

# RIGHT — logs a progress note without touching the anchor
clawpm log add --task 42 --action progress --summary "PR #125 opened, awaiting Codex review"
```

Use `start` only to transition `open` → `progress`. Use `done` / `block` / `unblock`
for the corresponding terminal transitions. Use `log add --action progress` for
everything in between.

> The CLI will warn (but not block) if you `clawpm start` a task that's already
> in-progress.

## Full Command Reference

### Projects
```bash
clawpm projects list [--all]            # List projects (--all includes untracked repos)
clawpm projects next                    # Next task across all projects
clawpm project context [project]        # Full project context
clawpm project init                     # Initialize project in current dir
```

### Tasks
```bash
clawpm tasks                            # List tasks (default: open+progress+blocked)
clawpm tasks list [-s open|done|blocked|progress|all] [--flat] [--all-projects]
                                        # --all-projects: portfolio-wide view across every ACTIVE project;
                                        # each row carries project_id, filters compose per-project (CLAWP-084)
clawpm tasks show <id>                  # Task details (includes scope)
clawpm tasks add -t "Title" [--priority 3] [--complexity m] [--parent <id>] [-b "body"] [--scope "glob/**"]
clawpm tasks edit <id> [--title "..."] [--priority N] [--complexity s|m|l|xl] [--body "..."] [--scope "glob/**"]
clawpm tasks state <id> open|progress|done|blocked [--note "..."] [--force]
clawpm tasks split <id>                 # Convert to parent directory for subtasks
clawpm tasks decompose <parent-id> --child "Title" --child '{"title":"...","success_criteria":["..."],"complexity":"m"}'
                                        # Split into child subtasks, each with its own rubric; parent
                                        # can't be DONE until all children are (CLAWP-037)
clawpm tasks emit-rubric <id> [--format markdown|outcome-payload]
                                        # Render success-criteria as a graded rubric (markdown for a
                                        # Stop-hook/human, outcome-payload for Anthropic's define_outcome)
clawpm tasks emit-tree [--dry-run] [--strict]
                                        # Persist a fully-contracted task-tree atomically from a JSON doc
                                        # on stdin. Validates all gates (reject-match, constitution,
                                        # ID-collision, baseline) before writing anything. Zero LLM calls.
                                        # This is the clawpm-planner emission target (CLAWP-056).
clawpm tasks dispatch <id> [--worktree] [--target-dir PATH] [--lease-ttl N]
                          [--fallback-policy requeue|route-secondary|escalate-to-human|fail]
                          [--confirm-close] [--refute-votes N] [--confirm-stale] [-f]
                                        # Write hook-wired .claude/settings.local.json so a hand-launched
                                        # subagent is Stop-hook-gated until the rubric is satisfied
                                        # (CLAWP-018). --worktree isolates parallel dispatches.
clawpm tasks teardown-dispatch [<id>] [--target-dir PATH] [-f]
                                        # Remove a dispatch settings.local.json (only clawpm-written ones
                                        # unless --force).
```

### Scope Conflicts
```bash
clawpm conflicts --scope "src/auth/**" --scope "tests/auth/**"
                                        # Check for in-flight tasks claiming overlapping files
clawpm conflicts --task CLAWP-042       # Same but reads scope from an existing task
clawpm conflicts --task 42 --project myproj
                                        # Explicit project for task lookup
```

### Work Log
```bash
clawpm log add --task <id> --action progress --summary "What I did"
clawpm log tail [--limit 10]            # Recent entries (auto-filtered to current project)
clawpm log tail --all                   # Recent entries across all projects
clawpm log tail --follow                # Live tail (like tail -f)
clawpm log last                         # Most recent entry (auto-filtered to current project)
clawpm log last --all                   # Most recent entry across all projects
clawpm log commit [-n 10]               # Log recent git commits to work log
clawpm log commit --dry-run             # Preview without logging
clawpm log commit --task <id>           # Associate commits with a task
```

Note: State changes (start/done/block) auto-log to work_log with git files_changed.

### Research
```bash
clawpm research list
clawpm research add --type investigation --title "Question"
clawpm research link --id <research_id> --session-key <key>
```

### Issues
```bash
clawpm issues add --type bug --severity high --actual "What happened"
clawpm issues add --type observation --severity low --tag depth-warning --summary "depth>2 subagent nesting"
clawpm issues list [--open] [--type observation] [--tag depth-warning]
```
Types: `bug | ux | docs | feature | observation`. `observation` is for neutral signals worth logging (depth warnings, ergonomic gaps, calibration deltas) that aren't bugs. `--tag` is repeatable; `issues list --tag` matches any.

### Parallel batch dispatch
```bash
clawpm next                             # Single next task
clawpm next --batch                     # Next parallel batch (tasks sharing the lowest
                                        # open parallel_group; group N+1 waits for group N)
```
Tag siblings with `tasks add --parallel-group N` to make them dispatch together.

### Agentic dispatch (parent-spawned subagent, one command)
```bash
clawpm agent dispatch --prompt "<subagent brief>" \
    --rubric-criteria "Tests pass" \
    --rubric-criteria '{"criterion":"...","gradeable_signal":"...","comparator":"..."}' \
    [--parent <id>] [--title "..."] [--agent-profile <p>] [--confirm-close] [--refute-votes N] \
    [--no-codegraph] [--judge-cmd-override <cmd>]
```
Wraps the full cycle in one call (CLAWP-024): create subtask → worktree → dispatch settings →
subprocess `claude --print` → judge grade against the rubric → mark DONE (+ reflection event)
or BLOCKED (+ iteration event). Contrast with `tasks dispatch`, which only writes the hooks for
a subagent you launch yourself.

### Judge primitives
```bash
clawpm judge tournament --rubric-file <file> \
    --candidate <fileA> --candidate <fileB> [--candidate ...] \
    [--label <labelA> --label <labelB>] [--judge-cmd-override <cmd>]
```
Pairwise comparative selection across candidate deliverables (CLAWP-044). Each pair is judged in
both position orders to cancel position bias; ambiguous pairs keep the higher seed (pass the
strongest-prior candidate first). The winner is SELECTED, not certified — verify it through
`hook eval-stop`.

### Leases (crash-safe dispatch)
```bash
clawpm lease grant --task <id> --ttl <seconds> \
    [--fallback-policy requeue|route-secondary|escalate-to-human|fail] \
    [--holder <id>] [--target-dir <dir>]
clawpm lease heartbeat --task <id> [--holder <id>]   # holder is alive (wired to the dispatch hook)
clawpm lease list [-p <project>]                     # active leases + expiry + fallback
clawpm lease release --task <id>                     # clean completion, no fallback on later sweeps
clawpm lease sweep [--dry-run]                        # detect expired leases, apply fallback (no daemon)
```
A dispatched holder heartbeats while alive; if it goes silent past the TTL, a sweep (run by
`clawpm doctor` and on the next `tasks dispatch`) transitions the task per its fallback policy
(CLAWP-039). `tasks dispatch --lease-ttl` grants the lease inline.

### Constitution (named invariants that constrain emission)
```bash
clawpm constitution add -n <name> \
    -k require_success_criteria|max_complexity|require_scope|advisory \
    [-d "description"] [--param KEY=VALUE ...]
clawpm constitution list
clawpm constitution remove -n <name>
```
Code-checkable or advisory invariants enforced by `tasks emit-tree`'s constitution gate.

### Mission (macro binary-outcome layer above tasks)
```bash
clawpm mission add -t "Ship-able statement" -o "YES/NO check at deadline" [-d <days>] [-b "body"]
clawpm mission add-goal <mission-id> --task <id> [--actor agent|human]  # link a task as a mini-goal
clawpm mission list
clawpm mission tasks <mission-id> [--actor agent|human]
clawpm mission status <mission-id>                                       # progress + outcome state
clawpm mission state <mission-id> active|complete|failed|cancelled
```
Tasks are a mission's mini-goals; the mission is the binary outcome they serve.

### Admin
```bash
clawpm setup               # Create portfolio (first-time)
clawpm setup --check       # Verify installation
clawpm status              # Project overview
clawpm context             # Full agent context
clawpm doctor              # Health check
clawpm doctor --strict     # Health check — exits non-zero if any warning (use in CI/pre-flight)
clawpm use [project]       # Set/show project context
clawpm use --clear         # Clear context
```

#### Doctor checks (Phase 1.6)
`clawpm doctor` now runs three additional diagnostic checks beyond basic file existence:

- **Stale tasks** — any `.progress.md` file not touched (mtime or work_log entry) in >7 days surfaces in `stale_tasks[]` with `days_stale` and `suggested_action`.
- **Filesystem-vs-state drift** — if a task file's frontmatter `state:` field disagrees with its location (`tasks/` = open, `tasks/done/` = done, etc.), it appears in `drift_tasks[]`.  Also flags half-renames (both `PROJ-001.md` and `PROJ-001.progress.md` exist simultaneously).
- **Prefix collisions** — two projects whose IDs share the same first-5 uppercase chars (the task-ID prefix) appear in `prefix_collisions[]`.  Colliding prefixes cause silent task-ID aliasing.

Doctor always exits 0; `--strict` exits 1 if any warning is present.

## Work Log Actions

- `start` - Started working (auto-logged on `clawpm start`)
- `progress` - Made progress (use `clawpm log add --action progress` for mid-task updates)
- `done` - Completed (auto-logged on `clawpm done`)
- `blocked` - Hit a blocker (auto-logged on `clawpm block`)
- `unblock` - Blocker resolved (auto-logged on `clawpm unblock`)
- `commit` - Git commit (logged via `clawpm log commit`)
- `pause` - Switching tasks
- `research` - Research note
- `note` - General observation

## Task States & File Locations

| State | File Pattern | Meaning |
|-------|--------------|---------|
| open | `tasks/CLAWP-042.md` | Ready to work |
| progress | `tasks/CLAWP-042.progress.md` | In progress |
| done | `tasks/done/CLAWP-042.md` | Completed |
| blocked | `tasks/blocked/CLAWP-042.md` | Waiting |

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

### Claude's mandate: ALWAYS predict on add

**When Claude adds a task on the operator's behalf, ALWAYS include predictions.**
Estimate at minimum: `--predict-duration`, `--predict-complexity`. Add `--hypothesis`
for any task with a non-trivial outcome ("if I do X, then Y will improve"). Add
`--predict-pitfalls` when the task touches code Claude is uncertain about. Empty
predictions defeat the reflection layer's purpose — they produce structurally-empty
events that yield no calibration signal.

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

**Phase 1 prediction flags** (on `tasks add` and `tasks edit`):
```
--predict-duration      Predicted duration: 90, 90m, 2h, 3d, 1w
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

## Tips

- **Flag order**: `clawpm [global flags] <command> [command flags]` — e.g. `clawpm -f text tasks list -s open`
- **JSON output**: All commands emit JSON by default; use `-f text` for human-readable
- **One command per call**: Don't chain clawpm commands with `&&` — run each separately
- **Portfolio root**: Default `~/clawpm`
- **Work log**: Append-only at `<portfolio>/work_log.jsonl`

## Dispatch discipline — rubric scoping & worktree safety

**A Stop-hook rubric is a hard mechanism that overrides soft policy.** A dispatched agent is hook-blocked from terminating until its rubric is satisfied, so the *fastest path to satisfaction is its incentive*. If that path runs through an action the agent is forbidden to take unilaterally but technically *can* (it holds the write capability), the hook actively incentivises the violation — e.g. an outcome rubric "the books balance" on a write-capable agent makes applying corrections *without* the required human approval the path of least resistance. The Stop hook has turned an approval gate into an obstacle to route around.

**Rule: a dispatched rubric must be satisfiable entirely within the agent's authorised action space; never scope it to an outcome that requires crossing an approval / human gate.** Phase the work so each dispatched phase terminates *at* the gate:

- **Compute phase rubric:** "divergence report for period X emitted; all candidates queued in `<ledger>`; **zero** state writes performed." The zero-writes criterion is itself gradeable from the audit log — a *prohibition* criterion, not just an achievement one.
- **Apply phase rubric:** "every decision with `status:approved` applied with an audit entry; **zero** writes lacking an approved decision ID."
- The outcome ("tie-out / books balance") is the rubric **only** for the final close task, dispatched *after* the human sign-off exists.

This is the dark-side counterpart to the rubric/Stop-hook power: a goal-scoped rubric is exactly as safe as the gap between "what satisfies it" and "what the agent is allowed to do." Keep that gap at zero.

**Never `--worktree`-dispatch a task that mutates a single shared store.** Worktree isolation is built for parallel *code* work with disjoint file scopes (see Scope-Aware Dispatch below). A task that appends to one shared ledger — a decisions store, `work_log.jsonl`, leases, any append-only state, **including clawpm's own JSONL stores** — must dispatch **in-place against the main repo dir**, or the ledger forks per worktree branch and records diverge or are lost. Worktree dispatch is for code; in-place dispatch is for state.

## Scope-Aware Dispatch

ClawPM can act as a file-claim registry for parallel agent runs. When multiple
Claude Code subagents operate in parallel (e.g. in separate git worktrees), they
can collide on shared files. Use `scope` to prevent this.

### Workflow

**1. Declare scope when creating or updating a task:**
```bash
clawpm tasks add -t "Refactor auth layer" \
    --scope "src/auth/**" --scope "tests/auth/**"

clawpm tasks edit CLAWP-042 --scope "src/auth/**" --scope "tests/auth/**"
```

**2. When a task transitions to `progress` its scope becomes "claimed".** Any
`progress`-state task with a non-empty `scope` is considered in-flight.

**3. Pre-flight check — run `clawpm conflicts` before dispatching a new agent:**
```bash
clawpm conflicts --scope "src/auth/login.py"
# → {"conflicts": [], "queried_scope": ["src/auth/login.py"]}  ← safe to dispatch

clawpm conflicts --task CLAWP-099   # read scope from the queued task
# → {"conflicts": [{"task_id": "CLAWP-042", "overlapping_globs": [...], ...}]}
```

An empty `conflicts` array means no in-flight task claims overlapping files —
safe to dispatch. Exit code is always 0; read the JSON array.

### Glob-overlap heuristic

The check uses a prefix-based heuristic:
- Strip `**`/`*`/`?` to get the literal prefix of each pattern.
- Two patterns overlap if either prefix starts with the other.
- Examples: `src/auth/**` ∩ `src/auth/handlers/**` → overlap; `src/auth/**` ∩ `src/billing/**` → no overlap.
- May produce false positives (safe: errs toward flagging rather than missing collisions).
- Does not handle character classes or negation patterns.

### Output format

```json
{
  "conflicts": [
    {
      "project_id": "polymarket-arb",
      "task_id": "POLYM-007",
      "title": "Fix auth flow",
      "scope": ["src/auth/**"],
      "state": "progress",
      "started_at": "2026-05-05T12:00:00+00:00",
      "overlapping_globs": ["src/auth/**"]
    }
  ],
  "queried_scope": ["src/auth/**", "tests/auth/**"]
}
```

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

## Inter-agent messaging — clawpm inbox

Filesystem-first, append-only, no-daemon messaging between agents. Each agent has its own
JSONL file at `~/clawpm/inbox/<agent-id>.jsonl`. Events are never rewritten or deleted —
acks are events too. Survives compaction and reboots; no polling daemon required.

Storage: `~/clawpm/inbox/<agent-id>.jsonl` (created on first send).

| Command | Description |
|---------|-------------|
| `clawpm inbox send --to <id> --message "..."` | Send a message to an agent's inbox |
| `clawpm inbox read --agent <id> [--unacked\|--all]` | Read messages (default: unacked only) |
| `clawpm inbox ack <msg-id> [<msg-id>...] [--agent <id>]` | Acknowledge messages |
| `clawpm inbox thread <msg-id>` | Show full thread (walks in_reply_to chain across all inboxes) |

Optional send flags: `--from <id>` (default `main`), `--in-reply-to <msg-id>`, `--project <id>`, `--task <id>`.
Read filters: `--since <YYYY-MM-DD or ISO>`, `--from <sender>`.

**Worked example — parent dispatches subagent, receives results:**

```bash
# 1. Parent sends pre-context to subagent at dispatch
clawpm inbox send --to researcher --message "Find pricing data for POLYM-007" \
    --project polymarket-arb --task POLYM-007
# → {"msg_id": "INBOX-20260508-a3f9", "to": "researcher", "ts": "..."}

# 2. Subagent reads inbox, acks, does work, sends results back
clawpm inbox read --agent researcher          # returns the message
clawpm inbox ack INBOX-20260508-a3f9 --agent researcher
clawpm inbox send --to main --message "Pricing data: BTC-USD spread 0.3%" \
    --in-reply-to INBOX-20260508-a3f9 --project polymarket-arb

# 3. Parent reads results (unacked by default)
clawpm inbox read --agent main
```

## Workflow Integrations

clawpm is the task layer. Other skills/plugins handle specialised lifecycle moments. Suggest these to the operator at the matching points — don't force them, but don't stay silent either.

### Skill suggestions by lifecycle moment

| Moment | Suggest | Why |
|---|---|---|
| Task creation, before any code | **`feature-dev`** | 7-phase guided workflow (explore → ask → architect → plan → implement → review → reflect). Particularly valuable when complexity is `l`/`xl` or `predict-pitfalls` is non-empty. Seed clawpm subtasks per phase. |
| Before commit | **`commit-commands /commit`** | Auto-drafts commit message from staged changes; we can add `Closes <task-id>` to the message and capture the SHA in `clawpm log commit` after. |
| Between commit and push (non-trivial diff) | **PRE-REVIEW subagent** | Reviewer subagent on the diff with no other context — catches what the implementer missed in self-review. See `codex-review` §3 for the canonical rule (skip for pure docs/config or ≤50 LOC AND mechanical; never skip for auth/serialization/data-storage/silently-failing invariants). Eats ~50% of what Codex would catch, locally, for ~30-60 sec wall-clock. |
| After commit, before PR | **`codex-review` (four surfaces)** | The standing code-PR review set is **four surfaces** (2026-06-30): **Codex** (GitHub app bot — cross-cutting correctness, hypothesis-driven bug finding, operating-context-aware) plus three local co-primaries run via `local_review.py`: **grok-build**, **grok-composer** (two separate standing xAI surfaces), and **Antigravity** (Gemini 3.x family via the official Google Antigravity SDK, `--engine antigravity`). All four are downstream of the local PRE-REVIEW layer; different model families catch different bug shapes, and `find_divergence.py` logs per-surface findings for cross-model correlation. Full mechanics in `codex-review` SKILL.md §5.5. Findings → `clawpm issues add`. **Retired:** the Gemini Code Assist GitHub bot + free-tier CLI (sunset 2026-06-24; the Gemini *family* is restored via the Antigravity SDK, not the bot). PR-Agent (descriptive-only) and CodeRabbit (paid, unsubscribed) are not in the pipeline. |
| Before merge (if PR is non-trivial) | **`pr-review-toolkit /review-pr`** | Six specialist agents (silent-failure-hunter, type-design-analyzer, comment-analyzer, pr-test-analyzer, code-reviewer, code-simplifier). Use selectively — silent-failure-hunter for any fix involving `try/except`, type-design-analyzer for new public types, etc. Findings → `clawpm issues add`. |
| Confidence-scored second opinion | **`code-review`** | Multi-agent independent review with confidence scoring to filter false positives. Useful for high-stakes merges where Codex alone isn't enough. |
| Branch cleanup | **`commit-commands /clean_gone`** | After PRs land — reaps branches whose remotes are gone. Worth running after `clawpm done` for tasks that resulted in merged PRs. |
| Cowork session start | **`clawpm-cowork`** | Bootstraps the ephemeral Cowork VM with portfolio repo + clawpm install + context resume. |

### Doctrine: layered review — independent surfaces, different model families

Redundancy comes from **independent surfaces**, not from stacking bots on every PR. Two layers always run: the local PRE-REVIEW layer (Claude + `pr-review-toolkit` specialists) upstream, and a review layer downstream.

The downstream layer's default for code PRs is **four surfaces** (2026-06-30), all local-or-bot and all logged for cross-model correlation:

- **Codex** — the GitHub-app bot (`chatgpt-codex-connector[bot]`); detect its reply via `wait-for-codex.py`, never the comments endpoint.
- **grok-build** and **grok-composer** — two separate standing xAI surfaces, run locally via `local_review.py --engine grok` (read-only, diff embedded in the prompt).
- **Antigravity** — the Gemini 3.x family via the official Google Antigravity SDK, `local_review.py --engine antigravity` (isolated `.venv-antigravity`, read-only, text-only).

Different model families catch different bug shapes; `find_divergence.py` reports per-surface unique/divergent findings so the set can be pruned or escalated over time. Each surface's verdict is a hint — read the body and triage like Codex; none is a sole gate on recovery/ingest/spec-critical merges, which want the operator's eyes.

**Retired:** the Gemini **Code Assist** GitHub bot and its free-tier CLI were sunset 2026-06-24 — do not summon `/gemini review` and do not treat that bot as a parallel primary. The Gemini *family* lives on only through the Antigravity SDK surface above. The earlier "reviewer triangle" assumed paid CodeRabbit + PR-Agent; neither is in the pipeline now (CodeRabbit unsubscribed; PR-Agent deprecated as descriptive-only). Mechanics for all surfaces live in `codex-review` SKILL.md §5.5.

The "Between commit and push" gate (PRE-REVIEW) is **upstream of the bot layer**. The diff that arrives at Codex already survived a self-review pass, so the bot review shifts from "find bugs" mode to "sanity-check the implementer's stated uncertainties" mode (provided the briefing carries 3-5 named concerns per the codex-review skill's template requirement). Round-1-clean reviews become the norm.

Skip PRE-REVIEW for ≤50 LOC mechanical changes or pure docs/config — full skip rule lives in `codex-review` SKILL.md §3. Never skip for auth, serialization, data storage, or any invariant that fails silently.

### How to invoke (ask the operator first)

When suggesting, phrase as a question, not an action:

> "This task has `complexity=l` and pitfalls noted. Want me to run it through the `feature-dev` workflow first? I'll seed the subtasks here in clawpm."

> "Ready to commit? `/commit` will draft the message — I'll add `Closes CLAWP-042` and run `clawpm log commit` after."

> "Before merging, want me to run `pr-review-toolkit /review-pr` with `silent-failure-hunter` and `pr-test-analyzer` enabled? Took us 3 hours of debug last week to catch a swallowed exception."

The operator may decline — record the decline in the task body so future agents see prior context.

### Hookpoint: work_log JSONL

Every clawpm state transition writes a JSON line to `~/clawpm/work_log.jsonl`. Other plugins/skills can subscribe (file-watch or periodic poll) to fire their own logic — e.g., a `pre-commit` review trigger when a task transitions to `progress`. The schema is documented in the README. clawpm doesn't dispatch these subscriptions itself — it stays the data layer.

## Supported multi-session model (CLAWP-051)

Two agent sessions operating on the **same project** are safe by default.  Here is
the layered model, from outermost guard to innermost:

### Layer 1 — branch-per-session (git discipline)

Each session does its code work on its own branch (`session/<topic>-<YYYYMMDD>` or
`claude/<slug>`), not directly on `main`.  Branches are pushed early (after the first
commit); `main` advances only via PR merge.  This prevents concurrent git mutations
(rebase, reset) from orphaning another session's unpushed commits.  The concurrent-
session git discipline in CLAUDE.md is the authoritative reference.

### Layer 2 — clawpm `scope` claim (disjoint scopes avoid collision at source)

Before dispatching parallel agents use `clawpm conflicts --scope <glob-list>` (or
`--task <id>`) to check for file-claim overlaps.  An empty `conflicts` array means no
two tasks in the queue claim the same files; agents can run safely in parallel without
ever needing to co-ordinate their task-state writes.  When scopes overlap, serialise
the tasks or decompose them so they don't.

### Layer 3 — per-project file lock (serialisation backstop)

Even when two sessions end up touching the same project's task tree concurrently
(e.g. both call `add_task`, or both try to transition the same task), clawpm holds an
exclusive advisory lock for the duration of each mutation:

- **ID allocation (TOCTOU) — `add_task`:** acquires `<tasks_dir>/.clawpm-tasks.lock`
  before scanning existing IDs and holds it until the new task file is written to disk.
  Two concurrent sessions see different `next_num` values and write different task IDs.
  Explicit-ID creates are guarded against clobber: if the target file already exists,
  `FileExistsError` is raised rather than silently overwriting the prior task.
- **Subtask allocation (TOCTOU) — `add_subtask`:** acquires the same lock around the
  scan→mint→write critical section.  Two sessions decomposing the same parent concurrently
  see different `next_num` values and never mint the same subtask ID.
- **State-transition moves — `change_task_state`:** the entire read→validate→mutate→reload
  transaction is held inside a single lock acquisition.  This covers the REJECTED
  frontmatter rewrite, the DONE/rollup re-check, the `shutil.move`, and the return-value
  reload — all under the same critical section.  If the source file has already been moved
  by another session, the operation raises `FileNotFoundError` with a message naming the
  concurrent-session cause — no opaque crash, no silent lost-update.

**Granularity is per-project:** the lock file lives at `<tasks_dir>/.clawpm-tasks.lock`
and serialises mutations *within one project's task tree* only.  Different projects
run entirely independently.

**Lock implementation:** a non-blocking acquire polled in a backoff loop up to a
configurable timeout (~120s) — `fcntl.flock(LOCK_EX | LOCK_NB)` on POSIX,
`msvcrt.locking(LK_NBLCK)` on Windows — so a long-held lock (large rollup,
slow/AV-scanned filesystem) is waited out rather than failing at Windows'
hard 10s `LK_LOCK` cap.  Only genuine lock-contention errnos are retried; a
permanent fault on the sentinel fails fast.  Both are advisory and
cross-process.  No third-party dependency.

**Deadlock safety:** the lock is never held across subprocess calls or I/O-heavy
operations (e.g. `resolve_baseline_ref`'s git subprocess runs before acquiring).
`file_lock` is **reentrant per-thread** (CLAWP-066): a locked mutator may call
another locked helper on the same path (e.g. `add_subtask` → `split_task`) — the
nested acquire bumps a thread-local depth and the OS lock releases only when the
outermost block exits. Cross-thread/cross-process callers still serialise on the
OS lock. All task-tree mutators (`change_task_state`, `add_task`, `add_subtask`,
`split_task`, `edit_task`, mission/serve task writes) hold the lock.

**The `.clawpm-tasks.lock` file** is a runtime sentinel.  It is gitignored and never
committed; it does not interfere with task-ID scanning (it doesn't match task globs).

### When the backstop is enough vs. when you still need scopes

Locks co-ordinate **clawpm state** (task IDs, file moves); they do not co-ordinate
**git refs** or **application state**.  Two sessions that write to the same source
file will still produce a merge conflict regardless of clawpm locks.  The right tool:
disjoint scopes (Layer 2) for application-file safety; git discipline (Layer 1) for
ref safety; locks (Layer 3) for task-metadata safety.

## Troubleshooting

```bash
clawpm doctor              # Check for issues
clawpm setup --check       # Verify installation
clawpm log tail            # See recent activity
```

**`add_failed` after `project init`?** Check `.project/settings.toml` — `repo_path` must use forward slashes on Windows (`F:/Git/...` not `F:\Git\...`). The CLI now warns when this is suspected, but old settings.toml files written by earlier versions may still be broken.

**Windows CLI caveats (observed 2026-07-05):**
- `--predict-scope` values containing glob metacharacters (`scripts/**`) get expanded into extra positional arguments and fail `tasks add` with "Got unexpected extra arguments". Use plain directory prefixes (`scripts/`) or exact file paths — the conflict heuristic strips glob chars anyway.
- `--predict-duration` rejects combined units (`2h30m`) — use a single unit (`150m`, `2h`).
- If multiple `clawpm.exe` shims are on PATH, a stale one can shadow the working install (`ModuleNotFoundError: No module named 'clawpm'`). Check with `where.exe clawpm` and invoke the shim next to the Python install that has clawpm importable.
- **`tasks edit` replaces the predictions block wholesale, not field-by-field.** Editing only `--hypothesis` nulls duration/complexity/confidence/pre-mortem/scope/filled_by. When editing any prediction field, re-pass ALL prediction flags you want to keep. Also: `--predicted-by` exists on `tasks add` but not `tasks edit`, so `filled_by` is lost on any prediction edit.
