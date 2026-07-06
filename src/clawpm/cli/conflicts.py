from __future__ import annotations

import sys

import click

from clawpm.models import Task, TaskState
from clawpm.output import OutputFormat, output_error, output_json
from clawpm.discovery import discover_projects
from clawpm.tasks import get_task, list_tasks
from clawpm.context import expand_task_id
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Conflicts command
# ============================================================================


def _glob_literal_prefix(pattern: str) -> str:
    """Return the longest literal (non-wildcard) prefix of a glob pattern.

    Examples:
      "src/auth/**"         -> "src/auth/"
      "src/auth/login.py"   -> "src/auth/login.py"
      "*.py"                -> ""
      "src/auth/handlers/*" -> "src/auth/handlers/"
    """
    for i, ch in enumerate(pattern):
        if ch in ("*", "?", "["):
            return pattern[:i].rstrip("/")
    return pattern  # No wildcard — the whole pattern is a literal path


def _globs_overlap(a: str, b: str) -> bool:
    """Heuristic check: do two glob patterns potentially claim overlapping paths?

    Perfect glob-intersection is undecidable in general. This implementation
    uses a pragmatic prefix-based heuristic that covers the common cases seen
    in parallel agent dispatch:

    1. Exact match: "src/auth/login.py" vs "src/auth/login.py"
    2. Subtree containment: "src/auth/**" vs "src/auth/handlers/**"
       — prefix of A is a prefix of B (or vice versa).
    3. Literal-under-glob: "src/auth/login.py" vs "src/auth/**"
       — the literal path starts with the glob's prefix.

    Limitations:
    - Does not handle character classes ([a-z]) or ? wildcards.
    - Does not resolve negation patterns (!).
    - May produce false positives (safe: errs toward flagging rather than missing
      a real conflict).
    """
    if a == b:
        return True

    prefix_a = _glob_literal_prefix(a)
    prefix_b = _glob_literal_prefix(b)

    # Neither pattern has a wildcard — compare as literal paths
    if prefix_a == a and prefix_b == b:
        return a == b

    # At least one is a glob — check whether either prefix contains the other
    def _prefix_contains(longer: str, shorter: str) -> bool:
        if shorter == "":
            return True  # Empty prefix matches everything
        return longer == shorter or longer.startswith(shorter + "/") or longer.startswith(shorter)

    return _prefix_contains(prefix_a, prefix_b) or _prefix_contains(prefix_b, prefix_a)


def _find_scope_conflicts(config, query_scope: list[str]) -> list[dict]:
    """Return all in-progress tasks across all projects whose scope overlaps query_scope."""
    import datetime as _dt

    conflicts = []
    projects = discover_projects(config)

    for proj in projects:
        in_flight = list_tasks(config, proj.id, state_filter=TaskState.PROGRESS)
        for task in in_flight:
            if not task.scope:
                continue
            overlapping = [
                qs
                for qs in query_scope
                for ts in task.scope
                if _globs_overlap(qs, ts)
            ]
            if overlapping:
                entry: dict = {
                    "project_id": proj.id,
                    "task_id": task.id,
                    "title": task.title,
                    "scope": task.scope,
                    "state": task.state.value,
                    "overlapping_globs": sorted(set(overlapping)),
                }
                if task.file_path:
                    mtime = task.file_path.stat().st_mtime
                    entry["started_at"] = (
                        _dt.datetime.fromtimestamp(mtime, tz=_dt.timezone.utc).isoformat()
                    )
                conflicts.append(entry)

    return conflicts


@main.command("conflicts")
@click.option(
    "--scope",
    "scope_globs",
    multiple=True,
    help="Glob pattern to check for conflicts (can specify multiple). "
         "Mutually exclusive with --task.",
)
@click.option(
    "--task",
    "task_ref",
    default=None,
    help="Task ID whose declared scope is used as the query. "
         "Mutually exclusive with --scope.",
)
@click.option(
    "--project", "-p", "task_project_id", default=None,
    help="Project containing --task (auto-detected if omitted).",
)
@click.pass_context
def conflicts(
    ctx: click.Context,
    scope_globs: tuple[str, ...],
    task_ref: str | None,
    task_project_id: str | None,
) -> None:
    """Check for in-progress tasks that claim overlapping file scopes.

    Mode A — by globs:
      clawpm conflicts --scope "src/auth/**" --scope "tests/auth/**"

    Mode B — by task ID (reads scope from the named task):
      clawpm conflicts --task CLAWP-042

    Exit code is always 0. Read the ``conflicts`` array: empty list means safe
    to dispatch.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if scope_globs and task_ref:
        output_error("ambiguous_query", "Specify either --scope globs or --task, not both.", fmt=fmt)
        sys.exit(1)

    if not scope_globs and not task_ref:
        output_error("missing_query", "Specify at least one --scope glob or a --task ID.", fmt=fmt)
        sys.exit(1)

    # Resolve scope from task reference
    if task_ref:
        if task_project_id:
            proj_id = task_project_id
        else:
            proj_id, _ = require_project(ctx, None, required=True, auto_init=False)

        task_id = expand_task_id(task_ref, proj_id)
        source_task = get_task(config, proj_id, task_id)
        if not source_task:
            output_error("task_not_found", f"No task with id '{task_id}' in project '{proj_id}'", fmt=fmt)
            sys.exit(1)

        query_scope = source_task.scope
        if not query_scope:
            result = {
                "conflicts": [],
                "queried_scope": [],
                "note": f"Task {task_id} has no scope declared",
            }
            if fmt == OutputFormat.JSON:
                output_json(result)
            else:
                click.echo(f"Task {task_id} has no scope declared - no conflicts possible.")
            return
    else:
        query_scope = list(scope_globs)

    conflict_list = _find_scope_conflicts(config, query_scope)
    result = {"conflicts": conflict_list, "queried_scope": query_scope}

    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        if not conflict_list:
            click.echo("No conflicts found. Safe to dispatch.")
            return
        click.echo(f"Found {len(conflict_list)} conflict(s):")
        for c in conflict_list:
            overlap_str = ", ".join(c["overlapping_globs"])
            click.echo(
                f"  [{c['project_id']}] {c['task_id']} - {c['title']}\n"
                f"    scope: {c['scope']}\n"
                f"    overlapping: {overlap_str}"
            )
