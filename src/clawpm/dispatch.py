"""Subagent dispatch via Claude Code hooks (CLAWP-018).

When clawpm dispatches a subagent to a subtask, it emits a per-target
``.claude/settings.local.json`` preloading hooks that integrate the
subagent with clawpm without the subagent needing to know clawpm exists:

  - **Stop** hook → ``clawpm hook eval-stop --task <id>`` enforces the
    task's success-criteria rubric. Subagent literally cannot terminate
    until the rubric is satisfied or impossibility is independently
    confirmed (CLAWP-017).
  - **PostToolUse** hook (Write|Edit) → ``clawpm log add --task <id>
    --action progress`` writes a work_log entry per code-touching tool
    use. Captures files-changed + timing for free.
  - **SessionStart** hook → injects the task body, predictions, and
    rubric into the subagent's session context as ``additionalContext``.

Integration by construction. The subagent uses Claude Code as normal;
clawpm gets state updates and contract enforcement at the dispatch
boundary.

Design tradeoffs:

  - **Single-task per target-dir.** A ``.claude/settings.local.json``
    can only carry one set of clawpm-managed hooks at a time without
    introducing per-hook tagging. v1 enforces this: re-dispatching with
    a different task in the same dir errors unless ``--force``. For
    parallel dispatch, use ``--worktree`` (creates a git worktree per
    task) or ``--target-dir`` to scope to a different directory.

  - **Cleanup via teardown.** Each successful ``tasks state … done``
    transitions a task auto-tears down any dispatch settings that
    reference it. ``clawpm doctor`` flags stale dispatch files
    (>24h old or referencing a non-existent task).

  - **clawpm marker** (``"_clawpm_dispatch"`` block) is the canonical
    way the file declares itself as clawpm-managed. Teardown only
    touches files carrying that marker — never clobbers operator-edited
    settings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

CLAWPM_MARKER_KEY = "_clawpm_dispatch"
"""Top-level key that identifies a clawpm-managed dispatch settings file.

Its presence (with a ``task_id``) means clawpm wrote this file and may
safely tear it down. Absence means an operator wrote it; we must not
overwrite without ``--force``.
"""


def settings_path(target_dir: Path) -> Path:
    """Path to the per-target dispatch settings file."""
    return target_dir / ".claude" / "settings.local.json"


def _command_for_dispatch(task_id: str, project_id: str, action: str) -> str:
    """Build the shell command for a hook entry.

    Path forms are POSIX-style — Claude Code on Windows accepts forward
    slashes here, and they avoid YAML/JSON escaping headaches.

    The ``action`` is encoded directly so the same builder produces all
    hook command strings without per-call branches.
    """
    if action == "eval-stop":
        return f"clawpm hook eval-stop --project {project_id} --task {task_id}"
    if action == "log-progress":
        # We rely on the Bash hook input format (JSON on stdin) being
        # available via a tiny inline jq-free read; keep it simple by
        # writing a heredoc-free summary.
        return (
            f"clawpm log add --project {project_id} --task {task_id} "
            f"--action progress --summary 'subagent tool use'"
        )
    raise ValueError(f"unknown action: {action!r}")


def build_settings_payload(
    task_id: str,
    project_id: str,
    rubric_markdown: Optional[str] = None,
) -> dict:
    """Build the settings.local.json payload for a dispatched task.

    The Stop hook is the load-bearing piece — it enforces the rubric.
    PostToolUse on Write|Edit logs progress without polluting reads.
    SessionStart injects the task's rubric as additionalContext so the
    subagent sees its own contract on startup.
    """
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        CLAWPM_MARKER_KEY: {
            "task_id": task_id,
            "project_id": project_id,
            "dispatched_at": now,
            "version": 1,
        },
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command_for_dispatch(
                                task_id, project_id, "eval-stop"
                            ),
                            "timeout": 90,
                        }
                    ]
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "Write|Edit",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _command_for_dispatch(
                                task_id, project_id, "log-progress"
                            ),
                            "timeout": 15,
                        }
                    ]
                }
            ],
        },
    }
    if rubric_markdown:
        # SessionStart hook with `additionalContext` injects the rubric
        # into the subagent's first turn. The output JSON has to follow
        # Claude Code's hookSpecificOutput.additionalContext shape, which
        # a command hook produces by printing the JSON to stdout.
        payload["hooks"]["SessionStart"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _session_start_command(rubric_markdown),
                        "timeout": 10,
                    }
                ]
            }
        ]
    return payload


def _session_start_command(rubric_markdown: str) -> str:
    """Build a SessionStart command that prints additionalContext JSON.

    We embed the rubric markdown as a heredoc-safe JSON-escaped string,
    so the resulting command is self-contained — no temp files, no
    auxiliary state. The hook output schema requires
    ``hookSpecificOutput.additionalContext``.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "## Your task rubric (clawpm-injected)\n\n"
                f"{rubric_markdown}\n\n"
                "Read the rubric carefully. The Stop hook will block "
                "termination until the rubric is satisfied or independently "
                "confirmed impossible. Do not self-mark complete."
            ),
        }
    }
    # `printf '%s'` is more portable than echo for arbitrary JSON; we
    # rely on shell-quoting via json.dumps with default ensure_ascii=True
    # so the embedded string has no shell-meta characters.
    json_payload = json.dumps(payload, ensure_ascii=True)
    # Single-quoted in shell — safe because ensure_ascii prevents
    # embedded single quotes. (json.dumps escapes quotes inside strings.)
    return f"printf '%s' {json.dumps(json_payload)}"


