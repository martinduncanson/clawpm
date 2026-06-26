"""Task operations for ClawPM."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import yaml

from .concurrency import file_lock, retry_transient
from .models import Task, TaskState, TaskComplexity, Predictions, PortfolioConfig
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
        elif item.is_dir() and not item.name.startswith(".") and item.name not in ("done", "blocked", "rejected"):
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


def list_tasks(
    config: PortfolioConfig,
    project_id: str,
    state_filter: TaskState | None = None,
) -> list[Task]:
    """List all tasks for a project."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return []

    tasks: list[Task] = []

    # CLAWP-053: rejected/ is a terminal-state silo like done/ and blocked/.
    # It is excluded from the default (no-filter) scan so rejected tasks never
    # surface in open listings. It is only added when explicitly requested.
    if state_filter == TaskState.REJECTED:
        locations = [tasks_dir / "rejected"]
    else:
        locations = [
            tasks_dir,  # Main dir - open or progress
            tasks_dir / "done",
            tasks_dir / "blocked",
        ]

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


def _candidate_task_paths(tasks_dir: Path, task_id: str) -> list[Path]:
    """All on-disk locations a task with ``task_id`` could occupy.

    Single source of truth for both ``get_task`` (which parses each) and the
    explicit-ID clobber guard in ``add_task`` (which only needs existence).
    Keeping the location set in one place stops the two from drifting — the
    clobber guard must check exactly where ``get_task`` would later find the
    task, or a duplicate could be created in a location the guard didn't probe
    (CLAWP-051 Finding 2 / Codex review).
    """
    # Extract parent ID from subtask ID (e.g., CLAWP-TEST-001 -> CLAWP-TEST)
    # Subtask IDs have format: PARENT-NNN where NNN is numeric
    parent_id = None
    if "-" in task_id:
        parts = task_id.rsplit("-", 1)
        if len(parts) == 2 and parts[1].isdigit():
            parent_id = parts[0]

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


