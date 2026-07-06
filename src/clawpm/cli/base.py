"""Shared CLI foundation: the root ``main`` group + cross-command helpers.

Group modules (``clawpm.cli.tasks``, ``clawpm.cli.log`` …) import ``main`` and
these helpers from here and decorate their commands onto the shared ``main``
group. Extracting them into a single import-cycle-free module (CLAWP-077) is
what lets each click group live in its own file: base.py depends on nothing in
the ``cli`` package, the group modules depend only on base, and
``cli/__init__.py`` orchestrates by importing every group module for its
registration side effect.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path

import click

from clawpm import __version__
from clawpm.concurrency import LockTimeout
from clawpm.output import OutputFormat, output_error
from clawpm.discovery import load_portfolio_config
from clawpm.context import (
    resolve_project,
    detect_untracked_repo_from_cwd,
    auto_init_if_untracked,
)


@contextmanager
def _mutation_errors(fmt, error_code: str):
    """Map a task-tree / mission mutator's exception contract to a clean
    ``output_error`` + ``exit(1)`` instead of a raw traceback (CLAWP-067).

    The mutators (change_task_state, add_task, add_subtask, split_task,
    edit_task, mission ops) raise a known set:
      - ``LockTimeout``       — per-project lock contended past its timeout
      - ``FileExistsError``   — explicit-id clobber guard (add_task)
      - ``FileNotFoundError`` — source moved by a concurrent session
      - ``ValueError``        — validation / corrupt-frontmatter refusal
    Each maps to a structured error. Anything OUTSIDE this contract (an
    unexpected OSError, a genuine bug) is deliberately NOT caught — it should
    surface as a traceback rather than be masked behind a misleading "failed"
    message (fail-open != fail-silent).
    """
    try:
        yield
    except LockTimeout as exc:
        output_error(
            "lock_timeout",
            f"Could not acquire the project lock (another session may be busy): {exc}",
            fmt=fmt,
        )
        sys.exit(1)
    except FileExistsError as exc:
        output_error("already_exists", str(exc), fmt=fmt)
        sys.exit(1)
    except FileNotFoundError as exc:
        output_error("not_found", str(exc), fmt=fmt)
        sys.exit(1)
    except ValueError as exc:
        output_error(error_code, str(exc), fmt=fmt)
        sys.exit(1)


# Global format option
pass_format = click.make_pass_decorator(OutputFormat, ensure=True)


@click.group()
@click.option(
    "--format", "-f",
    type=click.Choice(["json", "text"]),
    default="json",
    help="Output format (default: json)",
)
@click.option(
    "--project", "-p",
    "global_project",
    help="Project ID (overrides auto-detection)",
)
@click.option(
    "--no-hints", "no_hints", is_flag=True, default=False,
    help="Suppress runtime next-action hints (CLAWP-050). Also via CLAWPM_NO_HINTS.",
)
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, format: str, global_project: str | None, no_hints: bool) -> None:
    """ClawPM - Filesystem-first multi-project manager."""
    ctx.ensure_object(dict)
    ctx.obj["format"] = OutputFormat(format)
    ctx.obj["global_project"] = global_project
    ctx.obj["no_hints"] = no_hints


def get_format(ctx: click.Context) -> OutputFormat:
    """Get the output format from context."""
    return ctx.obj.get("format", OutputFormat.JSON)


def require_portfolio(ctx: click.Context):
    """Load portfolio config or exit with error."""
    config = load_portfolio_config()
    if not config:
        fmt = get_format(ctx)
        output_error(
            "portfolio_not_found",
            "No portfolio found at ~/clawpm (or CLAWPM_PORTFOLIO). Run setup or create portfolio.toml.",
            fmt=fmt,
        )
        sys.exit(1)
    return config


def _read_patterns_file(path: str, option_name: str, fmt) -> list[str]:
    """Read one-glob-pattern-per-line from *path*.

    Blank lines and lines starting with '#' are skipped.  Patterns are
    returned VERBATIM -- no shell or CRT glob-expansion is performed.

    This is the Windows-safe filing path for --scope, --predict-scope,
    and --out-of-scope: the file argument is a plain filesystem path, so
    it never becomes a glob token in argv and cannot be CRT-expanded.

    emit-tree JSON via stdin is already immune (the JSON blob is a single
    quoted argument, not a glob-valued token).

    Exits with error if *path* does not exist.
    """
    p = Path(path)
    if not p.exists():
        output_error(
            "scope_file_not_found",
            f"{option_name}: file not found: {path}",
            fmt=fmt,
        )
        sys.exit(1)
    lines = p.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def require_project(ctx: click.Context, project_id: str | None, required: bool = True, auto_init: bool = True) -> tuple[str | None, str]:
    """Resolve project from explicit arg, global flag, cwd, or context.

    Returns (project_id, source). Exits with error if required and not found.
    Priority: explicit arg > global --project flag > cwd > auto-init > context

    If auto_init=True and cwd is in an untracked git repo under project_roots,
    automatically initializes a .project/ structure.
    """
    # Check for global --project flag if no explicit arg
    if not project_id:
        project_id = ctx.obj.get("global_project")
        if project_id:
            return (project_id, "global")

    resolved_id, source = resolve_project(project_id)

    # If no project found and auto_init enabled, check for untracked git repo
    if not resolved_id and auto_init:
        untracked_repo = detect_untracked_repo_from_cwd()
        if untracked_repo:
            # Auto-initialize the project
            project = auto_init_if_untracked()
            if project:
                click.echo(f"Auto-initialized project '{project.id}' from git repo", err=True)
                return (project.id, "auto-init")

    # Show which project was auto-detected (text mode only, to stderr)
    if resolved_id and source in ("cwd", "context"):
        fmt = get_format(ctx)
        if fmt == OutputFormat.TEXT:
            click.echo(f"Using project: {resolved_id} (from {source})", err=True)

    if required and not resolved_id:
        fmt = get_format(ctx)
        output_error(
            "no_project",
            "No project specified. Use --project, cd into a project, or run 'clawpm use <project>'.",
            fmt=fmt,
        )
        sys.exit(1)

    return resolved_id, source
