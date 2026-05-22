# clawpm-sync hook

Claude Code hook that auto-logs `clawpm` CLI invocations and session boundaries to `<portfolio_root>/work_log.jsonl`. Removes the discipline burden of manually calling `clawpm log commit` / `clawpm log add` after every meaningful action.

## What it captures

- **PostToolUse** on `Bash` tool where command starts with `clawpm ` → one log entry per invocation (timestamp, command summary, exit code, cwd).
- **SessionStart** → session-start marker.
- **Stop** → session-stop marker.
- **SubagentStop** → subagent-stop marker.

Each entry lands in `<portfolio_root>/work_log.jsonl` as a single JSON line. Project / task fields are left `null` — the hook deliberately doesn't guess. Enrich downstream with `clawpm log commit` (which captures git context) or analyse via `clawpm log tail`.

## Why a hook instead of just calling `clawpm log` after each command

- Catches every `clawpm` invocation automatically, including those an agent forgets to follow up.
- Captures `Stop` / `SubagentStop` events that don't correspond to any clawpm command — useful for session-boundary auditing.
- Removes "did I remember to log this?" cognitive load.

## Install

1. Make sure `clawpm` is installed (`pipx install git+https://github.com/martinduncanson/clawpm`) and `CLAWPM_PORTFOLIO` resolves to your portfolio directory (or accept the `~/clawpm/` default).

2. Choose the scope:
   - **Global** (every Claude Code session) → edit `~/.claude/settings.json`.
   - **Per-project** → edit `<repo>/.claude/settings.json`.

3. Merge the snippet from `settings.example.json` into your settings file. Replace `REPLACE_WITH_ABSOLUTE_PATH` with the absolute path to your clawpm checkout (Windows: forward slashes, e.g. `F:/Git/clawpm`).

4. Restart Claude Code (or reload settings) and run `clawpm tasks list` — `work_log.jsonl` should grow by one entry.

## Behaviour guarantees

- **Never blocks the calling tool.** Hook exits 0 on every code path, including JSON parse errors, unwriteable portfolio, unexpected event shapes.
- **Best-effort writes.** If `work_log.jsonl` can't be opened (permission, disk full, locked file), the error goes to stderr but the hook returns 0.
- **No portfolio creation.** The handler creates the portfolio directory only if it doesn't exist; it never overwrites existing files.
- **No transcript reading.** The handler operates on the hook event payload only — does not parse the session transcript, does not call out to an LLM, does not capture anything the operator hasn't already invoked via `clawpm` directly.

## Test

```bash
# Smoke: feed a synthetic PostToolUse event
echo '{"hook_event_name":"PostToolUse","tool_name":"Bash","tool_input":{"command":"clawpm tasks list"},"tool_response":{"exit_code":0},"session_id":"test-123","cwd":"/tmp"}' \
  | python hooks/clawpm-sync/handler.py

# Verify
tail -1 ~/clawpm/work_log.jsonl
```

Expected entry shape:
```json
{
  "ts": "2026-05-22T12:34:56Z",
  "project": null,
  "task": null,
  "action": "tool_call",
  "agent": "claude-code",
  "session_key": "test-123",
  "summary": "clawpm tasks list",
  "exit_code": 0,
  "cwd": "/tmp",
  "source": "clawpm-sync hook"
}
```

## Uninstall

Remove the hook entries from `settings.json`. The handler script does nothing if it isn't invoked.

## Previous shape

The upstream `malphas-gh/clawpm` shipped a TypeScript handler targeting the OpenClaw harness's hook contract. That file was deleted in commit `ea45004` during the uv-tool consolidation. This rewrite targets Claude Code's modern JSON-config-plus-subprocess hook model, which is the default for any new Claude Code install.
