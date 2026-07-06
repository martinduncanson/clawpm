from __future__ import annotations


import click

from clawpm.models import ProjectStatus, TaskState
from clawpm.output import OutputFormat, output_json, output_projects_list, output_task_detail
from clawpm.discovery import discover_projects, discover_untracked_repos
from clawpm.tasks import get_next_task, list_tasks
from clawpm.cli.base import main, get_format, require_portfolio

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
                # ASCII bullet only — Windows cp1252 stdout cannot encode U+25CB and crashes the run.
                click.echo(f"  - {repo.name}{remote_hint}")


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
