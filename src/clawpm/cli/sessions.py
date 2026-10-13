from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from clawpm.cli.base import main, get_format, ExpandedPath
from clawpm.output import OutputFormat

# ============================================================================
# Sessions commands (CLAWP-147-003) — dispatched-worker write root
# ============================================================================

# Exit codes of `sessions write-root`, documented in README `## All commands`.
EXIT_NO_FENCE = 1
EXIT_MARKER_UNUSABLE = 3


@main.group("sessions")
def sessions_group() -> None:
    """Inspect the dispatched-worktree context of the current directory."""
    pass


@sessions_group.command("write-root")
@click.option(
    "--cwd", "cwd", type=ExpandedPath(), default=None,
    help="Directory to resolve from (default: the process cwd)",
)
@click.pass_context
def sessions_write_root(ctx, cwd) -> None:
    """Print the dispatched worker's absolute write root as JSON.

    The root is the nearest directory at or above the cwd carrying a dispatch
    marker (.claude/settings.local.json), so a harness hook can fence a worker
    to one source of truth. Needs no portfolio and writes nothing.

    Exit codes: 0 root found; 1 not inside a dispatched worktree (ok=false,
    reason no_dispatch_marker: nothing to fence); 3 a marker is present but
    cannot yield an unambiguous root (ok=false, reason marker_unusable).
    """
    from clawpm.sessions import resolve_write_root

    fmt = get_format(ctx)
    result = resolve_write_root(Path(cwd) if cwd else Path.cwd())
    if fmt == OutputFormat.JSON:
        print(json.dumps(result, indent=2))
    elif result["ok"]:
        print(result["root"])
    else:
        detail = result.get("detail")
        print(result["reason"] + (f": {detail}" if detail else ""), file=sys.stderr)
    if result["ok"]:
        return
    sys.exit(EXIT_NO_FENCE if result["reason"] == "no_dispatch_marker" else EXIT_MARKER_UNUSABLE)
