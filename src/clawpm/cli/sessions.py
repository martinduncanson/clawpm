from __future__ import annotations

import json
import os
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
    cannot yield an unambiguous root, or resolving hit any unexpected error
    (ok=false, reason marker_unusable). A resolved root that cannot be written
    (closed pipe) also exits 3, never 0 or 1.
    """
    from clawpm.sessions import resolve_write_root, unusable_write_root

    fmt = get_format(ctx)
    # Safety net: an unhandled exception would exit 1 with a traceback, which a
    # hook reads as "nothing to fence". Any surprise is "marker unusable" (3).
    try:
        result = resolve_write_root(Path(cwd) if cwd else Path.cwd())
    except Exception as exc:
        result = unusable_write_root(exc)
    if result["ok"]:
        code = 0
    elif result["reason"] == "no_dispatch_marker":
        code = EXIT_NO_FENCE
    else:
        code = EXIT_MARKER_UNUSABLE
    # Output is inside the fail-closed region too: a dead pipe must not escape
    # as a traceback (exit 1). A root we resolved but could not deliver is
    # marker_unusable (3); a genuine no-marker state stays 1.
    try:
        # Touch only the stream that carries the result: a broken stream we never
        # wrote to (e.g. stderr closed at launch) must not fail a delivered result.
        if fmt == OutputFormat.JSON:
            carrier = sys.stdout
            print(json.dumps(result, indent=2))
        elif result["ok"]:
            carrier = sys.stdout
            print(result["root"])
        else:
            carrier = sys.stderr
            detail = result.get("detail")
            print(result["reason"] + (f": {detail}" if detail else ""), file=carrier)
        # Flush now so a deferred failure is caught here, not at interpreter exit.
        carrier.flush()
    except Exception:
        if code == 0:
            code = EXIT_MARKER_UNUSABLE
        if code == EXIT_MARKER_UNUSABLE:
            try:
                print("marker_unusable: could not write result", file=sys.stderr)
                sys.stderr.flush()
            except Exception:
                pass
        # Unflushed bytes would fail again at exit and replace our code with 120.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except Exception:
            pass
    if code:
        sys.exit(code)
