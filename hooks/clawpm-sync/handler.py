#!/usr/bin/env python3
"""Claude Code PostToolUse + Stop hook — auto-logs clawpm invocations.

Reads the hook event payload from stdin, appends a structured entry to
`<portfolio_root>/work_log.jsonl` when:
- PostToolUse fires on Bash with a command beginning with `clawpm`.
- Stop / SubagentStop fires (session boundary marker).

Never blocks the calling tool — exits 0 on every code path, including
unrecognised events, missing portfolio, JSON parse errors. The portfolio
root comes from CLAWPM_PORTFOLIO env var, or `~/clawpm/` as the default
(matches the CLI's own resolution).

Install: see hooks/clawpm-sync/HOOK.md.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _portfolio_root() -> Path:
    """Resolve portfolio root from env or default ~/clawpm/."""
    env = os.environ.get("CLAWPM_PORTFOLIO")
    if env:
        return Path(env).expanduser()
    return Path.home() / "clawpm"


def _now_iso() -> str:
    """ISO-8601 UTC timestamp, Z-suffixed."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _append_work_log(entry: dict) -> None:
    """Append one JSON line to <portfolio>/work_log.jsonl. Best-effort."""
    root = _portfolio_root()
    log_path = root / "work_log.jsonl"
    try:
        root.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        # Surface to stderr (visible in transcript) but don't fail the hook.
        sys.stderr.write(f"[clawpm-sync] could not write work log at {log_path}\n")


def _handle_post_tool_use(event: dict) -> None:
    """If PostToolUse on a Bash `clawpm ...` command, log it."""
    if event.get("tool_name") != "Bash":
        return
    cmd = event.get("tool_input", {}).get("command", "")
    if not isinstance(cmd, str):
        return
    # Match "clawpm " at start, or after a shell op (&&, ||, ;, |, newline).
    # Skip the obvious recursive cases (our own writes).
    stripped = cmd.lstrip()
    if not stripped.startswith("clawpm "):
        return

    response = event.get("tool_response", {}) or {}
    exit_code = response.get("exit_code") if isinstance(response, dict) else None

    entry = {
        "ts": _now_iso(),
        "project": None,
        "task": None,
        "action": "tool_call",
        "agent": "claude-code",
        "session_key": event.get("session_id", ""),
        "summary": stripped[:200],
        "exit_code": exit_code,
        "cwd": event.get("cwd", ""),
        "source": "clawpm-sync hook",
    }
    _append_work_log(entry)


def _handle_session_boundary(event: dict, action: str) -> None:
    """Log a session-start or session-stop marker."""
    entry = {
        "ts": _now_iso(),
        "project": None,
        "task": None,
        "action": action,
        "agent": "claude-code",
        "session_key": event.get("session_id", ""),
        "summary": f"session {action}",
        "cwd": event.get("cwd", ""),
        "source": "clawpm-sync hook",
    }
    _append_work_log(entry)


def main() -> int:
    """Entry point. Read event JSON from stdin, dispatch, exit 0."""
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        event = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    if not isinstance(event, dict):
        return 0

    hook_name = event.get("hook_event_name", "")
    try:
        if hook_name == "PostToolUse":
            _handle_post_tool_use(event)
        elif hook_name == "Stop":
            _handle_session_boundary(event, "session_stop")
        elif hook_name == "SubagentStop":
            _handle_session_boundary(event, "subagent_stop")
        elif hook_name == "SessionStart":
            _handle_session_boundary(event, "session_start")
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[clawpm-sync] hook error: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
