from __future__ import annotations

import sys

import click

from clawpm.cli.base import main

# ============================================================================
# MCP server command (CLAWP-068)
# ============================================================================


@main.command("mcp")
@click.option(
    "--tools",
    "tools_tier",
    type=click.Choice(["core", "standard", "all"]),
    default=None,
    help="Tool surface to expose (default: $CLAWPM_MCP_TOOLS, then 'core'). "
         "Keeps a host's tool list lean; 'core' is the 10-tool default set.",
)
def mcp(tools_tier: str | None) -> None:
    """Start the MCP server over stdio.

    Exposes clawpm task/research/mission management to any MCP host (Cursor,
    Windsurf, VS Code, Claude Code, Amazon Q, …). Register per-project via a
    `.mcp.json` in the project root so the surface loads only inside a clawpm
    project (see the README). Communicates on stdin/stdout — do not print to
    stdout elsewhere while this runs.
    """
    try:
        from clawpm.mcp_server import run_stdio
    except ImportError:
        click.echo(
            "The clawpm MCP server requires the optional 'mcp' extra.\n"
            "Install it with:  pip install 'clawpm[mcp]'",
            err=True,
        )
        sys.exit(1)

    run_stdio(tools_tier)


if __name__ == "__main__":
    main()
