# ClawPM (martinduncanson fork)

Filesystem-first task tracker for AI agents (and humans). Persistent across sessions, JSON-first, multi-project. Survives compaction, subagent dispatch, and reboots.

> Forked from [`malphas-gh/clawpm`](https://github.com/malphas-gh/clawpm). Adds: Windows TOML/backslash bug fix, `scope:` field + `clawpm conflicts` for parallel-agent safety, the reflection/calibration layer (predictions → actuals → learned ratios), **verifiable goals** (`--success-criteria` + a Stop-hook judge), **subagent dispatch** with **crash-safe leases**, cross-platform locked JSONL appends, Cowork bootstrap skill, Codex AGENTS.md template.

[![ClawHub](https://img.shields.io/badge/ClawHub-clawpm-blue)](https://clawhub.ai/malphas-gh/clawpm)

> **Status:** actively dogfooded. Core loop (add/start/done, scope, work-log, reflection) is stable. The agentic layer — rubric + Stop-hook judge, `tasks dispatch` / `agent dispatch`, crash-safe leases, `reflect summarize`/`suggest` — is shipped and tested. `clawpm serve` (web UI) is utilitarian. Python 3.11+. No database, no daemon.

---

## Table of contents

- [What it is](#what-it-is) · [Why use it](#why-use-it) · [5-minute quickstart](#5-minute-quickstart)
- [When to use it](#when-to-use-it-and-when-not) · [Common workflows](#common-workflows)
- [**Verifiable goals & crash-safe dispatch**](#verifiable-goals--crash-safe-dispatch) — the agentic layer
- [All commands](#all-commands) · [How it works](#how-it-works-architecture-in-30-seconds)
- [Installation](#installation) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting)
- [Claude Code / Codex integration](#integration-with-claude-code) · [License](#license)

---

## What it is

A small Python CLI that turns your filesystem into a multi-project task manager. Every task, work-log entry, research note, and issue lives as a markdown / JSON file under `~/clawpm/`. No database. No daemon. No external service.

The point: **task state survives session boundaries.** When Claude Code, Codex, or you yourself stop and resume two days later, `clawpm context` brings everything back — what you were doing, what's next, what's blocked, what code changed.

## Why use it

Three problems it solves:

1. **Session-scoped TODOs vanish.** Native task lists in Claude Code / Codex / Cowork die when the session ends. clawpm tasks persist.
2. **Multiple projects, one head.** Track 8 client projects + 3 side projects from one CLI. `clawpm next` answers "what should I work on?" across all of them.
3. **Parallel agents collide.** When you dispatch 3 subagents in worktrees, they can write to overlapping files. clawpm's `scope:` field + `conflicts` query prevents the collision before it happens.

## 5-minute quickstart

```bash
# 1. Install
uv tool install git+https://github.com/martinduncanson/clawpm

# 2. Create your portfolio (one-time)
clawpm setup
# → ~/clawpm/  with portfolio.toml, projects/, work_log.jsonl

# 3. Initialize a project (run from inside the project's directory)
cd ~/code/my-project
clawpm project init
# → .project/settings.toml created in your repo

# 4. Add your first task
clawpm add "Migrate auth to JWT"
# → CLAWP-001 created
# (Output is JSON by default. Add `-f text` for human-readable.)

# 5. Start working
clawpm start 1            # Auto-logs the start
# ... do work, edit files, commit ...
git commit -m "feat: JWT skeleton"

# 6. Done
clawpm done 1 --note "JWT login + middleware shipped"
# → moves CLAWP-001 to done/, auto-logs files changed

# 7. Resume tomorrow (different session)
clawpm context
# → JSON with: in-progress task, next-up task, blockers, recent log,
#   git status, open issues. Everything you need to pick up where you left off.
```

That's the loop. **Add → start → done.** Everything else is optional sugar.

## When to use it (and when not to)

| Use clawpm | Don't use clawpm |
|---|---|
| Multi-step work that'll outlive this session | Quick TODOs you'll finish in the next 10 minutes |
| Cross-project work (e.g. "what's blocking me anywhere?") | One-off scripts in /tmp |
| Subagent dispatch where multiple agents touch shared files | Pure conversation / brainstorming with no concrete deliverables |
| You want a work-log audit (what did I commit, when, against which task?) | Tasks that have no code/file output |
| You want to track research notes, issues, blockers durably | Single-file notes that fit in your shell history |
| You want to capture predictions vs actuals (reflection) | Tasks where prediction-vs-reality has no learning value |

If you're using Claude Code's native `TaskCreate` and your tasks vanish at compaction or session end and you wished they hadn't — that's the trigger to switch to clawpm.

## Common workflows

### Workflow 1 — Solo dev, one project

```bash
cd ~/code/my-app
clawpm context                    # See what's open
clawpm next                       # Get the highest-priority next task
clawpm start <id>
# work...
git commit -m "..."
clawpm done <id> --note "Shipped"
```

Nothing fancy. clawpm just remembers what you were doing.

### Workflow 2 — Multi-project portfolio

```bash
# At the start of your day, no flags, anywhere:
clawpm projects next
# → next task across ALL your projects, ranked by priority
```

You don't need to remember which project's turn it is. Priority + dependencies decide.

### Workflow 3 — Parallel agent dispatch

Before sending 3 subagents off to different worktrees:

```bash
# Each task declares its file scope upfront
clawpm tasks add -t "Refactor auth" --scope "src/auth/**" --scope "tests/auth/**"
clawpm tasks add -t "Add billing flow" --scope "src/billing/**"
clawpm tasks add -t "Update docs" --scope "docs/**"

# Pre-flight check before dispatch — does this new task collide with any in-flight one?
clawpm conflicts --task CLAWP-NEW
# → empty conflicts = safe. Otherwise, list of overlapping in-flight tasks.

# Each subagent: clawpm start <id>, do work, clawpm done <id>
# When all 3 done, scope claims clear automatically.
```

This is the killer feature for parallel-agent setups. **No more "oh, both agents edited cli.py and now we have a merge conflict."**

### Workflow 4 — Capturing learning from each task (reflection layer)

Predict when you create the task; reflect when you finish:

```bash
# Up front — what do you think will happen?
clawpm tasks add -t "Migrate to Postgres 16" \
    --predict-duration 120 \
    --predict-complexity l \
    --predict-files-changed 8 \
    --predict-scope "migrations/**" --predict-scope "src/db/**" \
    --predict-frameworks alembic --predict-frameworks sqlalchemy \
    --predict-pitfalls "constraint conflicts on unique indexes" \
    --hypothesis "performance will improve 30% on the slow analytics queries"

# Later, when done — what actually happened?
clawpm done CLAWP-042 \
    --note "Migration shipped, queries 12% faster" \
    --reflect-note "constraint conflicts hit, took 2 extra hours" \
    --meta-reflect "should have run a dry-run dump first to surface the conflicts; will do that next time"
```

clawpm computes the deltas (duration ratio, scope overrun, complexity match, etc.) and writes a structured reflection event to `~/clawpm/reflections/<task-id>.jsonl`. Over weeks, you build a corpus you can mine: "tasks I labeled `m` complexity actually averaged 1.8× duration." `clawpm reflect summarize` quantifies actual-vs-predicted duration ratios bucketed by complexity / confidence / agent-profile, and `clawpm reflect suggest` deflates a fresh gut estimate by the learned median ratio (falling back to the global ratio when a bucket has <5 samples). `clawpm reflect history-import --source <dir>` back-fills the corpus by scanning historical agent log files for task-ID mentions.

v1.5 extends this with **applied-science framing**: `--success-criteria` (measurable performance contracts), `--predict-approach` (architectural choice), `--unknowns` (meta-curiosity), `--confidence` (1-5), `--reference-task` (outside-view anchoring), and `--pre-mortem` (Klein's pre-mortem). At completion, `--process-lesson` and `--surprise` (fixed taxonomy) close the recursive meta-loop. See `skills/clawpm/SKILL.md` for the full picture.

> **Agent discipline:** when an AI agent (e.g. Claude) adds a task on your behalf, it must always include `--predict-duration` and `--predict-complexity`. Empty predictions produce structurally-empty events with no calibration signal. Unit suffixes are accepted: `2h`, `3d`, `1w` (wall-clock, not 8-hour workday).

### Workflow 5 — Resuming after context loss

```bash
# Yesterday's session compacted, or you went home, or the laptop crashed.
clawpm context
# → JSON: in-progress task, body, work log entries, git status of project,
#   recent commits, open issues, predictions you set, recent reflections.
```

This single command is enough briefing for an agent (or you) to resume cold.

## Verifiable goals & crash-safe dispatch

This is what separates clawpm from a task list. A task isn't just a title — it can be a **verifiable contract** that an independent judge enforces, dispatched to a subagent that *cannot* declare itself done until the contract is met, with a **lease** that detects the subagent dying mid-task.

### Goals as contracts (`--success-criteria`)

Frame a task as a measurable goal, not a vague intent. "Add validation" → "write tests for invalid inputs, then make them pass."

```bash
clawpm tasks add -t "Migrate auth to JWT" \
    --success-criteria "P95 login latency < 200ms" \
    --success-criteria "session-table writes drop >= 50%"
clawpm tasks emit-rubric CLAWP-042 --format markdown   # render the gradeable rubric
```

### The Stop-hook judge

When a task is dispatched, a small LLM judge reads the subagent's transcript against the rubric and returns `{ok, reason}` / `{ok:false, impossible}` — the same contract the official Claude Code `/goal` evaluator uses. Wired as a **Stop hook**, the subagent literally cannot terminate until the rubric is satisfied *or* impossibility is independently confirmed. The judge is `claude --print` by default (override with `CLAWPM_JUDGE_CMD`) and **falls back to a local model** (Ollama) when the primary is unavailable, so grading keeps working subscription-cost-free. High-confidence closes can be gated by an **adversarial confirm-close** pass (`--confirm-close`): a refutation vote tries to disprove `ok=true` before the task closes, because a false "done" is the one terminal error.

### Subagent dispatch

```bash
clawpm tasks dispatch CLAWP-042 --worktree      # write hook-wired .claude/settings.local.json into an isolated worktree
clawpm agent dispatch --prompt "…" --rubric-criteria "…"   # one-command spawn + grade + persist verdict
```

`dispatch` instruments a directory so a hand-launched (or Task-tool-spawned) subagent gets the Stop-hook rubric gate, a PostToolUse work-log/heartbeat, and its rubric injected at SessionStart — integration by construction, the subagent never needs to know clawpm exists.

### Crash-safe leases

A dispatched holder can die mid-task — a crashed session, a killed worktree — and stall forever with no daemon to notice. A **lease** fixes that:

| Stage | What happens |
|---|---|
| `grant` | `tasks dispatch --lease-ttl 1800` grants a lease (TTL + fallback policy) |
| `heartbeat` | every code-touching tool use resets the TTL (wired to the PostToolUse hook) |
| `expiry` | a lazy **sweep** — run by `clawpm doctor` and on the next `tasks dispatch` — detects leases past TTL |
| `fallback` | the task transitions per policy: `requeue` / `route-secondary` / `escalate-to-human` / `fail` |

```bash
clawpm tasks dispatch CLAWP-042 --worktree --lease-ttl 1800 --fallback-policy requeue
clawpm lease list                 # active leases + expiry + policy
clawpm lease sweep                # reap dead holders now (or let doctor do it)
```

No daemon: expiry is detected lazily on sweep, never by a timer — preserving the filesystem-first / no-daemon thesis. Append-only `leases.jsonl`, replayed to reconstruct state. (The lease model is design-donored from [agenticq](https://github.com/martinduncanson/agenticq); see `docs/playbooks/` for dispatch patterns.)

## All commands

### Top-level shortcuts

| Command | Equivalent | Description |
|---|---|---|
| `clawpm add "Title"` | `clawpm tasks add -t "Title"` | Quick add a task |
| `clawpm add "Title" --parent 25` | — | Add subtask |
| `clawpm start 42` | `clawpm tasks state 42 progress` | Start working (auto-logs) |
| `clawpm done 42` | `clawpm tasks state 42 done` | Mark done (auto-logs files changed) |
| `clawpm block 42 --note "reason"` | `clawpm tasks state 42 blocked` | Mark blocked |
| `clawpm next` | `clawpm projects next` | Next task across projects |
| `clawpm status` | — | Project overview |
| `clawpm context` | — | Full agent context (spec, tasks, log, git, issues) |
| `clawpm use <id>` | — | Set project context |
| `clawpm conflicts --task <id>` | — | Pre-flight check for scope overlap |
| `clawpm serve` | — | Start web dashboard at http://127.0.0.1:8080 |

Short task IDs work everywhere: `42` expands to `CLAWP-042` based on project prefix.

### Projects

```bash
clawpm projects list [--all]       # List projects (--all shows untracked repos)
clawpm projects next               # Next task across all projects
clawpm project init [--id myproj]  # Initialize project in cwd
clawpm project context             # Full project context
clawpm project doctor              # Health check
```

### Tasks

```bash
clawpm tasks                       # List open + in-progress + blocked
clawpm tasks list [-s all]         # Filter by state (open/progress/done/blocked/all)
clawpm tasks show <id>             # Full task details (predictions, scope, body)
clawpm tasks add -t "Title" [-b "body"] [--parent <id>] [--scope "glob/**"]
                                   # plus prediction flags — see Reflection below
clawpm tasks edit <id> [--title/--priority/--complexity/--body/--scope]
clawpm tasks state <id> <state> [--note] [--reflect-note] [--meta-reflect]
clawpm tasks split <id>            # Convert to parent directory for subtasks
```

### Scope (parallel-agent safety)

```bash
clawpm tasks add -t "..." --scope "src/foo/**" --scope "tests/foo/**"
clawpm conflicts --scope "src/foo/login.py"  # ad-hoc query
clawpm conflicts --task CLAWP-042            # use task's declared scope
```

Always exits 0. Read the JSON `conflicts` array — empty = safe.

### Dispatch, judge & leases (the agentic layer)

```bash
clawpm tasks emit-rubric <id> [--format markdown]   # render a task's success-criteria as a gradeable rubric
clawpm tasks dispatch <id> [--worktree] [--confirm-close] \
       [--lease-ttl <secs>] [--fallback-policy requeue|route-secondary|escalate-to-human|fail]
clawpm agent dispatch --prompt "..." --rubric-criteria "..." [--confirm-close]
                                   # spawn a subagent, grade vs rubric, persist the verdict
clawpm hook eval-stop --task <id>  # the Stop-hook judge (invoked by dispatched settings; CLAWPM_JUDGE_CMD overridable)

clawpm lease grant --task <id> --ttl <secs> --fallback-policy <p>
clawpm lease list [--project <p>]  # active leases, expiry, policy
clawpm lease heartbeat --task <id> # liveness beat (the PostToolUse hook calls this)
clawpm lease release --task <id>   # clean completion — never swept
clawpm lease sweep [--dry-run]     # reap expired leases (doctor runs this too)
```

See [Verifiable goals & crash-safe dispatch](#verifiable-goals--crash-safe-dispatch) for the full picture.

### Work log

```bash
clawpm log add --task <id> --action progress --summary "What I did"
clawpm log tail [--limit 10]       # Recent entries (auto-filtered to project)
clawpm log tail --all              # All projects
clawpm log tail --follow           # Live tail
clawpm log last                    # Most recent entry
clawpm log commit [-n 10]          # Pull recent git commits into work log
```

State changes (`start`/`done`/`block`) auto-log with files changed.

For hands-off logging of *every* `clawpm` CLI invocation plus session boundaries, install the `hooks/clawpm-sync/` Claude Code hook — it fires on `PostToolUse` / `Stop` / `SessionStart` events and appends structured entries to `work_log.jsonl`. See `hooks/clawpm-sync/HOOK.md` for the JSON-config snippet.

### Research & issues

```bash
clawpm research add --type investigation --title "Question"
clawpm research list
clawpm issues add --type bug --severity high --actual "What happened"
clawpm issues list [--open]
```

### Reflection (predictions, actuals, deltas)

```bash
# Add task with predictions
clawpm tasks add -t "Title" \
    --predict-duration 90              # minutes
    --predict-complexity m              # s/m/l/xl
    --predict-files-changed 5
    --predict-scope "src/foo/**"        # repeatable
    --predict-frameworks fastapi        # repeatable
    --predict-pitfalls "free text"
    --hypothesis "free text"

# Complete with reflection
clawpm done <id> \
    --reflect-note "what surprised me"
    --meta-reflect "what could have been anticipated that wasn't"
```

A reflection event is written to `~/clawpm/reflections/<task-id>.jsonl` with predictions, actuals (computed from work log), and deltas (duration ratio, files-changed ratio, scope overrun/unused, complexity match).

```bash
clawpm reflect summarize             # actual/predicted duration ratios, bucketed by complexity/confidence/agent
clawpm reflect suggest --duration 2h # deflate a gut estimate by the learned median ratio
clawpm reflect history-import --source <dir>   # back-fill the corpus from session transcripts
```

### Web dashboard

```bash
clawpm serve                       # Start on http://127.0.0.1:8080
clawpm serve --port 8888           # Custom port
```

Real-time view of blockers, in-flight tasks, projects. Quick-add forms. Pause/resume projects.

### Admin

```bash
clawpm setup                       # First-time portfolio creation
clawpm setup --check               # Verify installation
clawpm doctor                      # Health check (settings.toml, paths, stale dispatches, expired leases)
clawpm doctor --apply              # Run deterministic remediation arms (incl. reaping expired leases)
clawpm doctor --check-codex        # Warn on projects without Codex GitHub app
clawpm doctor --check-encoding     # AST-scan .py for cp1252-risk patterns
clawpm version
```

Looking for a worked example portfolio to copy as a seed? See `examples/portfolio/` — drop-in fixtures with sample projects (alpha / beta / _inbox), tasks across all states, and a populated `work_log.jsonl`. Edit the absolute paths in `portfolio.toml` to match your machine before pointing `CLAWPM_PORTFOLIO` at it.

## How it works (architecture in 30 seconds)

```
~/clawpm/                                  ← portfolio root
├── portfolio.toml                         ← project registry, project_roots
├── work_log.jsonl                         ← append-only log of every state change
├── reflections/                           ← per-task reflection + iteration events
│   └── CLAWP-042.jsonl
├── dispatches.jsonl                        ← append-only dispatch registry (subagent targets)
├── leases.jsonl                            ← append-only lease ledger (TTL/heartbeat/expiry)
└── projects/                              ← legacy slot, mostly unused now

<your project repo>/.project/              ← per-project state (committed to repo)
├── settings.toml                          ← project ID, repo_path, prefix
├── spec.md                                ← project goals/spec (you author)
└── tasks/
    ├── CLAWP-001.md                       ← open tasks live FLAT here
    ├── CLAWP-002.progress.md              ← in-progress
    ├── done/CLAWP-003.md                  ← done
    ├── blocked/CLAWP-004.md               ← blocked
    └── CLAWP-005/                         ← parent task with subtasks
        ├── _task.md
        └── CLAWP-005-001.md
```

Two state stores:
- **Portfolio** (`~/clawpm/`) — global state across all projects: who exists, what was logged
- **Per-project `.project/`** — task files, project spec, settings; lives in the repo and gets committed

Output is JSON by default for agent consumption. Add `-f text` for humans.

## Project auto-detection

clawpm resolves your project automatically (priority order):

1. `--project` flag
2. Current directory (walks up to find `.project/settings.toml`)
3. Auto-init if you're inside an untracked git repo under `project_roots`
4. Context from `clawpm use <id>` (sticks across commands in the shell)

You almost never need `--project` if you `cd` into the project first.

## Task states & file locations

| State | File pattern | Meaning |
|---|---|---|
| open | `tasks/PROJ-042.md` | Ready to work |
| progress | `tasks/PROJ-042.progress.md` | In progress |
| done | `tasks/done/PROJ-042.md` | Completed |
| blocked | `tasks/blocked/PROJ-042.md` | Waiting on something |

State transitions move files between locations. Subtasks move with their parent.

## Installation

```bash
# Recommended (this fork — has all the recent improvements)
uv tool install git+https://github.com/martinduncanson/clawpm

# Editable for development
git clone git@github.com:martinduncanson/clawpm.git ~/clawpm/projects/clawpm
uv tool install -e ~/clawpm/projects/clawpm

# Upstream version (older, missing recent fixes/features)
# uv tool install git+https://github.com/malphas-gh/clawpm
```

Requires Python 3.11+. `uv` recommended; `pip install -e <path>` also works.

## Configuration

Defaults work out of the box. Override via env vars or `~/clawpm/portfolio.toml`:

| Setting | Default | Override |
|---|---|---|
| Portfolio root | `~/clawpm` | `CLAWPM_PORTFOLIO` env var |
| Project roots | `~/clawpm/projects` | `CLAWPM_PROJECT_ROOTS` env var |
| Work log | `<portfolio>/work_log.jsonl` | (fixed) |

## Troubleshooting

**`clawpm add` returns `add_failed`** even though the project exists?
→ Check `.project/settings.toml`: `repo_path` must use forward slashes on Windows (`F:/Git/foo`, not `F:\Git\foo`). The CLI now warns about this; old settings.toml files written by earlier versions may need fixing.

**`no_project` when you're in the project directory?**
→ Walk up looking for `.project/settings.toml`; if missing, `clawpm project init` to create it.

**`clawpm projects list --all` doesn't show a project that has `.project/`?**
→ The project isn't registered in `portfolio.toml`. `clawpm project init` should register on init; if it didn't (older versions), reinit or manually add to portfolio.toml.

**Tests failing with TOMLDecodeError on Windows?**
→ Same backslash bug, in test fixture this time. Fork has it fixed in `tests/test_subtasks.py` since commit `ac65023`.

**`clawpm doctor` returns warnings about a stale repo_path?**
→ A previously-tracked project's directory was moved or deleted. Either restore the path or remove the project from `portfolio.toml`.

## Integration with Claude Code

The fork ships a Claude Code skill (`skills/clawpm/SKILL.md`). When clawpm is on PATH and Claude Code's skill loader is configured to find this directory, the skill auto-loads — Claude knows when to invoke clawpm without you having to instruct it each time.

A complementary `clawpm-cowork` skill at `~/.claude/skills/clawpm-cowork/` handles the bootstrap dance for Cowork's ephemeral VMs.

For Codex (and other AGENTS.md runtimes), copy `AGENTS.md.template` from the repo root into your project, replace `<REPLACE-WITH-PROJECT-ID>` with your real project ID, and Codex picks up the integration automatically. See `codex-instructions.md` for deeper Codex adapter notes. The repo's own `AGENTS.md` (dogfooded) shows the instantiated shape.

For hands-off work-log capture, install `hooks/clawpm-sync/handler.py` as a Claude Code hook (see `hooks/clawpm-sync/HOOK.md`). Fires on every `clawpm` CLI invocation plus session boundaries; appends structured entries to `work_log.jsonl` without manual `clawpm log` discipline.

## Optimal use cases

- **AI-agent orchestration** — clawpm is the persistent state layer for Claude Code / Codex / Cowork sessions, so agents can resume cleanly across sessions and not duplicate work
- **Multi-client consultancy** — one CLI, all your client portfolios, durable work-log audit ("what did I do for client X this month?")
- **Long-running projects** — multi-month projects where session-scoped tools lose context every week
- **Parallel agent dispatch** — declared scopes prevent collisions; pre-flight `conflicts` check is the safety net
- **Disciplined retrospectives** — predictions + reflections turn ad-hoc gut feel into structured data you can mine

## Where it's not the right tool

- A team-wide bug tracker — clawpm is single-operator, filesystem-local. Use Linear / GitHub Issues for shared work
- A real-time kanban board with stakeholders — the web UI is utilitarian, not for non-technical viewers
- A replacement for `git` history — work log captures *task-related* commits, not all repo activity
- A pomodoro timer or time-tracker — duration is computed from start→done, not from minute-by-minute sessions

## OpenClaw integration

Install as a Claude Code skill via [ClawHub](https://clawhub.ai/malphas-gh/clawpm) — or for development, symlink the skill subtree:

```bash
ln -s ~/clawpm/projects/clawpm/skills/clawpm ~/.openclaw/skills/clawpm
```

## License

MIT
