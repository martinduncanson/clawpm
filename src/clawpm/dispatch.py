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
    introducing per-hook tagging. Re-dispatching with a different task
    in the same dir errors unless ``--force``. For parallel dispatch,
    use ``--worktree`` (creates a git worktree per task) or
    ``--target-dir`` to scope to a different directory.

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
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Codex P1 fix: task_id and project_id flow unchanged into shell commands.
# Reject anything outside the safe charset BEFORE interpolating, so an
# operator who runs `clawpm tasks add --id 'foo; rm -rf /'` cannot inject.
# clawpm's auto-generated IDs are `PREFIX-NNN` (uppercase + hyphen + digits);
# we widen slightly to accept lowercase + underscore + dot for project_ids
# that come from filesystem-derived names.
_SAFE_TASK_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SAFE_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _assert_safe_identifier(value: str, kind: str) -> None:
    """Raise ValueError if value contains shell-meta or path-traversal chars.

    Called before any string interpolation into a hook command OR before
    use as a path component / git ref name. The safe charset is narrow:
    letters, digits, dot, hyphen, underscore. Additionally rejected:

      - empty / over-64-chars
      - leading ``.`` or ``-`` (git refnames hostile, path-traversal risk)
      - ``..`` substring anywhere (path traversal)
      - trailing ``.`` (Windows hostile)
    """
    pattern = _SAFE_TASK_ID_RE if kind == "task_id" else _SAFE_PROJECT_ID_RE
    if (
        not pattern.match(value)
        or value.startswith(".")
        or value.startswith("-")
        or ".." in value
        or value.endswith(".")
    ):
        raise ValueError(
            f"Refusing to dispatch with unsafe {kind} {value!r} — "
            f"only [A-Za-z0-9._-]{{1,64}} allowed; no leading dot/hyphen, "
            f"no '..', no trailing dot (would risk shell injection or "
            f"path traversal in hook commands / worktree paths)"
        )


CLAWPM_MARKER_KEY = "_clawpm_dispatch"
"""Top-level key that identifies a clawpm-managed dispatch settings file.

Its presence (with a ``task_id``) means clawpm wrote this file and may
safely tear it down. Absence means an operator wrote it; we must not
overwrite without ``--force``.
"""


# Codex round-4 P2 fix: dispatches to custom --target-dir were not being
# torn down on done because the auto-teardown only checked repo_path +
# .clawpm-worktrees/. Solved via a portfolio-level append-only registry:
# every dispatch appends a "dispatched" event, every teardown appends a
# "torn_down" event, and `active_dispatch_dirs` replays the log to find
# dirs that are dispatched-but-not-torn-down. Matches clawpm's existing
# filesystem-first / no-daemon / append-only doctrine (mirrors work_log).
DISPATCH_REGISTRY_FILENAME = "dispatches.jsonl"


def _dispatch_registry_path(portfolio_root: Path) -> Path:
    return portfolio_root / DISPATCH_REGISTRY_FILENAME


