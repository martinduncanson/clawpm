# AGENTS.md — clawpm

> For agent runtimes that read `AGENTS.md` (Codex, agents-spec). For Claude Code, the canonical equivalent is `CLAUDE.md` (sibling file) and the `clawpm` skill auto-loads from `~/.claude/skills/clawpm/` or `~/.agents/skills/clawpm/`.

This project (clawpm) uses [`clawpm`](https://github.com/martinduncanson/clawpm) for task tracking and work-log persistence across agent sessions — **dogfooded.** Use it preferentially over session-scoped TODO lists.

Active fork: `martinduncanson/clawpm`. Upstream: `malphas-gh/clawpm`.

## Why

Session-scoped task tools disappear when the session ends. clawpm persists tasks, work logs, research notes, and issues to the filesystem — they survive session boundaries, agent handoffs, and sub-task dispatch.

## Project context

- **Project ID:** `clawpm` (matches `.project/settings.toml` `id` field)
- **Portfolio root:** `~/clawpm/` (override with `CLAWPM_PORTFOLIO` env var)
- **Settings file:** `.project/settings.toml` (forward slashes only on Windows)
- **SPEC:** `.project/SPEC.md`
- **Acceptance criteria:** `.project/acceptance.md`
- **Backlog:** `.project/tasks/`
- **Operator notes:** `.project/notes/` (read these before starting)

## Invocation patterns

```bash
# Resume work — read this first when starting any session
clawpm context --project clawpm

# List open tasks
clawpm tasks list --project clawpm --state open

# Add a task with predictions (propose-then-review for non-dictated work)
clawpm tasks add --project clawpm \
    -t "Title" -b "Description" \
    --predict-duration 60 --predict-complexity m \
    --predict-scope "src/clawpm/**" \
    --predicted-by agent --confidence 3 \
    --hypothesis "if X then Y" \
    --success-criteria "concrete measurable outcome" \
    --pre-mortem "most likely failure mode"

# Start work
clawpm start <task-id>

# Pre-flight scope check before dispatching parallel sub-tasks
clawpm conflicts --task <task-id>

# Close with reflection
clawpm done <task-id> \
    --note "what shipped" \
    --reflect-note "what surprised me" \
    --meta-reflect "what could have been anticipated that wasn't" \
    --actual-duration <minutes>

# Block on dependency
clawpm block <task-id> --note "Waiting on X"

# In-progress note (no state change)
clawpm log add --task <task-id> --action progress --summary "What I did"

# After git commit
clawpm log commit
```

## Lifecycle moments

| Moment | clawpm action |
|---|---|
| Session start | `clawpm context` to resume |
| Before any meaningful work unit (>~5 min) | `clawpm tasks add` + `clawpm start` |
| Before dispatching sub-tasks | `clawpm conflicts --task` to verify safe scope |
| On completion | `clawpm done` with `--reflect-note` |
| On blocker | `clawpm block --note` |
| Before commit | `clawpm log add --action progress` |
| After commit | `clawpm log commit` |
| Session end | `clawpm log tail --limit 5` to verify trail captured |

## Output format

JSON by default. `-f text` for human-readable. Agents consume JSON.

## Hard rules

- `.project/settings.toml` `repo_path` MUST use forward slashes on Windows. Backslashes parse silently and break path resolution.
- Per-project `.project/` IS committed (settings + spec + acceptance criteria + select notes).
- Never commit `~/clawpm/` — that's the operator's portfolio root, lives outside any repo.
- No emoji / non-ASCII glyphs in hand-written `click.echo` / `print` literals (Windows cp1252 crashes). Use `[OK]`, `[ ]`, `->`, `x`. Run `clawpm doctor --check-encoding` to verify.
- Sub-task depth > 2 is a smell — file `clawpm issues add --type observation --severity low --tag depth-warning`.

## Predictions block — "agent proposes, human reviews"

Propose all predictions in one block, then wait for confirm/edit. For operator-dictated tasks (operator gave title + description), file directly with predictions and surface in the end-of-turn summary.

```
"Adding CLAWP-XXX <title> with:
 - duration: 90min (confidence 3)
 - complexity: m
 - approach: <one-line>
 - success criteria: <measurable>
 - pre-mortem: <most likely failure>
 Confirm or edit?"
```

`--confidence` is honest: 1 = wild guess, 5 = done-this-exact-thing-before. The gut-vs-actual delta is the calibration substrate.

## Failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| `add_failed` | `repo_path` has backslashes | Edit `.project/settings.toml`, swap `\` → `/` |
| `no_project` | CWD walk found no `.project/settings.toml` | Pass `--project clawpm` explicitly or `cd` into project root |
| `portfolio_not_found` | First-time setup | `clawpm setup` |
| `UnicodeEncodeError` on stdout | Windows cp1252 + non-ASCII literal | Add `sys.stdout.reconfigure(encoding="utf-8", errors="replace")` at module top |

## Reference

- Active fork: https://github.com/martinduncanson/clawpm
- Upstream: https://github.com/malphas-gh/clawpm
- README: `README.md`
- Roadmap: `ROADMAP.md`
- Codex skill: `~/.agents/skills/clawpm/SKILL.md`
- Claude Code skill: `~/.claude/skills/clawpm/skills/clawpm/SKILL.md`
