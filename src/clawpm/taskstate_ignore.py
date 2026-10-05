"""Detect git-ignored ``.project/tasks`` state (CLAWP-134).

clawpm's value is persistent task state. A blanket ``.project/`` line in a
repo's ``.gitignore`` silently keeps every task file off git. The only file
that should be ignored is the lock (``.project/tasks/.clawpm-tasks.lock``).

Deterministic: asks ``git check-ignore -v`` and reports the matching rule.
Never edits a ``.gitignore`` and never commits -- that is a repo-owner call.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TASK_SUBDIRS = ("", "done", "blocked", "rejected")
_PROBE_NAME = "probe.md"  # check-ignore works on paths that don't exist yet

FIX_TEXT = "replace `.project/` with `.project/tasks/.clawpm-tasks.lock` in that ignore file"


def _check_ignore_source(project_dir: Path, rel_path: str) -> str | None:
    """Return ``<source>:<line>:<pattern>`` if git ignores *rel_path*, else None.

    *rel_path* is relative to *project_dir*; ``git -C`` makes git resolve it
    (and report the rule's source) correctly for a project that lives in a
    repo subdirectory. ``check-ignore -v`` also exits 0 when the last matching
    rule is a negation (``!pattern``), which means NOT ignored. Any failure
    (git missing, not a repo, timeout) is "no finding" and is logged at debug
    level -- this check must never raise or false-WARN.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(project_dir), "check-ignore", "-v", "--", rel_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("git check-ignore could not run for %s: %r", project_dir, exc)
        return None
    if result.returncode == 1:  # git's "not ignored"
        return None
    if result.returncode != 0:
        logger.debug(
            "git check-ignore degraded for %s: rc=%s stderr=%s",
            project_dir, result.returncode, result.stderr.strip(),
        )
        return None
    line = result.stdout.splitlines()[0] if result.stdout.strip() else ""
    source = line.split("\t", 1)[0]
    # <source>:<linenum>:<pattern>; the source may hold a drive-letter colon,
    # so anchor on the first ":<digits>:" instead of splitting on every colon.
    m = re.match(r"^(.*?):(\d+):(.*)$", source)
    if m and m.group(3).startswith("!"):
        return None
    return source or None


def find_ignored_task_state(project_dir: Path, *, require_tasks: bool = True) -> str | None:
    """Return the matching ignore rule (``file:line:pattern``) if the project's
    task files are git-ignored, else None.

    With *require_tasks* (doctor) a hypothetical new task path is probed in
    every task directory that already holds ``*.md`` files. A hypothetical
    path is used because ``check-ignore`` never reports TRACKED files, so a
    force-added task would mask a blanket rule that swallows new ones. Without
    it (project init, before any task exists) only the tasks root is probed.
    """
    tasks_dir = project_dir / ".project" / "tasks"
    for sub in _TASK_SUBDIRS:
        folder = tasks_dir / sub if sub else tasks_dir
        if require_tasks and not (folder.is_dir() and next(folder.glob("*.md"), None)):
            continue
        rel = "/".join(p for p in (".project", "tasks", sub, _PROBE_NAME) if p)
        source = _check_ignore_source(project_dir, rel)
        if source:
            return source
        if not require_tasks:
            break
    return None


def is_unversioned_ok(project_dir: Path) -> bool:
    """True when settings.toml opts out via ``unversioned_ok = true``.

    An unreadable settings file fails toward showing the warning (loud), and
    the cause is logged at debug level.
    """
    from .models import ProjectSettings

    settings = project_dir / ".project" / "settings.toml"
    try:
        return ProjectSettings.load(settings).unversioned_ok
    except Exception as exc:
        logger.debug("could not read unversioned_ok from %s: %r", settings, exc)
        return False


def format_warning(source: str) -> str:
    return (
        f"task state under .project/tasks is git-ignored by {source} -- "
        f"it will never be versioned; {FIX_TEXT} "
        f"(or set `unversioned_ok = true` in .project/settings.toml)"
    )


def warn_if_task_state_ignored(repo_path: Path) -> None:
    """Auto-init hook: log the warning (stderr via logging; stdout may be JSON)."""
    warning = ignored_task_state_warning(repo_path, require_tasks=False)
    if warning:
        logger.warning("%s", warning)


def ignored_task_state_warning(project_dir: Path, *, require_tasks: bool = True) -> str | None:
    """Full warning text, or None when there is nothing to report."""
    if is_unversioned_ok(project_dir):
        return None
    source = find_ignored_task_state(project_dir, require_tasks=require_tasks)
    return format_warning(source) if source else None
