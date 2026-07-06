"""Task operations for ClawPM."""

from __future__ import annotations

import os
import re
import shutil
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from .concurrency import (
    ConcurrentModificationError,
    commit_staged_pair,
    file_lock,
    guard_fs_tamper,
    retry_transient,
)
from .frontmatter import (
    FrontmatterError,
    parse_frontmatter,
    split_frontmatter,
    stamp_updated,
)
from .models import Task, TaskState, TaskComplexity, Predictions, PortfolioConfig, normalize_tags
from .discovery import get_project_dir, find_project_dir_fallback


def get_tasks_dir(config: PortfolioConfig, project_id: str) -> Path | None:
    """Get the tasks directory for a project."""
    project_dir = get_project_dir(config, project_id)
    if project_dir:
        tasks_dir = project_dir / "tasks"
        if tasks_dir.exists():
            return tasks_dir
    return None


def _scan_task_files(location: Path, tasks: list[Task], state_filter: TaskState | None) -> None:
    """Scan a directory for task files, recursing into nested directory tasks.

    Codex round-9 P2: after a subtask is itself decomposed via split_task,
    its files live at ``tasks/<parent>/<child>/...`` — one level deeper than
    the original scan reached. Recurse properly so nested grandchildren are
    visible to ``list_tasks`` / ``get_next_task``, not just ``get_task``.
    The ``_task.md`` filename is skipped in the file branch because the
    directory-task owner is added once in the dir branch, before recursing.
    """
    if not location.exists():
        return

    for item in location.iterdir():
        if item.is_file() and item.suffix == ".md":
            # Skip _task.md here — the dir branch below adds the directory
            # task once when it enters the dir (avoids duplicate appends).
            if item.name == "_task.md":
                continue
            try:
                task = Task.from_file(item)
                if state_filter is None or task.state == state_filter:
                    tasks.append(task)
            except Exception:
                continue
        elif item.is_dir() and not item.name.startswith(".") and item.name.lower() not in ("done", "blocked", "rejected", "archive"):
            # Directory task: add the _task.md, then recurse for subtasks
            # AND any nested directory subtasks. The recursion subsumes the
            # old non-recursive single-level glob; nested directories with
            # their own _task.md get added as we descend.
            parent_file = item / "_task.md"
            if parent_file.exists():
                try:
                    parent_task = Task.from_file(parent_file)
                    if state_filter is None or parent_task.state == state_filter:
                        tasks.append(parent_task)
                except Exception:
                    pass
            _scan_task_files(item, tasks, state_filter)


def _scan_locations(tasks_dir: Path, state_filter: TaskState | None) -> list[Path]:
    """Directory silos that can contain a task in ``state_filter`` (CLAWP-080).

    State is derived from the on-disk location in ``Task.from_file`` (a file
    under ``done/`` is DONE, under ``blocked/`` is BLOCKED, etc.), so this
    partition is exact — no file under ``done/`` is ever OPEN. Deriving the
    scan set from the filter lets callers skip reading and YAML-parsing entire
    silos, which matters once ``done/`` grows unboundedly with no archival.

    - OPEN / PROGRESS live only in the top-level ``tasks_dir`` (progress as a
      ``.progress.md`` sibling); the terminal silos never hold them.
    - DONE / BLOCKED / REJECTED live only in their respective silo.
    - No filter → the full portfolio view: main + done + blocked. ``rejected/``
      stays excluded (CLAWP-053: a hidden terminal silo, added only on an
      explicit REJECTED filter).
    """
    if state_filter == TaskState.REJECTED:
        return [tasks_dir / "rejected"]
    if state_filter in (TaskState.OPEN, TaskState.PROGRESS):
        return [tasks_dir]
    if state_filter == TaskState.DONE:
        return [tasks_dir / "done"]
    if state_filter == TaskState.BLOCKED:
        return [tasks_dir / "blocked"]
    return [tasks_dir, tasks_dir / "done", tasks_dir / "blocked"]


def list_tasks(
    config: PortfolioConfig,
    project_id: str,
    state_filter: TaskState | None = None,
    include_archived: bool = False,
) -> list[Task]:
    """List all tasks for a project.

    CLAWP-085: archived done tasks (under ``done/archive/``) are excluded from
    every scan by default — that is the whole point of archiving, keeping the
    hot path cheap. Pass ``include_archived=True`` to fold them back in; they
    are DONE tasks, so they only appear where DONE tasks would (no filter, or
    ``state_filter=DONE``).
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return []

    tasks: list[Task] = []

    locations = _scan_locations(tasks_dir, state_filter)
    # CLAWP-085: archived tasks are DONE, so only include them when the scan
    # would surface DONE tasks at all (no filter or an explicit DONE filter).
    # _scan_task_files skips the archive dir during the plain done/ scan, so
    # it must be scanned explicitly here.
    if include_archived and state_filter in (None, TaskState.DONE):
        locations = [*locations, tasks_dir / "done" / "archive"]

    for location in locations:
        _scan_task_files(location, tasks, state_filter)

    # Build parent-child relationships
    task_map = {t.id: t for t in tasks}
    for task in tasks:
        if task.parent and task.parent in task_map:
            parent = task_map[task.parent]
            if task.id not in parent.children:
                parent.children.append(task.id)

    # Sort by priority (lower is higher), then by ID
    tasks.sort(key=lambda t: (t.priority, t.id))

    return tasks


def distinct_tags(
    config: PortfolioConfig,
    project_id: str,
    include_done: bool = True,
) -> list[tuple[str, int]]:
    """Return distinct workstream tags with task counts (CLAWP-069).

    Scans every task state — open/progress/blocked plus the terminal done/ and
    rejected/ silos — so the discovered tag universe is complete (Codex + Grok
    review: rejected tasks, i.e. the won't-do ledger, were previously omitted).
    Sorted by count descending, then tag name for a stable ordering.

    ``include_done=False`` narrows to the active-work view (open/progress/
    blocked) by dropping the terminal states (DONE and REJECTED) from the tally.
    """
    # list_tasks(state_filter=None) excludes the rejected/ silo by design, so
    # union an explicit rejected scan to make the tag universe complete.
    tasks = list_tasks(config, project_id, state_filter=None)
    tasks = tasks + list_tasks(config, project_id, state_filter=TaskState.REJECTED)
    _terminal = {TaskState.DONE, TaskState.REJECTED}
    counts: dict[str, int] = {}
    for task in tasks:
        if not include_done and task.state in _terminal:
            continue
        for tag in task.tags:
            counts[tag] = counts.get(tag, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


def _parent_id_of(task_id: str) -> str | None:
    """Parent id of a subtask id (``PARENT-NNN`` -> ``PARENT``), else None."""
    if "-" in task_id:
        parts = task_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            return parts[0]
    return None


def _archive_candidate_paths(tasks_dir: Path, task_id: str) -> list[Path]:
    """Every ``done/archive/`` location a task with ``task_id`` could occupy.

    Shared by ``_candidate_task_paths`` (full resolution) and the lightweight
    archived-DONE existence checks in ``get_next_task`` / cascade — so the
    archive path shapes live in ONE place and can't drift (CLAWP-085 review r2:
    a nested decomposed archived subtask lives at
    ``done/archive/<parent>/<child>/_task.md`` and was previously unprobed).
    """
    archive = tasks_dir / "done" / "archive"
    paths = [
        archive / f"{task_id}.md",            # archived file task
        archive / task_id / "_task.md",       # archived directory task
    ]
    parent_id = _parent_id_of(task_id)
    if parent_id:
        paths.extend([
            archive / parent_id / f"{task_id}.md",        # archived subtask file
            archive / parent_id / task_id / "_task.md",   # nested decomposed archived subtask
        ])
    return paths


def _candidate_task_paths(tasks_dir: Path, task_id: str) -> list[Path]:
    """All on-disk locations a task with ``task_id`` could occupy.

    Single source of truth for both ``get_task`` (which parses each) and the
    explicit-ID clobber guard in ``add_task`` (which only needs existence).
    Keeping the location set in one place stops the two from drifting — the
    clobber guard must check exactly where ``get_task`` would later find the
    task, or a duplicate could be created in a location the guard didn't probe
    (CLAWP-051 Finding 2 / Codex review).
    """
    parent_id = _parent_id_of(task_id)

    # Check all possible locations and filenames
    possible_paths = [
        # Regular task files
        tasks_dir / f"{task_id}.md",
        tasks_dir / f"{task_id}.progress.md",
        tasks_dir / "done" / f"{task_id}.md",
        tasks_dir / "blocked" / f"{task_id}.md",
        # CLAWP-053 — rejected terminal state
        tasks_dir / "rejected" / f"{task_id}.md",
        # Task directories (parent tasks)
        tasks_dir / task_id / "_task.md",
        tasks_dir / "done" / task_id / "_task.md",
        tasks_dir / "blocked" / task_id / "_task.md",
        tasks_dir / "rejected" / task_id / "_task.md",
    ]

    # Add subtask paths if this looks like a subtask ID
    if parent_id:
        possible_paths.extend([
            tasks_dir / parent_id / f"{task_id}.md",
            tasks_dir / parent_id / f"{task_id}.progress.md",
            tasks_dir / "done" / parent_id / f"{task_id}.md",
            tasks_dir / "blocked" / parent_id / f"{task_id}.md",
            tasks_dir / "rejected" / parent_id / f"{task_id}.md",  # CLAWP-053
            # Codex round-8 P2: the subtask itself may have been split into
            # a directory task (i.e. decomposed further into grandchildren).
            # Its open/progress form lives at tasks/<parent>/<child>/_task.md.
            # When marked done/blocked the directory migrates to the top-
            # level done/<child>/ or blocked/<child>/ via change_task_state,
            # so the existing tasks_dir/done/<task_id>/_task.md probe
            # already covers the terminal states.
            tasks_dir / parent_id / task_id / "_task.md",
        ])

    # CLAWP-085 — archived done tasks live under done/archive/ (every path shape,
    # incl. nested decomposed subtasks). Resolvable by get_task so `tasks show`
    # works, and probed by the add_task clobber guard so an archived id can't be
    # reused — even though default scans skip the archive directory.
    possible_paths.extend(_archive_candidate_paths(tasks_dir, task_id))

    return possible_paths


def get_task(config: PortfolioConfig, project_id: str, task_id: str) -> Task | None:
    """Get a specific task by ID."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

    for path in _candidate_task_paths(tasks_dir, task_id):
        if path.exists():
            try:
                task = Task.from_file(path)
                # Populate children if this is a parent task
                if task.is_parent:
                    task_dir = path.parent
                    for subtask_file in task_dir.glob("*.md"):
                        if subtask_file.name != "_task.md":
                            try:
                                subtask = Task.from_file(subtask_file)
                                if subtask.id not in task.children:
                                    task.children.append(subtask.id)
                            except Exception:
                                continue
                return task
            except Exception:
                continue

    return None