def write_dispatch_settings(
    target_dir: Path,
    task_id: str,
    project_id: str,
    rubric_markdown: Optional[str] = None,
    force: bool = False,
) -> Path:
    """Emit settings.local.json for the dispatched task.

    Returns the path written. Raises:
      - ``FileExistsError`` if a non-clawpm-managed settings.local.json is
        present (we won't clobber operator config). Pass ``force=True`` to
        proceed by backing the existing file up to ``.bak``.
      - ``ValueError`` if a clawpm-managed file is present for a DIFFERENT
        task and ``force`` is False (would silently overwrite a
        concurrent dispatch).
    """
    path = settings_path(target_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            if not force:
                raise FileExistsError(
                    f"{path} exists and is not valid JSON; refusing to "
                    "overwrite. Use --force to back up + replace."
                )
            existing = None

        if existing is not None and CLAWPM_MARKER_KEY in existing:
            existing_task = existing[CLAWPM_MARKER_KEY].get("task_id")
            if existing_task != task_id and not force:
                raise ValueError(
                    f"{path} is already dispatched for task "
                    f"{existing_task!r}; refusing to overwrite for "
                    f"{task_id!r}. Use --force to override or "
                    f"`clawpm tasks teardown-dispatch` first."
                )
        elif existing is not None and not force:
            raise FileExistsError(
                f"{path} exists and is not clawpm-managed; refusing to "
                "overwrite operator config. Use --force to back up "
                "+ replace."
            )

        if force and path.exists():
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))

    payload = build_settings_payload(task_id, project_id, rubric_markdown)
    # Indent for human review — these files often end up in PR diffs.
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def read_dispatch_marker(target_dir: Path) -> Optional[dict]:
    """Return the clawpm dispatch marker block from settings.local.json, or None."""
    path = settings_path(target_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data.get(CLAWPM_MARKER_KEY)


def teardown_dispatch_settings(
    target_dir: Path,
    task_id: Optional[str] = None,
    force: bool = False,
) -> bool:
    """Remove a clawpm-managed dispatch settings file.

    Returns True iff a file was actually removed. If ``task_id`` is given,
    only removes when the marker matches; otherwise removes any
    clawpm-managed dispatch.

    Refuses to remove operator-authored settings unless ``force=True``.
    """
    path = settings_path(target_dir)
    if not path.exists():
        return False
    marker = read_dispatch_marker(target_dir)
    if marker is None:
        if not force:
            return False
        path.unlink()
        return True
    if task_id is not None and marker.get("task_id") != task_id:
        return False
    path.unlink()
    return True


def create_worktree(repo_path: Path, task_id: str) -> Path:
    """Create a git worktree under ``<repo_path>/.clawpm-worktrees/<task_id>/``.

    Returns the new worktree path. Branch name: ``clawpm/<task_id>``.

    If the branch already exists, reuses it. If the worktree path already
    exists, reuses it (idempotent). Raises ``subprocess.CalledProcessError``
    if git is unavailable or the repo is in a state that blocks the worktree.
    """
    worktree_root = repo_path / ".clawpm-worktrees" / task_id
    worktree_root.parent.mkdir(parents=True, exist_ok=True)

    if worktree_root.exists():
        return worktree_root

    branch_name = f"clawpm/{task_id.lower()}"

    # Check if branch already exists
    branch_check = subprocess.run(
        ["git", "rev-parse", "--verify", branch_name],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if branch_check.returncode == 0:
        # Branch exists — add worktree pointing at it
        subprocess.run(
            ["git", "worktree", "add", str(worktree_root), branch_name],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    else:
        # Branch doesn't exist — create + worktree in one step
        subprocess.run(
            ["git", "worktree", "add", "-b", branch_name, str(worktree_root)],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )

    return worktree_root
