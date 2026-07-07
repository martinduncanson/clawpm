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
@click.option("--summary", "--verdict", "summary", help="Verdict/summary written straight into the Summary section (single-shot capture).")
@click.option("--finding", "findings", multiple=True, help="A finding bullet for the Findings section (repeatable).")
@click.option("--conclusion", help="Conclusion written straight into the Conclusion section.")
@click.option("--open", "open_ended", is_flag=True, help="Progressive template with placeholder sections for a genuinely open investigation (no verdict yet).")
@click.pass_context
def research_add(
    ctx: click.Context,
    project_id: str | None,
    research_type: str,
    title: str,
    research_id: str | None,
    tags: tuple[str, ...],
    question: str | None,
    summary: str | None,
    findings: tuple[str, ...],
    conclusion: str | None,
    open_ended: bool,
) -> None:
    """Add a new research item.

    Default is single-shot capture: pass --summary (alias --verdict) and any
    number of --finding bullets to write the verdict straight into the
    sections at creation time. Use --open for the progressive template that
    keeps placeholder sections to fill in as an investigation proceeds.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    # --open is the progressive (fill-in-later) path; it can't also carry a
    # single-shot verdict, or the supplied content would be silently dropped.
    if open_ended and (summary or findings or conclusion):
        output_error(
            "open_conflict",
            "--open is for a progressive entry with no verdict yet; it cannot be "
            "combined with --summary/--verdict, --finding, or --conclusion.",
            fmt=fmt,
        )
        sys.exit(1)

    # Single-shot capture needs a verdict (the Summary); --finding/--conclusion
    # are optional additions and don't substitute for it.
    if not open_ended and not summary:
        output_error(
            "missing_verdict",
            "research add needs --summary/--verdict (single-shot capture) or --open "
            "for a progressive entry to fill in later.",
            fmt=fmt,
        )
        sys.exit(1)

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
        summary=summary or "",
        findings=list(findings) if findings else None,
        conclusion=conclusion or "",
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
