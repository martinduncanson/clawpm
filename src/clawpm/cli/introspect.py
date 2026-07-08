from __future__ import annotations

import click

from clawpm.introspect import build_introspection
from clawpm.output import output_json
from clawpm.cli.base import main

# ============================================================================
# Introspect command (machine-readable capability listing) — CLAWP-088
# ============================================================================


@main.command("introspect")
@click.pass_context
def introspect(ctx: click.Context) -> None:
    """Emit the full command tree as JSON (machine-readable capability listing).

    Generated purely from the live click registry — every group, command,
    option, argument, type, choice, and help string — so it can never drift
    from the real CLI. A fresh agent can construct any valid invocation from
    this output alone, without shelling ``--help`` per group.

    Always emits JSON regardless of ``--format``: the listing is definitionally
    structured data.
    """
    root = ctx.find_root().command
    output_json(build_introspection(root))
