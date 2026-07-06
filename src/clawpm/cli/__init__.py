"""ClawPM CLI - Filesystem-first multi-project manager."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import click

from clawpm import __version__
from clawpm.concurrency import LockTimeout
from clawpm.frontmatter import parse_frontmatter
from clawpm.models import (
    ProjectStatus,
    SuccessCriterion,
    Task,
    TaskState,
    TaskComplexity,
    WorkLogAction,
    ResearchType,
    ResearchStatus,
    Predictions,
    SURPRISE_TAXONOMY,
)
from clawpm.output import (
    OutputFormat,
    output_json,
    output_error,
    output_success,
    output_projects_list,
    output_tasks_list,
    output_task_detail,
    output_worklog_entries,
    output_research_list,
    output_context,
)
from clawpm.discovery import (
    get_portfolio_path,
    load_portfolio_config,
    discover_projects,
    discover_untracked_repos,
    get_project,
    validate_portfolio,
    init_project_from_repo,
    is_git_repo,
    path_for_config,
)
from clawpm.announce import (
    AnnounceEncodingError,
    find_existing_marker_file,
    select_target_file,
    write_or_replace_stanza,
)
from clawpm.tasks import (
    list_tasks,
    get_task,
    get_next_task,
    change_task_state,
    add_task,
    edit_task,
    split_task,
    add_subtask,
    touch_task_updated,
    distinct_tags,
)
from clawpm.worklog import (
    add_entry,
    filter_files_changed,
    tail_entries,
    get_last_entry,
    get_logged_commit_hashes,
    read_entries,
)
from clawpm.research import (
    list_research,
    get_research,
    add_research,
    link_research_session,
)
from clawpm.context import (
    resolve_project,
    expand_task_id,
    get_context_project,
    set_context_project,
    detect_project_from_cwd,
    detect_untracked_repo_from_cwd,
    auto_init_if_untracked,
)


# cp1252-safe stdio (CLAWP-011): Windows consoles default to the cp1252 codec,
# which cannot encode glyphs such as U+2192 and raises UnicodeEncodeError
# mid-render. Reconfigure stdout/stderr to UTF-8 (errors="replace") so NO output
# path -- echo args, --help text, command docstrings, tabulated rows -- can
# crash, regardless of which glyph a future line introduces. This is the
# root-cause fix the encoding_check scanner recommends; it supersedes
# whack-a-mole glyph swapping. Guarded because redirected / wrapped streams
# (e.g. click's CliRunner, a closed pipe) may lack reconfigure() or reject it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError, OSError) as _stdio_exc:  # pragma: no cover
    # Don't crash at import over a display-only setting. Stay SILENT by default:
    # the common failure is a benign wrapped/redirected stream (CliRunner, a
    # pipe) that legitimately lacks reconfigure(), and surfacing it would
    # false-alarm on every piped run. Under CLAWPM_DEBUG, leave a breadcrumb so a
    # genuine cp1252 console that refused UTF-8 is debuggable, not a mystery
    # UnicodeEncodeError three lines later (fail-open != fail-silent).
    if os.environ.get("CLAWPM_DEBUG"):
        sys.stderr.write(
            f"clawpm: {__name__} stdio reconfigure to utf-8 failed "
            f"({_stdio_exc!r}); non-ASCII output may crash on a cp1252 console\n"
        )


from clawpm.cli.base import (
    main,
    _mutation_errors,
    get_format,
    require_portfolio,
    require_project,
    _read_patterns_file,
    pass_format,
    _FALLBACK_POLICIES,
)

# --- group module registrations (import each for its command-registration side effect) ---
from clawpm.cli import agent as _agent  # noqa: F401 (registers commands)
from clawpm.cli import hook as _hook  # noqa: F401 (registers commands)
from clawpm.cli import judge as _judge  # noqa: F401 (registers commands)
from clawpm.cli import research as _research  # noqa: F401 (registers commands)
from clawpm.cli import mission as _mission  # noqa: F401 (registers commands)
from clawpm.cli import lease as _lease  # noqa: F401 (registers commands)
from clawpm.cli import issues as _issues  # noqa: F401 (registers commands)
from clawpm.cli import conflicts as _conflicts  # noqa: F401 (registers commands)
from clawpm.cli import inbox as _inbox  # noqa: F401 (registers commands)
from clawpm.cli import constitution as _constitution  # noqa: F401 (registers commands)
from clawpm.cli import serve as _serve  # noqa: F401 (registers commands)
from clawpm.cli import reflect as _reflect  # noqa: F401 (registers commands)
from clawpm.cli import log as _log  # noqa: F401 (registers commands)
from clawpm.cli import tasks as _tasks  # noqa: F401 (registers commands)
from clawpm.cli import projects as _projects  # noqa: F401 (registers commands)
from clawpm.cli import shortcuts as _shortcuts  # noqa: F401 (registers commands)
from clawpm.cli import project as _project  # noqa: F401 (registers commands)

# Re-exports: symbols that moved into group modules but are still referenced
# via the historical `clawpm.cli.<name>` path (by the domain layer and tests).
from clawpm.cli.conflicts import _globs_overlap  # noqa: F401
from clawpm.cli.serve import _load_web_server  # noqa: F401

# project_doctor (the `project doctor` subcommand, now in cli.project) is
# invoked by the top-level `doctor` command, still defined in this module until
# the setup/version/doctor group is extracted.
from clawpm.cli.project import project_doctor  # noqa: F401

# ============================================================================
# Use command (project context)
# ============================================================================


@main.command("use")
@click.argument("project_id", required=False)
@click.option("--clear", is_flag=True, help="Clear the current context")
@click.pass_context
def use_project(ctx: click.Context, project_id: str | None, clear: bool) -> None:
    """Set or show the current project context.
    
    When no project is specified, shows the current context.
    Use --clear to remove the context.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    if clear:
        set_context_project(None)
        output_success("Context cleared", fmt=fmt)
        return
    
    if project_id:
        # Verify project exists
        proj = get_project(config, project_id)
        if not proj:
            output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
            sys.exit(1)
        
        set_context_project(project_id)
        output_success(f"Now using project: {proj.name} ({proj.id})", fmt=fmt)
    else:
        # Show current context
        current = get_context_project()
        cwd_project = detect_project_from_cwd()
        
        result = {
            "context_project": current,
            "cwd_project": cwd_project.id if cwd_project else None,
            "effective": cwd_project.id if cwd_project else current,
        }
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            if cwd_project:
                click.echo(f"Current directory: {cwd_project.name} ({cwd_project.id})")
            elif current:
                click.echo(f"Context: {current}")
            else:
                click.echo("No project context set. Use 'clawpm use <project>' or cd into a project.")


