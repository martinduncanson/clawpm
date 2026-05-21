"""ClawPM CLI - Filesystem-first multi-project manager."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import click

from . import __version__
from .models import (
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
from .output import (
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
from .discovery import (
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
from .announce import (
    AnnounceEncodingError,
    find_existing_marker_file,
    select_target_file,
    write_or_replace_stanza,
)
from .tasks import (
    list_tasks,
    get_task,
    get_next_task,
    change_task_state,
    add_task,
    edit_task,
    split_task,
    add_subtask,
)
from .worklog import (
    add_entry,
    filter_files_changed,
    tail_entries,
    get_last_entry,
    get_logged_commit_hashes,
    read_entries,
)
from .research import (
    list_research,
    get_research,
    add_research,
    link_research_session,
)
from .context import (
    resolve_project,
    expand_task_id,
    get_context_project,
    set_context_project,
    detect_project_from_cwd,
    detect_untracked_repo_from_cwd,
    auto_init_if_untracked,
)


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
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, format: str, global_project: str | None) -> None:
    """ClawPM - Filesystem-first multi-project manager."""
    ctx.ensure_object(dict)
    ctx.obj["format"] = OutputFormat(format)
    ctx.obj["global_project"] = global_project


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
# Projects commands
# ============================================================================


@main.group()
def projects() -> None:
    """Manage projects."""
    pass


@projects.command("list")
@click.option(
    "--filter", "-f", "status_filter",
    type=click.Choice(["active", "paused", "archived"]),
    default=None,
    help="Filter by status",
)
@click.option("--all", "-a", "show_all", is_flag=True, help="Include untracked git repos")
@click.pass_context
def projects_list(ctx: click.Context, status_filter: str | None, show_all: bool) -> None:
    """List all projects (use --all to include untracked git repos)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    status = ProjectStatus(status_filter) if status_filter else None
    projects_found = discover_projects(config, status_filter=status)

    if show_all or fmt == OutputFormat.JSON:
        untracked = discover_untracked_repos(config)
    else:
        untracked = []
    
    if fmt == OutputFormat.JSON:
        result = {
            "projects": [p.to_dict() for p in projects_found],
            "untracked": [r.to_dict() for r in untracked],
        }
        output_json(result)
    else:
        # Collect task counts for text output
        task_counts = {}
        for proj in projects_found:
            counts = {}
            for state_name, state_val in [("open", TaskState.OPEN), ("progress", TaskState.PROGRESS), ("blocked", TaskState.BLOCKED)]:
                count = len(list_tasks(config, proj.id, state_filter=state_val))
                if count:
                    counts[state_name] = count
            task_counts[proj.id] = counts

        output_projects_list(projects_found, fmt=fmt, task_counts=task_counts)

        if untracked:
            click.echo("\nUntracked git repos (use 'clawpm project init' to add):")
            for repo in untracked:
                remote_hint = f" ({repo.remote.split('/')[-1].replace('.git', '')})" if repo.remote else ""
                # ASCII bullet only — Windows cp1252 stdout cannot encode U+25CB and crashes the run.
                click.echo(f"  - {repo.name}{remote_hint}")


@projects.command("next")
@click.pass_context
def projects_next(ctx: click.Context) -> None:
    """Get the next task across all active projects."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Get all active projects
    active_projects = discover_projects(config, status_filter=ProjectStatus.ACTIVE)

    # Find next task across all projects
    best_task = None
    best_project = None

    for project in active_projects:
        task = get_next_task(config, project.id)
        if task:
            if best_task is None or (project.priority, task.priority) < (best_project.priority, best_task.priority):
                best_task = task
                best_project = project

    if best_task and best_project:
        result = {
            "project": {
                "id": best_project.id,
                "name": best_project.name,
                "priority": best_project.priority,
            },
            "task": best_task.to_dict(),
        }
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            output_task_detail(best_task, fmt=fmt)
            click.echo(f"\nProject: {best_project.name} ({best_project.id})")
    else:
        if fmt == OutputFormat.JSON:
            output_json({"project": None, "task": None, "message": "No tasks available"})
        else:
            click.echo("No tasks available across active projects.")


# ============================================================================
# Project commands (singular)
# ============================================================================


@main.group()
def project() -> None:
    """Manage a single project."""
    pass


@project.command("context")
@click.argument("project_id", required=False)
@click.pass_context
def project_context(ctx: click.Context, project_id: str | None) -> None:
    """Get full context for a project (alias for top-level 'context')."""
    ctx.invoke(agent_context, project_id=project_id)


@project.command("init")
@click.option("--in-repo", "-r", "repo_path", type=click.Path(exists=True), default=".", help="Repository path")
@click.option("--id", "project_id", help="Project ID (defaults to directory name)")
@click.option("--name", "project_name", help="Project name")
@click.pass_context
def project_init(ctx: click.Context, repo_path: str, project_id: str | None, project_name: str | None) -> None:
    """Initialize a new project in a repository."""
    fmt = get_format(ctx)

    repo = Path(repo_path).resolve()
    project_dir = repo / ".project"

    if project_dir.exists():
        output_error("project_exists", f"Project already exists at {project_dir} (repo: {repo})", fmt=fmt)
        sys.exit(1)

    # Generate defaults
    if not project_id:
        project_id = repo.name.lower().replace(" ", "-").replace("_", "-")

    if not project_name:
        project_name = repo.name

    # Create structure
    project_dir.mkdir(parents=True)
    (project_dir / "tasks").mkdir()
    (project_dir / "tasks" / "done").mkdir()
    (project_dir / "tasks" / "blocked").mkdir()
    (project_dir / "research").mkdir()
    (project_dir / "notes").mkdir()

    # Create settings.toml
    repo_path_str = path_for_config(repo)
    settings_content = f'''id = "{project_id}"
name = "{project_name}"
status = "active"
priority = 5
repo_path = "{repo_path_str}"
labels = []
'''
    (project_dir / "settings.toml").write_text(settings_content, encoding="utf-8")

    # Create SPEC.md template
    spec_content = f"""# {project_name}

## Overview

(Describe the project here)

## Goals

- Goal 1
- Goal 2

## Non-Goals

- Non-goal 1

## Technical Notes

...
"""
    (project_dir / "SPEC.md").write_text(spec_content, encoding="utf-8")

    # Create learnings.md
    (project_dir / "learnings.md").write_text(f"# {project_name} Learnings\n\n", encoding="utf-8")

    # Phase 1.8: announce the project as a clawpm-tracked repo so future agents
    # see "use clawpm" in the first agent-doc they read.
    try:
        target_file, action = write_or_replace_stanza(repo, project_id, project_name)
        announce_msg = f"; announce {action} in {target_file.name}"
    except AnnounceEncodingError as exc:
        announce_msg = f"; announce skipped (target is not UTF-8: {exc})"
    except OSError as exc:
        announce_msg = f"; announce skipped ({exc})"

    output_success(f"Project initialized at {project_dir}{announce_msg}", fmt=fmt)


@project.command("announce")
@click.option("-p", "--project", "project_id", help="Project ID (auto-detected if not specified)")
@click.pass_context
def project_announce(ctx: click.Context, project_id: str | None) -> None:
    """Write or refresh the 'this project uses clawpm' stanza in the repo's
    agent-facing docs (CLAUDE.md > AGENTS.md > README.md, first-found wins).

    Idempotent — if the marker block already exists, it is replaced in place.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    resolved_id, _ = require_project(ctx, project_id, auto_init=False)
    proj = get_project(config, resolved_id)
    if proj is None:
        output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
        sys.exit(1)

    repo = proj.repo_path or (proj.project_dir.parent if proj.project_dir else None)
    if not repo or not repo.exists():
        output_error(
            "no_repo_path",
            f"Project '{resolved_id}' has no usable repo_path. Set 'repo_path' in settings.toml.",
            fmt=fmt,
        )
        sys.exit(1)

    try:
        target_file, action = write_or_replace_stanza(repo, proj.id, proj.name)
    except AnnounceEncodingError as exc:
        output_error(
            "announce_target_not_utf8",
            f"Refusing to rewrite a non-UTF-8 target. {exc} "
            f"Re-save the file as UTF-8 (e.g. open in your editor, save with UTF-8 encoding), "
            f"then re-run announce.",
            fmt=fmt,
        )
        sys.exit(1)
    except OSError as exc:
        output_error("announce_failed", f"Failed to write announce stanza: {exc}", fmt=fmt)
        sys.exit(1)

    if fmt == OutputFormat.JSON:
        output_json({
            "status": "ok",
            "action": action,
            "file": target_file.as_posix(),
            "project_id": proj.id,
        })
    else:
        click.echo(f"[OK] announce {action} in {target_file.as_posix()}")


