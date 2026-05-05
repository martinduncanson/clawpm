"""ClawPM CLI - Filesystem-first multi-project manager."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from . import __version__
from .models import (
    ProjectStatus,
    TaskState,
    TaskComplexity,
    WorkLogAction,
    ResearchType,
    ResearchStatus,
)
from .output import (
    OutputFormat,
    output_json,
    output_error,
    output_success,
    output_projects_list,
    output_tasks_list,
    output_task_detail,
    output_worklog_entries,
    output_research_list,
    output_context,
)
from .discovery import (
    get_portfolio_path,
    load_portfolio_config,
    discover_projects,
    discover_untracked_repos,
    get_project,
    validate_portfolio,
    init_project_from_repo,
    is_git_repo,
    path_for_config,
)
from .tasks import (
    list_tasks,
    get_task,
    get_next_task,
    change_task_state,
    add_task,
    edit_task,
    split_task,
    add_subtask,
)
from .worklog import (
    add_entry,
    tail_entries,
    get_last_entry,
    get_logged_commit_hashes,
)
from .research import (
    list_research,
    get_research,
    add_research,
    link_research_session,
)
from .context import (
    resolve_project,
    expand_task_id,
    get_context_project,
    set_context_project,
    detect_project_from_cwd,
    detect_untracked_repo_from_cwd,
    auto_init_if_untracked,
)


# Global format option
pass_format = click.make_pass_decorator(OutputFormat, ensure=True)


@click.group()
@click.option(
    "--format", "-f",
    type=click.Choice(["json", "text"]),
    default="json",
    help="Output format (default: json)",
)
@click.option(
    "--project", "-p",
    "global_project",
    help="Project ID (overrides auto-detection)",
)
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, format: str, global_project: str | None) -> None:
    """ClawPM - Filesystem-first multi-project manager."""
    ctx.ensure_object(dict)
    ctx.obj["format"] = OutputFormat(format)
    ctx.obj["global_project"] = global_project


def get_format(ctx: click.Context) -> OutputFormat:
    """Get the output format from context."""
    return ctx.obj.get("format", OutputFormat.JSON)


def require_portfolio(ctx: click.Context):
    """Load portfolio config or exit with error."""
    config = load_portfolio_config()
    if not config:
        fmt = get_format(ctx)
        output_error(
            "portfolio_not_found",
            "No portfolio found at ~/clawpm (or CLAWPM_PORTFOLIO). Run setup or create portfolio.toml.",
            fmt=fmt,
        )
        sys.exit(1)
    return config


def require_project(ctx: click.Context, project_id: str | None, required: bool = True, auto_init: bool = True) -> tuple[str | None, str]:
    """Resolve project from explicit arg, global flag, cwd, or context.

    Returns (project_id, source). Exits with error if required and not found.
    Priority: explicit arg > global --project flag > cwd > auto-init > context

    If auto_init=True and cwd is in an untracked git repo under project_roots,
    automatically initializes a .project/ structure.
    """
    # Check for global --project flag if no explicit arg
    if not project_id:
        project_id = ctx.obj.get("global_project")
        if project_id:
            return (project_id, "global")

    resolved_id, source = resolve_project(project_id)

    # If no project found and auto_init enabled, check for untracked git repo
    if not resolved_id and auto_init:
        untracked_repo = detect_untracked_repo_from_cwd()
        if untracked_repo:
            # Auto-initialize the project
            project = auto_init_if_untracked()
            if project:
                click.echo(f"Auto-initialized project '{project.id}' from git repo", err=True)
                return (project.id, "auto-init")

    # Show which project was auto-detected (text mode only, to stderr)
    if resolved_id and source in ("cwd", "context"):
        fmt = get_format(ctx)
        if fmt == OutputFormat.TEXT:
            click.echo(f"Using project: {resolved_id} (from {source})", err=True)

    if required and not resolved_id:
        fmt = get_format(ctx)
        output_error(
            "no_project",
            "No project specified. Use --project, cd into a project, or run 'clawpm use <project>'.",
            fmt=fmt,
        )
        sys.exit(1)

    return resolved_id, source


# ============================================================================
# Use command (project context)
# ============================================================================


@main.command("use")
@click.argument("project_id", required=False)
@click.option("--clear", is_flag=True, help="Clear the current context")
@click.pass_context
def use_project(ctx: click.Context, project_id: str | None, clear: bool) -> None:
    """Set or show the current project context.
    
    When no project is specified, shows the current context.
    Use --clear to remove the context.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    if clear:
        set_context_project(None)
        output_success("Context cleared", fmt=fmt)
        return
    
    if project_id:
        # Verify project exists
        proj = get_project(config, project_id)
        if not proj:
            output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
            sys.exit(1)
        
        set_context_project(project_id)
        output_success(f"Now using project: {proj.name} ({proj.id})", fmt=fmt)
    else:
        # Show current context
        current = get_context_project()
        cwd_project = detect_project_from_cwd()
        
        result = {
            "context_project": current,
            "cwd_project": cwd_project.id if cwd_project else None,
            "effective": cwd_project.id if cwd_project else current,
        }
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            if cwd_project:
                click.echo(f"Current directory: {cwd_project.name} ({cwd_project.id})")
            elif current:
                click.echo(f"Context: {current}")
            else:
                click.echo("No project context set. Use 'clawpm use <project>' or cd into a project.")


# ============================================================================
# Projects commands
# ============================================================================


@main.group()
def projects() -> None:
    """Manage projects."""
    pass


