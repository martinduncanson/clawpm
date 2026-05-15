---
name: clawpm-cowork
description: Bootstrap clawpm in a Cowork session. Cowork VMs are ephemeral — `~/clawpm/` resets each session. This skill clones the operator's portfolio repo, installs the clawpm CLI, and resumes context so cross-session task tracking works in Cowork. Use this skill at the start of any Cowork session where you'll need clawpm — task tracking, work-log persistence, research/issue logging, or resuming prior work. Trigger phrases: "set up clawpm in cowork", "bootstrap clawpm", "resume my clawpm context", "I need cross-session task tracking", or whenever a Cowork session begins and the operator references prior task state.
---

# clawpm-cowork

## What this skill does

Cowork sessions run in ephemeral VMs — the home directory resets every session. clawpm relies on `~/clawpm/` for portfolio config, work logs, and task state. Without bootstrapping, every Cowork session starts from zero.

This skill:

1. Clones the operator's portfolio repo to `~/clawpm/` (so portfolio.toml + projects/ + work_log.jsonl exist)
2. Installs the `clawpm` CLI from the latest source
3. Clones the `codex-review` skill into `~/.claude/skills/codex-review/` so PRE-REVIEW + Codex review discipline works on Cowork (it's not bundled with clawpm)
4. Runs `clawpm context` to resume where the operator left off
5. (Optional) Sets up a session-end push-back so changes during the session land back in the portfolio repo

## When to invoke

- **First action of any Cowork session** where clawpm will be used.
- When the operator says "I want to keep tracking this in clawpm" mid-session.
- When `clawpm context` returns "no portfolio found" — that's the trigger to bootstrap.

## Prerequisites the operator must have configured

Set environment variables in Cowork (or pass via the bootstrap command):

| Variable | Purpose | Example |
|---|---|---|
| `CLAWPM_PORTFOLIO_REPO` | Git URL of the operator's portfolio repo | `git@github.com:martinduncanson/clawpm-portfolio.git` |
| `CLAWPM_DEFAULT_PROJECT` | Project to set as active context after bootstrap | `polymarket-arb` |

The portfolio repo should contain at minimum: `portfolio.toml`, `projects/`, `work_log.jsonl`, optional `reflections/`.

## Bootstrap procedure

```bash
# 1. Clone portfolio
if [ -z "$CLAWPM_PORTFOLIO_REPO" ]; then
  echo "Set CLAWPM_PORTFOLIO_REPO to the operator's portfolio git URL first."
  exit 1
fi
git clone "$CLAWPM_PORTFOLIO_REPO" "$HOME/clawpm"

# 2. Install clawpm CLI
uv tool install git+https://github.com/martinduncanson/clawpm
# (Active fork — has all recent improvements. Upstream is malphas-gh/clawpm.)

# 3. Install codex-review skill (idempotent: clone if missing, pull if present)
mkdir -p "$HOME/.claude/skills"
if [ -d "$HOME/.claude/skills/codex-review/.git" ]; then
  git -C "$HOME/.claude/skills/codex-review" pull --ff-only
else
  git clone https://github.com/martinduncanson/codex-review.git "$HOME/.claude/skills/codex-review"
fi
# Public repo, no auth needed. Carries PRE-REVIEW step + 3-5 Concerns mandate + wait-for-codex.py.

# 4. Verify installation
clawpm setup --check

# 5. Resume context
if [ -n "$CLAWPM_DEFAULT_PROJECT" ]; then
  clawpm use "$CLAWPM_DEFAULT_PROJECT"
fi
clawpm context
```

## Persisting changes back to the portfolio repo

Cowork VMs lose state at session end. Push back periodically:

```bash
cd ~/clawpm
git add work_log.jsonl reflections/ projects/
git commit -m "cowork-session: $(date -u +%Y-%m-%dT%H:%MZ) progress"
git push
```

Hook this into a session-stop pattern so it's automatic. The operator's harness can wire a `Stop` hook that runs the commit-and-push.

## Common failures

- **Clone fails — auth missing**: ensure SSH keys are loaded in the Cowork session, or use HTTPS + a PAT
- **`uv tool install` fails — no internet**: Cowork should have outbound HTTP; if not, check VM network policy
- **`clawpm context` returns empty**: portfolio cloned but no projects yet — operator must `clawpm project init` for at least one project before context returns useful data
- **Merge conflict on push-back**: another session may have pushed since clone; pull and merge before pushing

## What this skill does NOT do

- Doesn't sync individual task files in real time. Sync happens on git push.
- Doesn't replace the Claude Code skill `clawpm` (which is loaded automatically when clawpm is on PATH). This skill only handles the bootstrap-and-restore lifecycle that Cowork specifically needs.
- Doesn't handle multiple concurrent Cowork sessions for the same operator — last push wins. If you're running parallel Cowork sessions, use scope claims (see clawpm `scope:` field) and dedicated branches.

## Reference

For the underlying clawpm CLI and workflow, see `clawpm` skill (auto-loaded once clawpm is on PATH).

For the rationale: see operator memory `feedback_clawpm_for_subagents.md` and the `clawpm-cowork` design notes in the clawpm repo at `cowork-bootstrap.sh`.
