# ClawPM (martinduncanson fork)

Filesystem-first, deterministic operating substrate for autonomous work. Multi-project task manager, persistent across sessions, JSON-first, no daemon. Survives compaction, subagent dispatch, and reboots.

> Forked from [`malphas-gh/clawpm`](https://github.com/malphas-gh/clawpm). Adds: Windows TOML/backslash bug fix, `scope:` field + `clawpm conflicts` for parallel-agent safety, the reflection/calibration layer (predictions to actuals to learned ratios), **verifiable goals** (`--success-criteria` + a Stop-hook judge), **subagent dispatch** with **crash-safe leases**, baseline-stamping with drift detection, per-task thrashing detection, the **won't-do / rejected ledger**, per-leaf contract fields (`out_of_scope` / `stop_conditions` / `delegability`), **project constitution** (named invariants constraining emission), **`tasks emit-tree`** (deterministic, atomic, zero-LLM tree ingestion), **hierarchical nesting via `parent_ref`** in a single emit, the **clawpm-planner skill** (objective to vetted task-tree on a capable model), cross-platform locked JSONL appends, Cowork bootstrap skill, Codex AGENTS.md template.

[![ClawHub](https://img.shields.io/badge/ClawHub-clawpm-blue)](https://clawhub.ai/malphas-gh/clawpm)

> **Status:** actively dogfooded. The core loop (add/start/done, scope, work-log, reflection) is stable. The agentic layer — rubric + Stop-hook judge, `tasks dispatch` / `agent dispatch`, crash-safe leases, baseline + drift detection, thrashing detection, `reflect summarize`/`suggest` — is shipped and tested. The planner layer (`clawpm-planner` skill + `tasks emit-tree`) is shipped. `clawpm serve` (web UI) is a read-only dashboard behind the optional `web` extra. Python 3.11+. No database, no daemon.

---

## Table of contents

- [What it is](#what-it-is)
- [The architecture — the judgment/facts seam](#the-architecture--the-judgmentfacts-seam)
- [The full loop](#the-full-loop)
- [The layers](#the-layers)
- [5-minute quickstart](#5-minute-quickstart)
- [Planner, emit, and dispatch walkthrough](#planner-emit-and-dispatch-walkthrough)
- [When to use it (and when not to)](#when-to-use-it-and-when-not-to)
- [All commands](#all-commands)
- [How it works](#how-it-works)
- [Installation](#installation)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Integration with Claude Code](#integration-with-claude-code)
- [License](#license)

---

## What it is

A small Python CLI that turns your filesystem into a multi-project task manager. Every task, work-log entry, research note, and issue lives as a markdown / JSON file under `~/clawpm/`. No database. No daemon. No external service.

The point: **task state survives session boundaries.** When Claude Code, Codex, or you yourself stop and resume two days later, `clawpm context` brings everything back — what you were doing, what's next, what's blocked, what code changed.

This fork extends the original task tracker into a **deterministic, local-first operating substrate for autonomous work**. The full loop: a capable model plans; a deterministic core persists the plan with zero model calls; cheap models execute under enforced contracts; independent judges verify; calibration data feeds the next estimate.

---

## The architecture — the judgment/facts seam

The central design decision is a hard seam between **judgment** (model-heavy, swappable) and **facts** (deterministic, zero LLM calls, filesystem-first).

```
┌─────────────────────────────────────────────────────────────────┐
│  JUDGMENT LAYER  (swappable skill, capable model)               │
│                                                                 │
│  clawpm-planner skill                                           │
│  constitution → recon (graph-grounded) → ideate → specify →    │
│  decompose (vertical slices) → vet (no-slop gate) →            │
│  fully-contracted tree JSON                                     │
└──────────────────────────────┬──────────────────────────────────┘
                               │  one transactional call
                               │  clawpm tasks emit-tree < tree.json
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  FACTS LAYER  (deterministic core, zero LLM calls)              │
│                                                                 │
│  validate → reject-match → constitution-check →                 │
│  baseline-stamp → stage (.emit-<uuid>/) → atomic promote        │
│  → per-leaf: rubric / scope / out_of_scope / stop_conditions /  │
│              delegability / baseline_ref                        │
└──────────────────────────────┬──────────────────────────────────┘
                               │  dispatched leaves
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  EXECUTION LAYER  (cheap model under contract)                  │
│                                                                 │
│  clawpm tasks dispatch → Stop-hook judge → blind refuter →      │
│  tournament → crash-safe lease + heartbeat                      │
│  thrashing detection (CLAWPM_THRASH_THRESHOLD)                  │
└──────────────────────────────┬──────────────────────────────────┘
                               │  done / blocked events
                               ▼
┌─────────────────────────────────────────────────────────────────┐
│  CALIBRATION LAYER  (deterministic, no model)                   │
│                                                                 │
│  reflect summarize → actual/predicted ratios by bucket →        │
│  reflect suggest → deflated next estimate                       │
└─────────────────────────────────────────────────────────────────┘
```

**The skill decides; core persists.** Every judgment — what to build, how to slice, who executes, effort/risk — is made in the planning skill on a capable model. Core receives a fully-contracted tree and writes it, making zero LLM calls. That lets an expensive model plan once and cheap models execute many times, with the substrate holding both to a contract.

**Project-agnostic.** The substrate works for code projects and knowledge work. Graph grounding uses codegraph (code) or graphify (mixed corpora, local Qwen) in the planner; the core CLI has no code-specific assumptions.

**Local-first.** No daemon, no external service. Filesystem-first; graph grounding optional.

---

## The full loop

```
GENERATE   clawpm-planner skill
           objective → graph-grounded recon → ideation → PRD/spec →
           vertical-slice decomposition → vet → fully-contracted tree JSON

PERSIST    clawpm tasks emit-tree < tree.json
           schema-validate → reject-match → constitution-check →
           baseline-stamp → stage → atomic promote (zero LLM calls)

EXECUTE    clawpm tasks dispatch <leaf>
           Stop-hook judge + blind refuter + tournament +
           crash-safe lease + heartbeat + thrashing detection

VERIFY     clawpm hook eval-stop / clawpm judge tournament
           Stop-hook blocks termination until rubric satisfied or
           impossibility confirmed; adversarial refuter overturn rate >=50% blocks close

CALIBRATE  clawpm reflect summarize / reflect suggest
           predicted-vs-actual ratios → deflated next estimate
```

---

## The layers

### Layer 1 — substrate / primitives

The core loop every other layer builds on:

- **Multi-project portfolio** (`~/clawpm/`) — task files live in `.project/` inside each repo; the portfolio tracks them all.
- **Scope fields + `conflicts`** — every task declares glob file scope; `clawpm conflicts` pre-flight prevents parallel-agent collisions.
- **Work log** — append-only JSONL; every state change auto-logs files changed; PostToolUse hook appends heartbeats.
- **Research + issues** — durable markdown notes tied to tasks; separate from task state.
- **Session resume** — `clawpm context` / `clawpm resume` bring the full picture back after compaction or a restart.

### Layer 2 — planner skill

`clawpm-planner` (lives in `skills/clawpm-planner/SKILL.md`) turns a free-text objective into a vetted task-tree and emits it. This is the **only** layer where model judgment lives. Stages:

| # | Stage | Does |
|---|-------|------|
| 1 | constitution | Load project invariants that constrain every leaf |
| 2 | recon | Orient on the ground; map structure/blast-radius via graph |
| 3 | ideate | Diverge: approaches and direction candidates (filed as research, not leaves) |
| 4 | specify | Draft the PRD/spec |
| 5 | decompose | Vertical-slice leaves, each draft-contracted |
| 6 | vet | Re-read ground; reject dup/by-design; diff vs won't-do ledger |
| 7 | emit | One transactional `clawpm tasks emit-tree` call |
| 8 | handoff | Dispatch delegable leaves; bounce human leaves to operator |

All stages are optional and composable. The scale dial (s/m/l/xl) governs depth:

| Scale | Stages run | Shape |
|-------|-----------|-------|
| s | recon(light) → decompose → emit | 1–2 flat leaves, no PRD |
| m | recon → (ideate) → specify → decompose → vet → emit | parent + several leaves, short PRD |
| l | full pipeline | 2-level tree |
| xl | full pipeline + opt-in personas + direction-candidate review | multi-milestone tree + PRD + ADR |

Default one tier lower when uncertain — over-planning a small objective is the regression to avoid.

### Layer 3 — emission core (`tasks emit-tree`)

`clawpm tasks emit-tree` is the deterministic sink. It reads a JSON tree on stdin and persists it atomically. **Zero LLM calls** — this is a test-enforced guarantee. Validation gates (all read-only, all before first write):

1. Schema validation (`schema_version: 1`, unique `ref`s, `parent_ref` resolution)
2. ID-collision pre-check
3. Won't-do reject-match against the ledger
4. Constitution invariant check
5. Baseline-ref resolution (`baseline_ref` stamped uniform across the tree)

Then: stage into `.emit-<uuid>/` on the same filesystem → atomic `Path.replace` promote. A crash during stage leaves an invisible dot-dir swept by `clawpm doctor`.

Each emitted leaf carries the full contract:

| Field | Purpose |
|-------|---------|
| `success_criteria` | Gradeable rubric — `criterion` / `gradeable_signal` / `comparator` |
| `scope` | File globs the leaf may touch |
| `out_of_scope` | Explicit exclusions — executor must not cross these |
| `stop_conditions` | Free-text escape-hatch triggers; if true, STOP and report |
| `delegability` | `agent` / `human` / `either` — dispatch gate |
| `baseline_ref` | Git HEAD SHA at emission time — basis for drift detection |
| `predictions` | Duration, complexity, confidence, approach, pre-mortem, reference tasks |

Hierarchical multi-level trees are expressed in one document via `parent_ref` — a leaf whose `parent_ref` points to another leaf's `ref` becomes its child. A PRD/spec is stored as a linked research entry and linked bidirectionally to the root task.

### Layer 4 — execution / dispatch

- **`clawpm tasks dispatch <id>`** — writes hook-wired `.claude/settings.local.json` so a hand-launched or Task-tool-spawned subagent gets the Stop-hook rubric gate, PostToolUse work-log/heartbeat, and rubric injected at SessionStart.
- **`--worktree`** — creates a git worktree under `.clawpm-worktrees/<id>/` so multiple subagents run in parallel without colliding.
- **`--confirm-stale`** — required when a task's in-scope files have changed since `baseline_ref` was stamped; without it, dispatch is blocked on detected drift.
- **`clawpm agent dispatch`** — one command wraps the full cycle: task create, dispatch settings write, subagent invoke, judge grade, state transition. The rubric is enforced on every parent-spawned subagent without the parent managing the six-step sequence manually.
- **`clawpm tasks decompose <id>`** — hand-typeable alternative: decompose an existing task into child subtasks, each with its own rubric; parent cannot be marked done until all children are done.

### Layer 5 — verification

- **Stop-hook judge (`clawpm hook eval-stop`)** — reads the subagent's transcript against the rubric; returns `{ok, reason}` or `{ok:false, impossible}`. Wired as a Stop hook; the subagent cannot terminate until the rubric is satisfied or impossibility is confirmed.
- **Blind refuter (`--confirm-close`, `--refute-votes`)** — before accepting `ok=true`, an adversarial pass attempts to disprove the close. If half or more of the refuter votes overturn, the close is blocked. Default 1 vote; ties overturn.
- **Tournament (`clawpm judge tournament`)** — comparative pairwise selection among multiple candidate deliverables. Winner is selected, not certified — run through `hook eval-stop` to verify it clears the rubric.
- **Thrashing detection** — after each iteration event, `detect_thrashing` checks whether the last N consecutive iteration events show no measurable progress. Threshold: per-task `predictions.thrash_threshold`, else `CLAWPM_THRASH_THRESHOLD` env, else 4. When thrashing is detected, the hook fires the fallback policy — preventing a subagent looping indefinitely at cost.
- **Judge fallback** — `claude --print` primary (override: `CLAWPM_JUDGE_CMD`); falls back to local Ollama when the primary is unavailable.

### Layer 6 — calibration

Every task carries predictions (duration, complexity, confidence, approach, pre-mortem, reference tasks). When done, actuals are computed from the work log. A structured reflection event is written to `~/clawpm/reflections/<task-id>.jsonl`.

- `clawpm reflect summarize` — actual/predicted duration ratios bucketed by complexity / confidence / agent_profile.
- `clawpm reflect suggest` — deflates a gut estimate by the learned median ratio (falls back to global ratio when a bucket has fewer than `--min-bucket` samples). Deterministic; no model call.
- `clawpm reflect history-import` — back-fills the corpus by scanning historical session transcripts for task-ID mentions.
- `clawpm reflect void` — marks a reflection event void (event-source discipline; appends a void marker, never deletes the original).

---

## 5-minute quickstart

```bash
# 1. Install
uv tool install git+https://github.com/martinduncanson/clawpm

# 2. Create your portfolio (one-time)
clawpm setup
# → ~/clawpm/  with portfolio.toml, projects/, work_log.jsonl

# 3. Initialise a project (run from inside the project's directory)
cd ~/code/my-project
clawpm project init
# → .project/settings.toml created in your repo

# 4. Add your first task
clawpm add "Migrate auth to JWT"
# → CLAWP-001 created
# (Output is JSON by default. Add -f text for human-readable.)

# 5. Start working
clawpm start 1            # Auto-logs the start

# 6. Done
clawpm done 1 --note "JWT login + middleware shipped"
# → moves CLAWP-001 to done/, auto-logs files changed

# 7. Resume tomorrow (different session)
clawpm context
# → JSON with: in-progress task, next-up task, blockers, recent log,
#   git status, open issues.
```

That's the core loop. **Add → start → done.** Everything else is optional.

---

## Planner, emit, and dispatch walkthrough

This is the agentic loop: capable model plans; deterministic core persists; cheap model executes under contract.

### Step 1 — invoke the planner

Load the `clawpm-planner` skill in Claude Code (capable model):

```
Plan this objective: add draft autosave to the editor so users never lose work
```

The skill runs its stage flow (constitution → recon → ideate → specify → decompose → vet) and produces a fully-contracted JSON tree. Direction candidates (adjacent ideas surfaced during ideation) are filed as `clawpm research` entries, not leaves.

### Step 2 — dry-run first

The skill pipes the tree to the CLI:

```bash
cat tree.json | clawpm tasks emit-tree --dry-run
```

Output reports what would be written, which leaves match the won't-do ledger, and any constitution violations — writing nothing. Fix any violations, then emit for real.

### Step 3 — emit atomically

```bash
cat tree.json | clawpm tasks emit-tree
```

Core validates all gates, stages the subtree, and promotes it atomically. Returns:

```json
{
  "status": "ok",
  "data": {
    "root_id": "CLAWP-071",
    "emitted": ["CLAWP-071-001", "CLAWP-071-002", "CLAWP-071-003"],
    "research_id": "RES-012",
    "baseline_ref": "a3f9c12",
    "rejected": [],
    "constitution_violations": [],
    "dry_run": false
  }
}
```

Each leaf carries its full contract. The PRD is stored as a linked research entry.

### Step 4 — dispatch a leaf

```bash
# Check for scope collisions first
clawpm conflicts --task CLAWP-071-001

# Dispatch (creates isolated worktree + hook-wired settings)
clawpm tasks dispatch CLAWP-071-001 --worktree --lease-ttl 1800 --fallback-policy requeue
```

The dispatched subagent (a cheaper model) cannot terminate until its rubric is satisfied. Heartbeats keep the lease alive. If the subagent goes silent past the TTL, the fallback policy fires automatically on the next `clawpm doctor` sweep.

### Step 5 — verify and close

```bash
clawpm done CLAWP-071-001 --note "Autosave fires every 30s, survives hard-refresh"
clawpm reflect summarize    # update the calibration corpus
```

---

## When to use it (and when not to)

| Use clawpm | Don't use clawpm |
|---|---|
| Multi-step work that will outlive this session | Quick TODOs you will finish in the next 10 minutes |
| Cross-project work ("what's blocking me anywhere?") | One-off scripts in /tmp |
| Subagent dispatch where multiple agents touch shared files | Pure conversation / brainstorming with no concrete deliverables |
| Work-log audit ("what did I commit, when, against which task?") | Tasks that have no file output |
| Capturing predictions vs actuals for calibration | Tasks where prediction-vs-reality has no learning value |
| An objective needs planning (not a single well-formed task) | A single already-well-formed task (just `clawpm add`) |
| Graph-grounded blast-radius estimation before decomposing | A team-wide shared bug tracker (use Linear / GitHub Issues) |

---

## All commands

### Top-level shortcuts

| Command | Equivalent | Description |
|---|---|---|
| `clawpm add "Title"` | `clawpm tasks add -t "Title"` | Quick add a task |
| `clawpm add "Title" --parent 25` | — | Add subtask |
| `clawpm start 42` | `clawpm tasks state 42 progress` | Start working (auto-logs) |
| `clawpm done 42` | `clawpm tasks state 42 done` | Mark done (auto-logs files changed) |
| `clawpm block 42 --note "reason"` | `clawpm tasks state 42 blocked` | Mark blocked |
| `clawpm next` | `clawpm projects next` | Next task across all projects |
| `clawpm status` | — | Project overview |
| `clawpm context` | — | Full agent context (spec, tasks, log, git, issues) |
| `clawpm use <id>` | — | Set project context |
| `clawpm conflicts --task <id>` | — | Pre-flight check for scope overlap |
| `clawpm serve` | — | Start read-only web dashboard at http://127.0.0.1:8080 (needs the `web` extra: `pip install 'clawpm[web]'`) |

Short task IDs work everywhere: `42` expands to `CLAWP-042` based on project prefix.

### Projects

```bash
clawpm projects list [--all]       # List projects (--all shows untracked repos)
clawpm projects next               # Next task across all projects
clawpm project init [--id myproj]  # Initialise project in cwd
clawpm project context             # Full project context
clawpm project doctor              # Health check
```

### Tasks

```bash
clawpm tasks                        # List open + in-progress + blocked
clawpm tasks list [-s all]          # Filter by state (open/progress/done/blocked/rejected/all)
clawpm tasks list -s rejected       # View the won't-do / rejected ledger
clawpm tasks show <id>              # Full task details (predictions, scope, contract, body)
clawpm tasks add -t "Title" [-b "body"] [--parent <id>] [--scope "glob/**"]
clawpm tasks edit <id> [--title/--priority/--complexity/--body/--scope]
clawpm tasks state <id> <state> [--note] [--reflect-note] [--meta-reflect]
clawpm tasks state <id> rejected --rationale "reason"   # add to won't-do ledger
clawpm tasks split <id>             # Convert to parent directory for subtasks
```

### Constitution

Project constitutions are named invariants that constrain what `emit-tree` may accept. Four kinds:

| Kind | Effect |
|------|--------|
| `require_success_criteria` | Every emitted leaf must have at least one success criterion |
| `max_complexity` | Cap the maximum complexity a leaf may declare (e.g. `--param max=l`) |
| `require_scope` | Every emitted leaf must declare at least one scope glob |
| `advisory` | Human-readable rule reported on violation but never blocks emission |

```bash
clawpm constitution list                              # List invariants for current project
clawpm constitution add -n "must-have-rubric" \
    -k require_success_criteria \
    -d "All code leaves need a verifiable rubric"
clawpm constitution remove -n "must-have-rubric"
```

Violations appear in the `constitution_violations` field of `emit-tree` output. Pass `--strict` to `emit-tree` to hard-fail on violations instead of report-back.

### Scope (parallel-agent safety)

```bash
clawpm tasks add -t "..." --scope "src/foo/**" --scope "tests/foo/**"
clawpm conflicts --scope "src/foo/login.py"   # ad-hoc query
clawpm conflicts --task CLAWP-042             # use task's declared scope
```

Always exits 0. Read the JSON `conflicts` array — empty = safe.

### Planner

The `clawpm-planner` skill (`skills/clawpm-planner/SKILL.md`) is a Claude Code skill, not a CLI command. Invoke it by loading the skill and providing an objective in natural language. It calls the CLI internally and pauses after the emit stage, returning control before any dispatch.

### Emission API (`tasks emit-tree`)

```bash
clawpm tasks emit-tree [--project ID] [--dry-run] [--strict] < tree.json
```

- **`--dry-run`** — validates all gates, reports what would be written, writes nothing. Use as a pre-flight.
- **`--strict`** — hard-fail on won't-do matches or constitution violations instead of report-back.

Input document shape (schema_version: 1):

```jsonc
{
  "schema_version": 1,
  "project": "my-project",
  "root": { "title": "New root task" },
  "prd": {
    "title": "Goal PRD",
    "type": "spike",
    "tags": ["prd"],
    "body_markdown": "## Problem\n..."
  },
  "leaves": [
    {
      "ref": "L1",
      "parent_ref": null,
      "title": "Subtask 1",
      "success_criteria": [
        { "criterion": "Tests pass", "gradeable_signal": "pytest exit 0", "comparator": "eq:0" }
      ],
      "scope": ["src/**"],
      "out_of_scope": ["docs/**"],
      "stop_conditions": ["test suite red"],
      "delegability": "agent",
      "predictions": { "duration_min": 120, "complexity": "m", "confidence": 3 },
      "leaf_key": "L1-stable-key"
    }
  ]
}
```

Multi-level trees: set `parent_ref` on a leaf to point to another leaf's `ref`. No separate nesting calls needed.

### Dispatch, judge, and leases (the agentic layer)

```bash
# Render a task's gradeable rubric
clawpm tasks emit-rubric <id> [--format markdown]

# Write hook-wired settings for a subagent
clawpm tasks dispatch <id> [--worktree] \
    [--confirm-close] [--refute-votes N] \
    [--lease-ttl <secs>] [--fallback-policy requeue|route-secondary|escalate-to-human|fail] \
    [--confirm-stale]

# Remove dispatch settings
clawpm tasks teardown-dispatch <id>

# One-command spawn + grade + persist verdict
clawpm agent dispatch --prompt "..." --rubric-criteria "..." \
    [--parent <id>] [--title "..."] \
    [--confirm-close] [--refute-votes N] \
    [--agent-profile "backend"] \
    [--judge-cmd-override "..."]

# Stop-hook judge (invoked by dispatched settings; also callable standalone)
clawpm hook eval-stop --task <id>

# SessionStart context sidecar
clawpm hook session-start

# Lease management
clawpm lease grant --task <id> --ttl <secs> --fallback-policy <p>
clawpm lease list [--project <p>]
clawpm lease heartbeat --task <id>
clawpm lease release --task <id>
clawpm lease sweep [--dry-run]

# Tournament selection
clawpm judge tournament \
    --rubric-file rubric.md \
    --candidate attempt1.txt \
    --candidate attempt2.txt \
    [--label "v1"] [--label "v2"] \
    [--judge-cmd-override "..."]

# Decompose an existing task into child subtasks
clawpm tasks decompose <parent-id> \
    --child "Plain title" \
    --child '{"title":"...","success_criteria":["..."],"complexity":"m","agent_profile":"backend"}'
```

### Work log

```bash
clawpm log add --task <id> --action progress --summary "What I did"
clawpm log tail [--limit 10]
clawpm log tail --all
clawpm log tail --follow
clawpm log last
clawpm log commit [-n 10]         # Pull recent git commits into work log
```

State changes (`start`/`done`/`block`) auto-log with files changed. Install `hooks/clawpm-sync/` as a Claude Code hook for hands-off logging on every tool use plus session boundaries — see `hooks/clawpm-sync/HOOK.md`.

### Research and issues

```bash
clawpm research add --type investigation --title "Question"
clawpm research list
clawpm issues add --type bug --severity high --actual "What happened"
clawpm issues list [--open]
```

### Reflection (predictions, actuals, calibration)

```bash
# Add task with predictions
clawpm tasks add -t "Migrate to Postgres 16" \
    --predict-duration 120 \
    --predict-complexity l \
    --predict-files-changed 8 \
    --predict-scope "migrations/**" \
    --predict-frameworks alembic \
    --predict-pitfalls "constraint conflicts on unique indexes" \
    --hypothesis "performance will improve 30%" \
    --confidence 3 \
    --predict-approach "rolling migration with blue/green" \
    --pre-mortem "most likely failure: mobile webview cookie edge case" \
    --reference-task CLAWP-042

# Complete with reflection
clawpm done CLAWP-042 \
    --note "Migration shipped, queries 12% faster" \
    --reflect-note "constraint conflicts hit, took 2 extra hours" \
    --meta-reflect "should have run a dry-run dump first"

# Query the calibration corpus
clawpm reflect summarize
clawpm reflect suggest --complexity m --predicted-duration 2h
clawpm reflect suggest CLAWP-042
clawpm reflect history-import --source <dir>
clawpm reflect void CLAWP-007 --reason "pre-bugfix actuals were wrong"
clawpm reflect void --all-empty-actuals --reason "Phase 1 corpus cleanup"
```

> **Agent discipline:** when an AI agent adds a task, it must always include `--predict-duration` and `--predict-complexity`. Empty predictions produce structurally empty events with no calibration signal. Unit suffixes are accepted: `2h`, `3d`, `1w` (wall-clock, not 8-hour workdays).

### Mission and inbox

```bash
clawpm mission                     # Mission Control — macro binary-outcome layer above tasks
clawpm inbox                       # Inter-agent messaging
```

### Web dashboard

Read-only view over the portfolio (projects, tasks, blockers, work-log). All
state changes go through the CLI. Requires the optional `web` extra:

```bash
pip install 'clawpm[web]'           # one-time: install fastapi + uvicorn
clawpm serve                       # Start on http://127.0.0.1:8080
clawpm serve --port 8888
```

### Admin

```bash
clawpm setup
clawpm setup --check
clawpm doctor
clawpm doctor --apply
clawpm doctor --check-codex
clawpm doctor --check-encoding
clawpm version
clawpm resume                      # 2-paragraph session-resume briefing
```

Looking for a seed portfolio? See `examples/portfolio/` — drop-in fixtures with sample projects, tasks across all states, and a populated `work_log.jsonl`. Edit the absolute paths in `portfolio.toml` before pointing `CLAWPM_PORTFOLIO` at it.

---

## How it works

### Filesystem layout

```
~/clawpm/                                  ← portfolio root
├── portfolio.toml                         ← project registry
├── work_log.jsonl                         ← append-only log of every state change
├── reflections/                           ← per-task reflection + iteration events
│   └── CLAWP-042.jsonl
├── dispatches.jsonl                       ← append-only dispatch registry
├── leases.jsonl                           ← append-only lease ledger (TTL/heartbeat/expiry)
└── projects/                             ← legacy slot

<your project repo>/.project/             ← per-project state (committed to repo)
├── settings.toml                         ← project ID, repo_path, prefix
├── spec.md                               ← project goals/spec (you author)
├── constitution.yaml                     ← named invariants (you declare)
└── tasks/
    ├── CLAWP-001.md                      ← open tasks live FLAT here
    ├── CLAWP-002.progress.md             ← in-progress
    ├── done/CLAWP-003.md                 ← done
    ├── blocked/CLAWP-004.md              ← blocked
    ├── rejected/CLAWP-005.md             ← won't-do ledger
    └── CLAWP-006/                        ← parent task with subtasks
        ├── _task.md
        └── CLAWP-006-001.md
```

Two state stores:

- **Portfolio** (`~/clawpm/`) — global state across all projects: who exists, what was logged, dispatch registry, lease ledger.
- **Per-project `.project/`** — task files, project spec, constitution, settings; lives in the repo and is committed.

Output is JSON by default for agent consumption. Add `-f text` for humans.

### Write atomicity

Every individual task write uses `tmp.write_text(...)` → `tmp.replace(target)` (atomic rename, same filesystem). JSONL side-effects route through locked append (`concurrency.append_jsonl_line`). `emit-tree` extends this to whole-tree atomicity: stage into `.emit-<uuid>/` on the same filesystem → single `Path.replace` for new-root trees (the entire subtree in one rename), ordered individual file promotions for `attach_to` trees. A crash during stage leaves an invisible dot-dir swept by `clawpm doctor`.

### Task states and file locations

| State | File pattern | Meaning |
|---|---|---|
| open | `tasks/PROJ-042.md` | Ready to work |
| progress | `tasks/PROJ-042.progress.md` | In progress |
| done | `tasks/done/PROJ-042.md` | Completed |
| blocked | `tasks/blocked/PROJ-042.md` | Waiting on something |
| rejected | `tasks/rejected/PROJ-042.md` | Won't-do ledger (requires `--rationale`) |

State transitions move files between locations. Subtasks move with their parent. Parent rollup: a parent cannot be marked done until all children are done.

### Project auto-detection

clawpm resolves your project automatically (priority order):

1. `--project` flag
2. Current directory (walks up to find `.project/settings.toml`)
3. Auto-init if you are inside an untracked git repo under `project_roots`
4. Context from `clawpm use <id>` (sticks across commands in the shell)

You almost never need `--project` if you `cd` into the project first.

### Graph grounding

The planner skill uses graph tools for recon and blast-radius estimation in decompose:

- **codegraph** — default for code projects. Indexed AST graph; `codegraph_context`, `codegraph_impact`, `codegraph_callers`, `codegraph_trace` are the primary recon tools. Already configured in this repo (see `.codegraph/`).
- **graphify** — for mixed corpora (code + docs + PDFs + SQL schemas + audio/video in one graph); Leiden community detection (deterministic, no inference); edge provenance. The grapher for knowledge-work objectives codegraph can't serve — **adopted for that slot now**, runnable on **local Qwen** (custom provider, no cloud key, no egress). Leiden clustering needs no model; only non-code extraction and optional community naming use the LLM. (The codegraph-vs-graphify bake-off only governs whether graphify *replaces codegraph as the code default* — not its mixed-corpus availability.) `install-gate` before first use in a new context.
- **Neither available** — the skill surfaces the gap and proposes remediation (`codegraph init -i` / install graphify) and tags every effort/risk estimate as UNGROUNDED. It does not silently present vibe estimates as grounded.

Graph facts complement semantic fan-out (Explore subagents) — they are not substitutes. Topology findings (dead code, reachability) carry a staleness caveat; never rest a security or correctness claim on the graph alone.

### Crash-safe leases

A dispatched subagent can die mid-task and leave a task stuck in `progress` with no daemon to notice. A lease fixes that:

| Stage | What happens |
|---|---|
| `grant` | `tasks dispatch --lease-ttl 1800` grants a lease (TTL + fallback policy) |
| `heartbeat` | Every code-touching tool use resets the TTL (wired to PostToolUse hook) |
| `expiry` | Lazy sweep — run by `clawpm doctor` and on the next `tasks dispatch` |
| `fallback` | Task transitions per policy: `requeue` / `route-secondary` / `escalate-to-human` / `fail` |

No daemon: expiry is detected lazily on sweep, preserving the filesystem-first / no-daemon thesis. Append-only `leases.jsonl`, replayed to reconstruct state.

### Thrashing detection

After each iteration event (a Stop-hook `ok=false` outcome), `detect_thrashing` checks whether the last N consecutive iteration events show no measurable progress. Threshold priority: per-task `predictions.thrash_threshold` → `CLAWPM_THRASH_THRESHOLD` env → default 4. When thrashing is detected, the hook fires the fallback policy — preventing a subagent looping indefinitely at cost.

### Baseline drift reconciliation

Each task is stamped with `baseline_ref` (git HEAD SHA) at creation. Before dispatch, clawpm checks whether any file in the task's declared `scope` has changed since `baseline_ref`. If drift is detected, `tasks dispatch` refuses to proceed unless `--confirm-stale` is passed — forcing an explicit decision before dispatching against a stale plan.

### The won't-do / rejected ledger

Tasks transitioned to `rejected` (requires `--rationale`) move to `tasks/rejected/`. The ledger is a permanent record of ideas considered and declined. The `emit-tree` validation barrier diffs every incoming leaf title/scope against the ledger; a match either aborts emission (in `--strict` mode) or populates `rejected` in the output envelope for the planner to surface.

View the ledger: `clawpm tasks list -s rejected`.

---

## Installation

```bash
# Recommended (this fork — has all recent improvements)
uv tool install git+https://github.com/martinduncanson/clawpm

# Editable for development
git clone git@github.com:martinduncanson/clawpm.git ~/clawpm/projects/clawpm
uv tool install -e ~/clawpm/projects/clawpm

# Upstream version (older, missing recent features)
# uv tool install git+https://github.com/malphas-gh/clawpm
```

Requires Python 3.11+. `uv` recommended; `pip install -e <path>` also works.

---

## Configuration

Defaults work out of the box. Override via env vars or `~/clawpm/portfolio.toml`:

| Setting | Default | Override |
|---|---|---|
| Portfolio root | `~/clawpm` | `CLAWPM_PORTFOLIO` env var |
| Project roots | `~/clawpm/projects` | `CLAWPM_PROJECT_ROOTS` env var |
| Work log | `<portfolio>/work_log.jsonl` | (fixed) |
| Judge command | `claude --print` | `CLAWPM_JUDGE_CMD` env var |
| Thrash threshold | `4` consecutive non-progress iterations | `CLAWPM_THRASH_THRESHOLD` env var |

---

## Troubleshooting

**`clawpm add` returns `add_failed`** even though the project exists?
→ Check `.project/settings.toml`: `repo_path` must use forward slashes on Windows (`F:/Git/foo`, not `F:\Git\foo`). The CLI warns about this; old settings.toml files may need fixing.

**`no_project` when you are in the project directory?**
→ Walk up looking for `.project/settings.toml`; if missing, `clawpm project init` to create it.

**`clawpm projects list --all` doesn't show a project that has `.project/`?**
→ The project isn't registered in `portfolio.toml`. `clawpm project init` should register on init; if it didn't (older versions), re-init or manually add to portfolio.toml.

**Tests failing with TOMLDecodeError on Windows?**
→ Same backslash bug, in test fixture this time. Fork has it fixed in `tests/test_subtasks.py` since commit `ac65023`.

**`clawpm doctor` returns warnings about a stale `repo_path`?**
→ A previously-tracked project's directory was moved or deleted. Either restore the path or remove the project from `portfolio.toml`.

**`clawpm tasks dispatch` blocked with "baseline drift detected"?**
→ Files in the task's declared scope have changed since `baseline_ref` was stamped. Review the drift, then pass `--confirm-stale` to proceed.

**`emit-tree` returns `constitution_violations`?**
→ The project constitution has invariants that one or more leaves violate. Fix the tree and re-emit, or pass `--strict` to treat violations as a hard failure.

**Subagent appears to be looping without progress?**
→ Thrashing detection should catch this automatically after N iteration events. If it isn't firing, check `CLAWPM_THRASH_THRESHOLD` and the per-task `thrash_threshold` prediction field. `clawpm doctor --apply` sweeps expired leases.

---

## Integration with Claude Code

This fork ships a suite of Claude Code skills in `skills/`:

| Skill | Location | Purpose |
|-------|----------|---------|
| `clawpm` | `skills/clawpm/SKILL.md` | Core skill: when to invoke clawpm, how to file tasks, reflection discipline |
| `clawpm-planner` | `skills/clawpm-planner/SKILL.md` | Objective → vetted task-tree → emit |
| `clawpm-cowork` | `skills/clawpm-cowork/` | Bootstrap dance for Cowork's ephemeral VMs |

When clawpm is on PATH and Claude Code's skill loader is configured to find the `skills/` directory, the skills auto-load.

For Codex and other AGENTS.md runtimes, copy `AGENTS.md.template` from the repo root into your project, replace `<REPLACE-WITH-PROJECT-ID>` with your real project ID. See `codex-instructions.md` for deeper Codex adapter notes. The repo's own `AGENTS.md` (dogfooded) shows the instantiated shape.

For hands-off work-log capture, install `hooks/clawpm-sync/handler.py` as a Claude Code hook (see `hooks/clawpm-sync/HOOK.md`). Fires on every tool use plus session boundaries; appends structured entries to `work_log.jsonl` without manual `clawpm log` discipline.

The fork is designed for Codex review: `AGENTS.md.template` includes the Codex integration contract, and `docs/playbooks/codex-fix-dispatch.md` gives a canonical dispatch iteration loop for Codex-clean→merge cycles.

---

## Fork — upstream relationship

This repo is developed primarily at `github.com/martinduncanson/clawpm`. The upstream `malphas-gh/clawpm` is the original source; changes are pushed there as a courtesy when stable, but the fork is canonical for development cadence.

- `fork` remote → `github.com/martinduncanson/clawpm` — active branches and PRs; `main` is the source of truth.
- `origin` remote → `github.com/malphas-gh/clawpm` — cross-fork PRs for courtesy upstreaming after the work is merged on the fork.

Upstream merge cadence is not a blocker. Periodically reconcile `origin/main` back into `main` to absorb upstream-only changes.

---

## License

MIT