@projects.command("list")
@click.option(
    "--filter", "-f", "status_filter",
    type=click.Choice(["active", "paused", "archived"]),
    default=None,
    help="Filter by status",
)
@click.option("--all", "-a", "show_all", is_flag=True, help="Include untracked git repos")
@click.pass_context
def projects_list(ctx: click.Context, status_filter: str | None, show_all: bool) -> None:
    """List all projects (use --all to include untracked git repos)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    status = ProjectStatus(status_filter) if status_filter else None
    projects_found = discover_projects(config, status_filter=status)

    if show_all or fmt == OutputFormat.JSON:
        untracked = discover_untracked_repos(config)
    else:
        untracked = []
    
    if fmt == OutputFormat.JSON:
        result = {
            "projects": [p.to_dict() for p in projects_found],
            "untracked": [r.to_dict() for r in untracked],
        }
        output_json(result)
    else:
        # Collect task counts for text output
        task_counts = {}
        for proj in projects_found:
            counts = {}
            for state_name, state_val in [("open", TaskState.OPEN), ("progress", TaskState.PROGRESS), ("blocked", TaskState.BLOCKED)]:
                count = len(list_tasks(config, proj.id, state_filter=state_val))
                if count:
                    counts[state_name] = count
            task_counts[proj.id] = counts

        output_projects_list(projects_found, fmt=fmt, task_counts=task_counts)

        if untracked:
            click.echo("\nUntracked git repos (use 'clawpm project init' to add):")
            for repo in untracked:
                remote_hint = f" ({repo.remote.split('/')[-1].replace('.git', '')})" if repo.remote else ""
                click.echo(f"  ○ {repo.name}{remote_hint}")


@projects.command("next")
@click.pass_context
def projects_next(ctx: click.Context) -> None:
    """Get the next task across all active projects."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Get all active projects
    active_projects = discover_projects(config, status_filter=ProjectStatus.ACTIVE)

    # Find next task across all projects
    best_task = None
    best_project = None

    for project in active_projects:
        task = get_next_task(config, project.id)
        if task:
            if best_task is None or (project.priority, task.priority) < (best_project.priority, best_task.priority):
                best_task = task
                best_project = project

    if best_task and best_project:
        result = {
            "project": {
                "id": best_project.id,
                "name": best_project.name,
                "priority": best_project.priority,
            },
            "task": best_task.to_dict(),
        }
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            output_task_detail(best_task, fmt=fmt)
            click.echo(f"\nProject: {best_project.name} ({best_project.id})")
    else:
        if fmt == OutputFormat.JSON:
            output_json({"project": None, "task": None, "message": "No tasks available"})
        else:
            click.echo("No tasks available across active projects.")


# ============================================================================
# Project commands (singular)
# ============================================================================


@main.group()
def project() -> None:
    """Manage a single project."""
    pass


@project.command("context")
@click.argument("project_id", required=False)
@click.pass_context
def project_context(ctx: click.Context, project_id: str | None) -> None:
    """Get full context for a project (alias for top-level 'context')."""
    ctx.invoke(agent_context, project_id=project_id)


@project.command("init")
@click.option("--in-repo", "-r", "repo_path", type=click.Path(exists=True), default=".", help="Repository path")
@click.option("--id", "project_id", help="Project ID (defaults to directory name)")
@click.option("--name", "project_name", help="Project name")
@click.pass_context
def project_init(ctx: click.Context, repo_path: str, project_id: str | None, project_name: str | None) -> None:
    """Initialize a new project in a repository."""
    fmt = get_format(ctx)

    repo = Path(repo_path).resolve()
    project_dir = repo / ".project"

    if project_dir.exists():
        output_error("project_exists", f"Project already exists at {project_dir} (repo: {repo})", fmt=fmt)
        sys.exit(1)

    # Generate defaults
    if not project_id:
        project_id = repo.name.lower().replace(" ", "-").replace("_", "-")

    if not project_name:
        project_name = repo.name

    # Create structure
    project_dir.mkdir(parents=True)
    (project_dir / "tasks").mkdir()
    (project_dir / "tasks" / "done").mkdir()
    (project_dir / "tasks" / "blocked").mkdir()
    (project_dir / "research").mkdir()
    (project_dir / "notes").mkdir()

    # Create settings.toml
    repo_path_str = path_for_config(repo)
    settings_content = f'''id = "{project_id}"
name = "{project_name}"
status = "active"
priority = 5
repo_path = "{repo_path_str}"
labels = []
'''
    (project_dir / "settings.toml").write_text(settings_content)

    # Create SPEC.md template
    spec_content = f"""# {project_name}

## Overview

(Describe the project here)

## Goals

- Goal 1
- Goal 2

## Non-Goals

- Non-goal 1

## Technical Notes

...
"""
    (project_dir / "SPEC.md").write_text(spec_content)

    # Create learnings.md
    (project_dir / "learnings.md").write_text(f"# {project_name} Learnings\n\n")

    output_success(f"Project initialized at {project_dir}", fmt=fmt)


@project.command("doctor")
@click.option("--project", "-p", "project_id", help="Check specific project")
@click.pass_context
def project_doctor(ctx: click.Context, project_id: str | None) -> None:
    """Check for issues with projects and portfolio."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    issues: list[dict] = []

    # Validate portfolio
    portfolio_issues = validate_portfolio(config)
    for issue in portfolio_issues:
        issues.append({"level": "error", "scope": "portfolio", "message": issue})

    # Check projects
    projects_to_check = []
    if project_id:
        proj = get_project(config, project_id)
        if proj:
            projects_to_check = [proj]
        else:
            issues.append({
                "level": "error",
                "scope": "project",
                "project": project_id,
                "message": f"Project not found: {project_id}",
            })
    else:
        projects_to_check = discover_projects(config)

    for proj in projects_to_check:
        if not proj.project_dir:
            continue

        project_path = proj.project_dir / ".project"

        # Check for required files
        if not (project_path / "settings.toml").exists():
            issues.append({
                "level": "error",
                "scope": "project",
                "project": proj.id,
                "message": "Missing settings.toml",
            })

        # Check tasks directory
        tasks_dir = project_path / "tasks"
        if not tasks_dir.exists():
            issues.append({
                "level": "warning",
                "scope": "project",
                "project": proj.id,
                "message": "Missing tasks directory",
            })

        # Check for broken repo_path
        if proj.repo_path and not proj.repo_path.exists():
            issues.append({
                "level": "warning",
                "scope": "project",
                "project": proj.id,
                "message": f"repo_path does not exist: {proj.repo_path}",
            })

    if fmt == OutputFormat.JSON:
        output_json({"issues": issues, "count": len(issues)})
    else:
        if not issues:
            click.echo("✓ No issues found")
        else:
            for issue in issues:
                level_color = {"error": "red", "warning": "yellow"}.get(issue["level"], "white")
                scope = issue.get("project", issue["scope"])
                click.echo(f"[{issue['level'].upper()}] [{scope}] {issue['message']}")


# ============================================================================
# Tasks commands
# ============================================================================


@main.group(invoke_without_command=True)
@click.pass_context
def tasks(ctx: click.Context) -> None:
    """Manage tasks (bare 'tasks' = list open+progress+blocked)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(tasks_list)