# ============================================================================
# Resume command (Claude-rendered 2-paragraph session briefing)
# ============================================================================


@main.command("resume")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--no-cache", is_flag=True, default=False, help="Bypass the 60-second briefing cache")
@click.pass_context
def resume_cmd(ctx: click.Context, project_id: str | None, no_cache: bool) -> None:
    """Render a 2-paragraph session-resume briefing (CLAWP-025).

    Gathers branch, in-progress task, recent commits, work_log tail, and
    reflection events for the project, then asks the same subprocess judge
    used by the Stop-hook for a tight where-you-are / what's-next summary.
    Falls back to a structured signals summary when the judge isn't on PATH.
    """
    from clawpm.resume import render_briefing

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    resolved_id, _ = require_project(ctx, project_id)

    proj = get_project(config, resolved_id)
    if not proj:
        output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
        sys.exit(1)

    briefing, status = render_briefing(
        config,
        resolved_id,
        use_cache=not no_cache,
    )

    if fmt == OutputFormat.JSON:
        payload = {
            "status": status,
            "project_id": resolved_id,
            "briefing": briefing,
        }
        if status == "degraded":
            payload["warning"] = (
                "resume judge unavailable (claude CLI not on PATH or failed); "
                "returned structured signals summary instead"
            )
        output_json(payload)
    else:
        if status == "degraded":
            click.echo(
                "[warning] resume judge unavailable - showing signals summary",
                err=True,
            )
        elif status == "cached":
            click.echo("[cached]", err=True)
        click.echo(briefing)


# ============================================================================
# Setup commands
# ============================================================================