def select_next_batch(
    config: PortfolioConfig, project_id: str,
) -> tuple[int | None, list[Task], list[dict]]:
    """Return the next dispatchable parallel batch for a project (CLAWP-021).

    Rules:
      - Only tasks with ``parallel_group`` set are batch-eligible.
      - The next group is the **lowest group number** such that:
        (a) at least one task in that group is OPEN or PROGRESS,
        (b) every task in group N-1 (and below, recursively) is DONE.
      - Within the eligible group, **all** OPEN/PROGRESS tasks form the
        candidate batch unless their scope sets overlap. Conflicts are
        surfaced as a structured list — the caller decides whether to
        dispatch only the non-conflicting subset or to refuse.

    Returns ``(group_number, candidate_tasks, conflicts)``:
      - ``group_number``: int or None if no group is dispatchable.
      - ``candidate_tasks``: tasks in the eligible group that are
        OPEN/PROGRESS.
      - ``conflicts``: pairs of overlapping tasks in the candidate set
        (heuristic via the same prefix-based overlap used by
        ``clawpm conflicts``).
    """
    # Local import to avoid circular: cli.py imports from tasks.py.
    from .cli import _globs_overlap

    tasks = list_tasks(config, project_id)
    by_id = {t.id: t for t in tasks}

    # Group tasks by parallel_group; ignore tasks without the field.
    groups: dict[int, list[Task]] = {}
    for t in tasks:
        if t.parallel_group is None:
            continue
        groups.setdefault(t.parallel_group, []).append(t)

    if not groups:
        return (None, [], [])

    # Find the lowest group whose predecessors are all DONE and which has
    # at least one OPEN/PROGRESS task remaining.
    sorted_groups = sorted(groups.keys())
    for g in sorted_groups:
        # All earlier groups must be entirely done
        predecessors_done = True
        for earlier in sorted_groups:
            if earlier >= g:
                break
            if any(
                t.state != TaskState.DONE for t in groups[earlier]
            ):
                predecessors_done = False
                break
        if not predecessors_done:
            continue

        candidates = [
            t for t in groups[g]
            if t.state in (TaskState.OPEN, TaskState.PROGRESS)
        ]
        if not candidates:
            # All tasks in this group are already done/blocked; try next.
            continue

        # Compute pairwise scope overlap among candidates.
        conflicts: list[dict] = []
        for i, ta in enumerate(candidates):
            for tb in candidates[i + 1:]:
                overlap_globs: list[tuple[str, str]] = []
                for ga in ta.scope:
                    for gb in tb.scope:
                        if _globs_overlap(ga, gb):
                            overlap_globs.append((ga, gb))
                if overlap_globs:
                    conflicts.append({
                        "task_a": ta.id,
                        "task_b": tb.id,
                        "overlapping_globs": overlap_globs,
                    })

        return (g, candidates, conflicts)

    return (None, [], [])


def _done_task_ids(tasks_dir: Path) -> set[str]:
    """IDs of all completed tasks, derived from filenames without parsing.

    A task's ID is its filename stem (regular task) or its directory name
    (a directory task's ``_task.md``) — an invariant the whole lookup layer
    relies on (see ``_candidate_task_paths``). Everything under ``done/`` is
    DONE by location (``Task.from_file``), so collecting membership from names
    alone is exact and lets ``get_next_task`` resolve dependency satisfaction
    without YAML-parsing an unboundedly-growing ``done/`` silo (CLAWP-080).
    Done files never carry the ``.progress`` suffix (progress lives in the main
    dir), so the stem is the bare ID.

    CLAWP-085: the ``done/archive/`` silo is PRUNED from this walk — it may grow
    unboundedly, and re-scanning it on every ``get_next_task`` would undo the
    hot-path win archiving exists to deliver. Archived DONE tasks are therefore
    absent from this set; ``get_next_task`` resolves any such dependency
    individually via the archive-aware ``get_task``.
    """
    done_dir = tasks_dir / "done"
    if not done_dir.exists():
        return set()
    ids: set[str] = set()
    for root, dirs, files in os.walk(done_dir):
        # Prune the archive silo (a direct child of done/) from the descent so
        # its contents are never enumerated on the hot path. Case-insensitive so
        # an "Archive"/"ARCHIVE" dir on a case-preserving Windows FS is still
        # pruned (team review).
        if Path(root) == done_dir:
            for d in [d for d in dirs if d.lower() == "archive"]:
                dirs.remove(d)
        for fn in files:
            if fn.endswith(".md"):
                ids.add(Path(root).name if fn == "_task.md" else fn[:-3])
    return ids


def get_next_task(config: PortfolioConfig, project_id: str) -> Task | None:
    """Get the next task to work on (highest priority open task with satisfied dependencies)."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

    # Candidates are OPEN/PROGRESS tasks, which live only in the top-level
    # tasks dir — scanning just it skips the done/ and blocked/ silos entirely
    # (CLAWP-080). _scan_task_files already excludes those subdirs as it
    # recurses, so this yields exactly the non-terminal tasks.
    candidates: list[Task] = []
    _scan_task_files(tasks_dir, candidates, None)

    # Mirror list_tasks' parent-child linking over the open subtree so a
    # returned directory task carries its open children like a full listing
    # would. Done children come only from the persisted frontmatter set
    # (authoritative post-CLAWP-037); unlike list_tasks(None) this scan does
    # not walk done/ to reverse-link legacy/manual done children — acceptable
    # here because the next task is by definition OPEN/PROGRESS work.
    task_map = {t.id: t for t in candidates}
    for task in candidates:
        if task.parent and task.parent in task_map:
            parent = task_map[task.parent]
            if task.id not in parent.children:
                parent.children.append(task.id)

    candidates.sort(key=lambda t: (t.priority, t.id))

    # Dependency satisfaction needs only the *set* of completed IDs, collected
    # cheaply from filenames rather than parsing every done file.
    done_ids = _done_task_ids(tasks_dir)

    for task in candidates:
        if task.state not in (TaskState.OPEN, TaskState.PROGRESS):
            continue

        # Check if all dependencies are satisfied. A dependency missing from the
        # cheap filename set may still be an archived DONE task (CLAWP-085:
        # done/archive is pruned from _done_task_ids). Probe only those
        # stragglers — usually zero — with lightweight path existence checks
        # against the archive silo (no YAML parse; anything under done/archive is
        # DONE by location), and treat a hit as satisfied.
        if task.depends:
            unresolved = [d for d in task.depends if d not in done_ids]
            if unresolved and not all(
                any(p.exists() for p in _archive_candidate_paths(tasks_dir, d))
                for d in unresolved
            ):
                continue

        return task

    return None


# Match the top-level `updated:` key however it's spaced (`updated: x`,
# `updated:x`, bare `updated:`, or `updated :` with space before the colon —
# valid YAML) so replacement is idempotent and never inserts a duplicate line
# (Grok review). `\s*` between `updated` and `:` can't match sibling keys like
# `updated_at:` (that's `updated_`, not whitespace + colon).
_UPDATED_LINE_RE = re.compile(r"^updated\s*:")


def _set_updated_line(text: str, stamp: str) -> str | None:
    """Insert or replace ONLY the top-level ``updated:`` line in ``text``'s
    frontmatter block, preserving every other byte (CLAWP-086, Codex review).

    Returns the rewritten text, or ``None`` if ``text`` has no well-formed
    leading ``---`` … ``---`` frontmatter fence (leave the file untouched).
    The stamp is written quoted (``updated: '2026-07-04'``) to match how PyYAML
    serialises the date-shaped string elsewhere, so ``Task.from_file`` reads it
    back as a ``str``.
    """
    if not text.startswith("---"):
        return None
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return None
    close_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break
    if close_idx is None:
        return None  # unterminated fence
    new_line = f"updated: '{stamp}'"
    for i in range(1, close_idx):
        if _UPDATED_LINE_RE.match(lines[i]):
            lines[i] = new_line
            return "\n".join(lines)
    lines.insert(close_idx, new_line)
    return "\n".join(lines)


def _stamp_updated_file(file_path: Path, when: str | None = None) -> None:
    """Bump the ``updated`` stamp in a task file's frontmatter, in place (CLAWP-086).

    Used by the move-only mutators (``change_task_state``, ``split_task``) whose
    frontmatter is otherwise untouched — the file is relocated, so without this
    the ``updated`` stamp would not reflect the state change and doctor's stale
    check would fall back to the (lying) file mtime.

    Surgical, NOT a reserialize (Codex review): these paths were previously
    move-only and preserved the file bytes verbatim. A full ``yaml.dump``
    round-trip would silently drop operator comments and rewrite key order /
    quoting on every routine ``start`` / ``block`` / ``done`` / ``split``.
    Instead only the single top-level ``updated:`` line is inserted or replaced;
    every other byte (comments, order, style, body) is preserved. A file with no
    well-formed frontmatter fence is left untouched.

    The read is wrapped in ``retry_transient`` (Codex review): this runs right
    after a successful ``shutil.move`` under the lock, so an un-retried read
    could hit the same transient Windows sharing/access fault the surrounding
    move/reload path already retries — raising after the move had committed and
    leaving state + work-log inconsistent.
    """
    text = retry_transient(lambda: file_path.read_text(encoding="utf-8"))
    new_text = _set_updated_line(text, when or date.today().isoformat())
    if new_text is None:
        return  # no well-formed frontmatter fence — leave the file untouched
    tmp = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        # Retry transient Windows sharing/access faults on the rename — this
        # runs under the per-project lock alongside concurrent scanners (CLAWP-051).
        retry_transient(tmp.replace, file_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def touch_task_updated(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    when: str | None = None,
) -> bool:
    """Bump a task's ``updated`` stamp without otherwise mutating it (CLAWP-086).

    The log-attach path (``clawpm log add --task X``): recording activity against
    a task is a mutation of its recency, so ``updated`` should reflect it and
    ``tasks show``/``list`` surface the new date. Best-effort — returns ``True``
    if the task file was found and stamped, ``False`` otherwise; never raises on
    a missing task, since the work-log entry is the primary artefact and must not
    be undone by a stamping failure.
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return False
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        task = get_task(config, project_id, task_id)
        if not task or not task.file_path or not task.file_path.exists():
            return False
        try:
            _stamp_updated_file(task.file_path, when)
        except Exception:
            # Best-effort: the work-log entry (already written by the caller) is
            # the primary artefact and must not be undone by a stamping failure
            # (Grok review). doctor's progress-stale check also consults the
            # work log, so recency stays covered even if the stamp is skipped.
            return False
        return True