@tasks.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option(
    "--state", "-s",
    type=click.Choice(["open", "progress", "done", "blocked", "all"]),
    default=None,
    help="Filter by state (default: all except done)",
)
@click.option("--flat", is_flag=True, help="Show flat list without hierarchy")
@click.pass_context
def tasks_list(ctx: click.Context, project_id: str | None, state: str | None, flat: bool) -> None:
    """List tasks for a project (default: open+progress+blocked, use -s all for everything)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    if state == "all":
        found_tasks = list_tasks(config, project_id, state_filter=None)
    elif state is None:
        # Default: show everything except done
        found_tasks = []
        for s in (TaskState.OPEN, TaskState.PROGRESS, TaskState.BLOCKED):
            found_tasks.extend(list_tasks(config, project_id, state_filter=s))
        found_tasks.sort(key=lambda t: (t.priority, t.id))
    else:
        found_tasks = list_tasks(config, project_id, state_filter=TaskState(state))

    output_tasks_list(found_tasks, fmt=fmt, flat=flat)


@tasks.command("show")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def tasks_show(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Show details for a specific task."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = get_task(config, project_id, task_id)
    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_task_detail(task, fmt=fmt)


@tasks.command("edit")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--title", "-t", help="New title")
@click.option("--priority", type=int, help="New priority (1-10)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), help="New complexity")
@click.option("--body", "-b", help="New body content (replaces description before ## sections)")
@click.option("--scope", "-s", "scope", multiple=True, help="File glob patterns claimed by this task (can specify multiple)")
@click.pass_context
def tasks_edit(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    title: str | None,
    priority: int | None,
    complexity: str | None,
    body: str | None,
    scope: tuple[str, ...],
) -> None:
    """Edit task metadata (title, priority, complexity, body, scope)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    if not any([title, priority is not None, complexity, body, scope]):
        output_error("no_changes", "Specify at least one field to edit (--title, --priority, --complexity, --body, --scope)", fmt=fmt)
        sys.exit(1)

    cmplx = TaskComplexity(complexity) if complexity else None
    scope_list = list(scope) if scope else None

    task = edit_task(
        config,
        project_id,
        task_id,
        title=title,
        priority=priority,
        complexity=cmplx,
        scope=scope_list,
        body=body,
    )

    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task_id} updated", data=task.to_dict(), fmt=fmt)


@tasks.command("state")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.argument("new_state", type=click.Choice(["open", "progress", "done", "blocked"]))
@click.option("--note", "-n", help="Note about the state change")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.pass_context
def tasks_state(ctx: click.Context, project_id: str | None, task_id: str, new_state: str, note: str | None, force: bool) -> None:
    """Change task state."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    state = TaskState(new_state)
    
    # Check for incomplete subtasks before attempting state change
    if state == TaskState.DONE and not force:
        task = get_task(config, project_id, task_id)
        if task and task.children:
            incomplete = []
            for child_id in task.children:
                child = get_task(config, project_id, child_id)
                if child and child.state != TaskState.DONE:
                    incomplete.append(f"{child_id} [{child.state.value}]")
            if incomplete:
                output_error(
                    "subtasks_incomplete",
                    f"Cannot complete {task_id} - subtasks incomplete:\n  " + "\n  ".join(incomplete) + "\nUse --force to complete anyway.",
                    fmt=fmt,
                )
                sys.exit(1)
    
    task = change_task_state(config, project_id, task_id, state, note=note, force=force)

    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # Auto-log state change
    action_map = {
        TaskState.OPEN: WorkLogAction.NOTE,
        TaskState.PROGRESS: WorkLogAction.START,
        TaskState.DONE: WorkLogAction.DONE,
        TaskState.BLOCKED: WorkLogAction.BLOCKED,
    }
    if state in action_map:
        # Auto-detect git files changed
        files_changed = None
        project = get_project(config, project_id)
        if project and project.repo_path and project.repo_path.exists():
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=project.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files_changed = [f for f in result.stdout.strip().split('\n') if f]
            except Exception:
                pass
        
        summary = note if note else f"Task marked {new_state}"
        add_entry(
            config,
            project=project_id,
            action=action_map[state],
            task=task_id,
            summary=summary,
            files_changed=files_changed,
            auto=True,
        )

    output_success(f"Task {task_id} moved to {new_state}", data=task.to_dict(), fmt=fmt)


@tasks.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--title", "-t", required=True, help="Task title")
@click.option("--id", "task_id", help="Task ID (auto-generated if not provided)")
@click.option("--priority", type=int, default=5, help="Priority (1-10, lower is higher)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), help="Complexity")
@click.option("--depends", "-d", multiple=True, help="Dependencies (can specify multiple)")
@click.option("--scope", multiple=True, help="File glob patterns claimed by this task (can specify multiple)")
@click.option("--parent", "parent_id", help="Parent task ID (creates subtask)")
@click.option("--description", help="Task description (deprecated, use --body)")
@click.option("--body", "-b", help="Task body content")
@click.option("--body-file", type=click.Path(exists=True), help="Read body from file")
@click.option("--stdin", "read_stdin", is_flag=True, help="Read body from stdin")
@click.pass_context
def tasks_add(
    ctx: click.Context,
    project_id: str | None,
    title: str,
    task_id: str | None,
    priority: int,
    complexity: str | None,
    depends: tuple[str, ...],
    scope: tuple[str, ...],
    parent_id: str | None,
    description: str | None,
    body: str | None,
    body_file: str | None,
    read_stdin: bool,
) -> None:
    """Add a new task (or subtask with --parent)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    # Determine body content
    task_body = ""
    if body:
        task_body = body
    elif body_file:
        task_body = Path(body_file).read_text()
    elif read_stdin:
        task_body = sys.stdin.read()
    elif description:
        task_body = description

    cmplx = TaskComplexity(complexity) if complexity else None
    scope_list = list(scope) if scope else None

    # Create subtask if parent specified
    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)
        task = add_subtask(
            config,
            project_id,
            parent_id,
            title,
            priority=priority,
            complexity=cmplx,
            description=task_body,
        )
    else:
        deps = list(depends) if depends else None
        task = add_task(
            config,
            project_id,
            title,
            task_id=task_id,
            priority=priority,
            complexity=cmplx,
            depends=deps,
            scope=scope_list,
            description=task_body,
        )

    if not task:
        # Give a more useful hint: check if the project exists locally but has
        # a malformed settings.toml (e.g. Windows backslashes in repo_path).
        from pathlib import Path as _Path
        _current = _Path.cwd().resolve()
        _settings_exists = False
        while _current != _current.parent:
            if (_current / ".project" / "settings.toml").exists():
                _settings_exists = True
                break
            _current = _current.parent

        if _settings_exists:
            output_error(
                "add_failed",
                f"Failed to add task to project '{project_id}'. "
                f"A .project/settings.toml exists locally but could not be loaded from the "
                f"portfolio registry - the file may contain Windows backslashes in repo_path. "
                f"Fix it by using forward slashes (e.g. F:/Git/...) then retry.",
                fmt=fmt,
            )
        else:
            output_error("add_failed", f"Failed to add task to project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task.id} created", data=task.to_dict(), fmt=fmt)