@main.command("setup")
@click.option("--check", is_flag=True, help="Check installation status")
@click.pass_context
def setup(ctx: click.Context, check: bool) -> None:
    """Setup or verify ClawPM installation."""
    fmt = get_format(ctx)

    if check:
        issues: list[str] = []

        # Check portfolio path (defaults to ~/clawpm)
        portfolio_path = get_portfolio_path()
        if not portfolio_path:
            issues.append("No portfolio found at ~/clawpm (or set CLAWPM_PORTFOLIO env var)")
        else:
            if not (portfolio_path / "work_log.jsonl").exists():
                issues.append(f"work_log.jsonl not found in {portfolio_path}")

        # Check portfolio config
        config = load_portfolio_config()
        if config:
            portfolio_issues = validate_portfolio(config)
            issues.extend(portfolio_issues)

        if fmt == OutputFormat.JSON:
            output_json({
                "status": "ok" if not issues else "issues",
                "portfolio_path": str(portfolio_path) if portfolio_path else None,
                "issues": issues,
            })
        else:
            if issues:
                click.echo("Issues found:")
                for issue in issues:
                    click.echo(f"  - {issue}")
            else:
                click.echo("[OK] ClawPM is properly configured")
                if portfolio_path:
                    click.echo(f"  Portfolio: {portfolio_path}")
    else:
        # Determine portfolio root
        env_portfolio = os.environ.get("CLAWPM_PORTFOLIO")
        if env_portfolio:
            portfolio_root = Path(env_portfolio).expanduser()
        else:
            portfolio_root = Path.home() / "clawpm"

        # Check if already set up
        if (portfolio_root / "portfolio.toml").exists():
            output_success(f"Already set up at {portfolio_root}", fmt=fmt)
            return

        # Create directory structure
        created: list[str] = []

        portfolio_root.mkdir(parents=True, exist_ok=True)
        created.append(str(portfolio_root))

        projects_dir = portfolio_root / "projects"
        projects_dir.mkdir(exist_ok=True)
        created.append(str(projects_dir))

        # Create portfolio.toml
        portfolio_toml = portfolio_root / "portfolio.toml"
        root_str = path_for_config(portfolio_root)
        projects_str = path_for_config(projects_dir)
        portfolio_toml.write_text(f'''# ClawPM Portfolio Configuration

portfolio_root = "{root_str}"

project_roots = [
    "{projects_str}"
]

[defaults]
status = "active"
''', encoding="utf-8")
        created.append(str(portfolio_toml))

        # Create empty work log
        work_log = portfolio_root / "work_log.jsonl"
        if not work_log.exists():
            work_log.touch()
            created.append(str(work_log))

        if fmt == OutputFormat.JSON:
            output_json({
                "status": "created",
                "portfolio_root": str(portfolio_root),
                "created": created,
            })
        else:
            click.echo(f"Portfolio created at {portfolio_root}")
            click.echo(f"  projects/       - clone or init repos here")
            click.echo(f"  portfolio.toml  - configuration")
            click.echo(f"  work_log.jsonl  - activity log")
            click.echo(f"\nNext: cd into a git repo and run 'clawpm add \"First task\"'")


@main.command("version")
@click.pass_context
def version(ctx: click.Context) -> None:
    """Show version."""
    fmt = get_format(ctx)
    if fmt == OutputFormat.JSON:
        output_json({"version": __version__})
    else:
        click.echo(f"clawpm {__version__}")


@main.command("doctor")
@click.option("--project", "-p", "project_id", help="Check specific project (default: whole portfolio)")
@click.option("--strict", is_flag=True, help="Exit non-zero if any warning is present (useful for CI)")
@click.option(
    "--commits-drift-threshold",
    type=int,
    default=5,
    show_default=True,
    help="Warn when project HEAD has >N commits authored after last work_log entry.",
)
@click.option(
    "--check-codex",
    is_flag=True,
    help="Network-backed check: scan last 5 closed PRs per github-remote project for Codex-bot presence. Off by default.",
)
@click.option("--apply", "apply_mode", is_flag=True, help="Run deterministic auto-remediation arms after detection (CLAWP-026).")
@click.option("--yes", "assume_yes", is_flag=True, help="Non-interactive mode for --apply.")
@click.option("--dry-run", "dry_run", is_flag=True, help="With --apply, report would-do actions without modifying state.")
@click.option("--no-apply-drift", "no_apply_drift", is_flag=True, help="Disable drift state-mismatch arm.")
@click.option("--no-apply-cascade", "no_apply_cascade", is_flag=True, help="Disable stale-blocked cascade arm.")
@click.option("--no-apply-stale-blocked", "no_apply_stale_blocked", is_flag=True, help="Alias for --no-apply-cascade.")
@click.option("--no-apply-half-rename", "no_apply_half_rename", is_flag=True, help="Disable drift half-rename arm.")
@click.option(
    "--check-encoding",
    is_flag=True,
    help="AST-scan tracked .py files for cp1252-risk patterns (non-ASCII in print/echo, file ops without encoding=, modules with print but no stdout reconfigure). Off by default.",
)
@click.pass_context
def doctor(
    ctx: click.Context,
    project_id: str | None,
    strict: bool,
    commits_drift_threshold: int,
    check_codex: bool,
    apply_mode: bool = False,
    assume_yes: bool = False,
    dry_run: bool = False,
    no_apply_drift: bool = False,
    no_apply_cascade: bool = False,
    no_apply_stale_blocked: bool = False,
    no_apply_half_rename: bool = False,
    check_encoding: bool = False,
) -> None:
    """Run full health check."""
    # Delegate to project doctor; project_id=None checks the whole portfolio.
    ctx.invoke(
        project_doctor,
        project_id=project_id,
        strict=strict,
        commits_drift_threshold=commits_drift_threshold,
        check_codex=check_codex,
        apply_mode=apply_mode,
        assume_yes=assume_yes,
        dry_run=dry_run,
        no_apply_drift=no_apply_drift,
        no_apply_cascade=no_apply_cascade,
        no_apply_stale_blocked=no_apply_stale_blocked,
        no_apply_half_rename=no_apply_half_rename,
        check_encoding=check_encoding,
    )

