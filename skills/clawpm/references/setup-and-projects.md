# ClawPM reference: setup and projects

Moved verbatim from `SKILL.md`. Covers: Install, first-time setup, creating projects, project auto-detection, short task IDs, subtasks, agent context, workflow example.

## Install

```bash
uv tool install git+https://github.com/martinduncanson/clawpm   # or: pipx install git+https://github.com/martinduncanson/clawpm
clawpm doctor                                                    # verify
```

`uv tool install` and `pipx install` put the package in an isolated environment and add only a `clawpm` shim to PATH. They do not make the package importable from your default Python, so `python -m clawpm` fails with `No module named clawpm`. Invoke `clawpm ...` through the shim. Use `python -m clawpm` only inside an environment where clawpm was installed with `pip install` (for example a dev checkout). If the shim is missing from PATH, run `uv tool update-shell` (or `pipx ensurepath`) and open a new terminal.

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