def get_next_task(config: PortfolioConfig, project_id: str) -> Task | None:
    """Get the next task to work on (highest priority open task with satisfied dependencies)."""
    tasks = list_tasks(config, project_id)

    # Get IDs of completed tasks
    done_ids = {t.id for t in tasks if t.state == TaskState.DONE}

    # Find open tasks with satisfied dependencies
    for task in tasks:
        if task.state not in (TaskState.OPEN, TaskState.PROGRESS):
            continue

        # Check if all dependencies are satisfied
        if task.depends:
            if not all(dep in done_ids for dep in task.depends):
                continue

        return task

    return None


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
    text = file_path.read_text(encoding="utf-8")
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm: dict = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                fm = {}
            body = parts[2]
        else:
            fm = {}
            body = text
    else:
        fm = {}
        body = text

    fm["rationale"] = rationale
    if supersedes:
        fm["supersedes"] = supersedes

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

    # Find the current task file
    task = get_task(config, project_id, task_id)
    if not task or not task.file_path:
        return None

    current_path = task.file_path
    is_directory_task = current_path.name == "_task.md"

    # CLAWP-053 — rationale is required for REJECTED; validate BEFORE acquiring
    # any lock and BEFORE any filesystem mutation so we fail fast without side
    # effects.  This pure-validation check has no FS mutation, so it is safe
    # (and cheaper) to run outside the critical section.
    if new_state == TaskState.REJECTED:
        if not rationale or not rationale.strip():
            raise ValueError(
                "A non-empty rationale is required when rejecting a task. "
                "Pass rationale='<reason>' to change_task_state()."
            )

    # CLAWP-051 — hold the per-project lock around the ENTIRE
    # read→validate→mutate→reload transaction, not just the final move.
    # Findings 1+4+5 (Codex review): pulling all five steps inside a single
    # critical section prevents three distinct races:
    #   (1) REJECTED frontmatter rewrite before lock (Finding 1)
    #   (4) parent rollup check evaluated before lock (Finding 4)
    #   (5) Task.from_file reload after lock release (Finding 5)
    #
    # REENTRANCY (CLAWP-066): file_lock is now reentrant per-thread, so a callee
    # that re-acquires the same lock path (e.g. split_task) nests safely instead
    # of self-deadlocking. The historical "keep this block flat" rule is no
    # longer a correctness requirement — it remains good hygiene for readability,
    # but nesting the same-path lock is safe.
    _lock_path = tasks_dir / ".clawpm-tasks.lock"

    if is_directory_task:
        # For directory-based tasks, move the entire directory
        task_dir = current_path.parent

        # Resolve the target directory before the lock (pure computation, no FS
        # mutation; mkdir with exist_ok is idempotent so it is safe here too).
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

        with file_lock(_lock_path):
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
                return retry_transient(Task.from_file, _task_md)

            # (e) Move (retry transient Windows sharing/access faults — CLAWP-051)
            retry_transient(shutil.move, str(task_dir), str(new_dir))

            # (f) Reload and return INSIDE the lock (Finding 5). Retry the read
            #     too: a scanner can hit the freshly-moved file transiently even
            #     though the move under the lock already committed (CLAWP-051).
            return retry_transient(Task.from_file, new_dir / "_task.md")

    # Regular file-based task.  Same five-step critical section as above.
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

    with file_lock(_lock_path):
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

        # (f) Reload and return INSIDE the lock (Finding 5). Retry the read too:
        #     a scanner can hit the freshly-moved file transiently even though
        #     the move under the lock already committed (CLAWP-051).
        return retry_transient(Task.from_file, new_path)


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
        all_deps_done = True
        for dep_id in task.depends:
            dep = by_id.get(dep_id)
            if dep is None or dep.state != TaskState.DONE:
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
        scan_dirs = [tasks_dir, tasks_dir / "done", tasks_dir / "blocked"]
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
    for scan_dir in (tasks_dir, tasks_dir / "done", tasks_dir / "blocked"):
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

            for scan_dir in [tasks_dir, tasks_dir / "done", tasks_dir / "blocked"]:
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

        # Build frontmatter
        frontmatter = {
            "id": task_id,
            "priority": priority,
            "created": date.today().isoformat(),
            "baseline_ref": _baseline_ref,
        }

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

        text = task.file_path.read_text(encoding="utf-8")

        # Parse frontmatter and content
        frontmatter: dict = {}
        content = text

        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) < 3:
                # Starts with --- but has no closing fence. Falling through with
                # frontmatter={}, content=text would rebuild a double-frontmatter,
                # metadata-wiped file (same hazard as the unparseable case below).
                # Refuse rather than corrupt (Codex review).
                raise ValueError(
                    f"Task {task_id} has an unterminated frontmatter fence; "
                    "refusing to edit (would corrupt the file)."
                )
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
                content = parts[2]
            except yaml.YAMLError as exc:
                # Do NOT swallow: leaving content=text (the original,
                # frontmatter-bearing bytes) and an empty frontmatter would
                # rebuild a double-frontmatter, field-wiped file. Refuse to
                # edit a task whose frontmatter we can't parse (Grok review).
                raise ValueError(
                    f"Task {task_id} frontmatter is unparseable; refusing "
                    f"to edit (would corrupt the file): {exc}"
                ) from exc

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

        return retry_transient(Task.from_file, new_path)


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
    """
    if parent_path.name != "_task.md" or not parent_path.exists():
        return
    text = parent_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        return
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return
    children = fm.get("children")
    if not isinstance(children, list):
        children = []
    if child_id in children:
        return  # idempotent
    children.append(child_id)
    fm["children"] = children
    body = parts[2].lstrip("\n")
    new_text = (
        "---\n"
        + yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
        + "\n---\n"
        + body
    )
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
        if parent.file_path and parent.file_path.name != "_task.md":
            parent = split_task(config, project_id, parent_id)
            if not parent:
                return None
        parent_dir = parent.file_path.parent if parent.file_path else None
        if not parent_dir:
            return None

        # Generate subtask ID. Codex round-2 P2 fix: union three sources so a
        # migrated/deleted earlier child can't have its id silently reused:
        #   (1) files still in the parent directory (open / progress)
        #   (2) migrated children in tasks/done/ and tasks/blocked/
        #   (3) the parent's persisted frontmatter children list (covers
        #       files that were deleted outright after creation)
        # Without (2)+(3), running `tasks decompose` again on a parent whose
        # earlier children have all moved to done/ would re-issue `P-001`,
        # colliding with the migrated record.
        existing_nums: set[int] = set()

        def _record_num_from_id(tid: str) -> None:
            try:
                num_str = tid.split("-")[-1].replace(".progress", "")
                existing_nums.add(int(num_str))
            except (IndexError, ValueError):
                pass

        for f in parent_dir.glob(f"{parent_id}-*.md"):
            _record_num_from_id(f.stem)
        for state_dir in (tasks_dir / "done", tasks_dir / "blocked"):
            if state_dir.exists():
                for f in state_dir.glob(f"{parent_id}-*.md"):
                    _record_num_from_id(f.stem)
        for cid in (parent.children or []):
            if cid.startswith(parent_id + "-"):
                _record_num_from_id(cid)

        next_num = (max(existing_nums) if existing_nums else 0) + 1
        subtask_id = f"{parent_id}-{next_num:03d}"

        # Build frontmatter
        frontmatter: dict = {
            "id": subtask_id,
            "priority": priority,
            "parent": parent_id,
            "created": date.today().isoformat(),
        }

        if complexity:
            frontmatter["complexity"] = complexity.value

        if agent_profile:
            frontmatter["agent_profile"] = agent_profile

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
        tmp_path = file_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(content, encoding="utf-8")
            # Retry transient Windows sharing/access faults on the rename (CLAWP-051)
            retry_transient(tmp_path.replace, file_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # CLAWP-037 round-1 fix: persist the child on the parent so the rollup
        # gate keeps it in view after the child migrates to done/ or blocked/.
        if parent.file_path is not None:
            _append_child_to_parent_frontmatter(parent.file_path, subtask_id)

        # Reload under the lock; retry_transient covers a scanner touching the
        # freshly-written child file even though the write committed (CLAWP-051).
        return retry_transient(Task.from_file, file_path)

