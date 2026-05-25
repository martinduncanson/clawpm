"""Task operations for ClawPM."""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

import yaml

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
    """Scan a directory for task files (both .md files and task directories)."""
    if not location.exists():
        return

    for item in location.iterdir():
        if item.is_file() and item.suffix == ".md":
            # Regular task file
            try:
                task = Task.from_file(item)
                if state_filter is None or task.state == state_filter:
                    tasks.append(task)
            except Exception:
                continue
        elif item.is_dir() and not item.name.startswith(".") and item.name not in ("done", "blocked"):
            # Task directory - check for _task.md (parent) and subtasks
            parent_file = item / "_task.md"
            if parent_file.exists():
                try:
                    parent_task = Task.from_file(parent_file)
                    if state_filter is None or parent_task.state == state_filter:
                        tasks.append(parent_task)
                except Exception:
                    continue

            # Scan for subtasks in the directory
            for subtask_file in item.glob("*.md"):
                if subtask_file.name == "_task.md":
                    continue
                try:
                    subtask = Task.from_file(subtask_file)
                    if state_filter is None or subtask.state == state_filter:
                        tasks.append(subtask)
                except Exception:
                    continue


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

    # Collect tasks from all locations
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


def get_task(config: PortfolioConfig, project_id: str, task_id: str) -> Task | None:
    """Get a specific task by ID."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None

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
        # Task directories (parent tasks)
        tasks_dir / task_id / "_task.md",
        tasks_dir / "done" / task_id / "_task.md",
        tasks_dir / "blocked" / task_id / "_task.md",
    ]

    # Add subtask paths if this looks like a subtask ID
    if parent_id:
        possible_paths.extend([
            tasks_dir / parent_id / f"{task_id}.md",
            tasks_dir / parent_id / f"{task_id}.progress.md",
            tasks_dir / "done" / parent_id / f"{task_id}.md",
            tasks_dir / "blocked" / parent_id / f"{task_id}.md",
        ])

    for path in possible_paths:
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


def change_task_state(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    new_state: TaskState,
    note: str | None = None,
    force: bool = False,
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

    # Check for incomplete subtasks when marking parent as done
    if new_state == TaskState.DONE and task.children and not force:
        incomplete = []
        for child_id in task.children:
            child = get_task(config, project_id, child_id)
            if child and child.state != TaskState.DONE:
                incomplete.append(child_id)
        if incomplete:
            # Return None to signal failure - caller should check and report
            return None

    if is_directory_task:
        # For directory-based tasks, move the entire directory
        task_dir = current_path.parent
        
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
        else:
            return None
        
        # Don't move if already in correct location
        if task_dir.resolve() == new_dir.resolve():
            return task
        
        # Move the directory
        shutil.move(str(task_dir), str(new_dir))
        
        # Reload and return
        return Task.from_file(new_dir / "_task.md")
    
    # Regular file-based task
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
    else:
        return None

    # Don't move if already in correct location
    if current_path.resolve() == new_path.resolve():
        return task

    # Move the file
    shutil.move(str(current_path), str(new_path))

    # Reload and return
    return Task.from_file(new_path)


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

    # Generate task ID if not provided
    if not task_id:
        # Get project prefix from ID (uppercase)
        prefix = project_id.upper()[:5]

        # Find highest existing task number.
        # We must check BOTH .md files and parent-task directories (e.g. OPENW-004/)
        # because split tasks convert the file to a directory.  The *.md glob misses
        # directories, so without this check add_task would re-issue the same number.
        # Subtask files (OPENW-004-001.md) live *inside* parent dirs; they don't
        # appear at the scan-dir level, so they won't pollute top-level numbering.
        _dir_pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")

        existing_nums = []

        for scan_dir in [tasks_dir, tasks_dir / "done", tasks_dir / "blocked"]:
            if not scan_dir.exists():
                continue
            # .md files at this level
            for f in scan_dir.glob(f"{prefix}-*.md"):
                try:
                    num = int(f.stem.split("-")[1].replace(".progress", ""))
                    existing_nums.append(num)
                except (IndexError, ValueError):
                    pass
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
    }

    if complexity:
        frontmatter["complexity"] = complexity.value

    if depends:
        frontmatter["depends"] = depends

    if scope:
        frontmatter["scope"] = scope

    if parallel_group is not None:
        frontmatter["parallel_group"] = parallel_group

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
    tmp_path = file_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(content, encoding="utf-8")
        tmp_path.replace(file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return Task.from_file(file_path)


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
) -> Task | None:
    """Edit task metadata (frontmatter) and optionally title/body."""
    task = get_task(config, project_id, task_id)
    if not task or not task.file_path:
        return None

    text = task.file_path.read_text(encoding="utf-8")

    # Parse frontmatter and content
    frontmatter: dict = {}
    content = text

    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                frontmatter = yaml.safe_load(parts[1]) or {}
                content = parts[2]
            except yaml.YAMLError:
                pass

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
        tmp_path.replace(task.file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return Task.from_file(task.file_path)


def split_task(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
) -> Task | None:
    """Convert a regular task file into a parent directory structure.
    
    Converts TASK-ID.md → TASK-ID/_task.md
    Works from any state directory (tasks/, done/, blocked/).
    """
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
    
    # Move file to _task.md inside directory
    new_path = task_dir / "_task.md"
    shutil.move(str(current_path), str(new_path))
    
    return Task.from_file(new_path)


def add_subtask(
    config: PortfolioConfig,
    project_id: str,
    parent_id: str,
    title: str,
    priority: int = 5,
    complexity: TaskComplexity | None = None,
    description: str = "",
) -> Task | None:
    """Add a subtask to a parent task.
    
    Auto-splits parent if not already a directory.
    Generates sequential subtask ID (PARENT-001, PARENT-002, etc.).
    """
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return None
    
    # Get or create parent as directory
    parent = get_task(config, project_id, parent_id)
    if not parent:
        return None
    
    # Split parent if not already a directory
    if parent.file_path and parent.file_path.name != "_task.md":
        parent = split_task(config, project_id, parent_id)
        if not parent:
            return None
    
    # Find parent directory
    parent_dir = parent.file_path.parent if parent.file_path else None
    if not parent_dir:
        return None
    
    # Generate subtask ID
    existing_nums = []
    for f in parent_dir.glob(f"{parent_id}-*.md"):
        try:
            num_str = f.stem.split("-")[-1].replace(".progress", "")
            num = int(num_str)
            existing_nums.append(num)
        except (IndexError, ValueError):
            pass
    
    next_num = max(existing_nums, default=0) + 1
    subtask_id = f"{parent_id}-{next_num:03d}"
    
    # Build frontmatter
    frontmatter = {
        "id": subtask_id,
        "priority": priority,
        "parent": parent_id,
        "created": date.today().isoformat(),
    }
    
    if complexity:
        frontmatter["complexity"] = complexity.value
    
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
        tmp_path.replace(file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise

    return Task.from_file(file_path)