@tasks.command("split")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def tasks_split(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Convert a task to a parent directory (for adding subtasks)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = split_task(config, project_id, task_id)

    if not task:
        output_error("split_failed", f"Failed to split task '{task_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task_id} converted to directory", data=task.to_dict(), fmt=fmt)


# ============================================================================
# Top-level task shortcuts
# ============================================================================


@main.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("title")
@click.option("--priority", type=int, default=5, help="Priority (1-10)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), default="m", help="Complexity")
@click.option("--parent", "parent_id", help="Parent task ID (creates subtask)")
@click.option("--body", "-b", help="Task description/body")
@click.pass_context
def quick_add(ctx: click.Context, project_id: str | None, title: str, priority: int, complexity: str, parent_id: str | None, body: str | None) -> None:
    """Quick add a task (alias for 'tasks add')."""
    ctx.invoke(tasks_add, project_id=project_id, title=title, priority=priority, complexity=complexity, parent_id=parent_id, body=body)


@main.command("done")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--note", "-n", help="Completion note")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.pass_context
def quick_done(ctx: click.Context, project_id: str | None, task_id: str, note: str | None, force: bool) -> None:
    """Mark a task as done (alias for 'tasks state <id> done')."""
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="done", note=note, force=force)


@main.command("start")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def quick_start(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Start working on a task (alias for 'tasks state <id> progress')."""
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="progress", note=None)


@main.command("block")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--note", "-n", help="Blocker description")
@click.pass_context
def quick_block(ctx: click.Context, project_id: str | None, task_id: str, note: str | None) -> None:
    """Mark a task as blocked (alias for 'tasks state <id> blocked')."""
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="blocked", note=note)


@main.command("next")
@click.option("--project", "-p", "project_id", help="Project ID (if not specified, searches all)")
@click.pass_context
def quick_next(ctx: click.Context, project_id: str | None) -> None:
    """Get the next task to work on."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    if project_id:
        # Get next task for specific project
        task = get_next_task(config, project_id)
        if task:
            output_task_detail(task, fmt=fmt)
        else:
            if fmt == OutputFormat.JSON:
                output_json({"task": None, "message": "No tasks available"})
            else:
                click.echo("No tasks available.")
    else:
        # Delegate to projects next
        ctx.invoke(projects_next)


@main.command("status")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.pass_context
def quick_status(ctx: click.Context, project_id: str | None) -> None:
    """Show current project status (tasks in progress, blockers, next up)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    resolved_id, source = require_project(ctx, project_id, required=False)
    
    if not resolved_id:
        # Show overview of all projects
        projects_found = discover_projects(config, status_filter=ProjectStatus.ACTIVE)
        
        result = {
            "projects": [],
            "total_active": 0,
            "total_blocked": 0,
        }
        
        for proj in projects_found:
            in_progress = list_tasks(config, proj.id, state_filter=TaskState.PROGRESS)
            blocked = list_tasks(config, proj.id, state_filter=TaskState.BLOCKED)
            
            proj_info = {
                "id": proj.id,
                "name": proj.name,
                "in_progress": len(in_progress),
                "blocked": len(blocked),
            }
            result["projects"].append(proj_info)
            result["total_active"] += len(in_progress)
            result["total_blocked"] += len(blocked)
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            click.echo(f"Active: {result['total_active']} tasks in progress, {result['total_blocked']} blocked\n")
            for proj in result["projects"]:
                status_str = []
                if proj["in_progress"]:
                    status_str.append(f"{proj['in_progress']} active")
                if proj["blocked"]:
                    status_str.append(f"{proj['blocked']} blocked")
                click.echo(f"  {proj['name']}: {', '.join(status_str) if status_str else 'idle'}")
    else:
        # Show specific project status
        proj = get_project(config, resolved_id)
        if not proj:
            output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
            sys.exit(1)
        
        in_progress = list_tasks(config, resolved_id, state_filter=TaskState.PROGRESS)
        blocked = list_tasks(config, resolved_id, state_filter=TaskState.BLOCKED)
        open_tasks = list_tasks(config, resolved_id, state_filter=TaskState.OPEN)
        next_task = get_next_task(config, resolved_id)
        
        result = {
            "project": proj.id,
            "name": proj.name,
            "source": source,
            "in_progress": [t.to_dict() for t in in_progress],
            "blocked": [t.to_dict() for t in blocked],
            "open_count": len(open_tasks),
            "next": next_task.to_dict() if next_task else None,
        }
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            click.echo(f"Project: {proj.name} ({source})")
            click.echo(f"Open: {len(open_tasks)} | In Progress: {len(in_progress)} | Blocked: {len(blocked)}")
            
            if in_progress:
                click.echo("\nIn Progress:")
                for t in in_progress:
                    click.echo(f"  → {t.id}: {t.title}")
            
            if blocked:
                click.echo("\nBlocked:")
                for t in blocked:
                    click.echo(f"  ✗ {t.id}: {t.title}")
            
            if next_task and next_task not in in_progress:
                click.echo(f"\nNext up: {next_task.id}: {next_task.title}")


@main.command("context")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--log-limit", "-l", type=int, default=5, help="Number of recent log entries")
@click.pass_context
def agent_context(ctx: click.Context, project_id: str | None, log_limit: int) -> None:
    """Get full agent context (project, tasks, blockers, recent work, git status).
    
    Optimized for LLM agent consumption - everything needed to resume work.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    resolved_id, source = require_project(ctx, project_id, required=False)
    
    if not resolved_id:
        output_error("no_project", "No project specified or detected. Use -p or cd into a project.", fmt=fmt)
        sys.exit(1)
    
    proj = get_project(config, resolved_id)
    if not proj:
        output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
        sys.exit(1)
    
    # Build comprehensive context
    context: dict = {
        "project": {
            "id": proj.id,
            "name": proj.name,
            "status": proj.status.value,
            "priority": proj.priority,
            "labels": proj.labels,
            "repo_path": str(proj.repo_path) if proj.repo_path else None,
        },
        "source": source,
    }
    
    # Read spec if exists
    if proj.project_dir:
        spec_file = proj.project_dir / ".project" / "SPEC.md"
        if spec_file.exists():
            spec_content = spec_file.read_text()
            # Truncate if too long
            if len(spec_content) > 2000:
                context["spec"] = spec_content[:2000] + "\n\n[...truncated...]"
            else:
                context["spec"] = spec_content
    
    # Current task (in progress)
    in_progress = list_tasks(config, resolved_id, state_filter=TaskState.PROGRESS)
    context["in_progress"] = [t.to_dict() for t in in_progress]
    
    # Next task if nothing in progress
    if not in_progress:
        next_task = get_next_task(config, resolved_id)
        if next_task:
            context["next_task"] = next_task.to_dict()
    
    # Blocked tasks
    blocked = list_tasks(config, resolved_id, state_filter=TaskState.BLOCKED)
    context["blockers"] = [t.to_dict() for t in blocked]
    
    # Open task count
    open_tasks = list_tasks(config, resolved_id, state_filter=TaskState.OPEN)
    context["open_count"] = len(open_tasks)
    
    # Recent work log
    recent_entries = tail_entries(config, project=resolved_id, limit=log_limit)
    context["recent_work"] = [e.to_dict() for e in recent_entries]
    
    # Git status if repo_path exists
    if proj.repo_path and proj.repo_path.exists():
        git_status = {}
        try:
            # Current branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                git_status["branch"] = result.stdout.strip()
            
            # Uncommitted changes
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                changes = [line for line in result.stdout.strip().split('\n') if line]
                git_status["uncommitted_count"] = len(changes)
                if changes:
                    git_status["uncommitted"] = changes[:10]  # Limit to 10
                    if len(changes) > 10:
                        git_status["uncommitted"].append(f"... and {len(changes) - 10} more")
            
            # Recent commits
            result = subprocess.run(
                ["git", "log", "--oneline", "-3"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                git_status["recent_commits"] = [line for line in result.stdout.strip().split('\n') if line]
        except Exception:
            pass
        
        if git_status:
            context["git"] = git_status
    
    # Open issues
    if proj.project_dir:
        import json as json_mod
        issues_file = proj.project_dir / ".agent" / "issues.jsonl"
        if issues_file.exists():
            try:
                open_issues = []
                with open(issues_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            issue = json_mod.loads(line)
                            if not issue.get("fixed"):
                                open_issues.append({
                                    "type": issue.get("type"),
                                    "severity": issue.get("severity"),
                                    "summary": (issue.get("actual") or issue.get("context", ""))[:100],
                                })
                if open_issues:
                    context["open_issues"] = open_issues[:5]
            except Exception:
                pass
    
    output_context(context, fmt=fmt)


# ============================================================================
# Log commands
# ============================================================================


@main.group()
def log() -> None:
    """Manage work log."""
    pass


@log.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "-t", "task_id", help="Task ID")
@click.option(
    "--action", "-a",
    type=click.Choice(["start", "progress", "done", "blocked", "commit", "pause", "research", "note"]),
    required=True,
    help="Action type",
)
@click.option("--summary", "-s", required=True, help="Summary of work")
@click.option("--next", "next_steps", help="Next steps")
@click.option("--files", "-f", multiple=True, help="Files changed")
@click.option("--blocker", "-b", help="Blocker description")
@click.option("--agent", default="main", help="Agent ID")
@click.option("--session-key", help="OpenClaw session key")
@click.pass_context
def log_add(
    ctx: click.Context,
    project_id: str | None,
    task_id: str | None,
    action: str,
    summary: str,
    next_steps: str | None,
    files: tuple[str, ...],
    blocker: str | None,
    agent: str,
    session_key: str | None,
) -> None:
    """Add a work log entry."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    
    # Expand task ID if provided
    if task_id:
        task_id = expand_task_id(task_id, project_id)

    # Auto-detect changed files from git if not manually specified
    if not files and project_id:
        project = get_project(config, project_id)
        if project and project.repo_path and project.repo_path.exists():
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=project.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files = tuple(f for f in result.stdout.strip().split('\n') if f)
            except Exception:
                pass  # No git or error - continue without files_changed

    entry = add_entry(
        config,
        project=project_id,
        action=WorkLogAction(action),
        task=task_id,
        summary=summary,
        next_steps=next_steps,
        files_changed=list(files) if files else None,
        blockers=blocker,
        agent=agent,
        session_key=session_key,
    )

    output_success("Entry added", data=entry.to_dict(), fmt=fmt)


@log.command("tail")
@click.option("--project", "-p", "project_id", help="Filter by project (auto-detected from cwd)")
@click.option("--limit", "-n", type=int, default=20, help="Number of entries")
@click.option("--follow", "-f", is_flag=True, help="Follow log output (like tail -f)")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all projects (skip auto-detection)")
@click.pass_context
def log_tail(ctx: click.Context, project_id: str | None, limit: int, follow: bool, show_all: bool) -> None:
    """Show recent work log entries (auto-filters to current project)."""
    import time
    import json as json_module
    from .models import WorkLogEntry
    from .worklog import get_worklog_path

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Auto-detect project from cwd unless --all or explicit --project
    if not project_id and not show_all:
        project_id, source = require_project(ctx, None, required=False, auto_init=False)

    entries = tail_entries(config, project=project_id, limit=limit)
    output_worklog_entries(entries, fmt=fmt)
    
    if not follow:
        return
    
    # Follow mode - watch for new entries
    worklog_path = get_worklog_path(config)
    
    # Track file position
    try:
        pos = worklog_path.stat().st_size if worklog_path.exists() else 0
    except OSError:
        pos = 0
    
    try:
        while True:
            time.sleep(1)  # Poll every second
            
            if not worklog_path.exists():
                continue
            
            try:
                current_size = worklog_path.stat().st_size
            except OSError:
                continue
            
            if current_size > pos:
                # New content - read from last position
                with open(worklog_path) as f:
                    f.seek(pos)
                    new_lines = f.read()
                    pos = f.tell()
                
                for line in new_lines.strip().split('\n'):
                    if not line:
                        continue
                    try:
                        data = json_module.loads(line)
                        entry = WorkLogEntry.from_dict(data)
                        
                        # Apply project filter
                        if project_id and entry.project != project_id:
                            continue
                        
                        output_worklog_entries([entry], fmt=fmt)
                    except (json_module.JSONDecodeError, KeyError, ValueError):
                        continue
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C


@log.command("last")
@click.option("--project", "-p", "project_id", help="Filter by project (auto-detected from cwd)")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show global last (skip auto-detection)")
@click.pass_context
def log_last(ctx: click.Context, project_id: str | None, show_all: bool) -> None:
    """Show the most recent work log entry (auto-filters to current project)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Auto-detect project from cwd unless --all or explicit --project
    if not project_id and not show_all:
        project_id, source = require_project(ctx, None, required=False, auto_init=False)

    entry = get_last_entry(config, project=project_id)

    if entry:
        output_worklog_entries([entry], fmt=fmt)
    else:
        if fmt == OutputFormat.JSON:
            output_json(None)
        else:
            click.echo("No entries found")


@log.command("commit")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--limit", "-n", type=int, default=10, help="Number of recent commits to check")
@click.option("--task", "-t", "task_id", help="Associate commits with a task")
@click.option("--dry-run", is_flag=True, help="Show what would be logged without logging")
@click.pass_context
def log_commit(ctx: click.Context, project_id: str | None, limit: int, task_id: str | None, dry_run: bool) -> None:
    """Log recent git commits to work log (pull-based, deduplicates)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    # Expand task ID if provided
    if task_id:
        task_id = expand_task_id(task_id, project_id)

    proj = get_project(config, project_id)
    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    repo_path = proj.repo_path or proj.project_dir
    if not repo_path or not repo_path.exists():
        output_error("no_repo", f"No repo path for project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # Get recent commits: hash, ISO date, subject
    try:
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%H%x00%aI%x00%s"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            output_error("git_error", f"git log failed: {result.stderr.strip()}", fmt=fmt)
            sys.exit(1)
    except Exception as e:
        output_error("git_error", f"Failed to run git: {e}", fmt=fmt)
        sys.exit(1)

    commits = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\x00', 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})

    if not commits:
        output_success("No commits found", fmt=fmt)
        return

    # Get already-logged hashes
    logged_hashes = get_logged_commit_hashes(config, project=project_id)

    # Filter to new commits only
    new_commits = [c for c in commits if c["hash"] not in logged_hashes]

    if not new_commits:
        output_success("All recent commits already logged", fmt=fmt)
        return

    if dry_run:
        result_data = {"would_log": len(new_commits), "commits": new_commits}
        if fmt == OutputFormat.JSON:
            output_json(result_data)
        else:
            click.echo(f"Would log {len(new_commits)} commit(s):")
            for c in new_commits:
                click.echo(f"  {c['hash'][:8]} {c['subject']}")
        return

    # Log each new commit (oldest first)
    logged = []
    for commit in reversed(new_commits):
        # Get files changed in this commit
        try:
            files_result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", commit["hash"]],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            files_changed = None
            if files_result.returncode == 0 and files_result.stdout.strip():
                files_changed = [line for line in files_result.stdout.strip().split('\n') if line]
        except Exception:
            files_changed = None

        # Parse commit timestamp
        from datetime import datetime, timezone
        try:
            commit_ts = datetime.fromisoformat(commit["date"])
        except (ValueError, TypeError):
            commit_ts = datetime.now(timezone.utc)

        # Extract task ID from commit message if not explicitly provided
        effective_task = task_id
        if not effective_task:
            # Look for PROJ-NNN pattern in commit message
            import re
            task_match = re.search(r'\b([A-Z]+-\d{3})\b', commit["subject"])
            if task_match:
                effective_task = task_match.group(1)

        entry = add_entry(
            config,
            project=project_id,
            action=WorkLogAction.COMMIT,
            task=effective_task,
            summary=commit["subject"],
            files_changed=files_changed,
            commit_hash=commit["hash"],
            auto=True,
            ts=commit_ts,
        )
        logged.append(entry)

    result_data = {
        "logged": len(logged),
        "skipped": len(commits) - len(new_commits),
        "entries": [e.to_dict() for e in logged],
    }

    if fmt == OutputFormat.JSON:
        output_json(result_data)
    else:
        click.echo(f"Logged {len(logged)} commit(s), skipped {len(commits) - len(new_commits)} already logged")
        for e in logged:
            click.echo(f"  {e.commit_hash[:8] if e.commit_hash else '?'} {e.summary}")


# ============================================================================
# Research commands
# ============================================================================


@main.group()
def research() -> None:
    """Manage research items."""
    pass


@research.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--status", "-s", type=click.Choice(["open", "complete", "stale"]), help="Filter by status")
@click.option("--tags", "-t", multiple=True, help="Filter by tags (must have all)")
@click.pass_context
def research_list(ctx: click.Context, project_id: str | None, status: str | None, tags: tuple[str, ...]) -> None:
    """List research items."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    status_filter = ResearchStatus(status) if status else None
    tags_filter = list(tags) if tags else None

    items = list_research(config, project_id, status_filter=status_filter, tags_filter=tags_filter)
    output_research_list(items, fmt=fmt)


@research.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--type", "-t", "research_type", type=click.Choice(["investigation", "spike", "decision", "reference"]), required=True)
@click.option("--title", required=True, help="Research title")
@click.option("--id", "research_id", help="Research ID (auto-generated if not provided)")
@click.option("--tags", multiple=True, help="Tags")
@click.option("--question", "-q", help="Research question")
@click.pass_context
def research_add(
    ctx: click.Context,
    project_id: str | None,
    research_type: str,
    title: str,
    research_id: str | None,
    tags: tuple[str, ...],
    question: str | None,
) -> None:
    """Add a new research item."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    # Support both -t tag1 -t tag2 and --tags tag1,tag2
    parsed_tags = []
    for tag in tags:
        parsed_tags.extend(t.strip() for t in tag.split(",") if t.strip())

    item = add_research(
        config,
        project_id,
        title,
        ResearchType(research_type),
        research_id=research_id,
        tags=parsed_tags if parsed_tags else None,
        question=question or "",
    )

    if not item:
        output_error("add_failed", f"Failed to add research to project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Research {item.id} created", data=item.to_dict(), fmt=fmt)


@research.command("link")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--id", "research_id", required=True, help="Research ID")
@click.option("--session-key", "-s", required=True, help="OpenClaw session key")
@click.option("--run-id", "-r", help="OpenClaw run ID")
@click.option("--spawned-by", help="Spawning session key")
@click.pass_context
def research_link(
    ctx: click.Context,
    project_id: str | None,
    research_id: str,
    session_key: str,
    run_id: str | None,
    spawned_by: str | None,
) -> None:
    """Link a research item to an OpenClaw session."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    item = link_research_session(
        config,
        project_id,
        research_id,
        session_key,
        run_id=run_id,
        spawned_by=spawned_by,
    )

    if not item:
        output_error("link_failed", f"Failed to link research '{research_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Research {research_id} linked to session", data=item.to_dict(), fmt=fmt)


# ============================================================================
# Setup commands
# ============================================================================


@main.command("setup")
@click.option("--check", is_flag=True, help="Check installation status")
@click.pass_context
def setup(ctx: click.Context, check: bool) -> None:
    """Setup or verify ClawPM installation."""
    fmt = get_format(ctx)

    if check:
        issues: list[str] = []

        # Check portfolio path (defaults to ~/clawpm)
        portfolio_path = get_portfolio_path()
        if not portfolio_path:
            issues.append("No portfolio found at ~/clawpm (or set CLAWPM_PORTFOLIO env var)")
        else:
            if not (portfolio_path / "work_log.jsonl").exists():
                issues.append(f"work_log.jsonl not found in {portfolio_path}")

        # Check portfolio config
        config = load_portfolio_config()
        if config:
            portfolio_issues = validate_portfolio(config)
            issues.extend(portfolio_issues)

        if fmt == OutputFormat.JSON:
            output_json({
                "status": "ok" if not issues else "issues",
                "portfolio_path": str(portfolio_path) if portfolio_path else None,
                "issues": issues,
            })
        else:
            if issues:
                click.echo("Issues found:")
                for issue in issues:
                    click.echo(f"  - {issue}")
            else:
                click.echo("✓ ClawPM is properly configured")
                if portfolio_path:
                    click.echo(f"  Portfolio: {portfolio_path}")
    else:
        # Determine portfolio root
        env_portfolio = os.environ.get("CLAWPM_PORTFOLIO")
        if env_portfolio:
            portfolio_root = Path(env_portfolio).expanduser()
        else:
            portfolio_root = Path.home() / "clawpm"

        # Check if already set up
        if (portfolio_root / "portfolio.toml").exists():
            output_success(f"Already set up at {portfolio_root}", fmt=fmt)
            return

        # Create directory structure
        created: list[str] = []

        portfolio_root.mkdir(parents=True, exist_ok=True)
        created.append(str(portfolio_root))

        projects_dir = portfolio_root / "projects"
        projects_dir.mkdir(exist_ok=True)
        created.append(str(projects_dir))

        # Create portfolio.toml
        portfolio_toml = portfolio_root / "portfolio.toml"
        root_str = path_for_config(portfolio_root)
        projects_str = path_for_config(projects_dir)
        portfolio_toml.write_text(f'''# ClawPM Portfolio Configuration

portfolio_root = "{root_str}"

project_roots = [
    "{projects_str}"
]

[defaults]
status = "active"
''')
        created.append(str(portfolio_toml))

        # Create empty work log
        work_log = portfolio_root / "work_log.jsonl"
        if not work_log.exists():
            work_log.touch()
            created.append(str(work_log))

        if fmt == OutputFormat.JSON:
            output_json({
                "status": "created",
                "portfolio_root": str(portfolio_root),
                "created": created,
            })
        else:
            click.echo(f"Portfolio created at {portfolio_root}")
            click.echo(f"  projects/       - clone or init repos here")
            click.echo(f"  portfolio.toml  - configuration")
            click.echo(f"  work_log.jsonl  - activity log")
            click.echo(f"\nNext: cd into a git repo and run 'clawpm add \"First task\"'")


@main.command("version")
@click.pass_context
def version(ctx: click.Context) -> None:
    """Show version."""
    fmt = get_format(ctx)
    if fmt == OutputFormat.JSON:
        output_json({"version": __version__})
    else:
        click.echo(f"clawpm {__version__}")


@main.command("doctor")
@click.pass_context
def doctor(ctx: click.Context) -> None:
    """Run full health check."""
    # Delegate to project doctor with no specific project
    ctx.invoke(project_doctor, project_id=None)


# ============================================================================
# Issues commands
# ============================================================================

@main.group("issues")
def issues_group() -> None:
    """Log and track issues found during work."""
    pass


@issues_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--type", "-t", "issue_type", type=click.Choice(["bug", "ux", "docs", "feature"]), default="bug", help="Issue type")
@click.option("--severity", "-s", type=click.Choice(["high", "medium", "low"]), default="medium", help="Severity")
@click.option("--command", "-c", "cmd", help="Command that triggered the issue")
@click.option("--expected", "-e", help="What was expected")
@click.option("--actual", "-a", help="What actually happened")
@click.option("--context", help="Additional context")
@click.pass_context
def issues_add(
    ctx: click.Context,
    project_id: str | None,
    issue_type: str,
    severity: str,
    cmd: str | None,
    expected: str | None,
    actual: str | None,
    context: str | None,
) -> None:
    """Log an issue for a project."""
    import json
    from datetime import datetime, timezone

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    # Create .agent directory if needed
    agent_dir = proj.project_dir / ".agent"
    agent_dir.mkdir(exist_ok=True)
    issues_file = agent_dir / "issues.jsonl"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": issue_type,
        "severity": severity,
        "command": cmd,
        "expected": expected,
        "actual": actual,
        "context": context,
        "fixed": False,
    }

    # Remove None values
    entry = {k: v for k, v in entry.items() if v is not None}

    with open(issues_file, "a") as f:
        f.write(json.dumps(entry) + "\n")

    if fmt == OutputFormat.JSON:
        output_json({"status": "logged", "file": str(issues_file), "entry": entry})
    else:
        click.echo(f"Logged {issue_type} issue ({severity}) to {issues_file}")


@issues_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--open", "open_only", is_flag=True, help="Show only unfixed issues")
@click.pass_context
def issues_list(ctx: click.Context, project_id: str | None, open_only: bool) -> None:
    """List issues for a project."""
    import json

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    issues_file = proj.project_dir / ".agent" / "issues.jsonl"
    if not issues_file.exists():
        if fmt == OutputFormat.JSON:
            output_json({"issues": [], "count": 0})
        else:
            click.echo("No issues logged yet.")
        return

    issues = []
    with open(issues_file) as f:
        for line in f:
            line = line.strip()
            if line:
                issue = json.loads(line)
                if open_only and issue.get("fixed"):
                    continue
                issues.append(issue)

    if fmt == OutputFormat.JSON:
        output_json({"issues": issues, "count": len(issues)})
    else:
        if not issues:
            click.echo("No issues found.")
            return
        for i, issue in enumerate(issues, 1):
            status = "✓" if issue.get("fixed") else "○"
            sev = issue.get("severity", "?")[0].upper()
            typ = issue.get("type", "?")
            click.echo(f"{status} [{sev}] {typ}: {issue.get('actual', issue.get('context', 'No description'))}")



# ============================================================================
# Conflicts command
# ============================================================================


def _glob_literal_prefix(pattern: str) -> str:
    """Return the longest literal (non-wildcard) prefix of a glob pattern.

    Examples:
      "src/auth/**"         -> "src/auth/"
      "src/auth/login.py"   -> "src/auth/login.py"
      "*.py"                -> ""
      "src/auth/handlers/*" -> "src/auth/handlers/"
    """
    for i, ch in enumerate(pattern):
        if ch in ("*", "?", "["):
            return pattern[:i].rstrip("/")
    return pattern  # No wildcard — the whole pattern is a literal path


def _globs_overlap(a: str, b: str) -> bool:
    """Heuristic check: do two glob patterns potentially claim overlapping paths?

    Perfect glob-intersection is undecidable in general. This implementation
    uses a pragmatic prefix-based heuristic that covers the common cases seen
    in parallel agent dispatch:

    1. Exact match: "src/auth/login.py" vs "src/auth/login.py"
    2. Subtree containment: "src/auth/**" vs "src/auth/handlers/**"
       — prefix of A is a prefix of B (or vice versa).
    3. Literal-under-glob: "src/auth/login.py" vs "src/auth/**"
       — the literal path starts with the glob's prefix.

    Limitations:
    - Does not handle character classes ([a-z]) or ? wildcards.
    - Does not resolve negation patterns (!).
    - May produce false positives (safe: errs toward flagging rather than missing
      a real conflict).
    """
    if a == b:
        return True

    prefix_a = _glob_literal_prefix(a)
    prefix_b = _glob_literal_prefix(b)

    # Neither pattern has a wildcard — compare as literal paths
    if prefix_a == a and prefix_b == b:
        return a == b

    # At least one is a glob — check whether either prefix contains the other
    def _prefix_contains(longer: str, shorter: str) -> bool:
        if shorter == "":
            return True  # Empty prefix matches everything
        return longer == shorter or longer.startswith(shorter + "/") or longer.startswith(shorter)

    return _prefix_contains(prefix_a, prefix_b) or _prefix_contains(prefix_b, prefix_a)


def _find_scope_conflicts(config, query_scope: list[str]) -> list[dict]:
    """Return all in-progress tasks across all projects whose scope overlaps query_scope."""
    import datetime as _dt

    conflicts = []
    projects = discover_projects(config)

    for proj in projects:
        in_flight = list_tasks(config, proj.id, state_filter=TaskState.PROGRESS)
        for task in in_flight:
            if not task.scope:
                continue
            overlapping = [
                qs
                for qs in query_scope
                for ts in task.scope
                if _globs_overlap(qs, ts)
            ]
            if overlapping:
                entry: dict = {
                    "project_id": proj.id,
                    "task_id": task.id,
                    "title": task.title,
                    "scope": task.scope,
                    "state": task.state.value,
                    "overlapping_globs": sorted(set(overlapping)),
                }
                if task.file_path:
                    mtime = task.file_path.stat().st_mtime
                    entry["started_at"] = (
                        _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).isoformat()
                    )
                conflicts.append(entry)

    return conflicts


@main.command("conflicts")
@click.option(
    "--scope",
    "scope_globs",
    multiple=True,
    help="Glob pattern to check for conflicts (can specify multiple). "
         "Mutually exclusive with --task.",
)
@click.option(
    "--task",
    "task_ref",
    default=None,
    help="Task ID whose declared scope is used as the query. "
         "Mutually exclusive with --scope.",
)
@click.option(
    "--project", "-p", "task_project_id", default=None,
    help="Project containing --task (auto-detected if omitted).",
)
@click.pass_context
def conflicts(
    ctx: click.Context,
    scope_globs: tuple[str, ...],
    task_ref: str | None,
    task_project_id: str | None,
) -> None:
    """Check for in-progress tasks that claim overlapping file scopes.

    Mode A — by globs:
      clawpm conflicts --scope "src/auth/**" --scope "tests/auth/**"

    Mode B — by task ID (reads scope from the named task):
      clawpm conflicts --task CLAWP-042

    Exit code is always 0. Read the ``conflicts`` array: empty list means safe
    to dispatch.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if scope_globs and task_ref:
        output_error("ambiguous_query", "Specify either --scope globs or --task, not both.", fmt=fmt)
        sys.exit(1)

    if not scope_globs and not task_ref:
        output_error("missing_query", "Specify at least one --scope glob or a --task ID.", fmt=fmt)
        sys.exit(1)

    # Resolve scope from task reference
    if task_ref:
        if task_project_id:
            proj_id = task_project_id
        else:
            proj_id, _ = require_project(ctx, None, required=True, auto_init=False)

        task_id = expand_task_id(task_ref, proj_id)
        source_task = get_task(config, proj_id, task_id)
        if not source_task:
            output_error("task_not_found", f"No task with id '{task_id}' in project '{proj_id}'", fmt=fmt)
            sys.exit(1)

        query_scope = source_task.scope
        if not query_scope:
            result = {
                "conflicts": [],
                "queried_scope": [],
                "note": f"Task {task_id} has no scope declared",
            }
            if fmt == OutputFormat.JSON:
                output_json(result)
            else:
                click.echo(f"Task {task_id} has no scope declared — no conflicts possible.")
            return
    else:
        query_scope = list(scope_globs)

    conflict_list = _find_scope_conflicts(config, query_scope)
    result = {"conflicts": conflict_list, "queried_scope": query_scope}

    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        if not conflict_list:
            click.echo("No conflicts found. Safe to dispatch.")
            return
        click.echo(f"Found {len(conflict_list)} conflict(s):")
        for c in conflict_list:
            overlap_str = ", ".join(c["overlapping_globs"])
            click.echo(
                f"  [{c['project_id']}] {c['task_id']} — {c['title']}\n"
                f"    scope: {c['scope']}\n"
                f"    overlapping: {overlap_str}"
            )


# ============================================================================
# Serve command
# ============================================================================

@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
def serve(host: str, port: int) -> None:
    """Start the web UI server."""
    import uvicorn
    from .serve import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
