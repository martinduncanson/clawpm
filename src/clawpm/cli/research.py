from __future__ import annotations

import sys

import click

from clawpm.models import ResearchStatus, ResearchType
from clawpm.output import output_error, output_research_list, output_success
from clawpm.research import add_research, link_research_session, list_research
from clawpm.cli.base import main, get_format, require_portfolio, require_project

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