def register_dispatch(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
    target_dir: Path,
) -> None:
    """Append a ``dispatched`` event to the portfolio dispatch registry."""
    path = _dispatch_registry_path(portfolio_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "action": "dispatched",
        "task_id": task_id,
        "project_id": project_id,
        "target_dir": str(target_dir.resolve()),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def register_teardown(
    portfolio_root: Path,
    task_id: str,
    target_dir: Path,
    project_id: Optional[str] = None,
) -> None:
    """Append a ``torn_down`` event to the registry.

    ``project_id`` is optional only for backwards-compat with callers that
    don't have it; future teardown events should always include it so
    cross-project isolation in ``active_dispatch_dirs`` is exact.
    """
    path = _dispatch_registry_path(portfolio_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {
        "action": "torn_down",
        "task_id": task_id,
        "target_dir": str(target_dir.resolve()),
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if project_id is not None:
        event["project_id"] = project_id
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def active_dispatch_dirs(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
) -> list[Path]:
    """Return target_dirs currently dispatched (not yet torn down) for a task.

    Replays the registry: a directory is "active" if its most recent
    event for this (task_id, project_id) pair is ``dispatched``. Returns
    absolute paths.

    **Cross-project isolation** (Codex round-5 P1 fix): clawpm task IDs
    are per-project (``PREFIX-NNN`` where PREFIX is the project's 5-char
    uppercase ID). Two projects sharing a prefix can produce identical
    task IDs — see the doctor prefix-collision check. Filtering on
    task_id alone would let ``tasks state PROJ_A-CLAWP-001 done`` tear
    down dispatch hooks in PROJ_B for its own CLAWP-001. Must filter on
    BOTH fields.
    """
    path = _dispatch_registry_path(portfolio_root)
    if not path.exists():
        return []
    # Map abs-target-dir -> latest action for THIS (task_id, project_id)
    latest: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8", errors="strict").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("task_id") != task_id:
                continue
            # Cross-project isolation: ignore events for a different
            # project that happens to share the task_id. Teardown events
            # historically omitted project_id; treat absent project_id
            # on a teardown event as matching ANY dispatched event so
            # legacy entries still close out correctly.
            ev_project = ev.get("project_id")
            action = ev.get("action")
            if action == "dispatched" and ev_project != project_id:
                continue
            if action == "torn_down" and ev_project not in (None, project_id):
                continue
            td = ev.get("target_dir")
            if td and action in ("dispatched", "torn_down"):
                latest[td] = action
    except UnicodeDecodeError:
        # Corrupted registry — return empty rather than guess. Doctor
        # check on the registry would be a v2 addition.
        return []
    return [Path(td) for td, action in latest.items() if action == "dispatched"]


def settings_path(target_dir: Path) -> Path:
    """Path to the per-target dispatch settings file."""
    return target_dir / ".claude" / "settings.local.json"


def _command_for_dispatch(task_id: str, project_id: str, action: str) -> str:
    """Build the shell command for a hook entry.

    Commands MUST be portable across cmd.exe (Windows default for Claude
    Code) and POSIX shells. The rules followed here:

      - No single quotes (cmd.exe treats them as literal characters, not
        quote delimiters).
      - No embedded shell metacharacters (``$``, backticks, redirection).
      - Summary text uses no whitespace so no quoting is needed at all —
        ``subagent-tool-use`` not ``"subagent tool use"``. This keeps the
        command parseable identically on every supported shell.

    The ``action`` is encoded directly so the same builder produces all
    hook command strings without per-call branches.

    Identifier safety: ``task_id`` and ``project_id`` are validated via
    ``_assert_safe_identifier`` to prevent shell injection — clawpm
    auto-generates safe IDs, but the operator can override with --id.
    """
    _assert_safe_identifier(task_id, "task_id")
    _assert_safe_identifier(project_id, "project_id")
    if action == "eval-stop":
        return f"clawpm hook eval-stop --project {project_id} --task {task_id}"
    if action == "log-progress":
        # Whitespace-free summary keeps the command shell-portable without
        # any quoting at all. The hook is a coarse-grained signal anyway
        # — file-level changes get captured by `clawpm log commit` later.
        return (
            f"clawpm log add --project {project_id} --task {task_id} "
            f"--action progress --summary subagent-tool-use"
        )
    if action == "session-start":
        return f"clawpm hook session-start --project {project_id} --task {task_id}"
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
                    # Match every code-writing tool Claude Code exposes.
                    # MultiEdit is the silent gap Codex round-3 caught —
                    # subagents using batched edits would otherwise skip
                    # the work_log entry. NotebookEdit covers Jupyter.
                    "matcher": "Write|Edit|MultiEdit|NotebookEdit",
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
        # SessionStart additionalContext is too large + escape-prone to
        # embed in a shell command string portably. Instead we write the
        # JSON payload to a sidecar file at dispatch time and the hook
        # invokes `clawpm hook session-start` which just prints it.
        # Cross-platform safe (no shell-embedded JSON, no printf, no
        # quoting headaches on cmd.exe).
        payload["hooks"]["SessionStart"] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": _command_for_dispatch(
                            task_id, project_id, "session-start"
                        ),
                        "timeout": 10,
                    }
                ]
            }
        ]
    return payload


