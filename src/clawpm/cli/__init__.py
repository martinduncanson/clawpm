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

# Re-exports: symbols that moved into group modules but are still referenced
# via the historical `clawpm.cli.<name>` path (by the domain layer and tests).
from clawpm.cli.conflicts import _globs_overlap  # noqa: F401
from clawpm.cli.serve import _load_web_server  # noqa: F401

# Task-mutation entry points consumed by the not-yet-extracted top-level
# shortcut commands (quick_add/done/start/block/…) still defined in this
# module. These imports move into cli/shortcuts.py when that group is extracted.
from clawpm.cli.tasks import (  # noqa: F401
    tasks_add,
    tasks_state,
    _do_state_change_isolated,
    _render_state_results,
)

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
    from datetime import date, datetime, timezone, timedelta

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
    semble_advice: list[dict] = []
    encoding_risks: list[dict] = []
    dangling_links: list[dict] = []  # CLAWP-082

    STALE_DAYS = 7
    CODEGRAPH_FILE_THRESHOLD = 50  # below this we don't bother advising
    DOC_FILE_THRESHOLD = 30  # prose files below which semble isn't worth it

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

        # --- CLAWP-082: dangling wiki-link check ---
        # A [[id]] whose target is not a known task/research/mission id in this
        # project is a broken reference — surface it. Typed edges have their
        # own integrity checks; this covers only the freeform wiki graph.
        try:
            from clawpm.links import find_dangling_links
            dangling_links.extend(find_dangling_links(config, proj.id))
        except Exception:
            # Never let a link-scan hiccup abort the whole health check.
            pass

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
            # CLAWP-086 — prefer the task's own `updated` stamp over file mtime,
            # which lies after git operations / syncs / external edits. Fall back
            # to mtime for legacy tasks that predate the stamp.
            last_touched = mtime
            try:
                _fm, _ = parse_frontmatter(
                    progress_file.read_text(encoding="utf-8", errors="replace")
                )
                _updated = _fm.get("updated") if isinstance(_fm, dict) else None
                if _updated:
                    _u = datetime.fromisoformat(str(_updated).rstrip("Z"))
                    if _u.tzinfo is None:
                        _u = _u.replace(tzinfo=timezone.utc)
                    last_touched = _u
            except (ValueError, OSError):
                pass
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
            # Defensive: one unreadable/odd file must never abort the whole
            # doctor drift walk (matches the pre-CLAWP-079 broad guard here).
            try:
                fm, _ = parse_frontmatter(text)
                if isinstance(fm, dict):
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
                # CLAWP-086 — prefer the task's `updated` stamp (blocked
                # transitions stamp it) over the blocked-file mtime, which lies
                # after a checkout/sync. `bt` is already parsed above. Fall back
                # to mtime for legacy tasks or an unparseable stamp.
                btime: datetime | None = None
                if bt.updated:
                    try:
                        # CLAWP-086 (Codex review): `updated` is a date-only
                        # stamp — too coarse for the 24h cutoff. Interpret it
                        # CONSERVATIVELY as end-of-day UTC so a task blocked late
                        # on day D isn't falsely reported the next morning;
                        # genuinely stale multi-day blocks are still caught.
                        _bd = date.fromisoformat(str(bt.updated).strip())
                        btime = datetime(
                            _bd.year, _bd.month, _bd.day, 23, 59, 59,
                            tzinfo=timezone.utc,
                        )
                    except ValueError:
                        btime = None
                if btime is None:
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
    from clawpm.codegraph import count_code_files, is_project_indexed
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

    # --- CLAWP-036: Semble advisory (doc / knowledge content shape) ---
    # INDEPENDENT of the CodeGraph advisory above. CodeGraph indexes code
    # symbols / call-graphs only and is blind to prose; semble adds
    # semantic search over markdown/prose + config via its --content
    # docs/all modes. A mixed repo (lots of code AND lots of docs) should
    # get BOTH advisories — neither suppresses the other. Soft signal,
    # never auto-applied. Idempotent via the .clawpm-semble index marker.
    from clawpm.semble import count_doc_files, is_doc_indexed
    for proj in projects_to_check:
        if not proj.repo_path or not proj.repo_path.exists():
            continue
        if is_doc_indexed(proj.repo_path):
            continue
        doc_count = count_doc_files(proj.repo_path)
        if doc_count >= DOC_FILE_THRESHOLD:
            semble_advice.append({
                "project_id": proj.id,
                "repo_path": proj.repo_path.as_posix(),
                "doc_files": doc_count,
                "suggested_action": (
                    f"Doc-heavy project ({doc_count} prose files). CodeGraph "
                    f"indexes code symbols only; semble can cover prose + "
                    f"config too — but its `index` subcommand defaults to "
                    f"code-only, so the docs/config must be pulled in at "
                    f"index time with --include-text-files (not --content, "
                    f"which is search-time only). Build a content-aware "
                    f"index: `uvx --from \"semble[mcp]\" semble index "
                    f"\"{proj.repo_path.as_posix()}\" --include-text-files -o "
                    f"\"{proj.repo_path.as_posix()}/.clawpm-semble\"`, then "
                    f"search it with `semble search \"<query>\" --index "
                    f"\"{proj.repo_path.as_posix()}/.clawpm-semble\" --content all`."
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
            try:
                findings = scan_path(proj.repo_path)
            except Exception as exc:
                # rglob can hit junctions / permission errors on Windows.
                # Surface as a structured finding rather than aborting doctor.
                encoding_risks.append({
                    "project_id": proj.id,
                    "file": proj.repo_path.as_posix(),
                    "line": 0,
                    "rule": "scan-failed",
                    "evidence": f"{type(exc).__name__}: {exc}",
                })
                continue
            for finding in findings:
                encoding_risks.append({"project_id": proj.id, **finding})

    # --- Phase 1.6 Check c: Cross-project prefix collisions (CLAWP-048) ---
    # Use each project's RESOLVED prefix (explicit task_prefix -> inferred from
    # existing tasks -> [:5] for the as-yet-unminted), so the check reflects the
    # IDs actually being minted: a task_prefix override clears a false collision,
    # and an inferred/derived prefix surfaces a real one the naive [:5] missed.
    from clawpm.tasks import resolve_existing_prefix as _resolve_prefix

    prefix_map: dict[str, list[str]] = {}
    all_projects = discover_projects(config)
    for proj in all_projects:
        prefix = _resolve_prefix(proj) or proj.id.upper()[:5]
        prefix_map.setdefault(prefix, []).append(proj.id)
    prefix_collisions = [
        {"prefix": pfx, "projects": pids}
        for pfx, pids in prefix_map.items()
        if len(pids) > 1
    ]

    # --- CLAWP-039: expired dispatch leases (crash-safe dispatch) ---
    # No daemon — doctor is one of the two lazy expiry detectors (the other is
    # the next `tasks dispatch`). Detect here; remediate (apply the fallback)
    # under --apply.
    expired_lease_findings: list[dict] = []
    try:
        from clawpm.leases import expired_leases as _expired_leases
        _now_doc = datetime.now(timezone.utc)
        for _l in _expired_leases(config.portfolio_root, _now_doc, project_id=project_id):
            _age = int((_now_doc - _l.last_heartbeat_at).total_seconds())
            expired_lease_findings.append({
                "task_id": _l.task_id,
                "project_id": _l.project_id,
                "ttl_seconds": _l.ttl_seconds,
                "age_seconds": _age,
                "fallback_policy": _l.fallback_policy.value,
                "suggested_action": "run `clawpm lease sweep` (or `clawpm doctor --apply`)",
            })
    except Exception as _lease_exc:
        # A diagnostic command must declare its blind spots, never imply "clean"
        # when it couldn't check (Codex/silent-failure). Surface as an issue.
        issues.append({
            "level": "warning", "scope": "leases",
            "message": f"could not evaluate dispatch leases: "
                       f"{type(_lease_exc).__name__}: {_lease_exc}",
        })
        expired_lease_findings = []

    # Build final output
    has_warnings = bool(
        stale_tasks or stale_blocked or drift_tasks or prefix_collisions or unreadable_files
        or commit_drift or missing_markers or codex_availability or encoding_risks
        or expired_lease_findings or dangling_links
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
            from clawpm.doctor_apply import run_apply_phase

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

            # CLAWP-039: expired-lease fallback is a deterministic remediation
            # arm. Apply it here so `doctor --apply` reaps dead holders. SCOPE
            # the sweep to project_id so a project-scoped run never reaps another
            # project's leases (cross-project isolation — Codex critical).
            if expired_lease_findings and not dry_run:
                from clawpm.leases import sweep as _lease_sweep
                for _act in _lease_sweep(config, config.portfolio_root, project_id=project_id):
                    target = f"{_act['task_id']} ({_act['project_id']})"
                    if _act.get("retired_without_fallback"):
                        applied.append({"class": "lease-expired", "target": target,
                                        "result": f"retired (task already {_act['resulting_state']})"})
                    elif _act.get("transitioned"):
                        applied.append({"class": "lease-expired", "target": target,
                                        "result": f"{_act['policy']} -> {_act['resulting_state']}"})
                    else:
                        # Transition failed — lease left ACTIVE for retry, NOT a
                        # success. Surface it as skipped, never as [APPLIED].
                        apply_skipped.append({"class": "lease-expired", "target": target,
                                             "reason": f"transition failed ({_act.get('transition_error')}); "
                                                       "lease kept active for retry"})
            elif expired_lease_findings and dry_run:
                for _f in expired_lease_findings:
                    apply_skipped.append({
                        "class": "lease-expired",
                        "target": f"{_f['task_id']} ({_f['project_id']})",
                        "reason": f"dry-run: would apply {_f['fallback_policy']}",
                    })

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
            "semble_advice": semble_advice,
            "encoding_risks": encoding_risks,
            "expired_leases": expired_lease_findings,
            "dangling_links": dangling_links,
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
            or commit_drift or missing_markers or codex_availability or codegraph_advice
            or semble_advice or encoding_risks or expired_lease_findings or dangling_links
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
            for sa in semble_advice:
                click.echo(
                    f"[ADVICE] [semble] {sa['project_id']} "
                    f"({sa['doc_files']} doc files, no .clawpm-semble) "
                    f"- {sa['suggested_action']}"
                )
            if encoding_risks:
                files_with_risk = {er["file"] for er in encoding_risks}
                click.echo(
                    f"[WARNING] [encoding-risk] {len(encoding_risks)} findings "
                    f"across {len(files_with_risk)} files"
                )
            for er in encoding_risks:
                click.echo(
                    f"[WARNING] [encoding-risk:{er['rule']}] {er['project_id']} "
                    f"{er['file']}:{er['line']} - {er['evidence']}"
                )
            for el in expired_lease_findings:
                click.echo(
                    f"[WARNING] [lease-expired] {el['task_id']} ({el['project_id']}) "
                    f"- no heartbeat for {el['age_seconds']}s (TTL {el['ttl_seconds']}s); "
                    f"fallback {el['fallback_policy']}. {el['suggested_action']}"
                )
            for dl in dangling_links:
                click.echo(
                    f"[WARNING] [dangling-link] {dl['source']} ({dl['project_id']}) "
                    f"-> [[{dl['target']}]] references an unknown id."
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
@click.argument("task_ids", nargs=-1, required=True)
@click.option("--note", "-n", help="Completion note (applies to ALL listed tasks)")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why?")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this?")
@click.option("--surprise", "surprise_tags", multiple=True, help="Surprise taxonomy tag (repeatable): unknown_unknown, scope_drift, dependency, tooling_friction, complexity_misread, assumption_broke, external_blocker")
@click.pass_context
def quick_done(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, force: bool, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...]) -> None:
    """Mark one or many tasks as done (alias for 'tasks state <ids...> done')."""
    ctx.invoke(tasks_state, project_id=project_id, task_ids=task_ids, new_state="done", note=note, force=force, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags)


@main.command("start")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_ids", nargs=-1, required=True)
@click.pass_context
def quick_start(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...]) -> None:
    """Start working on one or many tasks (alias for 'tasks state <ids...> progress').

    Note: if a task is already in progress, prefer 'clawpm log add --action progress'
    to avoid resetting the duration anchor.
    """
    config = require_portfolio(ctx)

    # Warn (but don't block) for any task already in progress.
    # Re-starting corrupts the duration anchor — the reflection layer computes
    # actuals from the *first* start event, so a re-start under-counts elapsed time.
    resolved_project_id, _ = require_project(ctx, project_id, required=False)
    if resolved_project_id:
        for tid in task_ids:
            try:
                _expanded = expand_task_id(tid, resolved_project_id)
                _task = get_task(config, resolved_project_id, _expanded)
                if _task and _task.state and _task.state.value == "progress":
                    click.echo(
                        f"Warning: {_expanded} is already in progress. "
                        "Re-starting resets the duration anchor and under-counts elapsed time. "
                        "Use 'clawpm log add --task <id> --action progress --summary \"...\"' "
                        "to log midway updates instead.",
                        err=True,
                    )
            except Exception as _guard_exc:
                # Never let the advisory guard break the start command. Stay
                # silent by default (a warning that can't render shouldn't abort
                # the batch), but leave a CLAWPM_DEBUG breadcrumb so a persistent
                # guard failure across a batch isn't wholly invisible.
                if os.environ.get("CLAWPM_DEBUG"):
                    click.echo(
                        f"clawpm: start guard for {tid!r} failed: {_guard_exc!r}",
                        err=True,
                    )

    ctx.invoke(tasks_state, project_id=project_id, task_ids=task_ids, new_state="progress", note=None)


@main.command("block")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_ids", nargs=-1, required=True)
@click.option("--note", "-n", help="Blocker description (applies to ALL listed tasks)")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why?")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this?")
@click.option("--surprise", "surprise_tags", multiple=True, help="Surprise taxonomy tag (repeatable): unknown_unknown, scope_drift, dependency, tooling_friction, complexity_misread, assumption_broke, external_blocker")
@click.pass_context
def quick_block(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...]) -> None:
    """Mark one or many tasks as blocked (alias for 'tasks state <ids...> blocked')."""
    ctx.invoke(tasks_state, project_id=project_id, task_ids=task_ids, new_state="blocked", note=note, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags)


