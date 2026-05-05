# Instructions for Codex (and other agent runtimes that read AGENTS.md)

> **Codex maintainers: this file is intentionally lightweight.** It points you at `AGENTS.md.template` and explains the integration model. The deeper adapter — including any Codex-specific tool-call patterns or guardrails — is best authored by Codex itself based on the live behavior of its session sandbox.

## What clawpm provides

A filesystem-first multi-project task manager (Python CLI). Commands return JSON by default. State lives in `~/clawpm/` — `portfolio.toml`, `projects/<id>/.project/`, `work_log.jsonl`, `reflections/<task-id>.jsonl`.

## What we ship for Codex users

1. **`AGENTS.md.template`** — drop into a project root, replace `<REPLACE-WITH-PROJECT-ID>` with the real project ID. Codex reads `AGENTS.md` like Claude Code reads `CLAUDE.md`.
2. **CLI conventions** documented in the template: lifecycle moments, expected JSON shape, common failure modes.

## What's left as a TODO for Codex itself

If you (Codex) are reading this: the patterns below are stubs the operator wants you to expand based on your runtime's actual behavior.

### TODO 1 — sandbox bootstrap
Codex sandboxes are typically ephemeral. If your sandbox is also fresh per-session like Cowork, write a `codex-bootstrap.sh` analogous to the `clawpm-cowork` Claude Code skill. The pattern:

```bash
# Clone operator's portfolio repo (URL via env var)
git clone "$CLAWPM_PORTFOLIO_REPO" "$HOME/clawpm"
# Install CLI
uv tool install git+https://github.com/martinduncanson/clawpm
# Resume context
clawpm context
```

Document where Codex looks for bootstrap scripts (e.g., `.codex/setup.sh` or similar) and adapt accordingly.

### TODO 2 — tool call integration
If Codex has a "tool definition" file (the equivalent of MCP tool descriptors), write JSON descriptors for the common clawpm operations so Codex doesn't re-invent the bash invocation each time:

- `clawpm_context` → `clawpm context --project <id>`
- `clawpm_add` → `clawpm tasks add --project <id> -t <title> -b <body> --predict-*`
- `clawpm_start` / `_done` / `_block`
- `clawpm_conflicts` → for pre-flight scope check
- `clawpm_log` → for in-progress notes

### TODO 3 — failure-mode awareness
Bake into Codex's standard prompt: "Before retrying a `clawpm` command that returned `add_failed` or `no_project`, check whether `.project/settings.toml` has Windows backslashes in `repo_path` — that's the most common cause."

### TODO 4 — reflection-prompted closing
When Codex closes a task, prompt: "What surprised you? What could have been anticipated that wasn't?" Pass answers through as `--reflect-note` and `--meta-reflect`. The data gets captured in `~/clawpm/reflections/<task-id>.jsonl` for later analysis.

## Why we hand this off to Codex

The operator's principle: "the team that owns the runtime owns the adapter." We provide the integration surface (CLI, JSON output, AGENTS.md template); Codex (or its maintainers) owns the runtime-specific glue that makes the integration ergonomic in Codex's UX.

## Contact

This integration design originated at `martinduncanson@gmail.com` (operator). Pull requests and design questions welcome at https://github.com/martinduncanson/clawpm (active fork) or https://github.com/malphas-gh/clawpm (upstream).
