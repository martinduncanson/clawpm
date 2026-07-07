from __future__ import annotations

import sys

import click

from clawpm.output import OutputFormat, output_error, output_json, output_success
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Constitution commands (CLAWP-057)
# ============================================================================


@main.group("constitution")
def constitution_group() -> None:
    """Project constitution — named invariants that constrain emission."""
    pass


@constitution_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--name", "-n", required=True, help="Invariant name (unique per project)")
@click.option(
    "--kind", "-k", required=True,
    type=click.Choice([
        "require_success_criteria",
        "max_complexity",
        "require_scope",
        "advisory",
    ]),
    help="Invariant kind (code-checkable or advisory)",
)
@click.option("--description", "-d", default=None, help="Human description (optional)")
@click.option(
    "--param", "params_raw", multiple=True, metavar="KEY=VALUE",
    help="Kind-specific parameters (repeatable, e.g. --param max=l)",
)
@click.pass_context
def constitution_add(
    ctx: click.Context,
    project_id: str | None,
    name: str,
    kind: str,
    description: str | None,
    params_raw: tuple[str, ...],
) -> None:
    """Add (or replace) a named invariant in the project constitution."""
    from clawpm.constitution import add_invariant

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    # Parse --param KEY=VALUE pairs into a dict
    params: dict = {}
    for pair in params_raw:
        if "=" not in pair:
            output_error("invalid_param", f"--param must be KEY=VALUE, got {pair!r}", fmt=fmt)
            sys.exit(1)
        k, v = pair.split("=", 1)
        params[k.strip()] = v.strip()

    try:
        inv = add_invariant(
            config,
            project_id,
            name=name,
            kind=kind,
            description=description,
            params=params or None,
        )
    except Exception as exc:
        output_error("constitution_add_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(f"Invariant '{name}' added to constitution", data=inv, fmt=fmt)


@constitution_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.pass_context
def constitution_list(ctx: click.Context, project_id: str | None) -> None:
    """List invariants declared in the project constitution."""
    from clawpm.constitution import load_constitution

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    doc = load_constitution(config, project_id)
    invariants = doc.get("invariants", [])

    if fmt == OutputFormat.JSON:
        output_json(invariants)
    else:
        if not invariants:
            click.echo("No invariants declared.")
            return
        for inv in invariants:
            kind = inv.get("kind", "?")
            desc = inv.get("description", "")
            params = inv.get("params", {})
            line = f"  [{kind}] {inv['name']}"
            if params:
                line += f"  params={params}"
            if desc:
                line += f"  -- {desc}"
            click.echo(line)


@constitution_group.command("remove")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--name", "-n", required=True, help="Invariant name to remove")
@click.pass_context
def constitution_remove(ctx: click.Context, project_id: str | None, name: str) -> None:
    """Remove a named invariant from the project constitution (idempotent)."""
    from clawpm.constitution import remove_invariant

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    try:
        removed = remove_invariant(config, project_id, name)
    except Exception as exc:
        output_error("constitution_remove_failed", str(exc), fmt=fmt)
        sys.exit(1)

    msg = f"Invariant '{name}' removed" if removed else f"Invariant '{name}' not found (no-op)"
    output_success(msg, data={"name": name, "removed": removed}, fmt=fmt)