@project.command("doctor")
@click.option("--project", "-p", "project_id", help="Check specific project")
@click.option("--strict", is_flag=True, help="Exit non-zero if any warning is present (useful for CI)")
@click.option(
    "--commits-drift-threshold",
    type=int,
    default=5,
    show_default=True,
    help="Warn when project HEAD has >N commits authored after last work_log entry (Phase 1.8 Check d).",
)
@click.option(
    "--check-codex",
    is_flag=True,
    help="Network-backed check: for each project with a github.com remote, scan the last 5 closed PRs for Codex-bot appearances. Off by default to keep doctor offline-fast.",
)
# --- CLAWP-026: --apply mode ---
@click.option(
    "--apply",
    "apply_mode",
    is_flag=True,
    help="After detecting warnings, run deterministic remediation arms (CLAWP-026). "
         "Half-rename drift, state-field drift, and stale-blocked cascades are auto-applied. "
         "Stale-tasks, prefix-collisions, unreadable-files, commit-drift, missing-markers, "
         "and codex-availability stay operator-judgment and are listed in apply_skipped[].",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Non-interactive mode: skip confirmation prompts when --apply is set.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    help="With --apply, populate the applied[] array with `would-...` results but do not "
         "modify the filesystem or run cascades.",
)
@click.option(
    "--no-apply-drift",
    "no_apply_drift",
    is_flag=True,
    help="Disable the drift state-field-mismatch arm of --apply.",
)
@click.option(
    "--no-apply-cascade",
    "no_apply_cascade",
    is_flag=True,
    help="Disable the stale-blocked cascade arm of --apply (alias of --no-apply-stale-blocked).",
)
@click.option(
    "--no-apply-stale-blocked",
    "no_apply_stale_blocked",
    is_flag=True,
    help="Disable the stale-blocked cascade arm of --apply (alias of --no-apply-cascade).",
)
@click.option(
    "--no-apply-half-rename",
    "no_apply_half_rename",
    is_flag=True,
    help="Disable the drift half-rename arm of --apply.",
)
@click.option(
    "--check-encoding",
    is_flag=True,
    help="AST-scan tracked .py files for cp1252-risk patterns: non-ASCII literals in print/click.echo, file ops without encoding= kwarg, modules with print/echo but no stdout reconfigure. Off by default — opt-in for Windows-targeting codebases (CLAWP-011).",
)
@click.pass_context
def project_doctor(
    ctx: click.Context,
    project_id: str | None,
    strict: bool = False,
    commits_drift_threshold: int = 5,
    check_codex: bool = False,
    apply_mode: bool = False,
    assume_yes: bool = False,
    dry_run: bool = False,
    no_apply_drift: bool = False,
    no_apply_cascade: bool = False,
    no_apply_stale_blocked: bool = False,
    no_apply_half_rename: bool = False,
    check_encoding: bool = False,
) -> None:
    """Check for issues with projects and portfolio.

    Phase 1.6 checks added:
    - Stale tasks (progress state, not touched in >7 days)
    - Filesystem-vs-state drift (file location vs frontmatter state field)
    - Cross-project prefix collisions (two projects sharing first-5 chars of ID)

    Phase 1.8 checks added:
    - Code-vs-tracking drift (commits authored after the last work_log entry)
    - Missing clawpm-requirement marker in repo agent docs (CLAUDE.md/AGENTS.md/README.md)
    """
    import json as _json_doc
    from datetime import datetime, timezone, timedelta

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    issues: list[dict] = []
    stale_tasks: list[dict] = []
    stale_blocked: list[dict] = []
    drift_tasks: list[dict] = []
    unreadable_files: list[dict] = []
    commit_drift: list[dict] = []
    missing_markers: list[dict] = []
    codex_availability: list[dict] = []
    codegraph_advice: list[dict] = []
    encoding_risks: list[dict] = []

    STALE_DAYS = 7
    CODEGRAPH_FILE_THRESHOLD = 50  # below this we don't bother advising

    # Validate portfolio
    portfolio_issues = validate_portfolio(config)
    for issue in portfolio_issues:
        issues.append({"level": "error", "scope": "portfolio", "message": issue})

    # Check projects
    projects_to_check = []
    if project_id:
        proj = get_project(config, project_id)
        if proj:
            projects_to_check = [proj]
        else:
            issues.append({
                "level": "error",
                "scope": "project",
                "project": project_id,
                "message": f"Project not found: {project_id}",
            })
    else:
        projects_to_check = discover_projects(config)

    for proj in projects_to_check:
        if not proj.project_dir:
            continue

        project_path = proj.project_dir / ".project"

        # Check for required files
        if not (project_path / "settings.toml").exists():
            issues.append({
                "level": "error",
                "scope": "project",
                "project": proj.id,
                "message": "Missing settings.toml",
            })

        # Check tasks directory
        tasks_dir = project_path / "tasks"
        if not tasks_dir.exists():
            issues.append({
                "level": "warning",
                "scope": "project",
                "project": proj.id,
                "message": "Missing tasks directory",
            })
            continue

        # Check for broken repo_path
        if proj.repo_path and not proj.repo_path.exists():
            issues.append({
                "level": "warning",
                "scope": "project",
                "project": proj.id,
                "message": f"repo_path does not exist: {proj.repo_path}",
            })

        # --- Phase 1.6 Check a: Stale tasks ---
        # Scan .progress.md files; flag if mtime > STALE_DAYS ago
        now_utc = datetime.now(timezone.utc)
        cutoff = now_utc - timedelta(days=STALE_DAYS)
        for progress_file in tasks_dir.glob("*.progress.md"):
            try:
                mtime = datetime.fromtimestamp(
                    progress_file.stat().st_mtime, tz=timezone.utc
                )
            except OSError:
                continue
            # Also check work_log for the most recent entry for this task
            task_id_stem = progress_file.name.replace(".progress.md", "")
            last_touched = mtime
            # Read work_log entries for this task to find more recent touch
            work_log_path = config.portfolio_root / "work_log.jsonl"
            if work_log_path.exists():
                for line in work_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = _json_doc.loads(line)
                        if entry.get("task") == task_id_stem:
                            ts_str = entry.get("ts", "")
                            try:
                                ts = datetime.fromisoformat(ts_str.rstrip("Z"))
                                if ts.tzinfo is None:
                                    ts = ts.replace(tzinfo=timezone.utc)
                                if ts > last_touched:
                                    last_touched = ts
                            except (ValueError, AttributeError):
                                pass
                    except _json_doc.JSONDecodeError:
                        pass
            if last_touched < cutoff:
                days_stale = (now_utc - last_touched).days
                stale_tasks.append({
                    "task_id": task_id_stem,
                    "project_id": proj.id,
                    "last_touched": last_touched.isoformat().replace("+00:00", "Z"),
                    "days_stale": days_stale,
                    "suggested_action": (
                        "Move to blocked with reason, or done if work was abandoned, "
                        "or update if still active"
                    ),
                })

        # --- Phase 1.6 Check b: Filesystem-vs-state drift ---
        # Walk tasks/, tasks/done/, tasks/blocked/ and compare location-derived
        # state against frontmatter 'state' field (if present).
        # Also flag progress.md without matching base file OR base file without progress.
        all_md_files: list[Path] = []
        for md_file in tasks_dir.glob("*.md"):
            all_md_files.append(md_file)
        for md_file in (tasks_dir / "done").glob("*.md"):
            all_md_files.append(md_file)
        for md_file in (tasks_dir / "blocked").glob("*.md"):
            all_md_files.append(md_file)

        for md_file in all_md_files:
            # Derive expected state from location
            parts = md_file.parts
            if "done" in parts:
                location_state = "done"
            elif "blocked" in parts:
                location_state = "blocked"
            elif ".progress" in md_file.name:
                location_state = "progress"
            else:
                location_state = "open"

            # Read frontmatter state (if present).
            # Foreign-source markdown (other projects' notes) may contain non-UTF-8
            # bytes (cp1252 smart-quotes/em-dashes are the common offender on Windows).
            # Use errors="replace" so a stray byte doesn't abort the whole doctor run,
            # and record the file in unreadable_files so the operator can clean it up.
            try:
                raw = md_file.read_bytes()
            except OSError:
                continue
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                unreadable_files.append({
                    "file": md_file.as_posix(),
                    "project_id": proj.id,
                    "encoding_hint": "utf-8",
                    "error": f"{exc.reason} at byte {exc.start}",
                })
                text = raw.decode("utf-8", errors="replace")
            fm_state: str | None = None
            if text.startswith("---"):
                import yaml as _yaml
                parts_split = text.split("---", 2)
                if len(parts_split) >= 3:
                    try:
                        fm = _yaml.safe_load(parts_split[1]) or {}
                        fm_state = fm.get("state")
                    except Exception:
                        pass

            if fm_state and fm_state != location_state:
                drift_tasks.append({
                    "file": md_file.as_posix(),
                    "project_id": proj.id,
                    "location_state": location_state,
                    "frontmatter_state": fm_state,
                    "issue": "state_mismatch",
                })

        # Check for half-renames: .progress.md without matching base .md (and vice versa)
        # A .progress.md at tasks/ root should have a corresponding task file
        # (but the .progress.md IS the task file for in-progress tasks — no dual file needed).
        # The real half-rename is: tasks/PROJ-001.md AND tasks/PROJ-001.progress.md both exist.
        base_names = {
            f.name for f in tasks_dir.glob("*.md")
            if ".progress" not in f.name
        }
        progress_names = {
            f.name.replace(".progress.md", "") for f in tasks_dir.glob("*.progress.md")
        }
        half_renamed = base_names & {p + ".md" for p in progress_names}
        for half in sorted(half_renamed):
            stem = half.replace(".md", "")
            drift_tasks.append({
                "file": (tasks_dir / half).as_posix(),
                "project_id": proj.id,
                "issue": "half_rename",
                "detail": f"Both {stem}.md and {stem}.progress.md exist — likely incomplete git mv",
            })

        # --- Cascade health check: stale-blocked tasks whose deps are all done.
        # The auto-cascade in tasks_state catches new transitions; this check
        # catches the historical backlog of tasks blocked before cascade landed.
        STALE_BLOCKED_HOURS = 24
        blocked_dir = tasks_dir / "blocked"
        if blocked_dir.exists():
            tasks_for_lookup = list_tasks(config, proj.id)
            by_id = {t.id: t for t in tasks_for_lookup}
            cutoff_blocked = now_utc - timedelta(hours=STALE_BLOCKED_HOURS)
            for blocked_file in blocked_dir.glob("*.md"):
                try:
                    bt = Task.from_file(blocked_file)
                except (OSError, UnicodeDecodeError) as exc:
                    # Surface unreadable files via the existing doctor
                    # channel rather than silently skipping them.
                    unreadable_files.append({
                        "file": blocked_file.as_posix(),
                        "project_id": proj.id,
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
                if not bt.depends:
                    continue
                # All deps resolved to done?
                deps_resolved = True
                missing_deps: list[str] = []
                for dep_id in bt.depends:
                    dep = by_id.get(dep_id)
                    if dep is None:
                        missing_deps.append(dep_id)
                        continue
                    if dep.state != TaskState.DONE:
                        deps_resolved = False
                        break
                if not deps_resolved:
                    continue
                try:
                    btime = datetime.fromtimestamp(
                        blocked_file.stat().st_mtime, tz=timezone.utc
                    )
                except OSError:
                    continue
                if btime > cutoff_blocked:
                    continue
                stale_blocked.append({
                    "task_id": bt.id,
                    "project_id": proj.id,
                    "blocked_since": btime.isoformat().replace("+00:00", "Z"),
                    "deps": bt.depends,
                    "missing_deps": missing_deps,
                    "suggested_action": (
                        "All deps are done — run `clawpm unblock` to promote, "
                        "or remove stale dep refs"
                    ),
                })

    # --- Phase 1.8 Check d: Code-vs-tracking drift ---
    # For each project with a repo_path, compare the timestamp of the latest
    # work_log entry to commits authored after that time. >threshold = warn.
    # Catches the pattern where code ships but `clawpm log` never gets called.
    for proj in projects_to_check:
        if not proj.repo_path or not proj.repo_path.exists():
            continue
        # Skip if not a git repo
        if not (proj.repo_path / ".git").exists():
            continue

        last_entry = None
        try:
            last_entry = get_last_entry(config, project=proj.id)
        except Exception:
            pass

        # Resolve `since` argument for git log. Always emit an explicit UTC
        # offset: `git log --since=<iso>` interprets naive timestamps in the
        # local timezone, which would shift the drift window by hours in
        # non-UTC environments and mis-count commits at the boundary.
        if last_entry is not None and getattr(last_entry, "ts", None):
            ts = last_entry.ts
            if hasattr(ts, "tzinfo") and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            since_arg = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
            log_status = "logged"
        else:
            # No work_log entries ever — count commits in the entire history.
            since_arg = None
            log_status = "never_logged"

        try:
            cmd = ["git", "log", "--oneline"]
            if since_arg:
                cmd.append(f"--since={since_arg}")
            result = subprocess.run(
                cmd,
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode != 0:
            continue
        commit_count = sum(1 for line in result.stdout.splitlines() if line.strip())
        if commit_count > commits_drift_threshold:
            commit_drift.append({
                "project_id": proj.id,
                "repo_path": proj.repo_path.as_posix(),
                "commits_since_last_log": commit_count,
                "last_log_ts": since_arg,
                "log_status": log_status,
                "suggested_action": (
                    "Run 'clawpm log' to capture recent work, or close stale tasks "
                    "in clawpm to acknowledge the drift."
                ),
            })

    # --- Phase 1.8 Check e: Missing clawpm-requirement marker in agent docs ---
    # Every clawpm-tracked repo should announce itself in CLAUDE.md/AGENTS.md/README.md
    # so future agents see "use clawpm" before improvising. `clawpm project announce`
    # writes the marker; this check surfaces repos where it was never run.
    for proj in projects_to_check:
        if not proj.repo_path or not proj.repo_path.exists():
            continue
        marker_file = find_existing_marker_file(proj.repo_path)
        if marker_file is None:
            missing_markers.append({
                "project_id": proj.id,
                "repo_path": proj.repo_path.as_posix(),
                "suggested_action": f"Run 'clawpm project announce --project {proj.id}' from the repo.",
            })

    # --- Phase 1.9 Check f (CLAWP-008): Codex availability heuristic ---
    # Off by default (--check-codex flag) — it makes network calls to the GitHub API
    # and would otherwise slow doctor down + require gh auth in every environment.
    # Walks the last 5 closed PRs per project with a github.com remote, looks for
    # any author login containing 'codex' (case-insensitive). Surfaces missing.
    if check_codex:
        from clawpm.codex_check import check_codex_availability
        for proj in projects_to_check:
            if not proj.repo_path:
                continue
            warning = check_codex_availability(proj.repo_path)
            if warning is not None:
                codex_availability.append({"project_id": proj.id, **warning})

    # --- CLAWP-031: CodeGraph advisory ---
    # Soft signal (NOT a warning). For projects with substantial code
    # but no .codegraph/, surface that `codegraph init` would speed up
    # subsequent agent operations. Operator-judgment class — never
    # auto-applied (codegraph creates `.claude/CLAUDE.md` which the
    # operator may not want).
    from .codegraph import count_code_files, is_project_indexed
    for proj in projects_to_check:
        if not proj.repo_path or not proj.repo_path.exists():
            continue
        if is_project_indexed(proj.repo_path):
            continue
        code_count = count_code_files(proj.repo_path)
        if code_count >= CODEGRAPH_FILE_THRESHOLD:
            codegraph_advice.append({
                "project_id": proj.id,
                "repo_path": proj.repo_path.as_posix(),
                "code_files": code_count,
                "suggested_action": (
                    f"Code-bearing project ({code_count} files) without "
                    f"CodeGraph. Run `codegraph init -i` from {proj.repo_path.as_posix()} "
                    "to add ~35% cheaper / ~70% fewer-tool-call code "
                    "exploration for any agent using this project."
                ),
            })

    # --- Phase 1.9 Check g (CLAWP-011): Encoding-risk AST scan ---
    # Off by default (--check-encoding flag). AST-scans each project's .py files
    # for the three cp1252-risk patterns documented in
    # feedback-windows-cp1252-write-text.md. Tooling-rule escalation after 6
    # confirmed incidents in 4 weeks.
    if check_encoding:
        from clawpm.encoding_check import scan_path
        for proj in projects_to_check:
            if not proj.repo_path or not proj.repo_path.exists():
                continue
            findings = scan_path(proj.repo_path)
            for finding in findings:
                encoding_risks.append({"project_id": proj.id, **finding})

    # --- Phase 1.6 Check c: Cross-project prefix collisions ---
    # Prefix = project_id.upper()[:5] (mirrors add_task logic)
    prefix_map: dict[str, list[str]] = {}
    all_projects = discover_projects(config)
    for proj in all_projects:
        prefix = proj.id.upper()[:5]
        prefix_map.setdefault(prefix, []).append(proj.id)
    prefix_collisions = [
        {"prefix": pfx, "projects": pids}
        for pfx, pids in prefix_map.items()
        if len(pids) > 1
    ]

    # Build final output
    has_warnings = bool(
        stale_tasks or stale_blocked or drift_tasks or prefix_collisions or unreadable_files
        or commit_drift or missing_markers or codex_availability or encoding_risks
        or any(i["level"] == "warning" for i in issues)
    )

    # --- CLAWP-026: --apply phase ---
    applied: list[dict] = []
    apply_skipped: list[dict] = []
    apply_aborted = False
    if apply_mode:
        # Confirmation gate for interactive (non --yes) runs that will actually
        # mutate state. Dry-run never prompts because nothing is at stake.
        proceed = True
        if has_warnings and not assume_yes and not dry_run:
            proceed = click.confirm(
                "Apply auto-remediation arms to the warnings above?",
                default=False,
            )
            if not proceed:
                apply_aborted = True

        if proceed:
            from .doctor_apply import run_apply_phase

            applied, apply_skipped = run_apply_phase(
                config=config,
                drift_tasks=drift_tasks,
                stale_blocked=stale_blocked,
                stale_tasks=stale_tasks,
                prefix_collisions=prefix_collisions,
                unreadable_files=unreadable_files,
                commit_drift=commit_drift,
                missing_markers=missing_markers,
                codex_availability=codex_availability,
                apply_drift_flag=not no_apply_drift,
                apply_cascade_flag=not no_apply_cascade,
                apply_stale_blocked_flag=not no_apply_stale_blocked,
                apply_half_rename_flag=not no_apply_half_rename,
                dry_run=dry_run,
            )

    if fmt == OutputFormat.JSON:
        payload = {
            "issues": issues,
            "count": len(issues),
            "stale_tasks": stale_tasks,
            "stale_blocked": stale_blocked,
            "drift_tasks": drift_tasks,
            "prefix_collisions": prefix_collisions,
            "unreadable_files": unreadable_files,
            "commit_drift": commit_drift,
            "missing_markers": missing_markers,
            "codex_availability": codex_availability,
            "codegraph_advice": codegraph_advice,
            "encoding_risks": encoding_risks,
        }
        if apply_mode:
            payload["applied"] = applied
            payload["apply_skipped"] = apply_skipped
            payload["dry_run"] = dry_run
        output_json(payload)
    else:
        # Codex PR#9 round-3 P2: codegraph_advice must factor into the
        # "anything to show?" guard, else text-mode operators with only
        # advisories see "[OK] No issues found" and miss the advice
        # entirely. Advisories are still NOT warnings (don't trip
        # --strict / has_warnings), but they ARE worth printing.
        if not (
            issues or stale_tasks or stale_blocked or drift_tasks or prefix_collisions or unreadable_files
            or commit_drift or missing_markers or codex_availability or codegraph_advice or encoding_risks
        ):
            click.echo("[OK] No issues found")
        else:
            for issue in issues:
                scope = issue.get("project", issue["scope"])
                click.echo(f"[{issue['level'].upper()}] [{scope}] {issue['message']}")
            for st in stale_tasks:
                click.echo(
                    f"[WARNING] [stale] {st['task_id']} ({st['project_id']}) "
                    f"- {st['days_stale']} days stale. {st['suggested_action']}"
                )
            for sb in stale_blocked:
                click.echo(
                    f"[WARNING] [stale-blocked] {sb['task_id']} ({sb['project_id']}) "
                    f"- all deps done but still in blocked/. {sb['suggested_action']}"
                )
            for dt in drift_tasks:
                click.echo(f"[WARNING] [drift] {dt['file']} - {dt['issue']}")
            for pc in prefix_collisions:
                click.echo(
                    f"[WARNING] [prefix] prefix '{pc['prefix']}' shared by: "
                    + ", ".join(pc["projects"])
                )
            for uf in unreadable_files:
                click.echo(
                    f"[WARNING] [encoding] {uf['file']} ({uf['project_id']}) "
                    f"- {uf['error']}; read with errors='replace' to continue"
                )
            for cd in commit_drift:
                click.echo(
                    f"[WARNING] [commit-drift] {cd['project_id']} "
                    f"- {cd['commits_since_last_log']} commits since last work_log entry "
                    f"({cd['log_status']}). {cd['suggested_action']}"
                )
            for mm in missing_markers:
                click.echo(
                    f"[WARNING] [no-announce] {mm['project_id']} "
                    f"- no clawpm-requirement marker in CLAUDE.md/AGENTS.md/README.md. "
                    f"{mm['suggested_action']}"
                )
            for ca in codex_availability:
                click.echo(
                    f"[WARNING] [codex-availability] {ca['project_id']} ({ca['repo']}) "
                    f"- {ca['suggested_action']}"
                )
            for cg in codegraph_advice:
                click.echo(
                    f"[ADVICE] [codegraph] {cg['project_id']} "
                    f"({cg['code_files']} code files, no .codegraph/) "
                    f"- {cg['suggested_action']}"
                )
            for er in encoding_risks:
                click.echo(
                    f"[WARNING] [encoding-risk:{er['rule']}] {er['project_id']} "
                    f"{er['file']}:{er['line']} - {er['evidence']}"
                )

        if apply_mode:
            prefix = "[DRY-RUN]" if dry_run else "[APPLIED]"
            for a in applied:
                click.echo(f"{prefix} [{a['class']}] {a.get('target')} -> {a['result']}")
            for s in apply_skipped:
                click.echo(
                    f"[SKIPPED] [{s['class']}] {s.get('target')} -> {s['reason']}"
                )

    if strict and has_warnings:
        sys.exit(1)


# ============================================================================
# Tasks commands
# ============================================================================


@main.group(invoke_without_command=True)
@click.pass_context
def tasks(ctx: click.Context) -> None:
    """Manage tasks (bare 'tasks' = list open+progress+blocked)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(tasks_list)


@tasks.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option(
    "--state", "-s",
    type=click.Choice(["open", "progress", "done", "blocked", "all"]),
    default=None,
    help="Filter by state (default: all except done)",
)
@click.option("--flat", is_flag=True, help="Show flat list without hierarchy")
@click.pass_context
def tasks_list(ctx: click.Context, project_id: str | None, state: str | None, flat: bool) -> None:
    """List tasks for a project (default: open+progress+blocked, use -s all for everything)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    if state == "all":
        found_tasks = list_tasks(config, project_id, state_filter=None)
    elif state is None:
        # Default: show everything except done
        found_tasks = []
        for s in (TaskState.OPEN, TaskState.PROGRESS, TaskState.BLOCKED):
            found_tasks.extend(list_tasks(config, project_id, state_filter=s))
        found_tasks.sort(key=lambda t: (t.priority, t.id))
    else:
        found_tasks = list_tasks(config, project_id, state_filter=TaskState(state))

    output_tasks_list(found_tasks, fmt=fmt, flat=flat)


@tasks.command("show")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def tasks_show(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Show details for a specific task."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = get_task(config, project_id, task_id)
    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # Phase 1.6: surface void tag if any reflection has been voided.
    # Cross-project isolation (round-7 audit + round-8 P2 follow-up):
    # the reflection JSONL filename is keyed by task_id alone, so two
    # projects sharing a task_id share a file. Filter by project_id —
    # but treat ABSENT project_id as legacy/unscoped and matching any
    # (back-compat for void events written before project_id stamping
    # was introduced).
    import json as _json_show
    reflections_voided = False
    ref_file = config.portfolio_root / "reflections" / f"{task_id}.jsonl"
    if ref_file.exists():
        for _line in ref_file.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line:
                continue
            try:
                _rec = _json_show.loads(_line)
                if _rec.get("event") != "void":
                    continue
                rec_proj = _rec.get("project_id")
                if rec_proj is None or rec_proj == project_id:
                    reflections_voided = True
                    break
            except _json_show.JSONDecodeError:
                pass

    if fmt == OutputFormat.JSON:
        task_dict = task.to_dict()
        task_dict["reflections_voided"] = reflections_voided
        output_json(task_dict)
    else:
        output_task_detail(task, fmt=fmt)
        if reflections_voided:
            click.echo("[reflections_voided: true]")


@tasks.command("edit")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--title", "-t", help="New title")
@click.option("--priority", type=int, help="New priority (1-10)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), help="New complexity")
@click.option("--body", "-b", help="New body content (replaces description before ## sections)")
@click.option("--scope", "-s", "scope", multiple=True, help="File glob patterns claimed by this task (can specify multiple)")
@click.option("--parallel-group", "parallel_group", type=int, default=None, help="Batch ordinal for parallel dispatch (CLAWP-021). Use --clear-parallel-group to remove.")
@click.option("--clear-parallel-group", "clear_parallel_group", is_flag=True, default=False, help="Remove parallel_group from the task — opts out of batch dispatch.")
# --- Prediction flags (all optional) ---
@click.option("--predict-duration", "predict_duration", default=None, help="Predicted duration: 90, 90m, 2h, 3d, 1w")
@click.option("--predict-complexity", "predict_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="Predicted complexity")
@click.option("--predict-files-changed", "predict_files_changed", type=int, default=None, help="Predicted number of files changed")
@click.option("--predict-scope", "predict_scope", multiple=True, help="Predicted file glob scope (can specify multiple)")
@click.option("--predict-frameworks", "predict_frameworks", multiple=True, help="Predicted frameworks/libraries to touch (can specify multiple)")
@click.option("--predict-pitfalls", "predict_pitfalls", default=None, help="Anticipated problematic areas (free text)")
@click.option("--hypothesis", "hypothesis", default=None, help="Goal/hypothesis: 'if I do X, then Y will improve'")
# --- Phase 1.5 prediction flags ---
@click.option("--success-criteria", "success_criteria", multiple=True, help="Measurable success contract (repeatable, e.g. 'P95 latency <200ms')")
@click.option("--predict-approach", "predict_approach", default=None, help="Predicted architectural approach / solution pattern (1-2 sentences)")
@click.option("--unknowns", "unknowns", default=None, help="What you do NOT know going in (meta-curiosity capture)")
@click.option("--confidence", "confidence", type=int, default=None, help="Operator confidence 1-5 (1=wild guess, 5=done this before)")
@click.option("--reference-task", "reference_tasks", multiple=True, help="Prior task IDs used as reference class (repeatable)")
@click.option("--pre-mortem", "pre_mortem", default=None, help="'If this task fails, the most likely cause is...'")
@click.option("--predict-iterations", "predict_iterations", type=int, default=None, help="Predicted iterate→grade→revise cycles (CLAWP-019). Default None; 1 means 'expected to land in one pass'.")
@click.pass_context
def tasks_edit(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    title: str | None,
    priority: int | None,
    complexity: str | None,
    body: str | None,
    scope: tuple[str, ...],
    parallel_group: int | None,
    clear_parallel_group: bool,
    predict_duration: str | None,
    predict_complexity: str | None,
    predict_files_changed: int | None,
    predict_scope: tuple[str, ...],
    predict_frameworks: tuple[str, ...],
    predict_pitfalls: str | None,
    hypothesis: str | None,
    success_criteria: tuple[str, ...],
    predict_approach: str | None,
    unknowns: str | None,
    confidence: int | None,
    reference_tasks: tuple[str, ...],
    pre_mortem: str | None,
    predict_iterations: int | None,
) -> None:
    """Edit task metadata (title, priority, complexity, body, scope)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    # Validate confidence range
    if confidence is not None and not (1 <= confidence <= 5):
        output_error("bad_confidence", f"--confidence must be 1-5, got {confidence}", fmt=fmt)
        sys.exit(1)

    has_predictions = any([
        predict_duration is not None,
        predict_complexity is not None,
        predict_files_changed is not None,
        predict_scope,
        predict_frameworks,
        predict_pitfalls is not None,
        hypothesis is not None,
        success_criteria,
        predict_approach is not None,
        unknowns is not None,
        confidence is not None,
        reference_tasks,
        pre_mortem is not None,
        predict_iterations is not None,
    ])

    if not any([title, priority is not None, complexity, body, scope, has_predictions, parallel_group is not None, clear_parallel_group]):
        output_error("no_changes", "Specify at least one field to edit (--title, --priority, --complexity, --body, --scope, --parallel-group, --clear-parallel-group, or --predict-*)", fmt=fmt)
        sys.exit(1)

    if parallel_group is not None and clear_parallel_group:
        output_error("conflicting_flags", "Cannot use both --parallel-group and --clear-parallel-group", fmt=fmt)
        sys.exit(1)

    cmplx = TaskComplexity(complexity) if complexity else None
    scope_list = list(scope) if scope else None

    predictions: Predictions | None = None
    if has_predictions:
        from .reflect import parse_duration as _parse_duration
        try:
            parsed_duration = _parse_duration(predict_duration)
        except Exception as exc:
            output_error("bad_duration", str(exc), fmt=fmt)
            sys.exit(1)
        predictions = Predictions(
            duration_min=parsed_duration,
            complexity=TaskComplexity(predict_complexity) if predict_complexity else None,
            files_changed=predict_files_changed,
            files_scope=list(predict_scope),
            frameworks=list(predict_frameworks),
            pitfalls=predict_pitfalls,
            hypothesis=hypothesis,
            success_criteria=[SuccessCriterion.from_cli(s) for s in success_criteria],
            approach=predict_approach,
            unknowns=unknowns,
            confidence=confidence,
            reference_tasks=list(reference_tasks),
            pre_mortem=pre_mortem,
            predicted_iterations=predict_iterations,
        )

    # --clear-parallel-group: explicit removal. --parallel-group N: set.
    # 0 is now a valid group ordinal (sorts first); use --clear- to remove.
    task = edit_task(
        config,
        project_id,
        task_id,
        title=title,
        priority=priority,
        complexity=cmplx,
        scope=scope_list,
        body=body,
        predictions=predictions,
        parallel_group=parallel_group,
        clear_parallel_group=clear_parallel_group,
    )

    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task_id} updated", data=task.to_dict(), fmt=fmt)


@tasks.command("state")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.argument("new_state", type=click.Choice(["open", "progress", "done", "blocked"]))
@click.option("--note", "-n", help="Note about the state change")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why? (stored in reflection event)")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this? (recursive meta-loop)")
@click.option("--surprise", "surprise_tags", multiple=True, help=f"Surprise taxonomy tag (repeatable): {', '.join(sorted(['unknown_unknown', 'scope_drift', 'dependency', 'tooling_friction', 'complexity_misread', 'assumption_broke', 'external_blocker']))}")
@click.pass_context
def tasks_state(ctx: click.Context, project_id: str | None, task_id: str, new_state: str, note: str | None, force: bool, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...]) -> None:
    """Change task state."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Validate surprise taxonomy tags early (before any state mutation)
    invalid_tags = [t for t in surprise_tags if t not in SURPRISE_TAXONOMY]
    if invalid_tags:
        output_error(
            "bad_surprise_tag",
            f"Unknown surprise tag(s): {invalid_tags}. "
            f"Valid values: {sorted(SURPRISE_TAXONOMY)}",
            fmt=fmt,
        )
        sys.exit(1)

    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    state = TaskState(new_state)

    # Check for incomplete subtasks before attempting state change
    if state == TaskState.DONE and not force:
        task = get_task(config, project_id, task_id)
        if task and task.children:
            incomplete = []
            for child_id in task.children:
                child = get_task(config, project_id, child_id)
                if child and child.state != TaskState.DONE:
                    incomplete.append(f"{child_id} [{child.state.value}]")
            if incomplete:
                output_error(
                    "subtasks_incomplete",
                    f"Cannot complete {task_id} - subtasks incomplete:\n  " + "\n  ".join(incomplete) + "\nUse --force to complete anyway.",
                    fmt=fmt,
                )
                sys.exit(1)

    # Capture task predictions before state transition (needed for reflection)
    pre_transition_task = get_task(config, project_id, task_id)

    task = change_task_state(config, project_id, task_id, state, note=note, force=force)

    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # Auto-log state change
    action_map = {
        TaskState.OPEN: WorkLogAction.NOTE,
        TaskState.PROGRESS: WorkLogAction.START,
        TaskState.DONE: WorkLogAction.DONE,
        TaskState.BLOCKED: WorkLogAction.BLOCKED,
    }
    if state in action_map:
        # Auto-detect git files changed
        files_changed = None
        project = get_project(config, project_id)
        if project and project.repo_path and project.repo_path.exists():
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=project.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    raw_files = [f for f in result.stdout.strip().split('\n') if f]
                    files_changed = filter_files_changed(raw_files, project.repo_path)
            except Exception:
                pass

        summary = note if note else f"Task marked {new_state}"
        add_entry(
            config,
            project=project_id,
            action=action_map[state],
            task=task_id,
            summary=summary,
            files_changed=files_changed,
            auto=True,
        )

    # Dependency cascade: when a task hits DONE, auto-promote any blocked
    # tasks whose dep list is now satisfied. Emit one work_log entry per
    # cascaded transition so the trigger is auditable.
    cascade_results: list[dict] = []
    cascade_errors: list[dict] = []
    teardowns: list[dict] = []
    teardown_errors: list[dict] = []
    if state == TaskState.DONE:
        from .tasks import cascade_unblock_dependents
        try:
            cascade_results = cascade_unblock_dependents(config, project_id, task_id)
        except (OSError, KeyError) as exc:
            # Filesystem or graph errors leave the cascade in a partial
            # state but must NOT block the user's done. Surface the
            # error in the response so it's visible — don't silently drop.
            cascade_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

        for cr in cascade_results:
            add_entry(
                config,
                project=project_id,
                action=WorkLogAction.CASCADE_UNBLOCK,
                task=cr["task_id"],
                summary=f"Auto-unblocked by completion of {cr['trigger']}",
                auto=True,
            )

        # Auto-teardown dispatch settings that reference the just-done task.
        # Codex round-4 fix: use the portfolio dispatch registry so we
        # find EVERY target_dir the operator dispatched to (custom
        # --target-dir, CWD-at-time-of-dispatch, repo subdirs, etc.) —
        # not just the hardcoded repo_path + worktree pair. Falls back
        # to the legacy locations as a belt-and-braces second pass for
        # dispatches that pre-date the registry.
        from .dispatch import (
            active_dispatch_dirs,
            read_dispatch_marker,
            teardown_dispatch_settings,
        )
        project = get_project(config, project_id)
        candidate_dirs: list[Path] = list(
            active_dispatch_dirs(
                config.portfolio_root, task_id, project_id
            )
        )
        # Legacy fallback: dispatches written before the registry was
        # introduced won't appear in active_dispatch_dirs. Probe the
        # canonical locations so existing in-flight dispatches still
        # get torn down on their next done.
        if project and project.repo_path and project.repo_path.exists():
            if project.repo_path not in candidate_dirs:
                candidate_dirs.append(project.repo_path)
            wt_dir = project.repo_path / ".clawpm-worktrees" / task_id
            if wt_dir.exists() and wt_dir not in candidate_dirs:
                candidate_dirs.append(wt_dir)
        seen_dirs: set[str] = set()
        for cand in candidate_dirs:
            # Dedup by resolved path so registry + legacy probes don't
            # double-fire on the same directory.
            try:
                key = str(cand.resolve())
            except OSError:
                key = str(cand)
            if key in seen_dirs:
                continue
            seen_dirs.add(key)
            marker = read_dispatch_marker(cand)
            # Codex round-6 P1: must match BOTH task_id AND project_id.
            # Without the project_id check on the marker, completing a
            # task in project A could tear down a same-task-id dispatch
            # in project B via the legacy fallback probe (registry
            # filter doesn't apply to the fallback candidates).
            if (
                marker
                and marker.get("task_id") == task_id
                and marker.get("project_id") == project_id
            ):
                try:
                    teardown_dispatch_settings(
                        cand,
                        task_id=task_id,
                        portfolio_root=config.portfolio_root,
                        project_id=project_id,
                    )
                    teardowns.append({
                        "target_dir": cand.as_posix(),
                        "task_id": task_id,
                    })
                except (OSError, PermissionError) as exc:
                    # Filesystem failure is the only realistic class here.
                    # Surface to the response — silent leftover settings.json
                    # is exactly the "stale dispatch" failure mode this
                    # entire feature exists to prevent.
                    teardown_errors.append({
                        "target_dir": cand.as_posix(),
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                    })

    # Write reflection event when task completes or is blocked
    if state in (TaskState.DONE, TaskState.BLOCKED) and pre_transition_task:
        try:
            from .reflect import write_reflection_event, _compute_actuals
            all_log_entries = read_entries(config, project=project_id)
            actuals = _compute_actuals(
                task_id,
                pre_transition_task.complexity,
                all_log_entries,
                portfolio_root=config.portfolio_root,
                project_id=project_id,
            )
            event_name = "task_done" if state == TaskState.DONE else "task_blocked"
            write_reflection_event(
                config.portfolio_root,
                event=event_name,
                task_id=task_id,
                project_id=project_id,
                predictions=pre_transition_task.predictions,
                actuals=actuals,
                note=reflect_note,
                meta_reflection=meta_reflect,
                process_lesson=process_lesson,
                surprise_taxonomy=list(surprise_tags) if surprise_tags else [],
            )
        except Exception:
            # Never let reflection failure block the state change
            pass

    data = task.to_dict()
    if cascade_results:
        data["cascade_unblocks"] = cascade_results
    if cascade_errors:
        data["cascade_errors"] = cascade_errors
    if teardowns:
        data["dispatch_teardowns"] = teardowns
    if teardown_errors:
        data["dispatch_teardown_errors"] = teardown_errors
    output_success(f"Task {task_id} moved to {new_state}", data=data, fmt=fmt)


@tasks.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--title", "-t", required=True, help="Task title")
@click.option("--id", "task_id", help="Task ID (auto-generated if not provided)")
@click.option("--priority", type=int, default=5, help="Priority (1-10, lower is higher)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), help="Complexity")
@click.option("--depends", "-d", multiple=True, help="Dependencies (can specify multiple)")
@click.option("--scope", multiple=True, help="File glob patterns claimed by this task (can specify multiple)")
@click.option("--parallel-group", "parallel_group", type=int, default=None, help="Batch ordinal for parallel dispatch (CLAWP-021). Tasks sharing a group dispatch together; group N+1 waits for group N.")
@click.option("--parent", "parent_id", help="Parent task ID (creates subtask)")
@click.option("--description", help="Task description (deprecated, use --body)")
@click.option("--body", "-b", help="Task body content")
@click.option("--body-file", type=click.Path(exists=True), help="Read body from file")
@click.option("--stdin", "read_stdin", is_flag=True, help="Read body from stdin")
# --- Prediction flags (all optional) ---
@click.option("--predict-duration", "predict_duration", default=None, help="Predicted duration: 90, 90m, 2h, 3d, 1w")
@click.option("--predict-complexity", "predict_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="Predicted complexity")
@click.option("--predict-files-changed", "predict_files_changed", type=int, default=None, help="Predicted number of files changed")
@click.option("--predict-scope", "predict_scope", multiple=True, help="Predicted file glob scope (can specify multiple)")
@click.option("--predict-frameworks", "predict_frameworks", multiple=True, help="Predicted frameworks/libraries to touch (can specify multiple)")
@click.option("--predict-pitfalls", "predict_pitfalls", default=None, help="Anticipated problematic areas (free text)")
@click.option("--hypothesis", "hypothesis", default=None, help="Goal/hypothesis: 'if I do X, then Y will improve'")
# --- Phase 1.5 prediction flags ---
@click.option("--success-criteria", "success_criteria", multiple=True, help="Measurable success contract (repeatable, e.g. 'P95 latency <200ms')")
@click.option("--predict-approach", "predict_approach", default=None, help="Predicted architectural approach / solution pattern (1-2 sentences)")
@click.option("--unknowns", "unknowns", default=None, help="What you do NOT know going in (meta-curiosity capture)")
@click.option("--confidence", "confidence", type=int, default=None, help="Operator confidence 1-5 (1=wild guess, 5=done this before)")
@click.option("--reference-task", "reference_tasks", multiple=True, help="Prior task IDs used as reference class (repeatable)")
@click.option("--pre-mortem", "pre_mortem", default=None, help="'If this task fails, the most likely cause is...'")
@click.option("--predict-iterations", "predict_iterations", type=int, default=None, help="Predicted iterate→grade→revise cycles (CLAWP-019). Default None; 1 means 'expected to land in one pass'.")
# --- Phase 1.6 attribution flag ---
@click.option(
    "--predicted-by", "predicted_by",
    type=click.Choice(["agent", "operator", "operator-edited", "retroactive"]),
    default=None,
    help="Who filled in these predictions (default: operator). Use 'operator-edited' when agent proposed and human reviewed.",
)
@click.pass_context
def tasks_add(
    ctx: click.Context,
    project_id: str | None,
    title: str,
    task_id: str | None,
    priority: int,
    complexity: str | None,
    depends: tuple[str, ...],
    scope: tuple[str, ...],
    parallel_group: int | None,
    parent_id: str | None,
    description: str | None,
    body: str | None,
    body_file: str | None,
    read_stdin: bool,
    predict_duration: str | None,
    predict_complexity: str | None,
    predict_files_changed: int | None,
    predict_scope: tuple[str, ...],
    predict_frameworks: tuple[str, ...],
    predict_pitfalls: str | None,
    hypothesis: str | None,
    success_criteria: tuple[str, ...],
    predict_approach: str | None,
    unknowns: str | None,
    confidence: int | None,
    reference_tasks: tuple[str, ...],
    pre_mortem: str | None,
    predict_iterations: int | None,
    predicted_by: str | None,
) -> None:
    """Add a new task (or subtask with --parent)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Validate confidence range
    if confidence is not None and not (1 <= confidence <= 5):
        output_error("bad_confidence", f"--confidence must be 1-5, got {confidence}", fmt=fmt)
        sys.exit(1)

    project_id, _ = require_project(ctx, project_id)

    # Determine body content
    task_body = ""
    if body:
        task_body = body
    elif body_file:
        task_body = Path(body_file).read_text(encoding="utf-8")
    elif read_stdin:
        task_body = sys.stdin.read()
    elif description:
        task_body = description

    cmplx = TaskComplexity(complexity) if complexity else None
    scope_list = list(scope) if scope else None

    # Parse human-friendly duration (e.g. "2h", "3d") → minutes
    from .reflect import parse_duration as _parse_duration
    try:
        parsed_predict_duration = _parse_duration(predict_duration)
    except Exception as exc:
        output_error("bad_duration", str(exc), fmt=fmt)
        sys.exit(1)

    # Resolve filled_by: default to "operator" when any prediction flag is set,
    # None when no predictions at all (nothing to attribute).
    _has_predictions = any([
        parsed_predict_duration is not None,
        predict_complexity is not None,
        predict_files_changed is not None,
        predict_scope,
        predict_frameworks,
        predict_pitfalls is not None,
        hypothesis is not None,
        success_criteria,
        predict_approach is not None,
        unknowns is not None,
        confidence is not None,
        reference_tasks,
        pre_mortem is not None,
        predict_iterations is not None,
    ])
    filled_by: str | None = predicted_by if predicted_by is not None else (
        "operator" if _has_predictions else None
    )

    # Build predictions object from flags (all optional)
    predictions = Predictions(
        duration_min=parsed_predict_duration,
        complexity=TaskComplexity(predict_complexity) if predict_complexity else None,
        files_changed=predict_files_changed,
        files_scope=list(predict_scope),
        frameworks=list(predict_frameworks),
        pitfalls=predict_pitfalls,
        hypothesis=hypothesis,
        success_criteria=[SuccessCriterion.from_cli(s) for s in success_criteria],
        approach=predict_approach,
        unknowns=unknowns,
        confidence=confidence,
        reference_tasks=list(reference_tasks),
        pre_mortem=pre_mortem,
        predicted_iterations=predict_iterations,
        filled_by=filled_by,
    )

    # Create subtask if parent specified
    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)
        task = add_subtask(
            config,
            project_id,
            parent_id,
            title,
            priority=priority,
            complexity=cmplx,
            description=task_body,
        )
    else:
        deps = list(depends) if depends else None
        task = add_task(
            config,
            project_id,
            title,
            task_id=task_id,
            priority=priority,
            complexity=cmplx,
            depends=deps,
            scope=scope_list,
            description=task_body,
            predictions=predictions,
            parallel_group=parallel_group,
        )

    if not task:
        # Give a more useful hint: check if the project exists locally but has
        # a malformed settings.toml (e.g. Windows backslashes in repo_path).
        from pathlib import Path as _Path
        _current = _Path.cwd().resolve()
        _settings_exists = False
        while _current != _current.parent:
            if (_current / ".project" / "settings.toml").exists():
                _settings_exists = True
                break
            _current = _current.parent

        if _settings_exists:
            output_error(
                "add_failed",
                f"Failed to add task to project '{project_id}'. "
                f"A .project/settings.toml exists locally but could not be loaded from the "
                f"portfolio registry - the file may contain Windows backslashes in repo_path. "
                f"Fix it by using forward slashes (e.g. F:/Git/...) then retry.",
                fmt=fmt,
            )
        else:
            output_error("add_failed", f"Failed to add task to project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # CLAWP-023: surface reference-task suggestions at predict-time when
    # the operator/agent didn't already pin them. Anchors new predictions
    # to the calibration corpus instead of pure inside view.
    task_dict = task.to_dict()
    if not reference_tasks and task.predictions and not task.predictions.is_empty():
        try:
            from .reflect import find_reference_tasks
            # CLAWP-030: pass repo_path so reference scoring can augment
            # with CodeGraph semantic-symbol overlap when the project is
            # indexed. find_reference_tasks degrades gracefully when not.
            _proj = get_project(config, project_id)
            _repo = _proj.repo_path if _proj else None
            suggestions = find_reference_tasks(
                config.portfolio_root,
                project_id=project_id,
                complexity=task.predictions.complexity,
                files_scope=task.predictions.files_scope,
                frameworks=task.predictions.frameworks,
                success_criteria_text=[
                    sc.criterion for sc in task.predictions.success_criteria
                ],
                repo_path=_repo,
                k=3,
            )
            if suggestions:
                task_dict["suggested_references"] = suggestions
        except Exception:
            # Reference suggestions are nice-to-have; don't fail task creation
            pass

    # CLAWP-027: auto-suggest files_scope when operator didn't pin one.
    # If a CodeGraph index exists for the project's repo, query it with
    # title+body and propose scope globs. Operator can copy into a
    # follow-up `tasks edit --predict-scope` or accept as-is.
    if not task.predictions.files_scope and not predict_scope:
        try:
            project = get_project(config, project_id)
            if project and project.repo_path and project.repo_path.exists():
                from .codegraph import suggest_scope_from_text
                query_text = (
                    (task.title or "")
                    + "\n"
                    + (task.body or task.content or "")
                )
                suggested = suggest_scope_from_text(
                    query_text.strip(),
                    project.repo_path,
                )
                if suggested:
                    task_dict["suggested_scope"] = suggested
        except Exception:
            # Scope suggestions are nice-to-have; don't fail task creation
            pass

    output_success(f"Task {task.id} created", data=task_dict, fmt=fmt)


@tasks.command("emit-rubric")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option(
    "--format", "rubric_format",
    type=click.Choice(["markdown", "outcome-payload"]),
    default="markdown",
    help=(
        "markdown: print the rubric for piping into a Stop-hook prompt or "
        "human review. outcome-payload: JSON shaped for Anthropic's "
        "user.define_outcome event."
    ),
)
@click.pass_context
def tasks_emit_rubric(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    rubric_format: str,
) -> None:
    """Render a task's success-criteria as a graded-criteria rubric.

    The same rubric drives both clawpm's local Stop-hook condition evaluator
    (CLAWP-017) and an Anthropic Managed Agents ``user.define_outcome``
    event — clawpm is the persistence layer, the rubric is the contract.
    """
    import json as _json_rub
    from .rubric import render_rubric_markdown, render_rubric_json_payload

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = get_task(config, project_id, task_id)
    if not task:
        output_error(
            "task_not_found",
            f"No task with id '{task_id}' in project '{project_id}'",
            fmt=fmt,
        )
        sys.exit(1)

    if rubric_format == "markdown":
        # The rubric IS the output — bypass output_success because the
        # consumer (a hook command, or pipe to file) usually wants the raw
        # markdown without a JSON envelope.
        if fmt == OutputFormat.JSON:
            output_json({
                "status": "ok",
                "task_id": task.id,
                "format": "markdown",
                "rubric": render_rubric_markdown(task),
            })
        else:
            click.echo(render_rubric_markdown(task))
    else:
        payload = render_rubric_json_payload(task)
        if fmt == OutputFormat.JSON:
            output_json({
                "status": "ok",
                "task_id": task.id,
                "format": "outcome-payload",
                "payload": payload,
            })
        else:
            click.echo(_json_rub.dumps(payload, indent=2))


@tasks.command("dispatch")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option(
    "--target-dir", "target_dir", type=click.Path(), default=None,
    help="Directory to write .claude/settings.local.json into. Default: current directory."
)
@click.option(
    "--worktree", is_flag=True, default=False,
    help="Create a git worktree at .clawpm-worktrees/<task-id>/ and dispatch there."
)
@click.option(
    "--no-session-context", is_flag=True, default=False,
    help="Skip SessionStart rubric injection (default: inject)."
)
@click.option(
    "--force", "-f", is_flag=True, default=False,
    help="Back up + overwrite an existing settings.local.json."
)
@click.pass_context
def tasks_dispatch(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    target_dir: str | None,
    worktree: bool,
    no_session_context: bool,
    force: bool,
) -> None:
    """Emit hook-wired .claude/settings.local.json for a dispatched subagent (CLAWP-018).

    The subagent uses Claude Code as normal; clawpm gets state updates and
    success-criteria enforcement at the dispatch boundary. The Stop hook
    blocks termination until the task's rubric (CLAWP-016) is satisfied,
    via the local condition evaluator (CLAWP-017).

    With --worktree, creates a git worktree under .clawpm-worktrees/<id>/
    so multiple subagents can be dispatched in parallel without colliding
    on a single .claude/settings.local.json.
    """
    from .dispatch import (
        create_worktree,
        settings_path,
        write_dispatch_settings,
    )
    from .rubric import render_rubric_markdown

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _source = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = get_task(config, project_id, task_id)
    if not task:
        output_error("task_not_found", f"No task with id '{task_id}'", fmt=fmt)
        sys.exit(1)

    project = get_project(config, project_id)
    # Resolve target directory
    if worktree:
        if not project or not project.repo_path or not project.repo_path.exists():
            output_error(
                "no_repo",
                "--worktree requires the project to have a valid repo_path "
                f"(got {(project.repo_path if project else None)!r})",
                fmt=fmt,
            )
            sys.exit(1)
        try:
            resolved_dir = create_worktree(project.repo_path, task_id)
        except subprocess.CalledProcessError as exc:
            output_error(
                "worktree_failed",
                f"git worktree add failed: {exc.stderr or exc.stdout}",
                fmt=fmt,
            )
            sys.exit(1)
    elif target_dir:
        resolved_dir = Path(target_dir)
        resolved_dir.mkdir(parents=True, exist_ok=True)
    else:
        resolved_dir = Path.cwd()

    rubric = None if no_session_context else render_rubric_markdown(task)

    try:
        path = write_dispatch_settings(
            target_dir=resolved_dir,
            task_id=task_id,
            project_id=project_id,
            rubric_markdown=rubric,
            force=force,
            portfolio_root=config.portfolio_root,
        )
    except (FileExistsError, ValueError) as exc:
        output_error("dispatch_blocked", str(exc), fmt=fmt)
        sys.exit(1)

    invocation = f"cd {resolved_dir.as_posix()} && claude"
    output_success(
        f"Task {task_id} dispatched to {resolved_dir}",
        data={
            "task_id": task_id,
            "target_dir": resolved_dir.as_posix(),
            "settings_path": path.as_posix(),
            "worktree": worktree,
            "invocation": invocation,
            "rubric_injected": rubric is not None,
        },
        fmt=fmt,
    )


@tasks.command("teardown-dispatch")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id", required=False)
@click.option(
    "--target-dir", "target_dir", type=click.Path(), default=None,
    help="Directory containing .claude/settings.local.json. Default: current directory."
)
@click.option(
    "--force", "-f", is_flag=True, default=False,
    help="Remove the file even if it's not clawpm-managed (dangerous)."
)
@click.pass_context
def tasks_teardown_dispatch(
    ctx: click.Context,
    project_id: str | None,
    task_id: str | None,
    target_dir: str | None,
    force: bool,
) -> None:
    """Remove a dispatch .claude/settings.local.json.

    By default, only removes files clawpm wrote (marker present) for the
    given task_id. Without task_id, removes any clawpm-managed dispatch.
    """
    from .dispatch import read_dispatch_marker, teardown_dispatch_settings

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    if task_id:
        project_id, _ = require_project(ctx, project_id)
        task_id = expand_task_id(task_id, project_id)

    resolved_dir = Path(target_dir) if target_dir else Path.cwd()
    marker = read_dispatch_marker(resolved_dir)

    removed = teardown_dispatch_settings(
        resolved_dir,
        task_id=task_id,
        force=force,
        portfolio_root=config.portfolio_root,
        project_id=project_id,
    )

    output_success(
        "Dispatch torn down" if removed else "Nothing to tear down",
        data={
            "removed": removed,
            "target_dir": resolved_dir.as_posix(),
            "previous_marker": marker,
        },
        fmt=fmt,
    )


@tasks.command("split")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def tasks_split(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Convert a task to a parent directory (for adding subtasks)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = split_task(config, project_id, task_id)

    if not task:
        output_error("split_failed", f"Failed to split task '{task_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task_id} converted to directory", data=task.to_dict(), fmt=fmt)


# ============================================================================
# Top-level task shortcuts
# ============================================================================


@main.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("title")
@click.option("--priority", type=int, default=5, help="Priority (1-10)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), default="m", help="Complexity")
@click.option("--parent", "parent_id", help="Parent task ID (creates subtask)")
@click.option("--body", "-b", help="Task description/body")
@click.pass_context
def quick_add(ctx: click.Context, project_id: str | None, title: str, priority: int, complexity: str, parent_id: str | None, body: str | None) -> None:
    """Quick add a task (alias for 'tasks add')."""
    ctx.invoke(tasks_add, project_id=project_id, title=title, priority=priority, complexity=complexity, parent_id=parent_id, body=body)


@main.command("done")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--note", "-n", help="Completion note")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why?")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this?")
@click.option("--surprise", "surprise_tags", multiple=True, help="Surprise taxonomy tag (repeatable): unknown_unknown, scope_drift, dependency, tooling_friction, complexity_misread, assumption_broke, external_blocker")
@click.pass_context
def quick_done(ctx: click.Context, project_id: str | None, task_id: str, note: str | None, force: bool, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...]) -> None:
    """Mark a task as done (alias for 'tasks state <id> done')."""
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="done", note=note, force=force, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags)


@main.command("start")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.pass_context
def quick_start(ctx: click.Context, project_id: str | None, task_id: str) -> None:
    """Start working on a task (alias for 'tasks state <id> progress').

    Note: if the task is already in progress, prefer 'clawpm log add --action progress'
    to avoid resetting the duration anchor.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Warn (but don't block) if the task is already in progress.
    # Re-starting corrupts the duration anchor — the reflection layer computes
    # actuals from the *first* start event, so a re-start under-counts elapsed time.
    resolved_project_id, _ = require_project(ctx, project_id, required=False)
    if resolved_project_id:
        try:
            _expanded = expand_task_id(task_id, resolved_project_id)
            _task = get_task(config, resolved_project_id, _expanded)
            if _task and _task.state and _task.state.value == "progress":
                click.echo(
                    f"Warning: {_expanded} is already in progress. "
                    "Re-starting resets the duration anchor and under-counts elapsed time. "
                    "Use 'clawpm log add --task <id> --action progress --summary \"...\"' "
                    "to log midway updates instead.",
                    err=True,
                )
        except Exception:
            pass  # Never let the guard break the start command

    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="progress", note=None)


@main.command("block")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--note", "-n", help="Blocker description")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why?")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this?")
@click.option("--surprise", "surprise_tags", multiple=True, help="Surprise taxonomy tag (repeatable): unknown_unknown, scope_drift, dependency, tooling_friction, complexity_misread, assumption_broke, external_blocker")
@click.pass_context
def quick_block(ctx: click.Context, project_id: str | None, task_id: str, note: str | None, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...]) -> None:
    """Mark a task as blocked (alias for 'tasks state <id> blocked')."""
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state="blocked", note=note, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags)


@main.command("unblock")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option("--note", "-n", help="Reason the blocker was resolved")
@click.option("--start", "also_start", is_flag=True, help="Also transition to in-progress (blocked → progress)")
@click.pass_context
def quick_unblock(ctx: click.Context, project_id: str | None, task_id: str, note: str | None, also_start: bool) -> None:
    """Move a blocked task back to open (or --start to go straight to in-progress).

    Shortcut for:
        clawpm tasks state <id> open   (default)
        clawpm tasks state <id> progress  (with --start)

    An 'unblock' action is logged in the work log with the provided note.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    full_task_id = expand_task_id(task_id, project_id)

    # Verify the task is actually blocked
    task = get_task(config, project_id, full_task_id)
    if not task:
        output_error("task_not_found", f"No task with id '{full_task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)
    if task.state != TaskState.BLOCKED:
        output_error(
            "not_blocked",
            f"Task {full_task_id} is in state '{task.state.value}', not 'blocked'. "
            "Use 'clawpm tasks state <id> open' to change state directly.",
            fmt=fmt,
        )
        sys.exit(1)

    # Transition to open (or progress if --start)
    new_state_str = "progress" if also_start else "open"
    ctx.invoke(tasks_state, project_id=project_id, task_id=task_id, new_state=new_state_str, note=note)

    # Log the explicit unblock action
    add_entry(
        config,
        project=project_id,
        action=WorkLogAction.UNBLOCK,
        task=full_task_id,
        summary=note or "Blocker resolved",
        auto=True,
    )


@main.command("next")
@click.option("--project", "-p", "project_id", help="Project ID (if not specified, searches all)")
@click.option(
    "--batch", "batch_mode", is_flag=True, default=False,
    help="Return the next parallel batch (tasks sharing the lowest open parallel_group) instead of a single task (CLAWP-021).",
)
@click.pass_context
def quick_next(ctx: click.Context, project_id: str | None, batch_mode: bool) -> None:
    """Get the next task to work on, or the next parallel batch with --batch."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if batch_mode:
        # Batch mode requires a single project — we don't aggregate batches
        # across projects (a batch is a unit of parallel dispatch, not a
        # cross-portfolio queue).
        project_id, _ = require_project(ctx, project_id)
        from .tasks import select_next_batch
        group, candidates, conflicts = select_next_batch(config, project_id)

        if group is None:
            payload = {
                "group": None,
                "candidates": [],
                "conflicts": [],
                "message": "No parallel batch available. Tasks need parallel_group: N in frontmatter to be batch-eligible.",
            }
        else:
            payload = {
                "group": group,
                "candidates": [t.to_dict() for t in candidates],
                "conflicts": conflicts,
                "dispatch_safe": len(conflicts) == 0,
            }
        if fmt == OutputFormat.JSON:
            output_json(payload)
        else:
            if group is None:
                click.echo(payload["message"])
            else:
                click.echo(f"Parallel group {group}: {len(candidates)} candidate task(s)")
                for t in candidates:
                    click.echo(f"  - {t.id} [{t.state.value}] {t.title}")
                if conflicts:
                    click.echo("\nSCOPE CONFLICTS — cannot dispatch as a single batch:")
                    for c in conflicts:
                        click.echo(
                            f"  {c['task_a']} <-> {c['task_b']}: "
                            f"{c['overlapping_globs']}"
                        )
                else:
                    click.echo("\nDispatch-safe: no scope overlaps.")
        return

    if project_id:
        # Get next task for specific project
        task = get_next_task(config, project_id)
        if task:
            output_task_detail(task, fmt=fmt)
        else:
            if fmt == OutputFormat.JSON:
                output_json({"task": None, "message": "No tasks available"})
            else:
                click.echo("No tasks available.")
    else:
        # Delegate to projects next
        ctx.invoke(projects_next)


@main.command("status")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.pass_context
def quick_status(ctx: click.Context, project_id: str | None) -> None:
    """Show current project status (tasks in progress, blockers, next up)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    resolved_id, source = require_project(ctx, project_id, required=False)
    
    if not resolved_id:
        # Show overview of all projects
        projects_found = discover_projects(config, status_filter=ProjectStatus.ACTIVE)
        
        result = {
            "projects": [],
            "total_active": 0,
            "total_blocked": 0,
        }
        
        for proj in projects_found:
            in_progress = list_tasks(config, proj.id, state_filter=TaskState.PROGRESS)
            blocked = list_tasks(config, proj.id, state_filter=TaskState.BLOCKED)
            
            proj_info = {
                "id": proj.id,
                "name": proj.name,
                "in_progress": len(in_progress),
                "blocked": len(blocked),
            }
            result["projects"].append(proj_info)
            result["total_active"] += len(in_progress)
            result["total_blocked"] += len(blocked)
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            click.echo(f"Active: {result['total_active']} tasks in progress, {result['total_blocked']} blocked\n")
            for proj in result["projects"]:
                status_str = []
                if proj["in_progress"]:
                    status_str.append(f"{proj['in_progress']} active")
                if proj["blocked"]:
                    status_str.append(f"{proj['blocked']} blocked")
                click.echo(f"  {proj['name']}: {', '.join(status_str) if status_str else 'idle'}")
    else:
        # Show specific project status
        proj = get_project(config, resolved_id)
        if not proj:
            output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
            sys.exit(1)
        
        in_progress = list_tasks(config, resolved_id, state_filter=TaskState.PROGRESS)
        blocked = list_tasks(config, resolved_id, state_filter=TaskState.BLOCKED)
        open_tasks = list_tasks(config, resolved_id, state_filter=TaskState.OPEN)
        next_task = get_next_task(config, resolved_id)
        
        result = {
            "project": proj.id,
            "name": proj.name,
            "source": source,
            "in_progress": [t.to_dict() for t in in_progress],
            "blocked": [t.to_dict() for t in blocked],
            "open_count": len(open_tasks),
            "next": next_task.to_dict() if next_task else None,
        }
        
        if fmt == OutputFormat.JSON:
            output_json(result)
        else:
            click.echo(f"Project: {proj.name} ({source})")
            click.echo(f"Open: {len(open_tasks)} | In Progress: {len(in_progress)} | Blocked: {len(blocked)}")
            
            if in_progress:
                click.echo("\nIn Progress:")
                for t in in_progress:
                    click.echo(f"  -> {t.id}: {t.title}")

            if blocked:
                click.echo("\nBlocked:")
                for t in blocked:
                    click.echo(f"  x {t.id}: {t.title}")
            
            if next_task and next_task not in in_progress:
                click.echo(f"\nNext up: {next_task.id}: {next_task.title}")


@main.command("context")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--log-limit", "-l", type=int, default=5, help="Number of recent log entries")
@click.pass_context
def agent_context(ctx: click.Context, project_id: str | None, log_limit: int) -> None:
    """Get full agent context (project, tasks, blockers, recent work, git status).
    
    Optimized for LLM agent consumption - everything needed to resume work.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    resolved_id, source = require_project(ctx, project_id, required=False)
    
    if not resolved_id:
        output_error("no_project", "No project specified or detected. Use -p or cd into a project.", fmt=fmt)
        sys.exit(1)
    
    proj = get_project(config, resolved_id)
    if not proj:
        output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
        sys.exit(1)
    
    # Build comprehensive context
    context: dict = {
        "project": {
            "id": proj.id,
            "name": proj.name,
            "status": proj.status.value,
            "priority": proj.priority,
            "labels": proj.labels,
            "repo_path": str(proj.repo_path) if proj.repo_path else None,
        },
        "source": source,
    }
    
    # Read spec if exists
    if proj.project_dir:
        spec_file = proj.project_dir / ".project" / "SPEC.md"
        if spec_file.exists():
            spec_content = spec_file.read_text(encoding="utf-8")
            # Truncate if too long
            if len(spec_content) > 2000:
                context["spec"] = spec_content[:2000] + "\n\n[...truncated...]"
            else:
                context["spec"] = spec_content
    
    # Current task (in progress)
    in_progress = list_tasks(config, resolved_id, state_filter=TaskState.PROGRESS)
    context["in_progress"] = [t.to_dict() for t in in_progress]
    
    # Next task if nothing in progress
    if not in_progress:
        next_task = get_next_task(config, resolved_id)
        if next_task:
            context["next_task"] = next_task.to_dict()
    
    # Blocked tasks
    blocked = list_tasks(config, resolved_id, state_filter=TaskState.BLOCKED)
    context["blockers"] = [t.to_dict() for t in blocked]
    
    # Open task count
    open_tasks = list_tasks(config, resolved_id, state_filter=TaskState.OPEN)
    context["open_count"] = len(open_tasks)
    
    # Recent work log
    recent_entries = tail_entries(config, project=resolved_id, limit=log_limit)
    context["recent_work"] = [e.to_dict() for e in recent_entries]
    
    # Git status if repo_path exists
    if proj.repo_path and proj.repo_path.exists():
        git_status = {}
        try:
            # Current branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                git_status["branch"] = result.stdout.strip()
            
            # Uncommitted changes
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                changes = [line for line in result.stdout.strip().split('\n') if line]
                git_status["uncommitted_count"] = len(changes)
                if changes:
                    git_status["uncommitted"] = changes[:10]  # Limit to 10
                    if len(changes) > 10:
                        git_status["uncommitted"].append(f"... and {len(changes) - 10} more")
            
            # Recent commits
            result = subprocess.run(
                ["git", "log", "--oneline", "-3"],
                cwd=proj.repo_path,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                git_status["recent_commits"] = [line for line in result.stdout.strip().split('\n') if line]
        except Exception:
            pass
        
        if git_status:
            context["git"] = git_status
    
    # Open issues
    if proj.project_dir:
        import json as json_mod
        issues_file = proj.project_dir / ".agent" / "issues.jsonl"
        if issues_file.exists():
            try:
                open_issues = []
                with open(issues_file) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            issue = json_mod.loads(line)
                            if not issue.get("fixed"):
                                open_issues.append({
                                    "type": issue.get("type"),
                                    "severity": issue.get("severity"),
                                    "summary": (issue.get("actual") or issue.get("context", ""))[:100],
                                })
                if open_issues:
                    context["open_issues"] = open_issues[:5]
            except Exception:
                pass
    
    output_context(context, fmt=fmt)


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
    from .resume import render_briefing

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
                "[warning] resume judge unavailable — showing signals summary",
                err=True,
            )
        elif status == "cached":
            click.echo("[cached]", err=True)
        click.echo(briefing)


# ============================================================================
# Log commands
# ============================================================================


@main.group()
def log() -> None:
    """Manage work log."""
    pass


@log.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "-t", "task_id", help="Task ID")
@click.option(
    "--action", "-a",
    type=click.Choice(["start", "progress", "done", "blocked", "commit", "pause", "research", "note"]),
    required=True,
    help="Action type",
)
@click.option("--summary", "-s", required=True, help="Summary of work")
@click.option("--next", "next_steps", help="Next steps")
@click.option("--files", "-f", multiple=True, help="Files changed")
@click.option("--blocker", "-b", help="Blocker description")
@click.option("--agent", default="main", help="Agent ID")
@click.option("--session-key", help="OpenClaw session key")
@click.pass_context
def log_add(
    ctx: click.Context,
    project_id: str | None,
    task_id: str | None,
    action: str,
    summary: str,
    next_steps: str | None,
    files: tuple[str, ...],
    blocker: str | None,
    agent: str,
    session_key: str | None,
) -> None:
    """Add a work log entry."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)
    
    # Expand task ID if provided
    if task_id:
        task_id = expand_task_id(task_id, project_id)

    # Auto-detect changed files from git if not manually specified
    if not files and project_id:
        project = get_project(config, project_id)
        if project and project.repo_path and project.repo_path.exists():
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=project.repo_path,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    files = tuple(f for f in result.stdout.strip().split('\n') if f)
            except Exception:
                pass  # No git or error - continue without files_changed

    entry = add_entry(
        config,
        project=project_id,
        action=WorkLogAction(action),
        task=task_id,
        summary=summary,
        next_steps=next_steps,
        files_changed=list(files) if files else None,
        blockers=blocker,
        agent=agent,
        session_key=session_key,
    )

    output_success("Entry added", data=entry.to_dict(), fmt=fmt)


@log.command("tail")
@click.option("--project", "-p", "project_id", help="Filter by project (auto-detected from cwd)")
@click.option("--limit", "-n", type=int, default=20, help="Number of entries")
@click.option("--follow", "-f", is_flag=True, help="Follow log output (like tail -f)")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show all projects (skip auto-detection)")
@click.pass_context
def log_tail(ctx: click.Context, project_id: str | None, limit: int, follow: bool, show_all: bool) -> None:
    """Show recent work log entries (auto-filters to current project)."""
    import time
    import json as json_module
    from .models import WorkLogEntry
    from .worklog import get_worklog_path

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Auto-detect project from cwd unless --all or explicit --project
    if not project_id and not show_all:
        project_id, source = require_project(ctx, None, required=False, auto_init=False)

    entries = tail_entries(config, project=project_id, limit=limit)
    output_worklog_entries(entries, fmt=fmt)
    
    if not follow:
        return
    
    # Follow mode - watch for new entries
    worklog_path = get_worklog_path(config)
    
    # Track file position
    try:
        pos = worklog_path.stat().st_size if worklog_path.exists() else 0
    except OSError:
        pos = 0
    
    try:
        while True:
            time.sleep(1)  # Poll every second
            
            if not worklog_path.exists():
                continue
            
            try:
                current_size = worklog_path.stat().st_size
            except OSError:
                continue
            
            if current_size > pos:
                # New content - read from last position
                with open(worklog_path) as f:
                    f.seek(pos)
                    new_lines = f.read()
                    pos = f.tell()
                
                for line in new_lines.strip().split('\n'):
                    if not line:
                        continue
                    try:
                        data = json_module.loads(line)
                        entry = WorkLogEntry.from_dict(data)
                        
                        # Apply project filter
                        if project_id and entry.project != project_id:
                            continue
                        
                        output_worklog_entries([entry], fmt=fmt)
                    except (json_module.JSONDecodeError, KeyError, ValueError):
                        continue
    except KeyboardInterrupt:
        pass  # Clean exit on Ctrl+C


@log.command("last")
@click.option("--project", "-p", "project_id", help="Filter by project (auto-detected from cwd)")
@click.option("--all", "-a", "show_all", is_flag=True, help="Show global last (skip auto-detection)")
@click.pass_context
def log_last(ctx: click.Context, project_id: str | None, show_all: bool) -> None:
    """Show the most recent work log entry (auto-filters to current project)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Auto-detect project from cwd unless --all or explicit --project
    if not project_id and not show_all:
        project_id, source = require_project(ctx, None, required=False, auto_init=False)

    entry = get_last_entry(config, project=project_id)

    if entry:
        output_worklog_entries([entry], fmt=fmt)
    else:
        if fmt == OutputFormat.JSON:
            output_json(None)
        else:
            click.echo("No entries found")


@log.command("commit")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--limit", "-n", type=int, default=10, help="Number of recent commits to check")
@click.option("--task", "-t", "task_id", help="Associate commits with a task")
@click.option("--dry-run", is_flag=True, help="Show what would be logged without logging")
@click.pass_context
def log_commit(ctx: click.Context, project_id: str | None, limit: int, task_id: str | None, dry_run: bool) -> None:
    """Log recent git commits to work log (pull-based, deduplicates)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    # Expand task ID if provided
    if task_id:
        task_id = expand_task_id(task_id, project_id)

    proj = get_project(config, project_id)
    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    repo_path = proj.repo_path or proj.project_dir
    if not repo_path or not repo_path.exists():
        output_error("no_repo", f"No repo path for project '{project_id}'", fmt=fmt)
        sys.exit(1)

    # Get recent commits: hash, ISO date, subject
    try:
        result = subprocess.run(
            ["git", "log", f"-{limit}", "--format=%H%x00%aI%x00%s"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            output_error("git_error", f"git log failed: {result.stderr.strip()}", fmt=fmt)
            sys.exit(1)
    except Exception as e:
        output_error("git_error", f"Failed to run git: {e}", fmt=fmt)
        sys.exit(1)

    commits = []
    for line in result.stdout.strip().split('\n'):
        if not line:
            continue
        parts = line.split('\x00', 2)
        if len(parts) == 3:
            commits.append({"hash": parts[0], "date": parts[1], "subject": parts[2]})

    if not commits:
        output_success("No commits found", fmt=fmt)
        return

    # Get already-logged hashes
    logged_hashes = get_logged_commit_hashes(config, project=project_id)

    # Filter to new commits only
    new_commits = [c for c in commits if c["hash"] not in logged_hashes]

    if not new_commits:
        output_success("All recent commits already logged", fmt=fmt)
        return

    if dry_run:
        result_data = {"would_log": len(new_commits), "commits": new_commits}
        if fmt == OutputFormat.JSON:
            output_json(result_data)
        else:
            click.echo(f"Would log {len(new_commits)} commit(s):")
            for c in new_commits:
                click.echo(f"  {c['hash'][:8]} {c['subject']}")
        return

    # Log each new commit (oldest first)
    logged = []
    for commit in reversed(new_commits):
        # Get files changed in this commit
        try:
            files_result = subprocess.run(
                ["git", "diff-tree", "--no-commit-id", "--name-status", "-r", commit["hash"]],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            files_changed = None
            if files_result.returncode == 0 and files_result.stdout.strip():
                files_changed = [line for line in files_result.stdout.strip().split('\n') if line]
        except Exception:
            files_changed = None

        # Parse commit timestamp
        from datetime import datetime, timezone
        try:
            commit_ts = datetime.fromisoformat(commit["date"])
        except (ValueError, TypeError):
            commit_ts = datetime.now(timezone.utc)

        # Extract task ID from commit message if not explicitly provided
        effective_task = task_id
        if not effective_task:
            # Look for PROJ-NNN pattern in commit message
            import re
            task_match = re.search(r'\b([A-Z]+-\d{3})\b', commit["subject"])
            if task_match:
                effective_task = task_match.group(1)

        entry = add_entry(
            config,
            project=project_id,
            action=WorkLogAction.COMMIT,
            task=effective_task,
            summary=commit["subject"],
            files_changed=files_changed,
            commit_hash=commit["hash"],
            auto=True,
            ts=commit_ts,
        )
        logged.append(entry)

    result_data = {
        "logged": len(logged),
        "skipped": len(commits) - len(new_commits),
        "entries": [e.to_dict() for e in logged],
    }

    if fmt == OutputFormat.JSON:
        output_json(result_data)
    else:
        click.echo(f"Logged {len(logged)} commit(s), skipped {len(commits) - len(new_commits)} already logged")
        for e in logged:
            click.echo(f"  {e.commit_hash[:8] if e.commit_hash else '?'} {e.summary}")


# ============================================================================
# Hook subcommands (called by Claude Code hooks; not for direct human use)
# ============================================================================


@main.group()
def hook() -> None:
    """Hook-callable subcommands for Claude Code integration.

    These commands are designed to be wired into ``.claude/settings.json``
    (or ``.claude/settings.local.json``) Stop / PostToolUse hooks. They
    read the standard hook stdin JSON and emit hook output JSON on stdout.
    """
    pass


@hook.command("session-start")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task ID whose SessionStart sidecar to emit")
@click.pass_context
def hook_session_start(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
) -> None:
    """Print the SessionStart additionalContext sidecar to stdout.

    Wired into Claude Code as a SessionStart command hook by
    `clawpm tasks dispatch`. Reads the sidecar JSON file co-located with
    settings.local.json and prints it verbatim — Claude Code's hook
    output schema accepts JSON on stdout. Cross-platform safe (no shell
    quoting, no embedded JSON in command strings).
    """
    import json as _json_ss
    from .dispatch import session_start_payload_path

    fmt = get_format(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    sidecar = session_start_payload_path(Path.cwd())
    if not sidecar.exists():
        # No sidecar = SessionStart was not configured for this dispatch
        # (or was torn down). Emit an empty hookSpecificOutput so the
        # hook is a no-op rather than a crash.
        click.echo(_json_ss.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }))
        return
    try:
        click.echo(sidecar.read_text(encoding="utf-8"))
    except OSError as exc:
        # Read failure must not crash the session start; emit a degraded
        # but valid hook output with the error surfaced.
        click.echo(_json_ss.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"(clawpm: failed to read SessionStart sidecar: {exc})"
                ),
            }
        }))


@hook.command("eval-stop")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task ID whose rubric to evaluate against")
@click.option("--transcript-file", "transcript_file", type=click.Path(), default=None,
              help="Path to the transcript file. Overrides hook stdin's transcript_path.")
@click.option("--rubric-file", "rubric_file", type=click.Path(), default=None,
              help="Path to a pre-rendered rubric markdown file. Default: render from the task.")
@click.pass_context
def hook_eval_stop(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    transcript_file: str | None,
    rubric_file: str | None,
) -> None:
    """Stop-hook condition evaluator (CLAWP-017).

    Reads the Claude Code Stop-hook input from stdin (JSON), extracts the
    transcript path, renders the task's success-criteria rubric, dispatches
    a Haiku-class judge, and emits a hook-output JSON deciding whether the
    subagent may stop.

    Local emulation of Anthropic Managed Agents' Outcomes evaluator — no
    paid API required; uses the operator's existing Claude Code subscription
    via subprocess to ``claude --print``. Override the judge with
    ``CLAWPM_JUDGE_CMD`` env var.
    """
    import json as _json_hook
    from .judges.stop_condition import (
        evaluate_stop_condition,
        load_transcript_from_hook_input,
        map_verdict_to_hook_output,
    )
    from .rubric import render_rubric_markdown

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    # 1. Load the rubric — from file if given, else render from the task.
    rubric: str
    if rubric_file:
        rubric = Path(rubric_file).read_text(encoding="utf-8")
    else:
        task = get_task(config, project_id, task_id)
        if not task:
            # Task-not-found is a dispatch-config bug, NOT a soft fail.
            # Block the Stop event so the operator sees the problem in
            # the transcript rather than discovering it after the subagent
            # has already finished gated work. Codex round-2 P1: use
            # `decision: "block"` + `reason` (forces agent to keep
            # working), NOT `continue: false` (which halts the entire
            # pipeline / terminates the agent).
            click.echo(_json_hook.dumps({
                "decision": "block",
                "reason": (
                    f"clawpm eval-stop: task {task_id} not found in "
                    f"project {project_id} — fix dispatch config "
                    f"(check `clawpm tasks dispatch --task-id`) before "
                    f"continuing."
                ),
            }))
            return
        rubric = render_rubric_markdown(task)

    # 2. Load the transcript — from --transcript-file, or from hook stdin.
    transcript: str
    if transcript_file:
        transcript = Path(transcript_file).read_text(encoding="utf-8", errors="replace")
    else:
        # Stop-hook input comes in on stdin.
        try:
            stdin_raw = sys.stdin.read()
        except OSError:
            stdin_raw = ""
        if stdin_raw.strip():
            try:
                hook_input = _json_hook.loads(stdin_raw)
            except _json_hook.JSONDecodeError:
                hook_input = {}
            try:
                transcript = load_transcript_from_hook_input(hook_input)
            except (ValueError, FileNotFoundError) as exc:
                # Can't find the transcript — surface to operator but don't
                # block the stop (would loop forever).
                click.echo(_json_hook.dumps({
                    "continue": True,
                    "systemMessage": f"clawpm eval-stop: transcript unavailable ({exc}); rubric not enforced",
                }))
                return
        else:
            click.echo(_json_hook.dumps({
                "continue": True,
                "systemMessage": "clawpm eval-stop: no stdin and no --transcript-file; rubric not enforced",
            }))
            return

    # 3. Dispatch the judge. Errors here are unexpected — surface them
    # in a way that's visible to doctor, not silently swallowed.
    try:
        verdict = evaluate_stop_condition(rubric=rubric, transcript=transcript)
    except RuntimeError as exc:
        # Judge error = enforcement-layer down. Fail-open (continue=true)
        # is defensible because blocking forever on a broken judge is
        # worse, but we MUST leave a doctor signal so repeated judge
        # errors don't silently degrade clawpm to no-enforcement.
        try:
            from .reflect import write_iteration_event
            write_iteration_event(
                portfolio_root=config.portfolio_root,
                task_id=task_id,
                project_id=project_id,
                verdict_ok=False,
                verdict_reason=f"JUDGE_ERROR: {exc}",
                verdict_impossible=False,
            )
        except OSError:
            # Writing the doctor signal failed too — last resort is the
            # systemMessage. Don't pile silent failures.
            pass
        click.echo(_json_hook.dumps({
            "continue": True,
            "systemMessage": (
                f"clawpm eval-stop: judge error ({exc}); rubric not "
                f"enforced. Consecutive judge errors will be flagged by "
                f"clawpm doctor — set CLAWPM_JUDGE_CMD or install Claude "
                f"Code if this keeps happening."
            ),
        }))
        return

    # CLAWP-019: capture the iteration event. This IS the calibration
    # spine — narrow exception so a real filesystem failure surfaces in
    # the systemMessage instead of silently nuking the iteration count.
    try:
        from .reflect import write_iteration_event
        write_iteration_event(
            portfolio_root=config.portfolio_root,
            task_id=task_id,
            project_id=project_id,
            verdict_ok=verdict.ok,
            verdict_reason=verdict.reason,
            verdict_impossible=verdict.impossible,
        )
    except OSError as exc:
        # Disk full / permission / encoding errors. Surface in the
        # hook output's systemMessage so the operator sees it in the
        # next transcript update.
        output = map_verdict_to_hook_output(verdict)
        # Preserve continue/block decision; just decorate systemMessage.
        existing_msg = output.get("systemMessage", "")
        output["systemMessage"] = (
            f"clawpm eval-stop: iteration event WRITE FAILED ({exc}); "
            f"calibration data lost for this cycle. {existing_msg}".strip()
        )
        click.echo(_json_hook.dumps(output))
        return

    output = map_verdict_to_hook_output(verdict)
    click.echo(_json_hook.dumps(output))


# ============================================================================
# Agent dispatch wrapper (CLAWP-024)
# ============================================================================


@main.group("agent")
def agent_group() -> None:
    """Parent-spawned subagent dispatch with Stop-hook judge integration.

    ``clawpm tasks dispatch`` (CLAWP-018) writes the per-target settings
    so a hand-launched subagent's Stop / PostToolUse / SessionStart hooks
    fire. ``clawpm agent dispatch`` (CLAWP-024) wraps the full cycle in
    one command — task create, dispatch settings write, subagent invoke,
    judge grade, state transition — so the rubric is enforced on every
    parent-spawned subagent without the parent needing to remember the
    six-step manual sequence.
    """
    pass


@agent_group.command("dispatch")
@click.option(
    "--project", "-p", "project_id",
    help="Project ID (auto-detected if not specified)",
)
@click.option(
    "--prompt", "prompt", required=True,
    help="The subagent prompt — becomes the subtask body AND is fed on stdin to the judge CLI.",
)
@click.option(
    "--parent", "parent_id", default=None,
    help="Optional parent task ID. Recorded in the reflection event for traceability.",
)
@click.option(
    "--rubric-criteria", "rubric_criteria", multiple=True,
    help="Success criterion (repeatable). Plain string OR JSON object "
         "{'criterion':'...','gradeable_signal':'...','comparator':'...'} — "
         "parsed via SuccessCriterion.from_cli.",
)
@click.option(
    "--title", "title", default=None,
    help="Optional subtask title. Defaults to a truncated prompt preview.",
)
@click.option(
    "--judge-cmd-override", "judge_cmd_override", default=None,
    help="Override the judge subprocess command (highest priority — beats "
         "CLAWPM_JUDGE_CMD env var). Use a stub here for offline testing.",
)
@click.option(
    "--no-codegraph", "no_codegraph", is_flag=True, default=False,
    help="Skip codegraph init+index inside the worktree (CLAWP-029). "
         "Default: init when codegraph is on PATH. Use this for batches "
         "where per-dispatch index cost dominates.",
)
@click.pass_context
def agent_dispatch(
    ctx: click.Context,
    project_id: str | None,
    prompt: str,
    parent_id: str | None,
    rubric_criteria: tuple[str, ...],
    title: str | None,
    judge_cmd_override: str | None,
    no_codegraph: bool,
) -> None:
    """Spawn a subagent, grade its output against the rubric, persist the verdict.

    Flow:
      1. ``add_task`` with prompt as body + rubric_criteria as
         ``predictions.success_criteria``.
      2. ``create_worktree`` under ``<repo>/.clawpm-worktrees/<subtask-id>/``.
      3. ``write_dispatch_settings`` into the worktree (Stop / PostToolUse /
         SessionStart hooks).
      4. Subprocess to ``claude --print`` (or ``--judge-cmd-override``)
         with the prompt on stdin; capture stdout as the transcript.
      5. ``evaluate_stop_condition(rubric, transcript)``.
      6. ``ok=True``  → mark subtask DONE + write reflection event.
         ``ok=False`` → mark subtask BLOCKED + write iteration event.

    The wrapper is testable without a real ``claude`` CLI by passing a
    ``--judge-cmd-override`` that points to a stub command, or by setting
    ``CLAWPM_JUDGE_CMD`` (legacy env var from CLAWP-017).
    """
    from .agent import AgentDispatchError, dispatch_agent

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)

    try:
        result = dispatch_agent(
            config=config,
            project_id=project_id,
            prompt=prompt,
            success_criteria=list(rubric_criteria),
            parent_id=parent_id,
            judge_cmd_override=judge_cmd_override,
            title=title,
            init_codegraph=not no_codegraph,
        )
    except AgentDispatchError as exc:
        output_error("agent_dispatch_failed", str(exc), fmt=fmt)
        sys.exit(1)

    # Surface the verdict-derived headline in the success message so
    # text-mode operators see at-a-glance whether the dispatch passed
    # without parsing JSON.
    verdict = result["verdict"]
    if verdict["ok"]:
        headline = f"Agent dispatch ok ({result['subtask_id']}): {verdict['reason'][:120]}"
    elif verdict["impossible"]:
        headline = (
            f"Agent dispatch IMPOSSIBLE ({result['subtask_id']}): "
            f"{verdict['reason'][:120]} — subtask marked blocked"
        )
    else:
        headline = (
            f"Agent dispatch failed ({result['subtask_id']}): "
            f"{verdict['reason'][:120]} — subtask marked blocked"
        )

    output_success(headline, data=result, fmt=fmt)


# ============================================================================
# Mission Control commands (CLAWP-022)
# ============================================================================


@main.group("mission")
def mission_group() -> None:
    """Mission Control — macro binary-outcome layer above tasks."""
    pass


@mission_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--title", "-t", required=True, help="Mission title (6-8 words, ship-able statement)")
@click.option("--binary-outcome", "-o", required=True, help="YES/NO check at the deadline (<=12 words)")
@click.option("--deadline-days", "-d", type=int, default=28, show_default=True, help="Days from now (7-42)")
@click.option("--body", "-b", default="", help="Mission description (optional)")
@click.option("--id", "mission_id", default=None, help="Override mission ID (auto-generated otherwise)")
@click.option("--force", is_flag=True, default=False, help="Overwrite an existing mission file with the same ID (destructive)")
@click.pass_context
def mission_add(
    ctx: click.Context,
    project_id: str | None,
    title: str,
    binary_outcome: str,
    deadline_days: int,
    body: str,
    mission_id: str | None,
    force: bool,
) -> None:
    """Create a new mission. Mini-goals are linked separately via add-goal."""
    from .mission import add_mission

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    try:
        mission = add_mission(
            config, project_id,
            title=title,
            binary_outcome=binary_outcome,
            deadline_days=deadline_days,
            description=body,
            mission_id=mission_id,
            force=force,
        )
    except ValueError as exc:
        output_error("mission_add_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(f"Mission {mission.id} created", data=mission.to_dict(), fmt=fmt)


@mission_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--status", "-s", "status_filter",
              type=click.Choice(["active", "complete", "failed", "cancelled"]),
              default=None,
              help="Filter by status")
@click.pass_context
def mission_list(
    ctx: click.Context, project_id: str | None, status_filter: str | None
) -> None:
    """List missions for a project."""
    from .mission import list_missions

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    missions = list_missions(config, project_id, status_filter=status_filter)
    if fmt == OutputFormat.JSON:
        output_json([m.to_dict() for m in missions])
    else:
        for m in missions:
            click.echo(f"{m.id} [{m.status}] {m.title} -> {m.binary_outcome[:60]}")


@mission_group.command("status")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.pass_context
def mission_status_cmd(
    ctx: click.Context, project_id: str | None, mission_id: str
) -> None:
    """Compute progress + outcome state for a mission."""
    from .mission import mission_status

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    result = mission_status(config, project_id, mission_id)
    if "error" in result:
        output_error("mission_not_found", result["error"], fmt=fmt)
        sys.exit(1)
    if fmt == OutputFormat.JSON:
        output_json(result)
    else:
        click.echo(f"{result['id']}: {result['title']}")
        click.echo(f"  outcome: {result['outcome_status']}")
        click.echo(f"  progress: {result['complete_count']}/{result['total_count']} ({result['pct_complete']}%)")
        if result['days_remaining'] is not None:
            click.echo(f"  deadline: {result['deadline_date']} ({result['days_remaining']}d remaining)")
        click.echo(f"  agent: {result['agent_counts']}")
        click.echo(f"  human: {result['human_counts']}")
        if result['missing_refs']:
            click.echo(f"  WARNING: missing task refs: {result['missing_refs']}")


@mission_group.command("tasks")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.option("--actor",
              type=click.Choice(["agent", "human"]),
              default=None,
              help="Filter by actor (default: both)")
@click.pass_context
def mission_tasks_cmd(
    ctx: click.Context, project_id: str | None, mission_id: str, actor: str | None
) -> None:
    """List mini-goal tasks for a mission, optionally filtered by actor."""
    from .mission import mission_tasks

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    tasks = mission_tasks(config, project_id, mission_id, actor_filter=actor)
    if fmt == OutputFormat.JSON:
        output_json([t.to_dict() for t in tasks])
    else:
        for t in tasks:
            click.echo(f"{t.id} [{t.state.value}] [{t.actor or 'agent'}] {t.title[:80]}")


@mission_group.command("add-goal")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.option("--task", "task_id", required=True, help="Task ID to link as a mini-goal")
@click.option("--actor",
              type=click.Choice(["agent", "human"]),
              default="agent",
              show_default=True,
              help="Who runs this mini-goal")
@click.pass_context
def mission_add_goal(
    ctx: click.Context,
    project_id: str | None,
    mission_id: str,
    task_id: str,
    actor: str,
) -> None:
    """Link an existing task to a mission as a mini-goal."""
    from .mission import add_mission_mini_goal

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    try:
        mission = add_mission_mini_goal(
            config, project_id, mission_id, task_id, actor=actor
        )
    except ValueError as exc:
        output_error("mission_add_goal_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(
        f"Linked {task_id} to {mission_id} as {actor}",
        data=mission.to_dict(),
        fmt=fmt,
    )


@mission_group.command("state")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.argument("mission_id")
@click.argument("new_status",
                type=click.Choice(["active", "complete", "failed", "cancelled"]))
@click.pass_context
def mission_state(
    ctx: click.Context,
    project_id: str | None,
    mission_id: str,
    new_status: str,
) -> None:
    """Transition a mission to complete / failed / cancelled / active."""
    from .mission import set_mission_status

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    try:
        mission = set_mission_status(config, project_id, mission_id, new_status)
    except ValueError as exc:
        output_error("mission_state_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(
        f"Mission {mission_id} -> {new_status}",
        data=mission.to_dict(),
        fmt=fmt,
    )


# ============================================================================
# Research commands
# ============================================================================


@main.group()
def research() -> None:
    """Manage research items."""
    pass


@research.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--status", "-s", type=click.Choice(["open", "complete", "stale"]), help="Filter by status")
@click.option("--tags", "-t", multiple=True, help="Filter by tags (must have all)")
@click.pass_context
def research_list(ctx: click.Context, project_id: str | None, status: str | None, tags: tuple[str, ...]) -> None:
    """List research items."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    status_filter = ResearchStatus(status) if status else None
    tags_filter = list(tags) if tags else None

    items = list_research(config, project_id, status_filter=status_filter, tags_filter=tags_filter)
    output_research_list(items, fmt=fmt)


@research.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--type", "-t", "research_type", type=click.Choice(["investigation", "spike", "decision", "reference"]), required=True)
@click.option("--title", required=True, help="Research title")
@click.option("--id", "research_id", help="Research ID (auto-generated if not provided)")
@click.option("--tags", multiple=True, help="Tags")
@click.option("--question", "-q", help="Research question")
@click.pass_context
def research_add(
    ctx: click.Context,
    project_id: str | None,
    research_type: str,
    title: str,
    research_id: str | None,
    tags: tuple[str, ...],
    question: str | None,
) -> None:
    """Add a new research item."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    # Support both -t tag1 -t tag2 and --tags tag1,tag2
    parsed_tags = []
    for tag in tags:
        parsed_tags.extend(t.strip() for t in tag.split(",") if t.strip())

    item = add_research(
        config,
        project_id,
        title,
        ResearchType(research_type),
        research_id=research_id,
        tags=parsed_tags if parsed_tags else None,
        question=question or "",
    )

    if not item:
        output_error("add_failed", f"Failed to add research to project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Research {item.id} created", data=item.to_dict(), fmt=fmt)


@research.command("link")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--id", "research_id", required=True, help="Research ID")
@click.option("--session-key", "-s", required=True, help="OpenClaw session key")
@click.option("--run-id", "-r", help="OpenClaw run ID")
@click.option("--spawned-by", help="Spawning session key")
@click.pass_context
def research_link(
    ctx: click.Context,
    project_id: str | None,
    research_id: str,
    session_key: str,
    run_id: str | None,
    spawned_by: str | None,
) -> None:
    """Link a research item to an OpenClaw session."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    
    project_id, _ = require_project(ctx, project_id)

    item = link_research_session(
        config,
        project_id,
        research_id,
        session_key,
        run_id=run_id,
        spawned_by=spawned_by,
    )

    if not item:
        output_error("link_failed", f"Failed to link research '{research_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Research {research_id} linked to session", data=item.to_dict(), fmt=fmt)


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
    # Delegate to project doctor with no specific project
    ctx.invoke(
        project_doctor,
        project_id=None,
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


# ============================================================================
# Issues commands
# ============================================================================

@main.group("issues")
def issues_group() -> None:
    """Log and track issues found during work."""
    pass


ISSUE_TYPES = ["bug", "ux", "docs", "feature", "observation"]


@issues_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--type", "-t", "issue_type", type=click.Choice(ISSUE_TYPES), default="bug", help="Issue type")
@click.option("--severity", "-s", type=click.Choice(["high", "medium", "low"]), default="medium", help="Severity")
@click.option("--command", "-c", "cmd", help="Command that triggered the issue")
@click.option("--expected", "-e", help="What was expected")
@click.option("--actual", "-a", help="What actually happened")
@click.option("--context", help="Additional context")
@click.option("--tag", "tags", multiple=True, help="Free-form tag (repeatable, e.g. --tag depth-warning --tag ergonomic)")
@click.option("--summary", help="One-line summary (alternative to --expected/--actual for observation-type entries)")
@click.pass_context
def issues_add(
    ctx: click.Context,
    project_id: str | None,
    issue_type: str,
    severity: str,
    cmd: str | None,
    expected: str | None,
    actual: str | None,
    context: str | None,
    tags: tuple[str, ...],
    summary: str | None,
) -> None:
    """Log an issue for a project."""
    import json
    from datetime import datetime, timezone

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    # Create .agent directory if needed
    agent_dir = proj.project_dir / ".agent"
    agent_dir.mkdir(exist_ok=True)
    issues_file = agent_dir / "issues.jsonl"

    entry = {
        "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "type": issue_type,
        "severity": severity,
        "command": cmd,
        "expected": expected,
        "actual": actual,
        "context": context,
        "summary": summary,
        "tags": list(tags) if tags else [],
        "fixed": False,
    }

    # Remove None values but always keep "tags" (even if empty list) and "fixed"
    entry = {k: v for k, v in entry.items() if v is not None}

    with open(issues_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    if fmt == OutputFormat.JSON:
        output_json({"status": "logged", "file": str(issues_file), "entry": entry})
    else:
        click.echo(f"Logged {issue_type} issue ({severity}) to {issues_file}")


@issues_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--open", "open_only", is_flag=True, help="Show only unfixed issues")
@click.option("--type", "-t", "issue_type", type=click.Choice(ISSUE_TYPES), help="Filter by type")
@click.option("--tag", "tag_filter", multiple=True, help="Filter by tag (repeatable; entries matching ANY supplied tag are shown)")
@click.pass_context
def issues_list(
    ctx: click.Context,
    project_id: str | None,
    open_only: bool,
    issue_type: str | None,
    tag_filter: tuple[str, ...],
) -> None:
    """List issues for a project."""
    import json

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    proj = get_project(config, project_id)

    if not proj:
        output_error("project_not_found", f"Project '{project_id}' not found", fmt=fmt)
        sys.exit(1)

    issues_file = proj.project_dir / ".agent" / "issues.jsonl"
    if not issues_file.exists():
        if fmt == OutputFormat.JSON:
            output_json({"issues": [], "count": 0})
        else:
            click.echo("No issues logged yet.")
        return

    tag_filter_set = set(tag_filter)
    issues = []
    with open(issues_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            issue = json.loads(line)
            if open_only and issue.get("fixed"):
                continue
            if issue_type and issue.get("type") != issue_type:
                continue
            if tag_filter_set and not (tag_filter_set & set(issue.get("tags", []))):
                continue
            issues.append(issue)

    if fmt == OutputFormat.JSON:
        output_json({"issues": issues, "count": len(issues)})
    else:
        if not issues:
            click.echo("No issues found.")
            return
        for i, issue in enumerate(issues, 1):
            status = "[OK]" if issue.get("fixed") else "[ ] "
            sev = issue.get("severity", "?")[0].upper()
            typ = issue.get("type", "?")
            tags = issue.get("tags") or []
            tag_str = f" [{','.join(tags)}]" if tags else ""
            desc = issue.get("summary") or issue.get("actual") or issue.get("context") or "No description"
            click.echo(f"{status} [{sev}] {typ}{tag_str}: {desc}")



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


# ============================================================================
# Reflect command group — Phase 2 stubs
# ============================================================================


@main.group()
def reflect() -> None:
    """Reflection layer — query predictions vs actuals (Phase 2 stubs)."""
    pass


@reflect.command("summarize")
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.pass_context
def reflect_summarize(ctx: click.Context, project_id: str | None) -> None:
    """[Phase 2] Summarize prediction accuracy across completed tasks.

    When implemented this will:
    - Aggregate all reflection events for the project
    - Compute distribution stats (duration ratio, files-changed ratio, complexity hit-rate)
    - Surface systematic over/under-estimation patterns for operator review
    """
    import json as _json
    click.echo(_json.dumps({
        "status": "phase2_pending",
        "message": "clawpm reflect summarize is not yet implemented (Phase 2)",
    }, indent=2))


@reflect.command("suggest")
@click.argument("task_id")
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.pass_context
def reflect_suggest(ctx: click.Context, task_id: str, project_id: str | None) -> None:
    """[Phase 2] Suggest predictions for a task based on past reflection history.

    When implemented this will:
    - Load reflection events for the project
    - Find tasks with similar title/scope/complexity to <task_id>
    - Derive calibrated predictions (e.g. 'similar tasks took ~2x longer than predicted')
    - Return suggested --predict-* values the operator can copy-paste
    """
    import json as _json
    click.echo(_json.dumps({
        "status": "phase2_pending",
        "message": f"clawpm reflect suggest is not yet implemented (Phase 2). Task: {task_id}",
    }, indent=2))


@reflect.command("history-import")
@click.option(
    "--source", "source_dir", default=None,
    envvar="CLAWPM_HISTORY_SOURCE",
    help="Path to history source directory (or set CLAWPM_HISTORY_SOURCE).",
)
@click.pass_context
def reflect_history_import(ctx: click.Context, source_dir: str | None) -> None:
    """[Phase 2] Import historical session data as reflection events.

    DESIGN CONSTRAINTS (do not violate in Phase 2 implementation):
    - Source path MUST come from --source flag or CLAWPM_HISTORY_SOURCE env var.
      NO hardcoded paths (e.g. ~/.openclaw/) — this was removed at commit a06a5b8
      precisely because static references to agent-runtime paths raise VirusTotal
      false positives and are an operational security smell.
    - The importer module must be lazy-imported inside this function so the
      clawpm binary does not statically reference suspicious path patterns.
    - clawpm setup should optionally prompt for the history source path during
      init (Phase 2 work) — add a 'history_source' key to portfolio.toml config,
      not a hardcoded default.

    When implemented this will:
    - Walk <source_dir> for session transcripts / agent logs
    - Extract task-ID references, timestamps, file changes
    - Synthesise Actuals records and write reflection events for historical tasks
    - Deduplicate by task_id + occurred_at to allow re-runs safely
    """
    import json as _json
    if not source_dir:
        click.echo(_json.dumps({
            "status": "phase2_pending",
            "message": "clawpm reflect history-import is not yet implemented (Phase 2). "
                       "Provide --source <dir> or set CLAWPM_HISTORY_SOURCE.",
        }, indent=2))
        return
    click.echo(_json.dumps({
        "status": "phase2_pending",
        "message": f"clawpm reflect history-import is not yet implemented (Phase 2). Source: {source_dir}",
    }, indent=2))


@reflect.command("void")
@click.argument("task_id", required=False, default=None)
@click.option("--project", "-p", "project_id", default=None, help="Project ID (auto-detected if not specified). Stamped on the void event for cross-project isolation.")
@click.option("--reason", required=True, help="Why this reflection is bad data (required)")
@click.option(
    "--all-empty-actuals", "all_empty_actuals", is_flag=True,
    help="Void all reflections across the corpus where actuals.duration_min is null",
)
@click.pass_context
def reflect_void(
    ctx: click.Context,
    task_id: str | None,
    project_id: str | None,
    reason: str,
    all_empty_actuals: bool,
) -> None:
    """Mark a reflection event void without deleting it (event-source discipline).

    Appends a void event to the task's .jsonl file.  Does NOT modify or delete
    the original event — consumers skip events with a matching void entry.

    Examples::

        clawpm reflect void PROJ-007 --reason "actuals were wrong — pre-bugfix"
        clawpm reflect void --all-empty-actuals --reason "Phase 1 corpus cleanup"
    """
    import json as _json
    from datetime import datetime, timezone

    config = require_portfolio(ctx)
    fmt = get_format(ctx)
    ref_dir = config.portfolio_root / "reflections"

    voided: list[dict] = []
    errors: list[dict] = []

    def _void_task_reflection(tid: str, project_hint: str | None = None) -> None:
        ref_file = ref_dir / f"{tid}.jsonl"
        if not ref_file.exists():
            errors.append({"task_id": tid, "error": "no_reflection_file"})
            return

        # Read existing events to check whether void already applied
        lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        existing_records = []
        for line in lines:
            try:
                existing_records.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass

        # Cross-project isolation (round-7 audit + round-8 P1 follow-up):
        # the JSONL filename is keyed by task_id alone, so two projects
        # sharing a task_id share a file. Stamp the void event with
        # project_id from the EXPLICIT command-line context only — do
        # not infer from prior file events, because in a shared file
        # the first record could belong to a different project than
        # the operator's `--project` context. When no command-line
        # project is given, fall through to legacy unscoped (matches
        # back-compat for older voids; consumers must treat absent
        # project_id as wildcard).
        resolved_project: str | None = project_hint

        # Build the void entry
        void_entry: dict = {
            "event": "void",
            "task_id": tid,
            "reason": reason,
            "voided_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if resolved_project is not None:
            void_entry["project_id"] = resolved_project

        # Append (atomic: write to .tmp then replace)
        tmp_file = ref_file.with_suffix(".jsonl.tmp")
        try:
            # Copy existing content + new void line
            existing_text = ref_file.read_text(encoding="utf-8")
            tmp_file.write_text(
                existing_text.rstrip("\n") + "\n" + _json.dumps(void_entry) + "\n",
                encoding="utf-8",
            )
            tmp_file.replace(ref_file)
            voided.append({"task_id": tid, "voided_at": void_entry["voided_at"]})
        except OSError as exc:
            errors.append({"task_id": tid, "error": str(exc)})

    # Resolve project_id context for the single-task path. ONLY use
    # the explicit --project flag, never auto-detect from CWD —
    # auto-detect can stamp the void with the dev environment's
    # project instead of the task's real project, which is worse than
    # stamping unscoped (legacy consumers wildcard on absent
    # project_id, scoped consumers in shared-task-id files get
    # mis-attribution). When --project is omitted we fall through to
    # legacy unscoped behaviour, which is back-compat-safe.
    cli_project_id: str | None = project_id  # explicit only

    if all_empty_actuals:
        if not ref_dir.exists():
            output_json({"voided": [], "errors": [], "message": "No reflections directory found"})
            return
        for ref_file in sorted(ref_dir.glob("*.jsonl")):
            derived_tid = ref_file.stem
            lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            has_empty_actuals = False
            for line in lines:
                try:
                    rec = _json.loads(line)
                    if rec.get("event") in ("task_done", "task_blocked"):
                        actuals = rec.get("actuals", {})
                        if actuals.get("duration_min") is None:
                            has_empty_actuals = True
                            break
                except _json.JSONDecodeError:
                    pass
            if has_empty_actuals:
                # Corpus sweep emits unscoped voids (cross-project-safe
                # only because the consumer wildcards on absent
                # project_id). Don't try to infer project from prior
                # records — same hazard Codex flagged.
                _void_task_reflection(derived_tid)
    elif task_id:
        _void_task_reflection(task_id, project_hint=cli_project_id)
    else:
        output_error(
            "missing_target",
            "Provide a TASK_ID or use --all-empty-actuals",
            fmt=fmt,
        )
        sys.exit(1)

    output_json({
        "voided": voided,
        "errors": errors,
        "count": len(voided),
    })


# ============================================================================
# Inbox commands
# ============================================================================

from .inbox import (  # noqa: E402 — local import to keep group self-contained
    send_message as _inbox_send,
    read_inbox as _inbox_read,
    ack_messages as _inbox_ack,
    get_thread as _inbox_thread,
)


@main.group("inbox")
def inbox_group() -> None:
    """Inter-agent messaging. Filesystem-first, append-only, no daemons."""
    pass


@inbox_group.command("send")
@click.option("--to", "to_agent", required=True, help="Recipient agent ID")
@click.option("--message", "message", default=None, help="Message text (or '-' to read from stdin)")
@click.option("--stdin", "read_stdin", is_flag=True, default=False, help="Read message from stdin")
@click.option("--from", "from_agent", default="main", help="Sender agent ID (default: main)")
@click.option("--in-reply-to", "in_reply_to", default=None, help="msg_id this message replies to")
@click.option("--project", "project_id", default=None, help="Project context for the message")
@click.option("--task", "task_id", default=None, help="Task context for the message")
@click.pass_context
def inbox_send(
    ctx: click.Context,
    to_agent: str,
    message: str | None,
    read_stdin: bool,
    from_agent: str,
    in_reply_to: str | None,
    project_id: str | None,
    task_id: str | None,
) -> None:
    """Send a message to an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if message == "-" or read_stdin:
        message = sys.stdin.read()
    if not message:
        output_error("missing_message", "Provide --message text or pass --stdin / --message -", fmt=fmt)
        sys.exit(1)

    event = _inbox_send(
        portfolio_root=config.portfolio_root,
        to=to_agent,
        message=message,
        from_agent=from_agent,
        in_reply_to=in_reply_to,
        project=project_id,
        task=task_id,
    )
    output_json({"msg_id": event["msg_id"], "to": event["to"], "ts": event["ts"]})


@inbox_group.command("read")
@click.option("--agent", "agent_id", required=True, help="Whose inbox to read")
@click.option("--unacked", "filter_mode", flag_value="unacked", default=True, help="Show only unacked messages (default)")
@click.option("--all", "filter_mode", flag_value="all", help="Show all messages including acked")
@click.option("--since", "since", default=None, help="Filter messages at or after this date/timestamp (YYYY-MM-DD or ISO)")
@click.option("--from", "from_filter", default=None, help="Filter messages from this sender")
@click.pass_context
def inbox_read(
    ctx: click.Context,
    agent_id: str,
    filter_mode: str,
    since: str | None,
    from_filter: str | None,
) -> None:
    """Read messages from an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    unacked_only = filter_mode == "unacked"
    messages = _inbox_read(
        portfolio_root=config.portfolio_root,
        agent_id=agent_id,
        unacked_only=unacked_only,
        since=since,
        from_filter=from_filter,
    )
    output_json(messages)


@inbox_group.command("ack")
@click.argument("msg_ids", nargs=-1, required=True)
@click.option("--agent", "acked_by", default="main", help="Agent performing the ack (default: main)")
@click.pass_context
def inbox_ack(ctx: click.Context, msg_ids: tuple[str, ...], acked_by: str) -> None:
    """Acknowledge one or more messages (marks them as read)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    result = _inbox_ack(
        portfolio_root=config.portfolio_root,
        msg_ids=list(msg_ids),
        acked_by=acked_by,
    )
    output_json(result)


@inbox_group.command("thread")
@click.argument("msg_id")
@click.pass_context
def inbox_thread(ctx: click.Context, msg_id: str) -> None:
    """Show the full thread containing a message, sorted by timestamp."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    thread = _inbox_thread(portfolio_root=config.portfolio_root, msg_id=msg_id)
    output_json(thread)


# ============================================================================
# Inbox commands
# ============================================================================

from .inbox import (  # noqa: E402 — local import to keep group self-contained
    send_message as _inbox_send,
    read_inbox as _inbox_read,
    ack_messages as _inbox_ack,
    get_thread as _inbox_thread,
)


@main.group("inbox")
def inbox_group() -> None:
    """Inter-agent messaging. Filesystem-first, append-only, no daemons."""
    pass


@inbox_group.command("send")
@click.option("--to", "to_agent", required=True, help="Recipient agent ID")
@click.option("--message", "message", default=None, help="Message text (or '-' to read from stdin)")
@click.option("--stdin", "read_stdin", is_flag=True, default=False, help="Read message from stdin")
@click.option("--from", "from_agent", default="main", help="Sender agent ID (default: main)")
@click.option("--in-reply-to", "in_reply_to", default=None, help="msg_id this message replies to")
@click.option("--project", "project_id", default=None, help="Project context for the message")
@click.option("--task", "task_id", default=None, help="Task context for the message")
@click.pass_context
def inbox_send(
    ctx: click.Context,
    to_agent: str,
    message: str | None,
    read_stdin: bool,
    from_agent: str,
    in_reply_to: str | None,
    project_id: str | None,
    task_id: str | None,
) -> None:
    """Send a message to an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if message == "-" or read_stdin:
        message = sys.stdin.read()
    if not message:
        output_error("missing_message", "Provide --message text or pass --stdin / --message -", fmt=fmt)
        sys.exit(1)

    event = _inbox_send(
        portfolio_root=config.portfolio_root,
        to=to_agent,
        message=message,
        from_agent=from_agent,
        in_reply_to=in_reply_to,
        project=project_id,
        task=task_id,
    )
    output_json({"msg_id": event["msg_id"], "to": event["to"], "ts": event["ts"]})


@inbox_group.command("read")
@click.option("--agent", "agent_id", required=True, help="Whose inbox to read")
@click.option("--unacked", "filter_mode", flag_value="unacked", default=True, help="Show only unacked messages (default)")
@click.option("--all", "filter_mode", flag_value="all", help="Show all messages including acked")
@click.option("--since", "since", default=None, help="Filter messages at or after this date/timestamp (YYYY-MM-DD or ISO)")
@click.option("--from", "from_filter", default=None, help="Filter messages from this sender")
@click.pass_context
def inbox_read(
    ctx: click.Context,
    agent_id: str,
    filter_mode: str,
    since: str | None,
    from_filter: str | None,
) -> None:
    """Read messages from an agent's inbox."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    unacked_only = filter_mode == "unacked"
    messages = _inbox_read(
        portfolio_root=config.portfolio_root,
        agent_id=agent_id,
        unacked_only=unacked_only,
        since=since,
        from_filter=from_filter,
    )
    output_json(messages)


@inbox_group.command("ack")
@click.argument("msg_ids", nargs=-1, required=True)
@click.option("--agent", "acked_by", default="main", help="Agent performing the ack (default: main)")
@click.pass_context
def inbox_ack(ctx: click.Context, msg_ids: tuple[str, ...], acked_by: str) -> None:
    """Acknowledge one or more messages (marks them as read)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    result = _inbox_ack(
        portfolio_root=config.portfolio_root,
        msg_ids=list(msg_ids),
        acked_by=acked_by,
    )
    output_json(result)


@inbox_group.command("thread")
@click.argument("msg_id")
@click.pass_context
def inbox_thread(ctx: click.Context, msg_id: str) -> None:
    """Show the full thread containing a message, sorted by timestamp."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    thread = _inbox_thread(portfolio_root=config.portfolio_root, msg_id=msg_id)
    output_json(thread)


# ============================================================================
# Serve command
# ============================================================================

@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
def serve(host: str, port: int) -> None:
    """Start the web UI server."""
    import uvicorn
    from .serve import create_app

    app = create_app()
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