def _write_rejection_frontmatter(
    file_path: Path,
    rationale: str,
    supersedes: str | None,
) -> None:
    """Rewrite the task file's YAML frontmatter to add rationale (and optional
    supersedes) before the file is moved to the rejected/ directory.

    Preserves all existing frontmatter keys; only adds/overwrites
    ``rationale`` and (if given) ``supersedes``.
    """
    # CLAWP-071 — this runs inside change_task_state's lock on a file the caller
    # already resolved; an external delete/move here means a non-clawpm process
    # removed it, so surface the friendly concurrent-modification error.
    with guard_fs_tamper(f"Task file '{file_path}'"):
        text = file_path.read_text(encoding="utf-8")
    # Lenient parse: unparseable YAML drops to fm={} while keeping the raw body,
    # so the rewrite replaces the bad frontmatter rather than doubling it. An
    # absent/unterminated fence keeps body=text and synthesises a fence below.
    fm, body = parse_frontmatter(text)

    fm["rationale"] = rationale
    if supersedes:
        fm["supersedes"] = supersedes
    stamp_updated(fm)  # CLAWP-086 — rejection is a mutation.

    new_text = (
        "---\n"
        + yaml.dump(fm, default_flow_style=False, allow_unicode=True)
        + "---"
        + body
    )
    tmp = file_path.with_suffix(".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        # Retry transient Windows sharing/access faults — this rewrites a file
        # that concurrent sessions contend on under the lock (CLAWP-051).
        retry_transient(tmp.replace, file_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def change_task_state(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    new_state: TaskState,
    note: str | None = None,
    force: bool = False,
    rationale: str | None = None,
    supersedes: str | None = None,
) -> Task | None:
    """Change a task's state by moving its file (or directory for parent tasks)."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

    # CLAWP-053 — rationale is required for REJECTED; validate BEFORE acquiring
    # any lock and BEFORE any filesystem mutation so we fail fast without side
    # effects.  This pure-validation check needs neither the task nor the lock,
    # so it is safe (and cheaper) to run outside the critical section.
    if new_state == TaskState.REJECTED:
        if not rationale or not rationale.strip():
            raise ValueError(
                "A non-empty rationale is required when rejecting a task. "
                "Pass rationale='<reason>' to change_task_state()."
            )

    # CLAWP-051 / CLAWP-071 — hold the per-project lock around the ENTIRE
    # resolve→classify→validate→mutate→reload transaction, not just the final
    # move. Findings 1+4+5 (Codex, CLAWP-051) plus the CLAWP-071 structural
    # TOCTOU fix mean the task RESOLUTION (get_task) and the directory-vs-file
    # CLASSIFICATION now happen INSIDE the lock too, so the whole operation acts
    # on one consistent snapshot — a concurrent split_task converting the file to
    # a directory (or vice-versa) between resolution and mutation can no longer
    # split the snapshot. Prevented races:
    #   (0) task resolution + is_directory_task classification before lock (CLAWP-071)
    #   (1) REJECTED frontmatter rewrite before lock (Finding 1)
    #   (4) parent rollup check evaluated before lock (Finding 4)
    #   (5) Task.from_file reload after lock release (Finding 5)
    #
    # REENTRANCY (CLAWP-066): file_lock is reentrant per-thread, so a callee that
    # re-acquires the same lock path nests safely instead of self-deadlocking.
    _lock_path = tasks_dir / ".clawpm-tasks.lock"

    with file_lock(_lock_path):
        # (0) Resolve + classify INSIDE the lock so the snapshot is consistent.
        task = get_task(config, project_id, task_id)
        if not task or not task.file_path:
            return None
        current_path = task.file_path
        is_directory_task = current_path.name == "_task.md"

        if is_directory_task:
            # For directory-based tasks, move the entire directory.
            task_dir = current_path.parent

            # Resolve the target directory (pure computation; mkdir with
            # exist_ok is idempotent so it is safe under the lock).
            if new_state == TaskState.OPEN:
                new_dir = tasks_dir / task_id
            elif new_state == TaskState.PROGRESS:
                # Progress doesn't move directory, just tracks in some other way
                # For now, keep in same location (progress is tracked differently for dirs)
                new_dir = task_dir
            elif new_state == TaskState.DONE:
                done_dir = tasks_dir / "done"
                done_dir.mkdir(exist_ok=True)
                new_dir = done_dir / task_id
            elif new_state == TaskState.BLOCKED:
                blocked_dir = tasks_dir / "blocked"
                blocked_dir.mkdir(exist_ok=True)
                new_dir = blocked_dir / task_id
            elif new_state == TaskState.REJECTED:
                rejected_dir = tasks_dir / "rejected"
                rejected_dir.mkdir(exist_ok=True)
                new_dir = rejected_dir / task_id
            else:
                return None

            # (a) Re-validate source exists — another session may have moved it.
            if not task_dir.exists():
                raise FileNotFoundError(
                    f"Task directory '{task_dir}' no longer exists — "
                    "it may have been moved by a concurrent session."
                )

            # (c) DONE + not force: re-run rollup INSIDE the lock so a child
            #     reopened between the outer check and here doesn't produce a
            #     false-ready parent (Finding 4). This runs BEFORE the no-op
            #     return so a reopened child still gates an already-`done/`
            #     parent (Codex review: the gate must precede the same-location
            #     early return, matching pre-CLAWP-051 ordering).
            if new_state == TaskState.DONE and not force:
                # Re-read the task from disk so the rollup sees current children.
                _fresh = get_task(config, project_id, task_id)
                if _fresh is None:
                    raise FileNotFoundError(
                        f"Task directory '{task_dir}' no longer exists — "
                        "it may have been moved by a concurrent session."
                    )
                status = parent_rollup_status(config, project_id, _fresh)
                if not status["ready"]:
                    return None

            # (d) REJECTED: write rationale frontmatter INSIDE the lock (Finding
            #     1 — directory-task branch: _task.md is the target). Runs BEFORE
            #     the no-op return so rerunning with a corrected rationale /
            #     supersedes still updates the ledger (Codex review).
            if new_state == TaskState.REJECTED:
                _task_md = task_dir / "_task.md"
                if not _task_md.exists():
                    raise FileNotFoundError(
                        f"Task metadata '{_task_md}' no longer exists — "
                        "it may have been moved by a concurrent session."
                    )
                _write_rejection_frontmatter(_task_md, rationale.strip(), supersedes)  # type: ignore[arg-type]

            # (b) Already in correct location — skip the MOVE only; the gate (c)
            #     and metadata write (d) above have already run. Reload so the
            #     return reflects any frontmatter just written. Guard _task.md
            #     here too: step (a) only checked task_dir.exists(), so a
            #     concurrent session removing just _task.md must surface the
            #     friendly message, not a raw FileNotFoundError (Codex/Grok).
            if task_dir.resolve() == new_dir.resolve():
                _task_md = task_dir / "_task.md"
                if not _task_md.exists():
                    raise FileNotFoundError(
                        f"Task metadata '{_task_md}' no longer exists — "
                        "it may have been moved by a concurrent session."
                    )
                # (b.1) CLAWP-086 (Codex review) — a directory task's PROGRESS
                #       transition keeps `_task.md` in place (no `.progress.md`
                #       rename), so the move-path stamp below never runs. Stamp
                #       here so `start` on a decomposed parent still bumps
                #       `updated`. REJECTED already stamped in step (d).
                if new_state != TaskState.REJECTED:
                    _stamp_updated_file(_task_md)
                return retry_transient(Task.from_file, _task_md)

            # (e) Move (retry transient Windows sharing/access faults — CLAWP-051)
            retry_transient(shutil.move, str(task_dir), str(new_dir))

            # (e.1) CLAWP-086 — stamp `updated` on the relocated file. REJECTED
            #       already stamped via _write_rejection_frontmatter before the
            #       move (whose divergent serializer we must not disturb), so
            #       skip it here.
            if new_state != TaskState.REJECTED:
                _stamp_updated_file(new_dir / "_task.md")

            # (f) Reload and return INSIDE the lock (Finding 5). Retry the read
            #     too: a scanner can hit the freshly-moved file transiently even
            #     though the move under the lock already committed (CLAWP-051).
            return retry_transient(Task.from_file, new_dir / "_task.md")

        # Regular file-based task.  Same critical section as above.
        if new_state == TaskState.OPEN:
            new_path = tasks_dir / f"{task_id}.md"
        elif new_state == TaskState.PROGRESS:
            new_path = tasks_dir / f"{task_id}.progress.md"
        elif new_state == TaskState.DONE:
            done_dir = tasks_dir / "done"
            done_dir.mkdir(exist_ok=True)
            new_path = done_dir / f"{task_id}.md"
        elif new_state == TaskState.BLOCKED:
            blocked_dir = tasks_dir / "blocked"
            blocked_dir.mkdir(exist_ok=True)
            new_path = blocked_dir / f"{task_id}.md"
        elif new_state == TaskState.REJECTED:
            rejected_dir = tasks_dir / "rejected"
            rejected_dir.mkdir(exist_ok=True)
            new_path = rejected_dir / f"{task_id}.md"
        else:
            return None

        # (a) Re-validate source exists.
        if not current_path.exists():
            raise FileNotFoundError(
                f"Task file '{current_path}' no longer exists — "
                "it may have been moved by a concurrent session."
            )

        # (c) DONE + not force: re-run rollup inside the lock (Finding 4). Runs
        #     BEFORE the no-op return so a reopened child still gates an
        #     already-`done/` parent (Codex review).
        if new_state == TaskState.DONE and not force:
            _fresh = get_task(config, project_id, task_id)
            if _fresh is None:
                raise FileNotFoundError(
                    f"Task file '{current_path}' no longer exists — "
                    "it may have been moved by a concurrent session."
                )
            status = parent_rollup_status(config, project_id, _fresh)
            if not status["ready"]:
                return None

        # (d) REJECTED: write rationale frontmatter INSIDE the lock (Finding 1).
        #     Runs BEFORE the no-op return so rerunning with a corrected
        #     rationale / supersedes still updates the ledger (Codex review).
        if new_state == TaskState.REJECTED:
            _write_rejection_frontmatter(current_path, rationale.strip(), supersedes)  # type: ignore[arg-type]

        # (b) Already in correct location — skip the MOVE only; the gate (c) and
        #     metadata write (d) above have already run. Reload for a fresh view.
        if current_path.resolve() == new_path.resolve():
            return retry_transient(Task.from_file, current_path)

        # (e) Move (retry transient Windows sharing/access faults — CLAWP-051)
        retry_transient(shutil.move, str(current_path), str(new_path))

        # (e.1) CLAWP-086 — stamp `updated` on the relocated file. REJECTED
        #       already stamped via _write_rejection_frontmatter before the move.
        if new_state != TaskState.REJECTED:
            _stamp_updated_file(new_path)

        # (f) Reload and return INSIDE the lock (Finding 5). Retry the read too:
        #     a scanner can hit the freshly-moved file transiently even though
        #     the move under the lock already committed (CLAWP-051).
        return retry_transient(Task.from_file, new_path)


def _newest_mtime(entry: Path) -> float:
    """Most-recent content mtime for a task ``entry`` (CLAWP-085).

    A plain file returns its own mtime. A directory task returns the newest
    mtime across the FILES in its tree, so a subtask edited within the window
    keeps its parent out of the archive even when ``_task.md`` itself is stale
    (review r2). The directory inode's own mtime is deliberately ignored — it
    bumps on any child add/remove, which is not task activity and would make a
    directory task effectively un-archivable. Per-file stat failures are
    skipped; an unreadable file entry surfaces its OSError to the caller.
    """
    if entry.is_file():
        return entry.stat().st_mtime
    newest: float | None = None
    for p in entry.rglob("*"):
        if not p.is_file():
            continue
        try:
            m = p.stat().st_mtime
        except OSError:
            continue
        if newest is None or m > newest:
            newest = m
    # Empty directory (no files) — fall back to the dir's own mtime.
    return newest if newest is not None else entry.stat().st_mtime


def archive_done_tasks(
    config: PortfolioConfig,
    project_id: str,
    *,
    older_than_days: float,
    dry_run: bool = False,
) -> list[dict]:
    """Move stale DONE tasks out of the hot path into ``done/archive/``.

    CLAWP-085. ``done/`` grows unboundedly and every ``list``/``next``/
    ``reflect`` scan pays for it. This relocates DONE tasks whose file has not
    been touched in ``older_than_days`` days into ``tasks/done/archive/`` —
    still on disk, still resolvable by ``get_task`` (the archive dir is in
    ``_candidate_task_paths``), but skipped by default scans.

    Move-not-delete: nothing is ever removed (destructive-ops doctrine). The
    archive lives *under* ``done/`` so ``Task.from_file`` still derives DONE
    state from the ``done`` path component — no state metadata is rewritten.

    Age signal is the newest mtime across the task entry (the whole tree for a
    directory task) — a proxy for last activity, so a recently-touched subtask
    keeps its parent out of the archive. There is no dedicated completion
    timestamp yet (CLAWP-086's ``updated`` frontmatter can sharpen this later).

    The calibration corpus (``~/clawpm/reflections/*.jsonl``) is keyed by task
    id and lives outside the repo — it is untouched, and ``find_reference_tasks``
    reads it directly, so archiving does not affect reference-class anchoring.

    Returns one record per qualifying candidate, each carrying ``{"id", "from",
    "to"}`` plus an outcome marker:

      - clean move (or, under ``dry_run``, a planned move): no extra marker;
      - ``"skipped"``: ``"destination_exists"`` (an id already archived — never
        clobbered) or ``"source_vanished"`` (a concurrent session moved it
        between the scan and the move);
      - ``"error"``: a ``stat`` failure on the candidate — surfaced, never
        swallowed, so a single bad file doesn't silently shrink the report
        (Grok review). Records are appended AFTER the move commits, so the list
        never claims a move that didn't happen.
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return []
    done_dir = tasks_dir / "done"
    if not done_dir.exists():
        return []

    archive_dir = done_dir / "archive"
    cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
    results: list[dict] = []

    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        for entry in sorted(done_dir.iterdir()):
            # Never recurse into (or re-archive) the archive silo itself, and
            # skip hidden/lock files. Case-insensitive match on the silo name so
            # an "Archive" dir on a case-preserving Windows FS isn't treated as a
            # task and moved into itself (team review).
            if entry.name.lower() == "archive" or entry.name.startswith("."):
                continue

            if entry.is_file() and entry.suffix == ".md":
                task_id = entry.stem
            elif entry.is_dir():
                if not (entry / "_task.md").exists():
                    continue
                task_id = entry.name
            else:
                continue

            dest = archive_dir / entry.name
            rec = {"id": task_id, "from": entry.as_posix(), "to": dest.as_posix()}

            try:
                # Age = newest mtime across the entry. For a directory task this
                # spans the whole tree so a recently-touched subtask keeps its
                # parent out of the archive (CLAWP-085 review r2 — Antigravity).
                mtime = _newest_mtime(entry)
            except OSError as exc:
                # A stat failure on an otherwise-qualifying entry is real signal,
                # not a silent skip — surface it in the report (Grok review).
                rec["error"] = f"stat_failed: {exc}"
                results.append(rec)
                continue
            if mtime > cutoff:
                continue  # too recent to archive

            # Destination-collision check runs for BOTH dry-run and the real move
            # so the preview never claims it would archive a task the real run
            # would skip (Codex review r2 P3). Unique task ids make a clash
            # anomalous (id already archived) — skip rather than clobber history.
            if dest.exists():
                rec["skipped"] = "destination_exists"
                results.append(rec)
                continue

            if dry_run:
                results.append(rec)
                continue

            archive_dir.mkdir(parents=True, exist_ok=True)
            # TOCTOU: a concurrent session may have moved the source between the
            # scan above and here. Record and continue rather than raising.
            if not entry.exists():
                rec["skipped"] = "source_vanished"
                results.append(rec)
                continue
            retry_transient(shutil.move, str(entry), str(dest))
            # Record only AFTER the move commits — the list never reports a move
            # that didn't happen (Grok review).
            results.append(rec)

    return results


def is_archived_path(path: Path | None) -> bool:
    """True iff ``path`` sits inside a ``done/archive/`` silo (CLAWP-085).

    Matches the specific ``.../done/archive/...`` sequence, not any path
    component literally named ``archive`` — so a portfolio or repo checked out
    under a directory named ``archive`` never marks a live ``tasks/`` file as
    archived (Codex review P3).
    """
    if path is None:
        return False
    # Case-insensitive on the dir names: a case-preserving/insensitive Windows
    # filesystem (or an externally-created dir) can surface "Archive"/"DONE",
    # and the freeze guard must still fire (team review).
    parts = [p.lower() for p in path.parts]
    return any(
        parts[i] == "done" and parts[i + 1] == "archive"
        for i in range(len(parts) - 1)
    )


def cascade_unblock_dependents(
    config: PortfolioConfig,
    project_id: str,
    completed_task_id: str,
) -> list[dict]:
    """Auto-promote blocked tasks whose deps are now all done.

    Walks blocked tasks; for each whose ``depends`` list includes
    ``completed_task_id`` AND whose entire ``depends`` set is now in DONE,
    transitions the task BLOCKED → OPEN.

    Returns one record per cascaded transition:
    ``{task_id, from_state, to_state, trigger}``. Caller is responsible for
    emitting work_log entries — keeping log I/O at the CLI boundary matches
    the rest of the module.

    The cascade is **shallow by design**: only direct dependents of
    ``completed_task_id`` are re-evaluated. Their own dependents cascade
    when *they* hit DONE later via the next ``done`` call. This is
    sufficient because the outer for-loop visits each task at most once
    and only acts on direct-dependent edges — there is no recursive
    descent that could loop on a malformed ``A -> B -> A`` graph. If a
    future iteration makes the cascade recursive, reintroduce a visited
    set.
    """
    all_tasks = list_tasks(config, project_id)
    by_id = {t.id: t for t in all_tasks}
    tasks_dir = get_tasks_dir(config, project_id)

    transitions: list[dict] = []

    for task in all_tasks:
        if task.state != TaskState.BLOCKED:
            continue
        if completed_task_id not in (task.depends or []):
            continue

        # All deps done?
        # Codex P1 fix: a MISSING dependency must be treated as
        # UNSATISFIED — silently treating a typoed/nonexistent dep ref
        # as "done" violates the dependency contract. The cascade will
        # not promote tasks with dangling deps; `clawpm doctor` already
        # surfaces these via its dangling-ref check.
        # CLAWP-085: a dep absent from by_id (which excludes done/archive) may
        # be an archived DONE task — probe the archive silo (lightweight, no
        # parse; archive holds only DONE) before treating it as unsatisfied, so
        # a blocked task still auto-unblocks when a dep has been archived.
        all_deps_done = True
        for dep_id in task.depends:
            dep = by_id.get(dep_id)
            if dep is not None and dep.state == TaskState.DONE:
                continue
            if dep is None and tasks_dir is not None and any(
                p.exists() for p in _archive_candidate_paths(tasks_dir, dep_id)
            ):
                continue
            all_deps_done = False
            break

        if not all_deps_done:
            continue

        moved = change_task_state(
            config, project_id, task.id, TaskState.OPEN
        )
        if moved is not None:
            transitions.append({
                "task_id": task.id,
                "from_state": "blocked",
                "to_state": "open",
                "trigger": completed_task_id,
            })

    return transitions


def parent_rollup_status(
    config: PortfolioConfig,
    project_id: str,
    task: Task,
) -> dict:
    """Report whether a parent task is ready to be marked DONE (CLAWP-037).

    A parent is *ready* only when every child in ``task.children`` resolves
    to a task in DONE state. A child id that resolves to no task on disk
    (dangling / typoed ref) counts as UNSATISFIED — mirroring the
    missing-dependency handling in ``cascade_unblock_dependents``: a ref we
    cannot verify is not silently treated as satisfied.

    Returns ``{"ready", "incomplete", "missing"}``:
      - ``incomplete``: ``[{"id", "state"}]`` for children not in DONE.
      - ``missing``: ``[id]`` for child refs with no task file.
    A task with no children is trivially ready.
    """
    # CLAWP-037 codex round-3 belt-and-braces: union the parent's persisted
    # children list with any task whose ``parent:`` frontmatter points at
    # this task across every state dir. Persistence (set by add_subtask) is
    # the fast common-path; this scan is the backstop for manually-created
    # or imported subtasks that bypassed add_subtask. Cost is one O(project)
    # glob walk per rollup check — rollup fires only on state transitions,
    # not in hot loops, so this is acceptable at typical project sizes.
    children: set[str] = set(task.children or [])
    tasks_dir = get_tasks_dir(config, project_id) if config is not None else None
    if tasks_dir is not None:
        # Every state dir a child can migrate to — incl. rejected/ (a
        # crash-orphaned split child later rejected lands in tasks/rejected/
        # <child>/_task.md and must still gate the parent's rollup — CLAWP-071
        # Codex r3).
        scan_dirs = [
            tasks_dir,
            tasks_dir / "done",
            tasks_dir / "blocked",
            tasks_dir / "rejected",
        ]
        # Directory-task subtask dir (open subtasks live alongside _task.md).
        if task.file_path is not None and task.file_path.name == "_task.md":
            scan_dirs.append(task.file_path.parent)
        for sd in scan_dirs:
            if not sd.exists():
                continue
            for f in sd.glob("*.md"):
                if f.name == "_task.md":
                    continue
                try:
                    t = Task.from_file(f)
                except Exception:
                    continue
                if t.parent == task.id:
                    children.add(t.id)
            # CLAWP-071 (Codex P2): also discover DIRECTORY-task children
            # (``<dir>/<child>/_task.md``). The ``*.md`` glob above only sees
            # immediate files, so a child that was split into a parent directory
            # would drop out of this backstop. That matters for the add_subtask
            # child-first residual window: a crash-orphaned flat child (present in
            # neither the persisted children list) that is later ``split_task``ed
            # would otherwise vanish from the rollup, letting the parent be marked
            # DONE while the child directory is still open.
            for f in sd.glob("*/_task.md"):
                try:
                    t = Task.from_file(f)
                except Exception:
                    continue
                if t.parent == task.id:
                    children.add(t.id)

    incomplete: list[dict] = []
    missing: list[str] = []
    for child_id in sorted(children):
        child = get_task(config, project_id, child_id)
        if child is None:
            missing.append(child_id)
        elif child.state != TaskState.DONE:
            incomplete.append({"id": child_id, "state": child.state.value})
    return {
        "ready": not incomplete and not missing,
        "incomplete": incomplete,
        "missing": missing,
    }


def parent_ready_signal(
    config: PortfolioConfig,
    project_id: str,
    child_task_id: str,
) -> dict | None:
    """After a child hits DONE, report if its parent is now fully rolled up.

    Returns ``{"parent_id", "children", "ready": True}`` when the just-
    completed child has a parent whose children are ALL now DONE and the
    parent is not already DONE; ``None`` otherwise. Pure read — does NOT
    transition the parent (a synthesis criterion or operator sign-off may
    still gate it); the caller surfaces this as an advisory so the operator
    knows the parent is now closeable.
    """
    child = get_task(config, project_id, child_task_id)
    if child is None or not child.parent:
        return None
    parent = get_task(config, project_id, child.parent)
    if parent is None or parent.state == TaskState.DONE:
        return None
    status = parent_rollup_status(config, project_id, parent)
    if status["ready"]:
        return {
            "parent_id": parent.id,
            "children": parent.children,
            "ready": True,
        }
    return None


# CLAWP-048 — task-ID prefix resolution. The prefix must be UNIQUE per project
# across the portfolio: two projects minting the same prefix break the "task id
# is a portfolio-unique handle" invariant (and feed the cross-project-isolation
# bug class). Resolution order: explicit ``task_prefix`` (settings.toml) -> the
# prefix inferred from the project's existing tasks (stability — never changes a
# project that has already minted) -> a collision-free prefix derived from the
# id (shortest extension of ``id.upper()[:5]`` no other project uses). The
# derived choice is pinned by the first minted task file, after which inference
# keeps it stable regardless of later portfolio changes.

_PREFIX_NUM_RE = re.compile(r"^([A-Z][A-Z0-9-]*?)-(\d+)(?:\.progress)?$")


def _infer_prefix_from_tasks(tasks_dir: Path) -> str | None:
    """Most common task-ID prefix among existing task files/dirs, or None.

    Anchored + non-greedy so a hyphenated prefix (``ARB-P``) is recovered intact
    from ``ARB-P-000`` (cf. CLAWP-047). Subtask files live inside parent dirs,
    not at this level, so they don't skew the count.
    """
    from collections import Counter

    counts: Counter[str] = Counter()
    # CLAWP-085: include done/archive so prefix inference stays stable even when
    # every non-archived task of a project has been archived out of the hot path.
    for scan_dir in (tasks_dir, tasks_dir / "done", tasks_dir / "blocked", tasks_dir / "done" / "archive"):
        if not scan_dir.exists():
            continue
        for entry in scan_dir.iterdir():
            name = entry.stem if entry.is_file() else entry.name
            m = _PREFIX_NUM_RE.match(name)
            if m:
                pfx = m.group(1)
                # Skip subtask-shaped names: a real prefix never ends in
                # -<digits> (that's a parent task id, so this file is a stray
                # subtask, not a top-level task). Mirrors the allocator's
                # anchored exclusion of {prefix}-NNN-MMM files.
                if re.search(r"-\d+$", pfx):
                    continue
                counts[pfx] += 1
    if not counts:
        return None
    # Most common; deterministic tie-break by longer prefix then lexical.
    return max(counts, key=lambda p: (counts[p], len(p), p))


def resolve_existing_prefix(settings) -> str | None:
    """A project's CURRENT prefix without minting: explicit -> inferred -> None.

    ``None`` means the project has no explicit prefix and no tasks yet, so its
    prefix isn't pinned. Used to build the portfolio collision set and by the
    doctor cross-project collision check.
    """
    if getattr(settings, "task_prefix", None):
        return settings.task_prefix.upper()
    if getattr(settings, "project_dir", None):
        # ProjectSettings.project_dir is the REPO ROOT (settings.toml.parent.parent),
        # so tasks live under <repo>/.project/tasks, not <repo>/tasks.
        inferred = _infer_prefix_from_tasks(settings.project_dir / ".project" / "tasks")
        if inferred:
            return inferred
    return None


def _portfolio_prefixes(config, exclude_id: str) -> set[str]:
    """Prefixes already claimed by OTHER projects (resolved, or ``[:5]`` for the
    task-less ones, so a new project can't grab a prefix another would derive)."""
    from .discovery import discover_projects

    used: set[str] = set()
    for p in discover_projects(config):
        if p.id == exclude_id:
            continue
        used.add(resolve_existing_prefix(p) or p.id.upper()[:5])
    return used


def assign_task_prefix(
    project_id: str, tasks_dir: Path, config, explicit_prefix: str | None = None
) -> str:
    """Resolve the prefix to mint a new task under (CLAWP-048).

    explicit ``task_prefix`` -> inferred-from-existing (stability) -> shortest
    collision-free extension of ``id.upper()[:5]``. A new project that would
    collide on ``[:5]`` gets the shortest longer prefix no other project uses.
    """
    if explicit_prefix:
        return explicit_prefix.upper()
    inferred = _infer_prefix_from_tasks(tasks_dir)
    if inferred:
        return inferred
    full = project_id.upper()
    used = _portfolio_prefixes(config, project_id)
    base = full[:5] if len(full) >= 5 else full
    if base and base not in used:
        return base
    for n in range(6, len(full) + 1):
        if full[:n] not in used:
            return full[:n]
    return full  # ids are portfolio-unique, so the full id can't collide


def add_task(
    config: PortfolioConfig,
    project_id: str,
    title: str,
    task_id: str | None = None,
    priority: int = 5,
    complexity: TaskComplexity | None = None,
    depends: list[str] | None = None,
    scope: list[str] | None = None,
    tags: list[str] | None = None,
    description: str = "",
    predictions: Predictions | None = None,
    parallel_group: int | None = None,
    agent_profile: str | None = None,
    out_of_scope: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    delegability: str | None = None,
) -> Task | None:
    """Add a new task to a project."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        # Registry lookup succeeded but tasks/ doesn't exist yet - or registry
        # lookup failed entirely.  Try registry first, then CWD-walk fallback.
        project_dot_dir = get_project_dir(config, project_id)
        if not project_dot_dir:
            # Registry failed (e.g. malformed settings.toml).  Fall back to CWD
            # walk so operators don't get a silent failure when inside the repo.
            project_dot_dir = find_project_dir_fallback(config, project_id)
        if not project_dot_dir:
            return None
        tasks_dir = project_dot_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

    # CLAWP-055 — resolve baseline_ref BEFORE entering the lock: this may
    # invoke a git subprocess, which must not be held inside a critical section.
    from .baseline import resolve_baseline_ref
    from .discovery import get_project as _get_project_for_baseline

    _proj_settings = _get_project_for_baseline(config, project_id)
    _repo_path = getattr(_proj_settings, "repo_path", None) if _proj_settings else None
    _baseline_ref = resolve_baseline_ref(_repo_path)

    # CLAWP-051 — per-project file lock serialises ID allocation (scan→write)
    # and explicit-ID creates so two concurrent sessions in the same project
    # can't derive the same next_num (TOCTOU) or clobber each other's create.
    # Granularity: one lock file per tasks-dir — different projects run freely.
    # DEADLOCK SAFETY: do NOT call any function that re-enters file_lock on
    # the same lock_path from within this block.
    _lock_path = tasks_dir / ".clawpm-tasks.lock"
    # Capture whether this is an explicit-ID create BEFORE entering the lock
    # so the clobber guard (Finding 2) can be applied inside atomically.
    _explicit_id = task_id is not None
    with file_lock(_lock_path):
        # Generate task ID if not provided (inside lock: scan is now serialised)
        if not task_id:
            # CLAWP-048: resolve a portfolio-unique prefix (explicit task_prefix ->
            # inferred from existing tasks -> collision-free derivation) instead of
            # the naive id.upper()[:5], which collides across near-name-twin ids.
            from .discovery import get_project

            _settings = get_project(config, project_id)
            prefix = assign_task_prefix(
                project_id,
                tasks_dir,
                config,
                explicit_prefix=getattr(_settings, "task_prefix", None) if _settings else None,
            )

            # Find highest existing task number.
            # We must check BOTH .md files and parent-task directories (e.g. OPENW-004/)
            # because split tasks convert the file to a directory.  The *.md glob misses
            # directories, so without this check add_task would re-issue the same number.
            # Subtask files (OPENW-004-001.md) live *inside* parent dirs; they don't
            # appear at the scan-dir level, so they won't pollute top-level numbering.
            # CLAWP-047: the prefix can ITSELF contain a hyphen — project id
            # "arb-prd" -> prefix "ARB-P" — so the old `f.stem.split("-")[1]`
            # grabbed the wrong segment ("P"), raised ValueError, skipped EVERY
            # matching file, and collapsed every new task to {prefix}-000, silently
            # overwriting prior tasks. Match the trailing number with an anchored
            # regex instead (the in-progress `.progress` suffix is part of the
            # stem) — the same shape the directory scan below already uses, so the
            # two scans can't disagree.
            _dir_pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
            _file_pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\.progress)?$")

            existing_nums = []

            # CLAWP-085: include done/archive so an archived task's number is
            # never re-minted. add_task is not a hot path, so paying the extra
            # archive scan here (unlike list/next/reflect) is the correct
            # trade — a silently reused ID would clobber archived history.
            for scan_dir in [tasks_dir, tasks_dir / "done", tasks_dir / "blocked", tasks_dir / "done" / "archive"]:
                if not scan_dir.exists():
                    continue
                # .md files at this level. Subtask files ({prefix}-000-001.md) live
                # inside parent dirs, not here, and the anchored pattern excludes
                # them regardless, so they never pollute top-level numbering.
                for f in scan_dir.glob(f"{prefix}-*.md"):
                    m = _file_pat.match(f.stem)
                    if m:
                        existing_nums.append(int(m.group(1)))
                # Parent-task directories at this level
                for entry in scan_dir.iterdir():
                    if entry.is_dir():
                        m = _dir_pat.match(entry.name)
                        if m:
                            existing_nums.append(int(m.group(1)))

            next_num = max(existing_nums, default=-1) + 1
            task_id = f"{prefix}-{next_num:03d}"

        # Build frontmatter. CLAWP-086 — `updated` equals `created` at add time.
        _today = date.today().isoformat()
        frontmatter = {
            "id": task_id,
            "priority": priority,
            "created": _today,
            "baseline_ref": _baseline_ref,
        }
        stamp_updated(frontmatter, _today)

        if complexity:
            frontmatter["complexity"] = complexity.value

        if depends:
            frontmatter["depends"] = depends

        if scope:
            frontmatter["scope"] = scope

        # CLAWP-069 — normalise tags at the write boundary so what lands on disk
        # matches what the filter/count paths expect (lowercased, deduped).
        if tags:
            norm_tags = normalize_tags(tags)
            if norm_tags:
                frontmatter["tags"] = norm_tags

        if parallel_group is not None:
            frontmatter["parallel_group"] = parallel_group

        if agent_profile:
            frontmatter["agent_profile"] = agent_profile
        # CLAWP-054 — contract fields
        if out_of_scope:
            frontmatter["out_of_scope"] = out_of_scope
        if stop_conditions:
            frontmatter["stop_conditions"] = stop_conditions
        if delegability and delegability != "either":
            frontmatter["delegability"] = delegability

        if predictions and not predictions.is_empty():
            pred_dict = predictions.to_dict()
            # Strip None / empty-list values to keep the file clean
            frontmatter["predictions"] = {
                k: v for k, v in pred_dict.items()
                if v is not None and v != []
            }

        # Build content
        content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()}
---
# {title}

{description}

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

"""

        # Write file — explicit utf-8 so Unicode titles (e.g. →, –, emoji) don't
        # raise UnicodeEncodeError on Windows where the default locale is cp1252.
        file_path = tasks_dir / f"{task_id}.md"

        # CLAWP-051 Finding 2 — explicit-ID clobber guard.
        # Generated IDs are fresh-by-construction (scan under the lock above) so
        # only the explicit-ID path needs this check. Probe EXISTENCE across the
        # same location set get_task searches — not get_task itself — so an ID
        # already present in ANY state (progress/done/blocked/rejected or as a
        # directory task) is caught even if its file is currently unparseable;
        # a presence check, unlike get_task's parse-and-continue scan, can't be
        # fooled into clobbering a corrupt prior file (Codex review).
        if _explicit_id and any(
            p.exists() for p in _candidate_task_paths(tasks_dir, task_id)
        ):
            raise FileExistsError(
                f"Task '{task_id}' already exists. "
                "Pass a different task_id or omit it to auto-generate one."
            )

        tmp_path = file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            # Retry transient Windows sharing/access faults on the rename (CLAWP-051)
            retry_transient(tmp_path.replace, file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # Reload and return INSIDE the lock — consistent with change_task_state's
        # reload-under-lock contract (CLAWP-051 Finding 5). The file was just
        # written under this lock, so the read can't race another clawpm writer;
        # retry_transient covers a scanner touching the fresh file (CLAWP-051).
        return retry_transient(Task.from_file, file_path)


def edit_task(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    title: str | None = None,
    priority: int | None = None,
    complexity: TaskComplexity | None = None,
    scope: list[str] | None = None,
    tags: list[str] | None = None,
    clear_tags: bool = False,
    body: str | None = None,
    predictions: Predictions | None = None,
    parallel_group: int | None = None,
    clear_parallel_group: bool = False,
    out_of_scope: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    delegability: str | None = None,
) -> Task | None:
    """Edit task metadata (frontmatter) and optionally title/body."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

    # CLAWP-066: edit_task is a task-tree mutator like change_task_state/add_task,
    # so it holds the per-project lock across the whole read→modify→write→reload
    # to serialise against concurrent state moves / splits (was previously an
    # unlocked, non-retried in-place rewrite). file_lock is reentrant per-thread.
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        task = get_task(config, project_id, task_id)
        if not task or not task.file_path:
            return None

        # CLAWP-071 — the task was resolved under this lock; a FileNotFound here
        # means an external process deleted/moved it. Map to the friendly
        # concurrent-modification error instead of a raw traceback.
        with guard_fs_tamper(f"Task {task_id}"):
            text = task.file_path.read_text(encoding="utf-8")

        # Parse frontmatter and content. An absent fence falls through with
        # frontmatter={}, content=text (a fence is synthesised on rebuild). An
        # unterminated or unparseable fence is refused rather than rebuilt into
        # a double-frontmatter, metadata-wiped file (Codex / Grok review).
        frontmatter: dict
        try:
            frontmatter, content = split_frontmatter(text)
        except FrontmatterError as exc:
            if exc.reason == "absent":
                frontmatter, content = {}, text
            elif exc.reason == "unterminated":
                raise ValueError(
                    f"Task {task_id} has an unterminated frontmatter fence; "
                    "refusing to edit (would corrupt the file)."
                ) from None
            else:
                cause = exc.__cause__ or exc
                raise ValueError(
                    f"Task {task_id} frontmatter is unparseable; refusing "
                    f"to edit (would corrupt the file): {cause}"
                ) from cause

        # Update frontmatter fields
        if priority is not None:
            frontmatter["priority"] = priority
        if complexity is not None:
            frontmatter["complexity"] = complexity.value
        if scope is not None:
            if scope:
                frontmatter["scope"] = scope
            else:
                frontmatter.pop("scope", None)
        # CLAWP-069 — tags REPLACE (mirrors scope): --tag X --tag Y sets the
        # full set; --clear-tags removes the field entirely. Normalise so on-
        # disk form matches the filter/count paths.
        # Removal is EXCLUSIVELY via clear_tags — a tags value that normalises
        # to empty (e.g. `--tag ""` / a blank shell-expansion) is treated as a
        # no-op, never a silent wipe of existing tags (Codex + Grok review).
        if clear_tags:
            frontmatter.pop("tags", None)
        elif tags is not None:
            norm_tags = normalize_tags(tags)
            if norm_tags:
                frontmatter["tags"] = norm_tags
        if clear_parallel_group:
            frontmatter.pop("parallel_group", None)
        elif parallel_group is not None:
            frontmatter["parallel_group"] = parallel_group
        if predictions is not None:
            if predictions.is_empty():
                frontmatter.pop("predictions", None)
            else:
                pred_dict = predictions.to_dict()
                frontmatter["predictions"] = {
                    k: v for k, v in pred_dict.items()
                    if v is not None and v != []
                }
        # CLAWP-054 — contract fields
        if out_of_scope is not None:
            if out_of_scope:
                frontmatter["out_of_scope"] = out_of_scope
            else:
                frontmatter.pop("out_of_scope", None)
        if stop_conditions is not None:
            if stop_conditions:
                frontmatter["stop_conditions"] = stop_conditions
            else:
                frontmatter.pop("stop_conditions", None)
        if delegability is not None:
            if delegability != "either":
                frontmatter["delegability"] = delegability
            else:
                frontmatter.pop("delegability", None)

        # Update title in content (first # heading)
        if title is not None:
            lines = content.split("\n")
            replaced = False
            for i, line in enumerate(lines):
                if line.startswith("# "):
                    lines[i] = f"# {title}"
                    replaced = True
                    break
            if not replaced:
                lines.insert(0, f"# {title}")
            content = "\n".join(lines)

        # Replace body (everything between title and ## sections)
        if body is not None:
            lines = content.split("\n")
            title_idx = None
            section_idx = None
            for i, line in enumerate(lines):
                if line.startswith("# ") and title_idx is None:
                    title_idx = i
                elif line.startswith("## ") and title_idx is not None:
                    section_idx = i
                    break

            if title_idx is not None:
                before = lines[:title_idx + 1]
                after = lines[section_idx:] if section_idx is not None else []
                content = "\n".join(before) + f"\n\n{body}\n\n" + "\n".join(after)

        # CLAWP-086 — every edit bumps the `updated` stamp.
        stamp_updated(frontmatter)

        # Rebuild file — utf-8 always so Unicode content survives on Windows
        new_text = f"---\n{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()}\n---\n{content}"
        tmp_path = task.file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(new_text, encoding="utf-8")
            retry_transient(tmp_path.replace, task.file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        return retry_transient(Task.from_file, task.file_path)


def split_task(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
) -> Task | None:
    """Convert a regular task file into a parent directory structure.
    
    Converts TASK-ID.md → TASK-ID/_task.md
    Works from any state directory (tasks/, done/, blocked/).
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

    # CLAWP-066: hold the per-project lock around the whole resolve→mutate→reload.
    # file_lock is reentrant per-thread, so this is safe whether split_task is
    # called directly (CLI / emit_tree — acquires for real) or from inside
    # add_subtask's critical section (nested re-acquire on the same path).
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        task = get_task(config, project_id, task_id)
        if not task or not task.file_path:
            return None

        # CLAWP-085: refuse to restructure an archived task — it is frozen DONE
        # history under done/archive/ (Codex review P2).
        if is_archived_path(task.file_path):
            raise ValueError(
                f"Task '{task_id}' is archived (done/archive/) — archived tasks "
                "are frozen history and cannot be split. Move it out of the "
                "archive first."
            )

        # Already a directory-based task
        if task.file_path.name == "_task.md":
            return task

        current_path = task.file_path
        parent_dir = current_path.parent

        # Create task directory in same location as current file
        task_dir = parent_dir / task_id
        task_dir.mkdir(exist_ok=True)

        # Move file to _task.md inside directory (retry transient Windows
        # sharing/access faults on the rename — consistent with the other moves).
        new_path = task_dir / "_task.md"
        retry_transient(shutil.move, str(current_path), str(new_path))

        # CLAWP-086 — splitting a leaf into a parent dir is a structural mutation.
        _stamp_updated_file(new_path)

        return retry_transient(Task.from_file, new_path)


def _child_append_text(parent_path: Path, child_id: str) -> str | None:
    """Compute the parent's frontmatter rewritten to include ``child_id`` in its
    ``children`` list, or ``None`` if the append is a genuine no-op.

    Pure (reads the parent, builds the new text, writes nothing) so callers can
    STAGE this alongside the child-file write and commit both together (CLAWP-071
    two-phase). Returns ``None`` ONLY for a true no-op — the parent is not a
    directory-task ``_task.md`` (nothing to persist into) or already lists the
    child (idempotent).

    RAISES on an inability to BUILD the update, so the two-phase caller writes
    NEITHER file rather than orphaning the child (Codex CLAWP-071 review — do not
    conflate "nothing to do" with "couldn't build"):
    - :class:`ConcurrentModificationError` if the parent ``_task.md`` vanished
      (an external delete/move under the lock).
    - :class:`FrontmatterError` (a ``ValueError``) if its frontmatter is
      absent / unterminated / unparseable.
    """
    if parent_path.name != "_task.md":
        return None  # not a directory-task parent — genuine no-op
    if not parent_path.exists():
        raise ConcurrentModificationError(
            f"Parent task '{parent_path}' vanished mid-update — a concurrent "
            "session or external process moved or deleted it. Retry the operation."
        )
    with guard_fs_tamper(f"Parent task '{parent_path}'"):
        text = parent_path.read_text(encoding="utf-8")
    fm, raw_body = split_frontmatter(text)  # raises FrontmatterError on malformation
    children = fm.get("children")
    if not isinstance(children, list):
        children = []
    if child_id in children:
        return None  # idempotent — already persisted
    children.append(child_id)
    fm["children"] = children
    stamp_updated(fm)  # CLAWP-086 — gaining a child mutates the parent.
    body = raw_body.lstrip("\n")
    return (
        "---\n"
        + yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
        + "\n---\n"
        + body
    )


def _append_child_to_parent_frontmatter(
    parent_path: Path, child_id: str,
) -> None:
    """Persist ``child_id`` into the parent's frontmatter ``children`` list.

    CLAWP-037 round-1 fix (codex P1): the parent's children list must survive
    a child migrating out of the parent directory (DONE → tasks/done/, BLOCKED
    → tasks/blocked/, or a deletion). Without persistence, dir-scan-derived
    children silently shrink and the rollup gate's missing/dangling-child
    handling never fires. Idempotent — repeated calls for the same child_id
    leave the list unchanged.

    Standalone writer kept for callers that update the parent independently of a
    child-file write (``emit_tree``); it stays LENIENT — a vanished or
    malformed-frontmatter parent is skipped, matching the pre-CLAWP-071 contract.
    ``add_subtask`` instead uses ``_child_append_text`` + :func:`commit_staged_pair`
    for two-phase atomicity, where a build failure is fatal (no orphan child).
    """
    try:
        new_text = _child_append_text(parent_path, child_id)
    except (FrontmatterError, ConcurrentModificationError):
        return  # lenient: skip a vanished / malformed-frontmatter parent
    if new_text is None:
        return
    tmp = parent_path.with_suffix(parent_path.suffix + ".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        # Retry transient Windows sharing/access faults — every subtask worker
        # rewrites this same parent _task.md under the lock, so the rename hits
        # post-write handle contention from the OS/AV scanner (CLAWP-051).
        retry_transient(tmp.replace, parent_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _child_state_dirs(tasks_dir: Path, parent_dir: Path) -> list[Path]:
    """Every directory a child of this parent can legitimately live in.

    A subtask's file starts under ``parent_dir`` but ``change_task_state`` MOVES
    it out on a terminal/blocked transition: DONE → ``tasks/done/``, BLOCKED →
    ``tasks/blocked/``, REJECTED → ``tasks/rejected/``. A child that was split
    into its own directory task and then REOPENED lands at the top-level
    ``tasks/<child>/_task.md`` (not back under the parent). Any of these
    locations can hold a crash-orphaned child that is absent from the parent's
    persisted ``children`` list, so the ID allocator and rollup backstop must
    scan all of them or they can reuse an occupied ordinal / miss an unresolved
    child (CLAWP-071 Codex r1-3).

    CLAWP-085: the ``done/archive/`` silo (standalone archived children) and an
    archived directory-task parent's own dir (its children travelled with it)
    are included too, so a re-decompose can never re-mint an ordinal that has
    been archived out of ``done/``.
    """
    parent_id = parent_dir.name
    return [
        parent_dir,
        tasks_dir,
        tasks_dir / "done",
        tasks_dir / "blocked",
        tasks_dir / "rejected",
        tasks_dir / "done" / "archive",
        tasks_dir / "done" / "archive" / parent_id,
    ]


def _existing_child_ordinals(
    tasks_dir: Path, parent_dir: Path, parent_id: str,
) -> set[int]:
    """Union of every ordinal already used by a child of ``parent_id``.

    Shared allocator for ``add_subtask`` and ``emit_tree``'s attach path so the
    two can't drift (Codex CLAWP-071 r3). Scans, across ALL state dirs
    (:func:`_child_state_dirs`):
    - flat children ``<parent>-NNN.md`` (excluding a directory's own ``_task.md``),
    - directory children ``<parent>-NNN/_task.md``,
    and unions the parent's persisted ``children`` list (covers a child deleted
    outright after creation, invisible to any dir scan). Reading the max of this
    set and adding 1 guarantees a fresh ordinal even when earlier children have
    migrated, reopened, been rejected, or crash-orphaned.
    """
    nums: set[int] = set()

    def _record(id_or_stem: str) -> None:
        try:
            nums.add(int(id_or_stem.split("-")[-1].replace(".progress", "")))
        except (IndexError, ValueError):
            pass

    for state_dir in _child_state_dirs(tasks_dir, parent_dir):
        if not state_dir.exists():
            continue
        for f in state_dir.glob(f"{parent_id}-*.md"):
            if f.name != "_task.md":
                _record(f.stem)
        for d in state_dir.glob(f"{parent_id}-*"):
            if d.is_dir() and (d / "_task.md").exists():
                _record(d.name)

    # Parent's persisted children — covers a child deleted after creation, which
    # no directory scan can see. Read leniently: a vanished/malformed parent must
    # not crash allocation (the dir scans above already bound the union).
    for parent_file in (parent_dir / "_task.md", tasks_dir / f"{parent_id}.md"):
        if not parent_file.exists():
            continue
        try:
            fm, _ = parse_frontmatter(parent_file.read_text(encoding="utf-8"))
        except OSError:
            continue
        # parse_frontmatter is lenient — it swallows YAML parse errors and returns
        # ({}, body), so malformed frontmatter never reaches here as an exception.
        # But it does NOT coerce a non-mapping document to dict (a list/scalar
        # frontmatter yields a non-dict), so guard the .get() to stay lenient
        # rather than raising AttributeError on that edge.
        children = fm.get("children") if isinstance(fm, dict) else None
        for cid in (children or []):
            if isinstance(cid, str) and cid.startswith(parent_id + "-"):
                _record(cid)

    return nums


def add_subtask(
    config: PortfolioConfig,
    project_id: str,
    parent_id: str,
    title: str,
    priority: int = 5,
    complexity: TaskComplexity | None = None,
    description: str = "",
    agent_profile: str | None = None,
    predictions: Predictions | None = None,
    depends: list[str] | None = None,
    scope: list[str] | None = None,
    parallel_group: int | None = None,
    out_of_scope: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    delegability: str | None = None,
    tags: list[str] | None = None,
) -> Task | None:
    """Add a subtask to a parent task.
    
    Auto-splits parent if not already a directory.
    Generates sequential subtask ID (PARENT-001, PARENT-002, etc.).
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None
    
    # CLAWP-051 Finding 6 — wrap the ENTIRE parent-resolution + allocate-and-create
    # in file_lock so concurrent sessions decomposing the same parent can't mint
    # the same subtask ID, clobber each other, OR read a half-written parent.
    # The parent read + split MUST be inside the lock too: a sibling worker
    # rewriting the parent's _task.md (frontmatter append) mid-rename would
    # otherwise make get_task here transiently return None and drop the subtask
    # (observed under contention). Serialising the read also serialises concurrent
    # splits, so the flat→directory conversion is race-free without a test seed.
    #
    # REENTRANCY (CLAWP-066): split_task now ALSO acquires this same per-project
    # lock. That is safe because file_lock is reentrant per-thread — split_task's
    # acquire here nests on the depth this block already holds rather than
    # self-deadlocking. get_task (fs scan) and _append_child_to_parent_frontmatter
    # (plain read/write) take no lock.
    _lock_path = tasks_dir / ".clawpm-tasks.lock"
    with file_lock(_lock_path):
        # Resolve the parent as a directory INSIDE the lock (read + optional split).
        parent = get_task(config, project_id, parent_id)
        if not parent:
            return None
        # CLAWP-085: an archived parent is frozen DONE history. Decomposing into
        # it would write the new child under done/archive/, making it path-DONE
        # and invisible to default scans. Refuse — the operator must unarchive
        # (or pick a live parent) first (Codex review P2).
        if is_archived_path(parent.file_path):
            raise ValueError(
                f"Task '{parent_id}' is archived (done/archive/) — archived tasks "
                "are frozen history and cannot take new subtasks. Move it out of "
                "the archive before decomposing."
            )
        if parent.file_path and parent.file_path.name != "_task.md":
            parent = split_task(config, project_id, parent_id)
            if not parent:
                return None
        parent_dir = parent.file_path.parent if parent.file_path else None
        if not parent_dir:
            return None

        # Generate subtask ID via the shared allocator, which unions every used
        # ordinal across ALL state dirs (flat + directory children, incl.
        # done/archive per CLAWP-085) plus the parent's persisted list — so a
        # migrated/reopened/rejected/deleted/archived or crash-orphaned child
        # can't have its number silently reused (CLAWP-071 Codex r1-3).
        # emit_tree's attach path routes through the same helper so the two
        # allocators can't drift.
        existing_nums = _existing_child_ordinals(tasks_dir, parent_dir, parent_id)
        next_num = (max(existing_nums) if existing_nums else 0) + 1
        subtask_id = f"{parent_id}-{next_num:03d}"

        # Build frontmatter. CLAWP-086 — `updated` equals `created` at add time.
        _today = date.today().isoformat()
        frontmatter: dict = {
            "id": subtask_id,
            "priority": priority,
            "parent": parent_id,
            "created": _today,
        }
        stamp_updated(frontmatter, _today)

        if complexity:
            frontmatter["complexity"] = complexity.value

        if depends:
            frontmatter["depends"] = depends

        if scope:
            frontmatter["scope"] = scope

        if parallel_group is not None:
            frontmatter["parallel_group"] = parallel_group

        if agent_profile:
            frontmatter["agent_profile"] = agent_profile

        # CLAWP-054 — contract fields, symmetric with add_task so a subtask is a
        # fully-specified verifiable goal (CLAWP-072-006: these were silently
        # dropped on the `tasks add --parent` path).
        if out_of_scope:
            frontmatter["out_of_scope"] = out_of_scope
        if stop_conditions:
            frontmatter["stop_conditions"] = stop_conditions
        if delegability and delegability != "either":
            frontmatter["delegability"] = delegability

        # CLAWP-069 — subtasks may carry their own workstream tags (no
        # propagation from the parent; each task is tagged independently).
        if tags:
            norm_tags = normalize_tags(tags)
            if norm_tags:
                frontmatter["tags"] = norm_tags

        # CLAWP-037 — children created via `tasks decompose` carry their own
        # success_criteria (and other predictions) so each subtask is a
        # verifiable goal, and the parent rolls up only when all pass.
        if predictions and not predictions.is_empty():
            pred_dict = predictions.to_dict()
            frontmatter["predictions"] = {
                k: v for k, v in pred_dict.items()
                if v is not None and v != []
            }

        # Build content
        content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()}
---
# {title}

{description}

## Notes

"""

        # Write file — utf-8 so Unicode in title/description survives on Windows
        file_path = parent_dir / f"{subtask_id}.md"

        # CLAWP-071 — two-phase atomicity of the child-create + parent children-
        # list append. Compute the parent's rewritten frontmatter BEFORE writing
        # anything (CLAWP-037: persist the child so the rollup gate keeps it in
        # view after the child migrates to done/ or blocked/), then stage + commit
        # both files together. The child is committed FIRST: its ``parent:``
        # frontmatter is the authoritative link and an orphan child (child on
        # disk, parent cache not yet updated) is reconciled by
        # ``parent_rollup_status`` (which scans flat AND directory-task children).
        #
        # ``_child_append_text`` returns None ONLY for a genuine no-op (child
        # already listed — impossible for a fresh subtask id) and RAISES on a
        # build failure (parent vanished / unparseable). So the single-write path
        # below is taken only for the true no-op; a build failure propagates and
        # writes NEITHER file, instead of orphaning the child (Codex review).
        parent_new_text = _child_append_text(parent.file_path, subtask_id)
        if parent_new_text is None:
            # True no-op on the parent — single atomic child write.
            tmp_path = file_path.with_suffix(".tmp")
            try:
                tmp_path.write_text(content, encoding="utf-8")
                # Retry transient Windows sharing/access faults (CLAWP-051).
                retry_transient(tmp_path.replace, file_path)
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise
        else:
            commit_staged_pair(
                (file_path, content),
                (parent.file_path, parent_new_text),
            )

        # Reload under the lock; retry_transient covers a scanner touching the
        # freshly-written child file even though the write committed (CLAWP-051).
        return retry_transient(Task.from_file, file_path)

