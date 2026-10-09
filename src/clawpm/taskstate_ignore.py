"""Detect git-ignored ``.project/tasks`` state (CLAWP-134).

clawpm's value is persistent task state. A blanket ``.project/`` line in a
repo's ``.gitignore`` silently keeps every task file off git. The only file
that should be ignored is the lock (``.project/tasks/.clawpm-tasks.lock``).

Deterministic: asks ``git check-ignore -v`` and reports the matching rule.
Never edits a ``.gitignore`` and never commits -- that is a repo-owner call.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_TASK_SUBDIRS = ("", "done", "done/archive", "blocked", "rejected")
_DEFAULT_WIDTH = 3  # the allocator mints ``{prefix}-{num:03d}``

FIX_TEXT = "replace `.project/` with `.project/tasks/.clawpm-tasks.lock` in that ignore file"


def _run_git(project_dir: Path, verb: str, *args: str) -> subprocess.CompletedProcess | None:
    """Run ``git -C project_dir <verb> *args``; None when git could not run.

    Fail-open (this check must never raise) but not fail-SILENT: a git that is
    missing or times out leaves a WARNING, because the caller carries on
    without that answer and a blanket ignore could go unreported (CLAWP-094
    marker style). Exit codes are the caller's call -- see ``_degraded``.
    """
    try:
        return subprocess.run(
            ["git", "-C", str(project_dir), verb, *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env={**os.environ, "LC_ALL": "C"},  # stable text for the not-a-repo test
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "clawpm: git %s could not run for %s (%r); "
            "the git-ignored task-state check is degraded",
            verb, project_dir, exc,
        )
        return None


def _degraded(project_dir: Path, verb: str, result: subprocess.CompletedProcess) -> None:
    """Log a git exit that is not an answer: WARNING, or debug for "not a repo".

    "Not a git repository" is the normal state of an unversioned project, so
    it must not warn on every doctor run.
    """
    stderr = result.stderr.strip()
    log = logger.debug if "not a git repository" in stderr else logger.warning
    log(
        "clawpm: git %s failed for %s (rc=%s: %s); "
        "the git-ignored task-state check is degraded",
        verb, project_dir, result.returncode, stderr,
    )


def _degraded_prefix(project_dir: Path, what: str, exc: BaseException) -> None:
    """WARNING marker: the probe prefix is a fallback, not the allocator's answer.

    A wrong probe prefix can let a filename allowlist (``!...CODE-???.md``)
    accept the probe and so mask a real blanket-ignore finding.
    """
    logger.warning(
        "clawpm: %s for %s (%r); the git-ignored task-state check is degraded "
        "(probing a fallback task prefix)",
        what, project_dir, exc,
    )


def _check_ignore_source(project_dir: Path, rel_path: str) -> str | None:
    """Return ``<source>:<line>:<pattern>`` if git ignores *rel_path*, else None.

    *rel_path* is relative to *project_dir*; ``git -C`` makes git resolve it
    (and report the rule's source) correctly for a project that lives in a
    repo subdirectory. ``check-ignore -v`` also exits 0 when the last matching
    rule is a negation (``!pattern``), which means NOT ignored. Any failure
    (git missing, timeout, unexpected exit) is "no finding" with a WARNING
    marker; "not a repo" is debug-logged only. Never raises or false-WARNs
    about the project's ignore rules.
    """
    result = _run_git(project_dir, "check-ignore", "-v", "--", rel_path)
    if result is None:
        return None
    if result.returncode == 1:  # git's "not ignored"
        return None
    if result.returncode != 0:
        _degraded(project_dir, "check-ignore", result)
        return None
    line = result.stdout.splitlines()[0] if result.stdout.strip() else ""
    source = line.split("\t", 1)[0]
    # <source>:<linenum>:<pattern>; the source may hold a drive-letter colon,
    # so anchor on the first ":<digits>:" instead of splitting on every colon.
    m = re.match(r"^(.*?):(\d+):(.*)$", source)
    if m and m.group(3).startswith("!"):
        return None
    return source or None


def _task_shape(project_dir: Path, folder: Path) -> tuple[str, int]:
    """``(prefix, digit width)`` of the hypothetical probe task.

    A real task in *folder* gives both exactly (stem split on the last ``-``),
    so filename allowlists such as ``!.project/tasks/GI-???.md`` judge the probe
    as they judge real tasks. With none (project init), use the project's own
    prefix and the allocator's zero-pad width.
    """
    for path in [*folder.glob("*.md"), *folder.glob("*/_task.md")]:
        stem = path.parent.name if path.name == "_task.md" else path.stem
        prefix, sep, num = stem.rpartition("-")
        if sep and prefix and num.isdigit():
            return prefix, len(num)
    try:
        from .models import ProjectSettings

        settings = ProjectSettings.load(project_dir / ".project" / "settings.toml")
        prefix = _allocator_prefix(project_dir, settings)
    except FileNotFoundError as exc:  # no settings.toml: not an initialised project
        logger.debug("no settings to derive a task prefix from in %s: %r", project_dir, exc)
        prefix = "TASK"
    except Exception as exc:  # unreadable or malformed settings, or a failed own-project scan
        _degraded_prefix(project_dir, "could not derive a task prefix", exc)
        prefix = "TASK"
    return prefix, _DEFAULT_WIDTH


def _allocator_prefix(project_dir: Path, settings) -> str:
    """The prefix the allocator will mint this project's first task under.

    A prefix collision with a sibling (``CODE`` taken -> ``CODE-B``) means the
    naive ``id[:5]`` placeholder is not what real tasks will carry, so ask the
    allocator itself. Without a portfolio config, use the project's current or
    naive prefix quietly. When the allocator raises (malformed portfolio,
    failed sibling scan, candidates exhausted) keep that fallback but log a
    WARNING, since the fallback may differ from the real prefix.
    """
    from .discovery import load_portfolio_config
    from .tasks import _naive_prefix_placeholder, assign_task_prefix, resolve_existing_prefix

    try:
        config = load_portfolio_config()
        if config is not None:
            return assign_task_prefix(
                settings.id,
                project_dir / ".project" / "tasks",
                config,
                explicit_prefix=getattr(settings, "task_prefix", None),
            )
    except Exception as exc:
        # Absence is not an exception here (no portfolio.toml yields defaults
        # and an unregistered project is minted like any other), so anything
        # raised -- malformed portfolio, failed sibling scan, exhausted
        # candidates -- is a real degradation.
        _degraded_prefix(project_dir, "allocator could not resolve a task prefix", exc)
    return resolve_existing_prefix(settings) or _naive_prefix_placeholder(settings.id)


def _index_names(project_dir: Path, sub: str) -> set[str]:
    """Task-file stems git tracks directly under the state folder *sub*.

    ``check-ignore`` never reports a tracked path, so a probe naming a file
    that is in the index -- even one deleted from disk but not yet staged as
    removed -- would hide a blanket ignore. A failed read is "no names" but
    leaves a WARNING: the probe may then land on a tracked path and the
    blanket-ignore finding can be masked.
    """
    rel = "/".join(p for p in (".project", "tasks", sub) if p)
    result = _run_git(project_dir, "ls-files", "-z", "--", rel)
    if result is None:
        return set()
    if result.returncode != 0:
        _degraded(project_dir, "ls-files", result)
        return set()
    names: set[str] = set()
    for entry in result.stdout.split("\0"):
        head = entry[len(rel) + 1:].split("/", 1)[0] if entry.startswith(rel + "/") else ""
        if head:
            names.add(head[:-3] if head.endswith(".md") else head)
    return names


def _probe_path(project_dir: Path, sub: str, folder: Path) -> str:
    """Relative path of a task-shaped file that does NOT exist, in the layout in use.

    ``check-ignore`` never reports tracked files, so the number is the highest
    of the real width that is neither on disk nor in the git index.
    """
    prefix, width = _task_shape(project_dir, folder)
    tracked = _index_names(project_dir, sub)

    def taken(stem: str) -> bool:
        return stem in tracked or (folder / f"{stem}.md").exists() or (folder / stem).exists()

    # When the whole width is taken (GI-000..GI-999) move to the next width
    # rather than return a real file.
    while True:
        num = next(
            (n for n in range(10**width - 1, -1, -1) if not taken(f"{prefix}-{n:0{width}d}")),
            None,
        )
        if num is not None:
            break
        width += 1
    name = f"{prefix}-{num:0{width}d}"
    split_only = (
        folder.is_dir()
        and next(folder.glob("*.md"), None) is None
        and next(folder.glob("*/_task.md"), None) is not None
    )
    leaf = f"{name}/_task.md" if split_only else f"{name}.md"
    return "/".join(p for p in (".project", "tasks", sub, leaf) if p)


def find_ignored_task_state(project_dir: Path, *, require_tasks: bool = True) -> str | None:
    """Return the matching ignore rule (``file:line:pattern``) if the project's
    task files are git-ignored, else None.

    With *require_tasks* (doctor) a hypothetical new task path is probed in
    every task directory that already holds task files (flat ``*.md`` or
    split ``<id>/_task.md``). A hypothetical path is used because
    ``check-ignore`` never reports TRACKED files, so a force-added task would
    mask a blanket rule that swallows new ones; it is task-shaped (real
    prefix) so filename allowlists are judged as they apply to real tasks.
    Without *require_tasks* (project init, before any task exists) only the
    tasks root is probed.
    """
    tasks_dir = project_dir / ".project" / "tasks"
    for sub in _TASK_SUBDIRS:
        folder = tasks_dir / sub if sub else tasks_dir
        if require_tasks and not (
            folder.is_dir()
            and (next(folder.glob("*.md"), None) or next(folder.glob("*/_task.md"), None))
        ):
            continue
        source = _check_ignore_source(project_dir, _probe_path(project_dir, sub, folder))
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
