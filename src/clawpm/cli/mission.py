from __future__ import annotations

import sys

import click

from clawpm.models import Task
from clawpm.output import OutputFormat, output_error, output_json, output_success
from clawpm.context import expand_task_id
from clawpm.cli.base import main, _mutation_errors, get_format, require_portfolio, require_project

# ============================================================================
# Mission Control commands (CLAWP-022)
# ============================================================================


@main.group("mission")
def mission_group() -> None:
    """Mission Control — macro binary-outcome layer above tasks."""
    pass


@mission_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--title", "-t", required=True, help="Mission title (6-8 words, ship-able statement)")
@click.option("--binary-outcome", "-o", required=True, help="YES/NO check at the deadline (<=12 words)")
@click.option("--deadline-days", "-d", type=int, default=28, show_default=True, help="Days from now (7-42)")
@click.option("--body", "-b", default="", help="Mission description (optional)")
@click.option("--id", "mission_id", default=None, help="Override mission ID (auto-generated otherwise)")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing mission file with the same ID (destructive)")
@click.pass_context
def mission_add(
    ctx: click.Context,
    project_id: str | None,
    title: str,
    binary_outcome: str,
    deadline_days: int,
    body: str,
    mission_id: str | None,
    force: bool,
) -> None:
    """Create a new mission. Mini-goals are linked separately via add-goal."""
    from clawpm.mission import add_mission

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    try:
        mission = add_mission(
            config, project_id,
            title=title,
            binary_outcome=binary_outcome,
            deadline_days=deadline_days,
            description=body,
            mission_id=mission_id,
            force=force,
        )
    except ValueError as exc:
        output_error("mission_add_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(f"Mission {mission.id} created", data=mission.to_dict(), fmt=fmt)


@mission_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--status", "-s", "status_filter",
              type=click.Choice(["active", "complete", "failed", "cancelled"]),
              default=None,
              help="Filter by status")
@click.pass_context
def mission_list(
    ctx: click.Context, project_id: str | None, status_filter: str | None
) -> None:
    """List missions for a project."""
    from clawpm.mission import list_missions

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    missions = list_missions(config, project_id, status_filter=status_filter)
    if fmt == OutputFormat.JSON:
        output_json([m.to_dict() for m in missions])
    else:
        for m in missions:
            click.echo(f"{m.id} [{m.status}] {m.title} -> {m.binary_outcome[:60]}")


@mission_group.command("status")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.pass_context
def mission_status_cmd(
    ctx: click.Context, project_id: str | None, mission_id: str
) -> None:
    """Compute progress + outcome state for a mission."""
    from clawpm.mission import mission_status

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    result = mission_status(config, project_id, mission_id)
    if "error" in result:
        output_error("mission_not_found", result["error"], fmt=fmt)
        sys.exit(1)
    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        click.echo(f"{result['id']}: {result['title']}")
        click.echo(f"  outcome: {result['outcome_status']}")
        click.echo(f"  progress: {result['complete_count']}/{result['total_count']} ({result['pct_complete']}%)")
        if result['days_remaining'] is not None:
            click.echo(f"  deadline: {result['deadline_date']} ({result['days_remaining']}d remaining)")
        click.echo(f"  agent: {result['agent_counts']}")
        click.echo(f"  human: {result['human_counts']}")
        if result['missing_refs']:
            click.echo(f"  WARNING: missing task refs: {result['missing_refs']}")


@mission_group.command("tasks")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.option("--actor",
              type=click.Choice(["agent", "human"]),
              default=None,
              help="Filter by actor (default: both)")
@click.pass_context
def mission_tasks_cmd(
    ctx: click.Context, project_id: str | None, mission_id: str, actor: str | None
) -> None:
    """List mini-goal tasks for a mission, optionally filtered by actor."""
    from clawpm.mission import mission_tasks

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    tasks = mission_tasks(config, project_id, mission_id, actor_filter=actor)
    if fmt == OutputFormat.JSON:
        output_json([t.to_dict() for t in tasks])
    else:
        for t in tasks:
            click.echo(f"{t.id} [{t.state.value}] [{t.actor or 'agent'}] {t.title[:80]}")


@mission_group.command("add-goal")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.option("--task", "task_id", required=True, help="Task ID to link as a mini-goal")
@click.option("--actor",
              type=click.Choice(["agent", "human"]),
              default="agent",
              show_default=True,
              help="Who runs this mini-goal")
@click.pass_context
def mission_add_goal(
    ctx: click.Context,
    project_id: str | None,
    mission_id: str,
    task_id: str,
    actor: str,
) -> None:
    """Link an existing task to a mission as a mini-goal."""
    from clawpm.mission import add_mission_mini_goal

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    with _mutation_errors(fmt, "mission_add_goal_failed"):
        mission = add_mission_mini_goal(
            config, project_id, mission_id, task_id, actor=actor
        )

    output_success(
        f"Linked {task_id} to {mission_id} as {actor}",
        data=mission.to_dict(),
        fmt=fmt,
    )


@mission_group.command("state")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.argument("new_status",
                type=click.Choice(["active", "complete", "failed", "cancelled"]))
@click.pass_context
def mission_state(
    ctx: click.Context,
    project_id: str | None,
    mission_id: str,
    new_status: str,
) -> None:
    """Transition a mission to complete / failed / cancelled / active."""
    from clawpm.mission import set_mission_status

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    with _mutation_errors(fmt, "mission_state_failed"):
        mission = set_mission_status(config, project_id, mission_id, new_status)

    output_success(
        f"Mission {mission_id} -> {new_status}",
        data=mission.to_dict(),
        fmt=fmt,
    )
