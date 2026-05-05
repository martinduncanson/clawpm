# ClawPM

Filesystem-first multi-project task manager for AI agents. JSON CLI for tasks, work logs, and research across projects with [OpenClaw](https://clawhub.ai) skill integration.

[![ClawHub](https://img.shields.io/badge/ClawHub-clawpm-blue)](https://clawhub.ai/malphas-gh/clawpm)

## Features

- **Filesystem-first** — all state lives in markdown, TOML, and JSONL. No database.
- **JSON output** — every command emits JSONL by default for agent consumption
- **Multi-project** — manage tasks across a portfolio from one CLI
- **Auto-detection** — run commands from any project directory, no flags needed
- **Subtasks** — directory-based hierarchy with automatic parent/child tracking
- **Work log** — append-only JSONL log with auto-logging on state changes
- **OpenClaw skill** — installable as a Claude Code skill via ClawHub

## Installation

```bash
# From GitHub
uv tool install git+https://github.com/malphas-gh/clawpm

# For development
git clone git@github.com:malphas-gh/clawpm.git ~/clawpm/projects/clawpm
uv tool install -e ~/clawpm/projects/clawpm
```

## Quick Start

```bash
clawpm setup                       # Create portfolio at ~/clawpm/

cd /path/to/your/repo
clawpm project init                # Initialize project in any directory
clawpm add "Implement feature X"   # Add a task
clawpm start 1                     # Start working (auto-logs)
clawpm done 1 --note "Shipped"     # Complete it (auto-logs)

clawpm next                        # Next task across all projects
clawpm context                     # Full agent context for resuming work
```

## Shortcuts

| Command | Description |
|---------|-------------|
| `clawpm add "Title"` | Quick add a task |
| `clawpm add "Title" --parent 25` | Add subtask |
| `clawpm start 42` | Start working on task |
| `clawpm done 42` | Mark task done |
| `clawpm block 42 --note "reason"` | Mark blocked |
| `clawpm next` | Get next task across projects |
| `clawpm status` | Project overview |
| `clawpm context` | Full agent context (spec, tasks, log, git, issues) |
| `clawpm use <id>` | Set project context |

Short task IDs work everywhere: `42` expands to `CLAWP-042` based on project prefix.

## Commands

### Projects
```bash
clawpm projects list [--all]       # List projects (--all shows untracked repos)
clawpm projects next               # Next task across all projects
clawpm project init [--id myproj]  # Initialize project in cwd
clawpm project context             # Full project context
```

### Tasks
```bash
clawpm tasks                       # List open + in-progress + blocked
clawpm tasks list [-s all]         # Filter by state
clawpm tasks show <id>             # Full task details (includes scope)
clawpm tasks add -t "Title" [-b "body"] [--parent <id>] [--scope "src/**"]
clawpm tasks edit <id> [--title/--priority/--complexity/--body] [--scope "src/**"]
clawpm tasks state <id> open|progress|done|blocked [--note]
clawpm tasks split <id>            # Convert to parent directory
```

### Scope-Aware Dispatch

Declare file-glob scope on tasks so parallel agents don't collide:

```bash
# Declare scope when adding or editing a task
clawpm tasks add -t "Refactor auth" --scope "src/auth/**" --scope "tests/auth/**"

# Pre-flight check before dispatching a new agent
clawpm conflicts --scope "src/auth/login.py"
# → {"conflicts": [], "queried_scope": [...]}  ← safe to dispatch

# Or check by task ID (reads its declared scope)
clawpm conflicts --task CLAWP-042
```

Empty `conflicts` array = safe to dispatch. Exit code always 0.

### Work Log
```bash
clawpm log add --task <id> --action progress --summary "What I did"
clawpm log tail [--limit 10]       # Recent entries (auto-filtered to project)
clawpm log tail --all              # All projects
clawpm log tail --follow           # Live tail
clawpm log last                    # Most recent entry
clawpm log commit                  # Pull git commits into work log
```

State changes (`start`/`done`/`block`) auto-log with git files changed.

### Research & Issues
```bash
clawpm research add --type investigation --title "Question"
clawpm research list
clawpm issues add --type bug --severity high --actual "What happened"
clawpm issues list [--open]
```

## Reflection Layer (predictions vs actuals)

Capture predictions at task creation and mine the delta when tasks complete:

```bash
# Predict when adding a task
clawpm tasks add -t "Refactor auth" \
    --predict-duration 90 --predict-complexity m \
    --predict-scope "src/auth/**" \
    --hypothesis "JWT will cut session table contention by 80%"

# Reflect when done
clawpm done CLAWP-042 \
    --reflect-note "DB migration took 3x longer than expected" \
    --meta-reflect "should have checked existing schema constraints"
```

Reflection events are written to `~/clawpm/reflections/<task-id>.jsonl` with
predictions, actuals (computed from work log), and deltas (duration ratio,
files-changed ratio, scope overrun/unused, complexity match).

`clawpm reflect summarize/suggest/history-import` are Phase 2 stubs.

## Project Auto-Detection

ClawPM resolves your project automatically (in priority order):

1. `--project` flag
2. Current directory (walks up to find `.project/settings.toml`)
3. Auto-init if in an untracked git repo under project roots
4. Context from `clawpm use <project>`

## Task States

| State | File Location | Meaning |
|-------|---------------|---------|
| open | `tasks/PROJ-042.md` | Ready to work |
| progress | `tasks/PROJ-042.progress.md` | In progress |
| done | `tasks/done/PROJ-042.md` | Completed |
| blocked | `tasks/blocked/PROJ-042.md` | Waiting |

## Configuration

Works out of the box with defaults:
- Portfolio: `~/clawpm` (override: `CLAWPM_PORTFOLIO`)
- Project roots: `~/clawpm/projects` (override: `CLAWPM_PROJECT_ROOTS`)
- Work log: `~/clawpm/work_log.jsonl`

Optional `~/clawpm/portfolio.toml` for custom roots.

## OpenClaw Integration

Install as a Claude Code skill:

```bash
# Symlink skill for development
ln -s ~/clawpm/projects/clawpm/skills/clawpm ~/.openclaw/skills/clawpm
```

Or install via [ClawHub](https://clawhub.ai/malphas-gh/clawpm).

## License

MIT