@main.command("unblock")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_ids", nargs=-1, required=True)
@click.option("--note", "-n", help="Reason the blocker was resolved (applies to ALL listed tasks)")
@click.option("--start", "also_start", is_flag=True, help="Also transition to in-progress (blocked → progress)")
@click.pass_context
def quick_unblock(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, also_start: bool) -> None:
    """Move one or many blocked tasks back to open (or --start to go straight to in-progress).

    Shortcut for:
        clawpm tasks state <ids...> open   (default)
        clawpm tasks state <ids...> progress  (with --start)

    An 'unblock' action is logged in the work log for each task the transition
    succeeds on. Per-task error isolation: a task that is not blocked (or does
    not exist) fails that entry without aborting the rest of the batch.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)
    new_state_str = "progress" if also_start else "open"

    seen: set[str] = set()
    results: list[dict] = []
    for raw in task_ids:
        full_task_id = expand_task_id(raw, project_id)
        if full_task_id in seen:
            continue
        seen.add(full_task_id)

        # Verify the task is actually blocked (per-task, isolated).
        task = get_task(config, project_id, full_task_id)
        if not task:
            results.append({
                "ok": False, "task_id": full_task_id, "error": "task_not_found",
                "message": f"No task with id '{full_task_id}' in project '{project_id}'",
            })
            continue
        if task.state != TaskState.BLOCKED:
            results.append({
                "ok": False, "task_id": full_task_id, "error": "not_blocked",
                "message": (
                    f"Task {full_task_id} is in state '{task.state.value}', not 'blocked'. "
                    "Use 'clawpm tasks state <id> open' to change state directly."
                ),
            })
            continue

        r = _do_state_change_isolated(
            len(task_ids) > 1, config,
            project_id=project_id, task_id=full_task_id, new_state=new_state_str,
            note=note,
        )
        if r.get("ok"):
            # Log the explicit unblock action only when the transition landed.
            # Best-effort: the transition is already durable, so a work-log
            # append failure must not abort the batch — surface it as a marker.
            try:
                add_entry(
                    config,
                    project=project_id,
                    action=WorkLogAction.UNBLOCK,
                    task=full_task_id,
                    summary=note or "Blocker resolved",
                    auto=True,
                )
            except Exception as exc:
                r.setdefault("data", {}).setdefault("log_errors", []).append(
                    {"error_class": type(exc).__name__, "message": str(exc)}
                )
        results.append(r)

    _render_state_results(results, new_state_str, project_id, fmt, batch=len(task_ids) > 1)


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
        from clawpm.tasks import select_next_batch
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
                    click.echo("\nSCOPE CONFLICTS - cannot dispatch as a single batch:")
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
            from clawpm.hints import hints_for_next_task, hints_enabled
            _h = hints_for_next_task(task) if hints_enabled(ctx) else None
            output_task_detail(task, fmt=fmt, hints=_h)
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
    
    # CLAWP-082 — build the derived link index once and attach backlinks
    # (`linked_from`) to every task dict surfaced below. `links` (outbound
    # wiki-links) already rides along in each task's to_dict().
    from clawpm.links import build_link_index
    _link_index = build_link_index(config, resolved_id)

    def _with_backlinks(t: Task) -> dict:
        d = t.to_dict()
        d["linked_from"] = _link_index.linked_from(t.id)
        return d

    # Current task (in progress)
    in_progress = list_tasks(config, resolved_id, state_filter=TaskState.PROGRESS)
    context["in_progress"] = [_with_backlinks(t) for t in in_progress]

    # Next task if nothing in progress
    if not in_progress:
        next_task = get_next_task(config, resolved_id)
        if next_task:
            context["next_task"] = _with_backlinks(next_task)

    # Blocked tasks
    blocked = list_tasks(config, resolved_id, state_filter=TaskState.BLOCKED)
    context["blockers"] = [_with_backlinks(t) for t in blocked]
    
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
                encoding="utf-8",  # CLAWP-046: git output is UTF-8, not cp1252
                errors="replace",
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
                encoding="utf-8",  # CLAWP-046: git output is UTF-8, not cp1252
                errors="replace",
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
                encoding="utf-8",  # CLAWP-046: git output is UTF-8, not cp1252
                errors="replace",
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
                with open(issues_file, encoding="utf-8", errors="replace") as f:
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

