# Example portfolio

Reference fixtures showing the canonical `clawpm` portfolio layout. Use these to learn the shape, or as a seed when bootstrapping your own portfolio.

## Layout

```
examples/portfolio/
├── portfolio.toml                  # portfolio-root config
├── work_log.jsonl                  # append-only session log
└── projects/
    ├── alpha/.project/             # active project, priority 2
    │   ├── settings.toml           # id, name, status, repo_path, labels
    │   ├── research/               # research notes (markdown)
    │   └── tasks/
    │       ├── ALPHA-001.md        # open
    │       ├── ALPHA-002.progress.md  # in-progress
    │       ├── blocked/ALPHA-003.md
    │       └── done/ALPHA-000.md
    ├── beta/.project/              # paused project
    └── _inbox/.project/            # catch-all project (priority 99)
```

## Using the example

These files use placeholder paths (`/home/user/...`). To run the example locally:

```bash
# Copy fixtures somewhere writable
cp -r examples/portfolio ~/clawpm-demo

# Edit portfolio.toml to point at the real location
# Replace portfolio_root and project_roots with absolute paths.
# On Windows, use forward slashes: F:/path/to/portfolio

# Point CLAWPM_PORTFOLIO at the demo and run
export CLAWPM_PORTFOLIO=~/clawpm-demo
clawpm projects list
clawpm tasks list --project alpha
clawpm context --project alpha
```

## Hard rules

- `portfolio.toml` `portfolio_root` and `project_roots` are absolute paths.
- On Windows, **forward slashes only** in all TOML path values. Backslashes parse silently but break path resolution (the failure mode is `add_failed` / `no_project` with no useful error).
- `.project/settings.toml` `id` field MUST match the parent directory name for canonical discovery.

## Schema reference

See `<repo>/.project/SPEC.md` for the full schema. Quick reference:

**portfolio.toml**
```toml
portfolio_root = "/absolute/path/to/portfolio"
project_roots = ["/absolute/path/to/portfolio/projects"]

[defaults]
status = "active"
```

**`.project/settings.toml`**
```toml
id = "project-id"                     # matches dir name
name = "Human Project Name"
status = "active"                     # active | paused | archived
priority = 2                          # lower = higher priority
repo_path = "/absolute/path/to/repo"  # optional; where the source lives
labels = ["tag1", "tag2"]             # optional
```

**`.project/tasks/<TASK-ID>.md`**
```yaml
---
id: ALPHA-001
priority: 2
complexity: m       # s | m | l
depends: []
---
# Task title

Description of what needs doing.
```

Modern tasks (post Phase 1) also carry predictions (duration, scope, hypothesis, confidence, success criteria, pre-mortem) — see `<repo>/skills/clawpm/SKILL.md` for the full `clawpm tasks add` invocation.
