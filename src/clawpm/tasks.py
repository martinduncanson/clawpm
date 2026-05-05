"""Task operations for ClawPM."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

import yaml

from .models import Task, TaskState, TaskComplexity, PortfolioConfig
from .discovery import get_project_dir


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
) -> Task | None:
    """Add a new task to a project."""
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        # Create tasks directory if project exists
        from .discovery import get_project_dir
        project_dir = get_project_dir(config, project_id)
        if not project_dir:
            return None
        tasks_dir = project_dir / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)

    # Generate task ID if not provided
    if not task_id:
        # Get project prefix from ID (uppercase)
        prefix = project_id.upper()[:5]

        # Find highest existing task number
        existing_nums = []
        for f in tasks_dir.glob(f"{prefix}-*.md"):
            try:
                num = int(f.stem.split("-")[1].replace(".progress", ""))
                existing_nums.append(num)
            except (IndexError, ValueError):
                pass

        # Also check subdirectories
        for subdir in ["done", "blocked"]:
            sub = tasks_dir / subdir
            if sub.exists():
                for f in sub.glob(f"{prefix}-*.md"):
                    try:
                        num = int(f.stem.split("-")[1])
                        existing_nums.append(num)
                    except (IndexError, ValueError):
                        pass

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

    # Build content
    content = f"""---
{yaml.dump(frontmatter, default_flow_style=False).strip()}
---
# {title}

{description}

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

"""

    # Write file
    file_path = tasks_dir / f"{task_id}.md"
    file_path.write_text(content)

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
) -> Task | None:
    """Edit task metadata (frontmatter) and optionally title/body."""
    task = get_task(config, project_id, task_id)
    if not task or not task.file_path:
        return None

    text = task.file_path.read_text()

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

    # Rebuild file
    new_text = f"---\n{yaml.dump(frontmatter, default_flow_style=False).strip()}\n---\n{content}"
    task.file_path.write_text(new_text)

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
{yaml.dump(frontmatter, default_flow_style=False).strip()}
---
# {title}

{description}

## Notes

"""
    
    # Write file
    file_path = parent_dir / f"{subtask_id}.md"
    file_path.write_text(content)
    
    return Task.from_file(file_path)