def session_start_payload_path(target_dir: Path) -> Path:
    """Sidecar path where the SessionStart JSON payload lives.

    Co-located with settings.local.json so teardown removes both with a
    single ``.claude/`` cleanup pass.
    """
    return target_dir / ".claude" / "clawpm-session-start.json"


def write_session_start_sidecar(
    target_dir: Path, rubric_markdown: str
) -> Path:
    """Write the SessionStart additionalContext JSON to a sidecar file."""
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
    path = session_start_payload_path(target_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def write_dispatch_settings(
    target_dir: Path,
    task_id: str,
    project_id: str,
    rubric_markdown: Optional[str] = None,
    force: bool = False,
    portfolio_root: Optional[Path] = None,
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
    # Pretty-print so dispatch settings are review-friendly when they
    # land in PR diffs.
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    if rubric_markdown:
        # Side-car file holds the additionalContext JSON; the hook reads
        # it via `clawpm hook session-start`. See module docstring for
        # the cross-platform reasoning.
        write_session_start_sidecar(target_dir, rubric_markdown)
    # Codex round-4: register the dispatch so on-done teardown can find
    # ALL target_dirs, not just the legacy repo_path + worktree pair.
    if portfolio_root is not None:
        register_dispatch(portfolio_root, task_id, project_id, target_dir)
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
    portfolio_root: Optional[Path] = None,
) -> bool:
    """Remove a clawpm-managed dispatch settings file (and sidecar).

    Returns True iff settings.local.json was actually removed.

    Behaviour:
      - Operator-authored file (no clawpm marker): returns False without
        modifying anything, unless ``force=True`` (in which case the file
        is removed without backup — caller is asserting they know what
        they're doing).
      - Marker present, ``task_id`` given but mismatches: returns False;
        the dispatch belongs to a different task.
      - Marker present, matches (or ``task_id`` not given): removes
        settings.local.json AND the SessionStart sidecar if present.

    No exception is raised in the not-removed paths — the caller reads
    the bool to decide what to surface.
    """
    path = settings_path(target_dir)
    sidecar = session_start_payload_path(target_dir)
    if not path.exists():
        # Sidecar without settings is an orphan from a partial earlier
        # failure; clean it up so doctor doesn't surface it forever.
        if sidecar.exists():
            sidecar.unlink()
        return False
    marker = read_dispatch_marker(target_dir)
    if marker is None:
        if not force:
            return False
        path.unlink()
        if sidecar.exists():
            sidecar.unlink()
        return True
    if task_id is not None and marker.get("task_id") != task_id:
        return False
    path.unlink()
    if sidecar.exists():
        sidecar.unlink()
    # Codex round-4: append a torn_down event to the registry so
    # active_dispatch_dirs reflects reality. Pass project_id from the
    # marker (round-5 P1: cross-project isolation requires it).
    if portfolio_root is not None:
        resolved_task = task_id or marker.get("task_id")
        resolved_project = marker.get("project_id")
        if resolved_task:
            register_teardown(
                portfolio_root,
                resolved_task,
                target_dir,
                project_id=resolved_project,
            )
    return True


def create_worktree(repo_path: Path, task_id: str) -> Path:
    """Create a git worktree under ``<repo_path>/.clawpm-worktrees/<task_id>/``.

    Returns the new worktree path. Branch name: ``clawpm/<task_id>``.

    If the branch already exists, reuses it. If the worktree path already
    exists, reuses it (idempotent). Raises ``subprocess.CalledProcessError``
    if git is unavailable or the repo is in a state that blocks the worktree.

    Identifier safety: ``task_id`` is validated to prevent path traversal
    (``../foo``) or git ref name abuse (``..``, ``-`` prefix, etc.).
    """
    _assert_safe_identifier(task_id, "task_id")
    worktree_root = repo_path / ".clawpm-worktrees" / task_id
    worktree_root.parent.mkdir(parents=True, exist_ok=True)

    if worktree_root.exists():
        return worktree_root

    # Keep the branch and directory names in the SAME case so case-
    # sensitive filesystems (Linux ext4, git refnames everywhere) don't
    # cause re-dispatch to miss the existing branch.
    branch_name = f"clawpm/{task_id}"

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
