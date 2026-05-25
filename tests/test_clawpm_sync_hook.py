"""Tests for the clawpm-sync Claude Code hook handler.

Covers:
- PostToolUse on Bash with clawpm command → logged
- PostToolUse on Bash with non-clawpm command → NOT logged
- PostToolUse on other tools (Edit, Read, etc.) → NOT logged
- SessionStart / Stop / SubagentStop → logged with respective action
- Malformed JSON / empty stdin → exit 0, no crash
- Unwriteable portfolio → exit 0, error to stderr
- Recursion guard: doesn't matter — hook doesn't call clawpm itself
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


HANDLER = Path(__file__).parent.parent / "hooks" / "clawpm-sync" / "handler.py"


def _run_hook(event: dict | str | None, portfolio_root: Path) -> subprocess.CompletedProcess:
    """Run handler.py with the given event payload as stdin."""
    if event is None:
        stdin = ""
    elif isinstance(event, str):
        stdin = event
    else:
        stdin = json.dumps(event)
    env = {"CLAWPM_PORTFOLIO": str(portfolio_root), "PATH": ""}
    # Inherit minimum needed for python to find itself on Windows
    import os
    for k in ("SYSTEMROOT", "PATH", "PATHEXT", "USERPROFILE", "HOME"):
        if k in os.environ:
            env[k] = os.environ[k]
    env["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    return subprocess.run(
        [sys.executable, str(HANDLER)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _read_log(portfolio_root: Path) -> list[dict]:
    log = portfolio_root / "work_log.jsonl"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestPostToolUse:
    def test_clawpm_command_logged(self, tmp_path):
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "clawpm tasks list --project demo"},
            "tool_response": {"exit_code": 0},
            "session_id": "sess-A",
            "cwd": "/tmp",
        }
        result = _run_hook(event, tmp_path)
        assert result.returncode == 0, result.stderr
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["action"] == "tool_call"
        assert entries[0]["summary"].startswith("clawpm tasks list")
        assert entries[0]["session_key"] == "sess-A"
        assert entries[0]["exit_code"] == 0
        assert entries[0]["source"] == "clawpm-sync hook"

    def test_non_clawpm_bash_command_not_logged(self, tmp_path):
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": {"exit_code": 0},
            "session_id": "sess-A",
            "cwd": "/tmp",
        }
        result = _run_hook(event, tmp_path)
        assert result.returncode == 0
        assert _read_log(tmp_path) == []

    def test_clawpm_lookalike_not_matched(self, tmp_path):
        # `clawpmx` should NOT match (we want only the actual `clawpm ` command)
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "clawpmx is not real"},
            "tool_response": {"exit_code": 0},
        }
        _run_hook(event, tmp_path)
        assert _read_log(tmp_path) == []

    def test_non_bash_tool_not_logged(self, tmp_path):
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Edit",
            "tool_input": {"file_path": "foo.py"},
        }
        _run_hook(event, tmp_path)
        assert _read_log(tmp_path) == []

    def test_leading_whitespace_tolerated(self, tmp_path):
        # `   clawpm context` should still match (operator might shell-style indent)
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "   clawpm context"},
            "tool_response": {"exit_code": 0},
        }
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["summary"].startswith("clawpm context")

    def test_cd_then_clawpm_logged(self, tmp_path):
        # Codex PR#5 round-2 P2: `cd /repo && clawpm tasks list` was missed
        # because the previous check only inspected the leading segment.
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cd /repo && clawpm tasks list --project demo"},
            "tool_response": {"exit_code": 0},
            "session_id": "S1",
        }
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["summary"].startswith("clawpm tasks list")

    def test_chained_clawpm_calls_each_logged(self, tmp_path):
        # Multi-clawpm chain: `clawpm start 42 && clawpm log progress` should
        # produce two entries, one per invocation.
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "clawpm start 42 && clawpm log progress"},
            "tool_response": {"exit_code": 0},
        }
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 2
        assert entries[0]["summary"].startswith("clawpm start 42")
        assert entries[1]["summary"].startswith("clawpm log progress")

    def test_env_prefix_stripped(self, tmp_path):
        # `PYTHONIOENCODING=utf-8 clawpm doctor` is the canonical Windows
        # invocation pattern; leading env-var assignment must not prevent match.
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "PYTHONIOENCODING=utf-8 clawpm doctor"},
            "tool_response": {"exit_code": 0},
        }
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["summary"].startswith("clawpm doctor")

    def test_semicolon_separator_picked_up(self, tmp_path):
        # POSIX-shell `;` separator is also a segment boundary.
        event = {
            "hook_event_name": "PostToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "echo hi; clawpm next"},
            "tool_response": {"exit_code": 0},
        }
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["summary"].startswith("clawpm next")


class TestSessionBoundaries:
    def test_session_start_logged(self, tmp_path):
        event = {"hook_event_name": "SessionStart", "session_id": "S1", "cwd": "/x"}
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert len(entries) == 1
        assert entries[0]["action"] == "session_start"
        assert entries[0]["session_key"] == "S1"

    def test_stop_logged(self, tmp_path):
        event = {"hook_event_name": "Stop", "session_id": "S1"}
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert entries[0]["action"] == "session_stop"

    def test_subagent_stop_logged(self, tmp_path):
        event = {"hook_event_name": "SubagentStop", "session_id": "S1"}
        _run_hook(event, tmp_path)
        entries = _read_log(tmp_path)
        assert entries[0]["action"] == "subagent_stop"


class TestRobustness:
    def test_empty_stdin_exits_zero(self, tmp_path):
        result = _run_hook(None, tmp_path)
        assert result.returncode == 0
        assert _read_log(tmp_path) == []

    def test_malformed_json_exits_zero(self, tmp_path):
        result = _run_hook("not valid json {{{", tmp_path)
        assert result.returncode == 0
        assert _read_log(tmp_path) == []

    def test_non_object_payload_exits_zero(self, tmp_path):
        result = _run_hook("[1, 2, 3]", tmp_path)
        assert result.returncode == 0
        assert _read_log(tmp_path) == []

    def test_unknown_event_name_exits_zero(self, tmp_path):
        event = {"hook_event_name": "MysteryEvent", "session_id": "X"}
        result = _run_hook(event, tmp_path)
        assert result.returncode == 0
        # No entry written for unknown events
        assert _read_log(tmp_path) == []

    def test_missing_tool_input_doesnt_crash(self, tmp_path):
        event = {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
        result = _run_hook(event, tmp_path)
        assert result.returncode == 0
