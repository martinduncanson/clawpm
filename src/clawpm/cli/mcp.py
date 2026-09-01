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
    # The guard wraps ONLY construction (build_server(), which is where the
    # deferred `import mcp` SDK import actually lives — see mcp_server.py) —
    # not the blocking `.run()` call below. Wrapping the whole blocking
    # lifetime (as an earlier version of this fix did) would catch a much
    # later, unrelated ModuleNotFoundError raised during real server
    # operation and misreport a live crash as "install the mcp extra"
    # (grok-4.5 review, round 3) — the opposite of what CLAWP-068 review F1
    # was trying to fix.
    try:
        from clawpm.mcp_server import build_server

        server = build_server(tools_tier)
    except ModuleNotFoundError as exc:
        # Narrowed to the `mcp` package specifically (antigravity review) so
        # an unrelated ModuleNotFoundError during construction isn't
        # misreported as a missing extra either.
        if exc.name != "mcp" and not (exc.name or "").startswith("mcp."):
            raise
        click.echo(
            "The clawpm MCP server requires the optional 'mcp' extra.\n"
            "Install it with:  pip install 'clawpm[mcp]'",
            err=True,
        )
        sys.exit(1)

    server.run()


if __name__ == "__main__":
    main()
