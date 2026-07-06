from __future__ import annotations

import os
from pathlib import Path

import click

from clawpm import __version__
from clawpm.output import OutputFormat, output_json, output_success
from clawpm.discovery import get_portfolio_path, load_portfolio_config, path_for_config, validate_portfolio
from clawpm.cli.project import project_doctor
from clawpm.cli.base import main, get_format

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
