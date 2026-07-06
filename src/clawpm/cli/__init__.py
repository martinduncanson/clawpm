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
    type=click.Choice(["open", "progress", "done", "blocked", "rejected", "all"]),
    default=None,
    help="Filter by state (default: open+progress+blocked; use 'rejected' for the won't-do ledger)",
)
@click.option("--flat", is_flag=True, help="Show flat list without hierarchy")
@click.option("--tag", "tags", multiple=True, help="Filter by workstream tag (CLAWP-069, repeatable). Repeated --tag is OR (matches any); add --all-tags for AND (matches all).")
@click.option("--all-tags", "all_tags", is_flag=True, default=False, help="Require ALL --tag values (AND) instead of the default any-of (OR).")
@click.option("--text", "text", default=None, help="Filter by text over title + body (CLAWP-082). Substring by default; add --regex for a case-insensitive regex.")
@click.option("--regex", "use_regex", is_flag=True, default=False, help="Treat --text as a case-insensitive regular expression.")
@click.option("--priority", "priority", default=None, help="Filter by priority (CLAWP-082): exact ('5') or comparator ('<=3', '>7').")
@click.option("--complexity", "complexities", multiple=True, type=click.Choice(["s", "m", "l", "xl"]), help="Filter by complexity (CLAWP-082, repeatable, OR).")
@click.option("--parent", "parent", default=None, help="Only the direct subtasks of this parent task id (CLAWP-082).")
@click.option("--linked", "linked", default=None, help="Only tasks referencing this id via a [[wiki-link]] or a typed edge (CLAWP-082).")
@click.option("--limit", "limit", type=int, default=None, help="Cap the number of results after filtering + sorting (CLAWP-082).")
@click.pass_context
def tasks_list(ctx: click.Context, project_id: str | None, state: str | None, flat: bool, tags: tuple[str, ...], all_tags: bool, text: str | None, use_regex: bool, priority: str | None, complexities: tuple[str, ...], parent: str | None, linked: str | None, limit: int | None) -> None:
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

    # CLAWP-069/082 — composable filter pass. Every axis is a `by_*` predicate
    # combined with AND via apply_filters (a task must satisfy all of them).
    from clawpm.filters import (
        apply_filters, by_complexity, by_linked, by_parent, by_priority,
        by_tags, by_text,
    )
    filter_list = []
    if tags:
        filter_list.append(by_tags(tags, match_all=all_tags))
    if text:
        filter_list.append(by_text(text, use_regex=use_regex))
    if priority is not None:
        filter_list.append(by_priority(priority))
    if complexities:
        filter_list.append(by_complexity(complexities))
    if parent:
        filter_list.append(by_parent(expand_task_id(parent, project_id)))
    if linked:
        from clawpm.links import build_link_index
        index = build_link_index(config, project_id)
        # Resolve both the expanded (task-style) id and the raw ref so --linked
        # works for research/mission ids that expand_task_id would leave alone.
        refs: set[str] = set()
        for target in {expand_task_id(linked, project_id), linked}:
            refs |= index.referencing_ids(target)
        filter_list.append(by_linked(refs))

    if filter_list:
        found_tasks = apply_filters(found_tasks, filter_list)

    if limit is not None and limit >= 0:
        found_tasks = found_tasks[:limit]

    output_tasks_list(found_tasks, fmt=fmt, flat=flat)


@tasks.command("tags")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--include-done/--no-include-done", "include_done", default=True, help="Include terminal (done + rejected) tasks in the tally (default: yes).")
@click.pass_context
def tasks_tags(ctx: click.Context, project_id: str | None, include_done: bool) -> None:
    """List distinct workstream tags with task counts (CLAWP-069)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    project_id, _ = require_project(ctx, project_id)

    pairs = distinct_tags(config, project_id, include_done=include_done)

    if fmt == OutputFormat.JSON:
        output_json([{"tag": tag, "count": count} for tag, count in pairs])
    else:
        if not pairs:
            click.echo("No tags found")
            return
        for tag, count in pairs:
            click.echo(f"{count:>4}  {tag}")


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

    from clawpm.hints import hints_for_shown_task, hints_enabled
    _hints = hints_for_shown_task(task) if hints_enabled(ctx) else None

    # CLAWP-082 — derive backlinks at read time. `links` (outbound wiki-links)
    # is already on the task; `linked_from` unifies inbound wiki + typed edges.
    from clawpm.links import build_link_index
    _index = build_link_index(config, project_id)
    _linked_from = _index.linked_from(task_id)

    if fmt == OutputFormat.JSON:
        task_dict = task.to_dict()
        task_dict["reflections_voided"] = reflections_voided
        task_dict["linked_from"] = _linked_from
        if _hints:
            task_dict["hints"] = _hints
        output_json(task_dict)
    else:
        output_task_detail(task, fmt=fmt, hints=_hints)
        if task.links:
            click.echo("[links: " + ", ".join(task.links) + "]")
        if _linked_from:
            click.echo(
                "[linked_from: "
                + ", ".join(f"{lf['id']} ({lf['via']})" for lf in _linked_from)
                + "]"
            )
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
@click.option("--scope-file", "scope_file", default=None, type=click.Path(), help="Read scope glob patterns from file (one per line). Windows-safe: bypasses CRT argv glob-expansion. Use instead of --scope when patterns contain wildcards.")
@click.option("--parallel-group", "parallel_group", type=int, default=None, help="Batch ordinal for parallel dispatch (CLAWP-021). Use --clear-parallel-group to remove.")
@click.option("--clear-parallel-group", "clear_parallel_group", is_flag=True, default=False, help="Remove parallel_group from the task — opts out of batch dispatch.")
@click.option("--tag", "tags", multiple=True, help="Workstream tags (CLAWP-069, repeatable). REPLACES the task's tag set (mirrors --scope). Use --clear-tags to remove all.")
@click.option("--clear-tags", "clear_tags", is_flag=True, default=False, help="Remove all tags from the task.")
# --- Prediction flags (all optional) ---
@click.option("--predict-duration", "predict_duration", default=None, help="Predicted duration: 90, 90m, 2h, 3d, 1w")
@click.option("--predict-complexity", "predict_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="Predicted complexity")
@click.option("--predict-files-changed", "predict_files_changed", type=int, default=None, help="Predicted number of files changed")
@click.option("--predict-scope", "predict_scope", multiple=True, help="Predicted file glob scope (can specify multiple)")
@click.option("--predict-scope-file", "predict_scope_file", default=None, type=click.Path(), help="Read predicted-scope patterns from file (one per line). Windows-safe alternative to --predict-scope for glob patterns.")
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
@click.option("--predict-iterations", "predict_iterations", type=int, default=None, help="Predicted iterate->grade->revise cycles (CLAWP-019). Default None; 1 means 'expected to land in one pass'.")
# --- CLAWP-054 dispatch contract fields ---
@click.option("--out-of-scope", "out_of_scope", multiple=True, help="Boundary items the executor MUST NOT touch (repeatable).")
@click.option("--out-of-scope-file", "out_of_scope_file", default=None, type=click.Path(), help="Read out-of-scope patterns from file (one per line). Windows-safe alternative to --out-of-scope for glob patterns.")
@click.option("--stop-condition", "stop_conditions", multiple=True, help="Escape-hatch conditions (repeatable).")
@click.option(
    "--delegability", "delegability",
    type=click.Choice(["agent", "human", "either"]),
    default=None,
    help="Who may execute this task. 'human' means auto-dispatch is REFUSED.",
)
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
    scope_file: str | None,
    parallel_group: int | None,
    clear_parallel_group: bool,
    tags: tuple[str, ...],
    clear_tags: bool,
    predict_duration: str | None,
    predict_complexity: str | None,
    predict_files_changed: int | None,
    predict_scope: tuple[str, ...],
    predict_scope_file: str | None,
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
    out_of_scope: tuple[str, ...] = (),
    out_of_scope_file: str | None = None,
    stop_conditions: tuple[str, ...] = (),
    delegability: str | None = None,
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

    # Merge file-sourced patterns (literal, no CRT expansion) with inline flags.
    # File patterns are appended so --scope and --scope-file coexist naturally.
    if scope_file:
        scope = tuple(list(scope) + _read_patterns_file(scope_file, "--scope-file", fmt))
    if predict_scope_file:
        predict_scope = tuple(list(predict_scope) + _read_patterns_file(predict_scope_file, "--predict-scope-file", fmt))
    if out_of_scope_file:
        out_of_scope = tuple(list(out_of_scope) + _read_patterns_file(out_of_scope_file, "--out-of-scope-file", fmt))

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

    if not any([title, priority is not None, complexity, body, scope, scope_file, has_predictions, parallel_group is not None, clear_parallel_group,
                 out_of_scope, out_of_scope_file, stop_conditions, delegability is not None, tags, clear_tags]):
        output_error("no_changes", "Specify at least one field to edit (--title, --priority, --complexity, --body, --scope, --scope-file, --parallel-group, --clear-parallel-group, --tag, --clear-tags, --predict-*, --out-of-scope, --out-of-scope-file, --stop-condition, or --delegability)", fmt=fmt)
        sys.exit(1)

    if parallel_group is not None and clear_parallel_group:
        output_error("conflicting_flags", "Cannot use both --parallel-group and --clear-parallel-group", fmt=fmt)
        sys.exit(1)

    if tags and clear_tags:
        output_error("conflicting_flags", "Cannot use both --tag and --clear-tags", fmt=fmt)
        sys.exit(1)

    cmplx = TaskComplexity(complexity) if complexity else None
    scope_list = list(scope) if scope else None

    predictions: Predictions | None = None
    if has_predictions:
        from clawpm.reflect import parse_duration as _parse_duration
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
    with _mutation_errors(fmt, "edit_failed"):
        task = edit_task(
            config,
            project_id,
            task_id,
            title=title,
            priority=priority,
            complexity=cmplx,
            scope=scope_list,
            tags=list(tags) if tags else None,
            clear_tags=clear_tags,
            body=body,
            predictions=predictions,
            parallel_group=parallel_group,
            clear_parallel_group=clear_parallel_group,
            out_of_scope=list(out_of_scope) if out_of_scope else None,
            stop_conditions=list(stop_conditions) if stop_conditions else None,
            delegability=delegability,
        )

    if not task:
        output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
        sys.exit(1)

    output_success(f"Task {task_id} updated", data=task.to_dict(), fmt=fmt)


def _do_state_change(
    config,
    *,
    project_id: str,
    task_id: str,
    new_state: str,
    note: str | None = None,
    force: bool = False,
    reflect_note: str | None = None,
    meta_reflect: str | None = None,
    process_lesson: str | None = None,
    surprise_tags: tuple[str, ...] = (),
    rationale: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Transition ONE task's state and return a structured result.

    CLAWP-083: this is the per-task unit that single- and bulk-mode state
    commands loop over. It NEVER renders output or calls ``sys.exit`` — the
    caller renders — so one task's failure is isolated from the rest of a batch.

    Success -> ``{"ok": True, "task_id": <expanded>, "message": ..., "data": {...}}``
    Failure -> ``{"ok": False, "task_id": <expanded>, "error": <code>, "message": ...}``

    Only the known mutator contract (LockTimeout / FileExistsError /
    FileNotFoundError / ValueError) is mapped to an isolated failure result; an
    unexpected exception still propagates as a traceback (fail-open !=
    fail-silent), mirroring the single-task ``_mutation_errors`` contract.
    """
    task_id = expand_task_id(task_id, project_id)
    state = TaskState(new_state)

    # CLAWP-037 — parent rollup gate. Compute readiness up front so we can
    # either block (no --force) or proceed-and-log (--force). A missing
    # child ref counts as unsatisfied (see parent_rollup_status).
    #
    # Codex round-4 fix: do NOT short-circuit on task.children being empty —
    # parent_rollup_status's belt-and-braces parent-ref scan handles
    # manually-created subtasks that bypassed the persistence path. Tasks
    # with no children at all return ready=True from the scan immediately.
    rollup_incomplete: list[str] = []
    if state == TaskState.DONE:
        _rollup_task = get_task(config, project_id, task_id)
        if _rollup_task:
            from clawpm.tasks import parent_rollup_status
            _status = parent_rollup_status(config, project_id, _rollup_task)
            rollup_incomplete = (
                [f"{c['id']} [{c['state']}]" for c in _status["incomplete"]]
                + [f"{m} [missing]" for m in _status["missing"]]
            )
            if rollup_incomplete and not force:
                return {
                    "ok": False,
                    "task_id": task_id,
                    "error": "subtasks_incomplete",
                    "message": (
                        f"Cannot complete {task_id} - subtasks incomplete:\n  "
                        + "\n  ".join(rollup_incomplete)
                        + "\nUse --force to complete anyway."
                    ),
                }

    # Capture task predictions before state transition (needed for reflection)
    pre_transition_task = get_task(config, project_id, task_id)

    # Map the mutator contract to isolated failure results so one bad task does
    # not abort a bulk run (CLAWP-083). Anything OUTSIDE the contract (an
    # unexpected OSError, a genuine bug) is deliberately NOT caught — it should
    # surface as a traceback rather than be masked behind a "failed" result.
    try:
        task = change_task_state(
            config, project_id, task_id, state,
            note=note, force=force,
            rationale=rationale, supersedes=supersedes,
        )
    except LockTimeout as exc:
        return {
            "ok": False, "task_id": task_id, "error": "lock_timeout",
            "message": f"Could not acquire the project lock (another session may be busy): {exc}",
        }
    except FileExistsError as exc:
        return {"ok": False, "task_id": task_id, "error": "already_exists", "message": str(exc)}
    except FileNotFoundError as exc:
        return {"ok": False, "task_id": task_id, "error": "not_found", "message": str(exc)}
    except ValueError as exc:
        return {"ok": False, "task_id": task_id, "error": "state_change_failed", "message": str(exc)}

    if not task:
        # change_task_state returns None for a genuinely absent task. It can
        # ALSO return None for the DONE rollup gate re-checked inside the lock
        # (a child reopened in the outer-check→lock window) — a pre-existing,
        # rare concurrency nuance that the single-task path has always reported
        # as task_not_found. Disambiguating it honestly requires the mutator to
        # raise a distinct gate error (a tasks.py contract change touching all
        # callers), which is out of scope for CLAWP-083 and belongs with the
        # concurrency-integrity work (CLAWP-071); preserved as-is here for parity.
        return {
            "ok": False, "task_id": task_id, "error": "task_not_found",
            "message": f"No task with id '{task_id}' in project '{project_id}'",
        }

    # The primary state change is now durable. Every step below is a BEST-EFFORT
    # secondary side effect: it must never raise out of this per-task unit,
    # because that would abort the rest of a bulk batch AND misreport the
    # already-committed change as failed. Work-log appends are the main such
    # step, so route them through a marker-collecting wrapper (fail-open WITH a
    # marker, matching the cascade/teardown error handling below).
    log_errors: list[dict] = []
    reflection_errors: list[dict] = []
    lease_errors: list[dict] = []
    files_changed_errors: list[dict] = []
    parent_ready_errors: list[dict] = []

    def _safe_add_entry(**kwargs) -> None:
        try:
            add_entry(config, **kwargs)
        except Exception as exc:
            log_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

    # Auto-log state change
    # CLAWP-053: REJECTED is a terminal state; log as NOTE with the rationale.
    action_map = {
        TaskState.OPEN: WorkLogAction.NOTE,
        TaskState.PROGRESS: WorkLogAction.START,
        TaskState.DONE: WorkLogAction.DONE,
        TaskState.BLOCKED: WorkLogAction.BLOCKED,
        TaskState.REJECTED: WorkLogAction.NOTE,
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
                    encoding="utf-8",  # CLAWP-046: UTF-8, not cp1252
                    errors="replace",
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    raw_files = [f for f in result.stdout.strip().split('\n') if f]
                    files_changed = filter_files_changed(raw_files, project.repo_path)
            except Exception as exc:
                # files_changed enrichment is advisory; a git failure just drops
                # it (the work-log entry is still written). Record a marker so a
                # persistent failure isn't wholly invisible (fail-open WITH a
                # marker), consistent with the other secondaries.
                files_changed_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

        summary = note if note else f"Task marked {new_state}"
        _safe_add_entry(
            project=project_id,
            action=action_map[state],
            task=task_id,
            summary=summary,
            files_changed=files_changed,
            auto=True,
        )

    # CLAWP-037 — when --force completes a parent over incomplete/missing
    # children, record which were still outstanding so the override is
    # auditable in the work_log.
    if state == TaskState.DONE and force and rollup_incomplete:
        _safe_add_entry(
            project=project_id,
            action=WorkLogAction.NOTE,
            task=task_id,
            summary="Force-completed over incomplete subtasks: " + ", ".join(rollup_incomplete),
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
        from clawpm.tasks import cascade_unblock_dependents
        try:
            cascade_results = cascade_unblock_dependents(config, project_id, task_id)
        except (OSError, KeyError, ValueError) as exc:
            # The primary state change already committed (it ran under the
            # _mutation_errors contract above). This dependency cascade is a
            # BEST-EFFORT secondary step: any mutator-contract error from a
            # cascaded change_task_state — LockTimeout / FileNotFoundError
            # (both OSError subclasses) or a ValueError from corrupt-frontmatter
            # refusal — must NOT fail the user's (already durable) done. This
            # DELIBERATELY diverges from the CLAWP-067 exit-1 contract: the error
            # is surfaced in the response data so it's visible (fail-open WITH a
            # marker, not fail-silent) and the operator can retry the unblock —
            # failing the command here would misreport the successful done as
            # failed, and in a bulk batch it would abort the remaining tasks.
            # (CLAWP-067 review: intentional, not an oversight.)
            cascade_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

        for cr in cascade_results:
            _safe_add_entry(
                project=project_id,
                action=WorkLogAction.CASCADE_UNBLOCK,
                task=cr["task_id"],
                summary=f"Auto-unblocked by completion of {cr['trigger']}",
                auto=True,
            )

        # CLAWP-039: release any crash-safety lease on clean completion so a
        # finished task is never swept into a fallback. (The sweep also guards
        # against moving non-PROGRESS tasks, but releasing here retires the
        # lease immediately rather than waiting for the next sweep.)
        try:
            from clawpm.leases import release_lease
            release_lease(config.portfolio_root, task_id, project_id)
        except Exception as exc:
            # Best-effort: a lease left un-released is recoverable (the sweep
            # will eventually retire it) and must not fail an already-durable
            # done — but record a marker so a persistent failure is visible
            # rather than silently leaving stale leases (fail-open WITH a marker).
            lease_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

        # Auto-teardown dispatch settings that reference the just-done task.
        # Codex round-4 fix: use the portfolio dispatch registry so we
        # find EVERY target_dir the operator dispatched to (custom
        # --target-dir, CWD-at-time-of-dispatch, repo subdirs, etc.) —
        # not just the hardcoded repo_path + worktree pair. Falls back
        # to the legacy locations as a belt-and-braces second pass for
        # dispatches that pre-date the registry.
        from clawpm.dispatch import (
            active_dispatch_dirs,
            read_dispatch_marker,
            teardown_dispatch_settings,
        )
        # Building the candidate set (registry read + legacy probes) is itself a
        # BEST-EFFORT secondary: a registry/FS error here must not raise out of
        # this per-task unit and turn an already-durable done into a failed
        # result (Codex/Grok review) — record a marker instead.
        candidate_dirs: list[Path] = []
        try:
            project = get_project(config, project_id)
            candidate_dirs = list(
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
        except Exception as exc:
            teardown_errors.append({"error_class": type(exc).__name__, "message": str(exc)})
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
            # read_dispatch_marker reads a settings file: an unreadable /
            # non-UTF-8 file raises past the JSONDecodeError it catches. Guard
            # it so it can't abort the (already durable) done (Codex P2).
            try:
                marker = read_dispatch_marker(cand)
            except Exception as exc:
                teardown_errors.append({
                    "target_dir": cand.as_posix(),
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                })
                continue
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
                except Exception as exc:
                    # Broad by design: this runs AFTER the primary change is
                    # durable, so NOTHING here may raise out of the per-task unit
                    # (that would misreport a committed done as failed / abort a
                    # batch). Surface to the response — silent leftover
                    # settings.json is exactly the "stale dispatch" failure mode
                    # this feature exists to prevent (fail-open WITH a marker).
                    teardown_errors.append({
                        "target_dir": cand.as_posix(),
                        "error_class": type(exc).__name__,
                        "message": str(exc),
                    })

    # Write reflection event when task completes or is blocked
    if state in (TaskState.DONE, TaskState.BLOCKED) and not pre_transition_task:
        # The transition succeeded but the pre-transition snapshot (taken before
        # the mutator) was unavailable — e.g. get_task returned None on a
        # transient read. Calibration data is lost; mark it so the drop is
        # visible rather than silent (Grok review).
        reflection_errors.append({
            "error_class": "MissingPreTransitionSnapshot",
            "message": "pre-transition task snapshot unavailable; reflection event skipped",
        })
    if state in (TaskState.DONE, TaskState.BLOCKED) and pre_transition_task:
        try:
            from clawpm.reflect import write_reflection_event, _compute_actuals
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
                agent_profile=pre_transition_task.agent_profile,
            )
        except Exception as exc:
            # Never let reflection failure block the (already durable) state
            # change — but record a marker so a lost calibration event is
            # visible rather than silently dropped (fail-open WITH a marker,
            # consistent with log_errors / cascade_errors).
            reflection_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

    # CLAWP-037 — if completing this task fully rolled up its parent, surface
    # a parent-ready advisory so the operator knows the parent is now
    # closeable. Pure advisory; the parent is not auto-completed.
    parent_ready = None
    if state == TaskState.DONE:
        from clawpm.tasks import parent_ready_signal
        try:
            parent_ready = parent_ready_signal(config, project_id, task_id)
        except Exception as exc:
            # Advisory only; on error we simply don't surface a parent-ready
            # hint. Record a marker for consistency with the other best-effort
            # post-mutation steps (fail-open WITH a marker).
            parent_ready = None
            parent_ready_errors.append({"error_class": type(exc).__name__, "message": str(exc)})

    data = task.to_dict()
    if parent_ready:
        data["parent_ready"] = parent_ready
    if cascade_results:
        data["cascade_unblocks"] = cascade_results
    if cascade_errors:
        data["cascade_errors"] = cascade_errors
    if teardowns:
        data["dispatch_teardowns"] = teardowns
    if teardown_errors:
        data["dispatch_teardown_errors"] = teardown_errors
    if log_errors:
        data["log_errors"] = log_errors
    if reflection_errors:
        data["reflection_errors"] = reflection_errors
    if lease_errors:
        data["lease_errors"] = lease_errors
    if files_changed_errors:
        data["files_changed_errors"] = files_changed_errors
    if parent_ready_errors:
        data["parent_ready_errors"] = parent_ready_errors
    return {
        "ok": True,
        "task_id": task_id,
        "message": f"Task {task_id} moved to {new_state}",
        "data": data,
    }


def _do_state_change_isolated(batch: bool, config, **kwargs) -> dict:
    """Call :func:`_do_state_change`, isolating an UNEXPECTED exception in bulk
    mode (CLAWP-083, Grok review).

    ``_do_state_change`` already maps the known mutator contract to failure
    results, but a truly unexpected exception (a genuine bug, a new OSError
    subclass, a corrupt-file read in ``get_task``) would otherwise unwind the
    whole batch loop and discard every result collected so far. In BATCH mode we
    convert it to a visible failure result (``error="unexpected_error"`` +
    class + message) so the batch still renders an honest summary and non-zero
    exit — fail-open WITH a marker, not fail-silent. In SINGLE mode we re-raise
    to preserve the traceback for a genuine bug (fail-open != fail-silent).
    """
    try:
        return _do_state_change(config, **kwargs)
    except Exception as exc:
        if not batch:
            raise
        return {
            "ok": False,
            "task_id": kwargs.get("task_id"),
            "error": "unexpected_error",
            "error_class": type(exc).__name__,
            "message": str(exc),
        }


def _render_state_results(
    results: list[dict], new_state: str, project_id: str, fmt: OutputFormat,
    *, batch: bool,
) -> None:
    """Render single- or bulk-mode state-change results, then exit (CLAWP-083).

    ``batch`` reflects how many ids the caller SUPPLIED, not the post-dedup
    count — so ``done X`` renders the historical single-task contract while
    ``done X X`` (which dedups to one result) still renders the aggregate
    envelope, keeping the output shape a function of the command line.

    Single mode preserves the historical output contract exactly
    (``output_success`` on success; ``output_error`` + ``exit(1)`` on failure).
    Batch mode emits an aggregate envelope carrying every per-task result plus a
    summary; the process exits non-zero if ANY task failed, and the JSON reports
    exactly which (honest exit code + machine-readable breakdown).
    """
    if not results:
        # Defensive: nargs=-1 + required=True guarantees >=1 supplied id and the
        # dedup preserves >=1 result, so this is unreachable via the CLI — but
        # guard rather than IndexError if a future caller reaches render with an
        # empty set (Grok review).
        output_error("no_tasks", "No task ids to process.", fmt=fmt)
        sys.exit(2)
    if not batch:
        r = results[0]
        if r.get("ok"):
            output_success(r["message"], data=r["data"], fmt=fmt)
        else:
            output_error(r["error"], r["message"], fmt=fmt)
            sys.exit(1)
        return

    succeeded = [r for r in results if r.get("ok")]
    failed = [r for r in results if not r.get("ok")]
    payload = {
        "status": "ok" if not failed else "error",
        "message": f"{len(succeeded)}/{len(results)} task(s) moved to {new_state}",
        "state": new_state,
        "project": project_id,
        "results": results,
        "summary": {
            "total": len(results),
            "succeeded": len(succeeded),
            "failed": len(failed),
        },
    }
    if fmt == OutputFormat.JSON:
        output_json(payload)
    else:
        # Secondary side-effect markers that a durable success may still carry
        # (best-effort work-log / reflection / lease / teardown / cascade
        # failures). Surface them in text mode too, so a succeeded-but-degraded
        # task isn't silent outside JSON.
        marker_keys = (
            "log_errors", "reflection_errors", "lease_errors",
            "files_changed_errors", "parent_ready_errors",
            "cascade_errors", "dispatch_teardown_errors",
        )
        for r in results:
            if r.get("ok"):
                click.echo(f"ok   {r['task_id']} moved to {new_state}")
                degraded = [k for k in marker_keys if (r.get("data") or {}).get(k)]
                if degraded:
                    click.echo(f"     (degraded: {', '.join(degraded)})")
            else:
                click.echo(f"FAIL {r['task_id']}: {r['error']} - {r['message']}")
        click.echo(f"{len(succeeded)}/{len(results)} succeeded, {len(failed)} failed")
    if failed:
        sys.exit(1)


@tasks.command("state")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_ids", nargs=-1, required=True)
@click.argument("new_state", type=click.Choice(["open", "progress", "done", "blocked", "rejected"]))
@click.option("--note", "-n", help="Note about the state change (applies to ALL listed tasks)")
@click.option("--force", "-f", is_flag=True, help="Force completion even if subtasks incomplete")
@click.option("--reflect-note", "reflect_note", default=None, help="What surprised you (stored in reflection event; applies to ALL listed tasks)")
@click.option("--meta-reflect", "meta_reflect", default=None, help="What could have been anticipated that wasn't, and why? (stored in reflection event)")
@click.option("--process-lesson", "process_lesson", default=None, help="What update to your prediction PROCESS would have caught this? (recursive meta-loop)")
@click.option("--surprise", "surprise_tags", multiple=True, help=f"Surprise taxonomy tag (repeatable): {', '.join(sorted(['unknown_unknown', 'scope_drift', 'dependency', 'tooling_friction', 'complexity_misread', 'assumption_broke', 'external_blocker']))}")
# CLAWP-053 — won't-do ledger: rationale is required when rejecting a task.
@click.option("--rationale", "-r", "rationale", default=None,
              help="Required when state=rejected: one-line reason this idea was considered and rejected.")
@click.option("--supersedes", "supersedes", default=None,
              help="Optional task-id that supersedes this rejected task (e.g. a replacement task).")
@click.pass_context
def tasks_state(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], new_state: str, note: str | None, force: bool, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...], rationale: str | None, supersedes: str | None) -> None:
    """Change one or many tasks' state (CLAWP-083 bulk mode).

    ``clawpm tasks state 72 73 74 done`` transitions each listed task with
    per-task error isolation and an aggregate JSON result; the exit code is
    non-zero if ANY transition failed. --note and the reflection flags apply to
    ALL listed tasks. Bulk ``rejected`` is refused — each rejected task must
    record its own --rationale in the won't-do ledger, so reject one at a time.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Validate surprise taxonomy tags early (flag-level; applies to all tasks)
    invalid_tags = [t for t in surprise_tags if t not in SURPRISE_TAXONOMY]
    if invalid_tags:
        output_error(
            "bad_surprise_tag",
            f"Unknown surprise tag(s): {invalid_tags}. "
            f"Valid values: {sorted(SURPRISE_TAXONOMY)}",
            fmt=fmt,
        )
        sys.exit(1)

    # CLAWP-053 — reject rationale must be validated before any IO.
    if new_state == "rejected":
        # CLAWP-083 interactive-input-refusal policy: rejection rationale is
        # inherently PER-TASK (the won't-do ledger records why THIS idea was
        # dropped). A single shared --rationale must not be smeared across a
        # batch, so bulk rejection is refused rather than silently mis-recorded.
        if len(task_ids) > 1:
            output_error(
                "bulk_reject_unsupported",
                "Bulk rejection is not supported: each rejected task records its "
                "own --rationale in the won't-do ledger. Reject tasks one at a time.",
                fmt=fmt,
            )
            sys.exit(2)
        if not rationale or not rationale.strip():
            output_error(
                "rationale_required",
                "Rejecting a task requires a non-empty --rationale. "
                "Pass --rationale '<reason>' to record why this was considered and rejected.",
                fmt=fmt,
            )
            sys.exit(1)

    project_id, _ = require_project(ctx, project_id)

    # De-dup while preserving order so a repeated id in one batch does not
    # double-fire the cascade / work-log / reflection side effects.
    batch = len(task_ids) > 1
    seen: set[str] = set()
    results: list[dict] = []
    for raw in task_ids:
        expanded = expand_task_id(raw, project_id)
        if expanded in seen:
            continue
        seen.add(expanded)
        results.append(
            _do_state_change_isolated(
                batch, config,
                project_id=project_id, task_id=expanded, new_state=new_state,
                note=note, force=force,
                reflect_note=reflect_note, meta_reflect=meta_reflect,
                process_lesson=process_lesson, surprise_tags=surprise_tags,
                rationale=rationale, supersedes=supersedes,
            )
        )

    _render_state_results(results, new_state, project_id, fmt, batch=batch)


@tasks.command("decompose")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("parent_id")
@click.option(
    "--child", "child_specs", multiple=True, required=True,
    help="A child subtask (repeatable). Either a plain title, OR a JSON object "
         '{"title":"...","success_criteria":["..."],"complexity":"s|m|l|xl",'
         '"agent_profile":"..."}. JSON lets each child carry its own rubric so '
         "the parent rolls up only when every child's criteria pass.",
)
@click.pass_context
def tasks_decompose(
    ctx: click.Context,
    project_id: str | None,
    parent_id: str,
    child_specs: tuple[str, ...],
) -> None:
    """Decompose a parent task into child subtasks, each with its own rubric (CLAWP-037).

    Records the decomposition durably: every ``--child`` becomes a subtask
    under PARENT (auto-splitting PARENT into a directory task), and the
    parent then cannot be marked DONE until all children are DONE
    (``clawpm tasks done <parent>`` enforces the rollup gate). Unlike an
    ephemeral swarm decomposition, predicted-vs-actual per child is captured
    for calibration.
    """
    import json as _json

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    parent_id = expand_task_id(parent_id, project_id)

    parent = get_task(config, project_id, parent_id)
    if not parent:
        output_error(
            "parent_not_found",
            f"No task with id '{parent_id}' in project '{project_id}'",
            fmt=fmt,
        )
        sys.exit(1)

    created: list[dict] = []
    # NOTE: the id-collision concern Codex re-flags on this loop is
    # addressed inside `add_subtask` (tasks.py) — its id generator unions
    # parent_dir glob + tasks/done + tasks/blocked + parent's persisted
    # frontmatter `children`. See test_subtask_id_does_not_collide_with_migrated_child.
    for spec in child_specs:
        title: str | None = spec
        criteria: list = []
        cmplx = None
        ap = None
        stripped = spec.strip()
        if stripped.startswith("{"):
            try:
                obj = _json.loads(stripped)
            except _json.JSONDecodeError as exc:
                output_error(
                    "bad_child_spec",
                    f"--child JSON parse failed ({exc}): {spec!r}",
                    fmt=fmt,
                )
                sys.exit(1)
            title = obj.get("title")
            if not title:
                output_error(
                    "bad_child_spec",
                    f"--child JSON missing 'title': {spec!r}",
                    fmt=fmt,
                )
                sys.exit(1)
            criteria = obj.get("success_criteria") or []
            # Codex round-5 P3: surface invalid complexity as a structured
            # bad_child_spec error instead of letting TaskComplexity(...)
            # raise an unhandled ValueError + Click traceback.
            _c = obj.get("complexity")
            cmplx = None
            if _c is not None:
                try:
                    cmplx = TaskComplexity(_c)
                except ValueError:
                    output_error(
                        "bad_child_spec",
                        f"--child has invalid complexity {_c!r} "
                        f"(expected one of s|m|l|xl): {spec!r}",
                        fmt=fmt,
                    )
                    sys.exit(1)
            ap = obj.get("agent_profile")

        # Predictions.__post_init__ normalises str | dict | SuccessCriterion.
        preds = Predictions(
            success_criteria=list(criteria),
            filled_by="agent" if criteria else None,
        )
        with _mutation_errors(fmt, "decompose_failed"):
            child = add_subtask(
                config, project_id, parent_id, title,
                complexity=cmplx, description="",
                agent_profile=ap, predictions=preds,
            )
        if not child:
            output_error(
                "decompose_failed",
                f"Failed to create child subtask for parent '{parent_id}'",
                fmt=fmt,
            )
            sys.exit(1)
        created.append(child.to_dict())

    output_success(
        f"Decomposed {parent_id} into {len(created)} child task(s); "
        f"parent is now gated until all children are DONE.",
        data={"parent_id": parent_id, "children": created},
        fmt=fmt,
    )


@tasks.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--title", "-t", required=True, help="Task title")
@click.option("--id", "task_id", help="Task ID (auto-generated if not provided)")
@click.option("--priority", type=int, default=5, help="Priority (1-10, lower is higher)")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), help="Complexity")
@click.option("--depends", "-d", multiple=True, help="Dependencies (can specify multiple)")
@click.option("--scope", multiple=True, help="File glob patterns claimed by this task (can specify multiple)")
@click.option("--scope-file", "scope_file", default=None, type=click.Path(), help="Read scope glob patterns from file (one per line). Windows-safe: bypasses CRT argv glob-expansion. Use instead of --scope when patterns contain wildcards.")
@click.option("--parallel-group", "parallel_group", type=int, default=None, help="Batch ordinal for parallel dispatch (CLAWP-021). Tasks sharing a group dispatch together; group N+1 waits for group N.")
@click.option("--agent-profile", "agent_profile", default=None, help="Capability/skill profile (CLAWP-038). Recorded on the task and propagated to reflection/iteration events so calibration can segment predicted-vs-actual by profile.")
@click.option("--tag", "tags", multiple=True, help="Cross-cutting workstream tag (CLAWP-069, repeatable, e.g. --tag concurrency --tag mcp). Normalised to lowercase.")
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
@click.option("--predict-scope-file", "predict_scope_file", default=None, type=click.Path(), help="Read predicted-scope patterns from file (one per line). Windows-safe alternative to --predict-scope for glob patterns.")
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
@click.option("--predict-iterations", "predict_iterations", type=int, default=None, help="Predicted iterate->grade->revise cycles (CLAWP-019). Default None; 1 means 'expected to land in one pass'.")
# --- Phase 1.6 attribution flag ---
@click.option(
    "--predicted-by", "predicted_by",
    type=click.Choice(["agent", "operator", "operator-edited", "retroactive"]),
    default=None,
    help="Who filled in these predictions (default: operator). Use 'operator-edited' when agent proposed and human reviewed.",
)
# --- CLAWP-054 dispatch contract fields ---
@click.option("--out-of-scope", "out_of_scope", multiple=True, help="Boundary items the executor MUST NOT touch (repeatable; file globs or named topics). Rendered verbatim in the agent preamble.")
@click.option("--out-of-scope-file", "out_of_scope_file", default=None, type=click.Path(), help="Read out-of-scope patterns from file (one per line). Windows-safe alternative to --out-of-scope for glob patterns.")
@click.option("--stop-condition", "stop_conditions", multiple=True, help="Escape-hatch condition: if triggered, executor must STOP and report back (repeatable, free text).")
@click.option(
    "--delegability", "delegability",
    type=click.Choice(["agent", "human", "either"]),
    default=None,
    help="Who may execute this task. 'human' means auto-dispatch is REFUSED. Default: either.",
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
    scope_file: str | None,
    parallel_group: int | None,
    agent_profile: str | None,
    tags: tuple[str, ...],
    parent_id: str | None,
    description: str | None,
    body: str | None,
    body_file: str | None,
    read_stdin: bool,
    predict_duration: str | None,
    predict_complexity: str | None,
    predict_files_changed: int | None,
    predict_scope: tuple[str, ...],
    predict_scope_file: str | None,
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
    out_of_scope: tuple[str, ...] = (),
    out_of_scope_file: str | None = None,
    stop_conditions: tuple[str, ...] = (),
    delegability: str | None = None,
) -> None:
    """Add a new task (or subtask with --parent)."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Validate confidence range
    if confidence is not None and not (1 <= confidence <= 5):
        output_error("bad_confidence", f"--confidence must be 1-5, got {confidence}", fmt=fmt)
        sys.exit(1)

    project_id, _ = require_project(ctx, project_id)

    # Merge file-sourced patterns (literal, no CRT expansion) with inline flags.
    if scope_file:
        scope = tuple(list(scope) + _read_patterns_file(scope_file, "--scope-file", fmt))
    if predict_scope_file:
        predict_scope = tuple(list(predict_scope) + _read_patterns_file(predict_scope_file, "--predict-scope-file", fmt))
    if out_of_scope_file:
        out_of_scope = tuple(list(out_of_scope) + _read_patterns_file(out_of_scope_file, "--out-of-scope-file", fmt))

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
    from clawpm.reflect import parse_duration as _parse_duration
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

    # Resolve the parent id (pure string op) OUTSIDE the mutation wrapper, so the
    # wrapper only spans the actual mutator call (matches every other site).
    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)
    # Create subtask if parent specified
    with _mutation_errors(fmt, "add_failed"):
        tags_list = list(tags) if tags else None
        if parent_id:
            deps = list(depends) if depends else None
            task = add_subtask(
                config,
                project_id,
                parent_id,
                title,
                priority=priority,
                complexity=cmplx,
                description=task_body,
                agent_profile=agent_profile,
                predictions=predictions,
                depends=deps,
                scope=scope_list,
                parallel_group=parallel_group,
                out_of_scope=list(out_of_scope) if out_of_scope else None,
                stop_conditions=list(stop_conditions) if stop_conditions else None,
                delegability=delegability,
                tags=tags_list,
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
                tags=tags_list,
                description=task_body,
                predictions=predictions,
                parallel_group=parallel_group,
                agent_profile=agent_profile,
                out_of_scope=list(out_of_scope) if out_of_scope else None,
                stop_conditions=list(stop_conditions) if stop_conditions else None,
                delegability=delegability,
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
            from clawpm.reflect import find_reference_tasks
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
                from clawpm.codegraph import suggest_scope_from_text
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

    # CLAWP-050: terse, code-derived next-action hints to steer the agent.
    from clawpm.hints import hints_for_added_task, attach_hints
    attach_hints(ctx, task_dict, hints_for_added_task(task))

    output_success(f"Task {task.id} created", data=task_dict, fmt=fmt)


@tasks.command("emit-rubric")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_id")
@click.option(
    "--rubric-format", "rubric_format",
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
    from clawpm.rubric import render_rubric_markdown, render_rubric_json_payload

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


# CLAWP-039: fallback policies for crash-safe dispatch leases. Defined here
# (before `tasks dispatch` which references it in an option) and reused by the
# `lease` command group.
_FALLBACK_POLICIES = ["requeue", "route-secondary", "escalate-to-human", "fail"]


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
@click.option(
    "--confirm-close/--no-confirm-close", "confirm_close", default=None,
    help="CLAWP-041: wire the Stop hook to run an adversarial refutation pass "
         "before the rubric closes the task. Default: auto-on when the task's "
         "predicted confidence >= 4, else off."
)
@click.option(
    "--refute-votes", "refute_votes", type=int, default=1,
    help="CLAWP-041: lens-varied refutation votes baked into the Stop-hook "
         "command when confirm-close is active (>=half of refuters that ran "
         "overturn the close; ties overturn). Also sizes the hook timeout. "
         "Default 1.",
)
@click.option(
    "--lease-ttl", "lease_ttl", type=int, default=None,
    help="CLAWP-039: grant a crash-safety lease with this TTL (seconds). The "
         "subagent heartbeats via the PostToolUse hook; if it goes silent past "
         "the TTL, a doctor/dispatch sweep applies the fallback policy.",
)
@click.option(
    "--fallback-policy", "fallback_policy", type=click.Choice(_FALLBACK_POLICIES),
    default="requeue", show_default=True,
    help="CLAWP-039: what to do with the task if its lease expires.",
)
@click.option(
    "--confirm-stale", "confirm_stale", is_flag=True, default=False,
    help="CLAWP-055: acknowledge that the task's in-scope files have changed since "
         "the baseline_ref was stamped, and proceed with dispatch anyway. Without "
         "this flag, dispatch is blocked when drift is detected.",
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
    confirm_close: bool | None,
    refute_votes: int,
    lease_ttl: int | None,
    fallback_policy: str,
    confirm_stale: bool,
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
    from clawpm.dispatch import (
        create_worktree,
        settings_path,
        write_dispatch_settings,
    )
    from clawpm.rubric import render_rubric_markdown

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _source = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    task = get_task(config, project_id, task_id)
    if not task:
        output_error("task_not_found", f"No task with id '{task_id}'", fmt=fmt)
        sys.exit(1)

    # CLAWP-054: refuse auto-dispatch for human-delegability tasks
    if getattr(task, "delegability", "either") == "human":
        output_error(
            "human_delegability",
            f"Task {task_id!r} has delegability=human and cannot be auto-dispatched. "
            "An operator must execute this task manually.",
            fmt=fmt,
        )
        sys.exit(1)

    # CLAWP-055: pre-dispatch drift reconciliation.
    # Check whether in-scope paths changed since the task's baseline_ref.
    # Blocked on drift unless --confirm-stale is passed.
    # Skipped gracefully when: no scope, no baseline_ref, non-git project,
    # or the baseline sha can't be verified (fail-open — never crash dispatch).
    # CLAWP-063: ERROR-class skips (git failure / unverifiable ref) emit a
    # 'drift-not-checked' warning so the operator knows the check didn't run.
    # EXPECTED-class skips (no scope, no baseline, ts: marker, non-git) stay silent.
    if not confirm_stale:
        from clawpm.baseline import detect_scope_drift
        _proj_for_drift = get_project(config, project_id)
        _repo_for_drift = getattr(_proj_for_drift, "repo_path", None) if _proj_for_drift else None
        _drift_result = detect_scope_drift(
            repo_path=_repo_for_drift,
            scope=getattr(task, "scope", []),
            baseline_ref=getattr(task, "baseline_ref", None),
        )
        if _drift_result["status"] == "drifted":
            changed = _drift_result.get("changed_files", [])
            output_error(
                "stale_baseline",
                f"Task {task_id!r} was specified against baseline "
                f"{_drift_result.get('baseline_ref')!r} but {len(changed)} in-scope "
                f"file(s) have changed since then: {changed[:5]}"
                + (" (+ more)" if len(changed) > 5 else "")
                + ". Reconcile the task spec with the current codebase, then re-run "
                "dispatch, or pass --confirm-stale to proceed anyway.",
                fmt=fmt,
            )
            sys.exit(1)
        elif (
            _drift_result["status"] == "skipped"
            and _drift_result.get("skip_class") == "error"
        ):
            # Fail-open intact: dispatch proceeds, but the operator must know the
            # check didn't run so they can investigate the git/ref failure.
            click.echo(
                f"[WARNING] [drift-not-checked] task {task_id!r}: drift gate skipped "
                f"due to git error - {_drift_result.get('reason', 'unknown error')}. "
                "Proceeding with dispatch (fail-open). Verify the baseline_ref manually."
            )

    # CLAWP-039: validate the lease TTL BEFORE writing any settings (Codex P2),
    # so a bad --lease-ttl never leaves the target half-dispatched.
    if lease_ttl is not None and lease_ttl <= 0:
        output_error("lease_grant_failed",
                     f"--lease-ttl must be positive, got {lease_ttl}", fmt=fmt)
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

    # CLAWP-041: auto-gate the adversarial confirm-close pass. Explicit flag
    # wins; otherwise enable when the task's predicted confidence is high
    # (>= 4) — a confident "done" is exactly where an over-charitable judge
    # is most likely to wave through unverified work.
    #
    # Guard the type: predictions.confidence is meant to be int|None, but task
    # frontmatter is committed/hand-editable state and a legacy file may store
    # it as a quoted YAML string ("4"). Comparing str >= int raises TypeError
    # and would crash dispatch before any settings are written (Codex P2).
    # Treat a non-int confidence as "unset" → auto-off (safe degrade).
    if confirm_close is None:
        task_confidence = (
            task.predictions.confidence if task.predictions else None
        )
        confirm_close = (
            isinstance(task_confidence, int)
            and not isinstance(task_confidence, bool)
            and task_confidence >= 4
        )

    refute_votes = max(1, refute_votes)

    # CLAWP-039: opportunistic lease sweep before granting — this is one of the
    # two no-daemon expiry detectors (the other is `clawpm doctor`). A holder
    # that died is reaped here, on the next dispatch, instead of lingering.
    # Run on EVERY dispatch, not only leased ones (Codex P2): a dead holder
    # from an earlier lease must be reaped on the next dispatch even if this one
    # isn't requesting a lease. Cheap — a no-op when leases.jsonl is absent.
    from clawpm.leases import sweep as _lease_sweep
    swept = []
    sweep_error = None
    try:
        # Scope to the dispatched project (Codex P2): `dispatch --project A`
        # must not reap project B's leased tasks. Portfolio-wide reaping is
        # `clawpm doctor`'s job, not a side effect of an A-scoped dispatch.
        swept = _lease_sweep(config, config.portfolio_root, project_id=project_id)
    except Exception as exc:
        # A sweep failure must not block the dispatch (the user's actual
        # intent), but must not be silent either — else `leases_swept: 0`
        # hides a broken janitor (Codex/silent-failure).
        swept = []
        sweep_error = f"{type(exc).__name__}: {exc}"

    try:
        path = write_dispatch_settings(
            target_dir=resolved_dir,
            task_id=task_id,
            project_id=project_id,
            rubric_markdown=rubric,
            force=force,
            portfolio_root=config.portfolio_root,
            confirm_close=confirm_close,
            refute_votes=refute_votes,
            lease_heartbeat=lease_ttl is not None,
        )
    except (FileExistsError, ValueError) as exc:
        output_error("dispatch_blocked", str(exc), fmt=fmt)
        sys.exit(1)

    # Grant the lease AFTER settings are written (so a settings failure doesn't
    # leave a lease with no heartbeat source).
    if lease_ttl is not None:
        from clawpm.leases import FallbackPolicy, grant_lease, holder_token
        # Store an ABSOLUTE target dir (Codex P2): a relative --target-dir would
        # make a later sweep (run from another CWD) tear down the wrong path.
        # The holder is a shell-safe TOKEN of that path (Codex P2) — the same
        # token the heartbeat hook bakes in — so a path with spaces can't break
        # the hook or the holder match.
        _abs_target = resolved_dir.resolve().as_posix()
        try:
            grant_lease(
                config.portfolio_root, task_id, project_id,
                ttl_seconds=lease_ttl,
                fallback_policy=FallbackPolicy(fallback_policy),
                holder_id=holder_token(_abs_target),
                target_dir=_abs_target,
            )
        except ValueError as exc:
            output_error("lease_grant_failed", str(exc), fmt=fmt)
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
            "confirm_close": confirm_close,
            "refute_votes": refute_votes if confirm_close else None,
            "lease_ttl": lease_ttl,
            "fallback_policy": fallback_policy if lease_ttl is not None else None,
            "leases_swept": len(swept),
            "sweep_error": sweep_error,
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
    from clawpm.dispatch import read_dispatch_marker, teardown_dispatch_settings

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


@tasks.command("emit-tree")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--dry-run", is_flag=True, default=False, help="Run all gates and report what would be written; write nothing.")
@click.option("--strict", is_flag=True, default=False, help="Hard-fail on won't-do / constitution violations instead of report-back.")
@click.pass_context
def tasks_emit_tree(
    ctx: click.Context,
    project_id: str | None,
    dry_run: bool,
    strict: bool,
) -> None:
    """Persist a fully-contracted task-tree atomically (CLAWP-056).

    Reads a JSON tree document from stdin. Validates all gates (reject-match,
    constitution, ID-collision, baseline-resolution) before writing anything,
    then stages and promotes the entire subtree atomically. Zero LLM calls.

    \\b
    Input document shape (schema_version: 1):
      {
        "schema_version": 1,
        "project": "my-project",
        "root": { "title": "New root task" },
        "prd": { "title": "Goal PRD", "type": "spike", "tags": ["prd"],
                 "body_markdown": "## Problem\\n..." },
        "leaves": [
          { "ref": "L1", "parent_ref": null, "title": "Subtask 1",
            "success_criteria": [{"criterion": "Tests pass", "gradeable_signal": "pytest exit 0",
                                   "comparator": "eq:0"}],
            "scope": ["src/**"], "out_of_scope": ["docs/**"],
            "stop_conditions": ["test suite red"], "delegability": "agent",
            "predictions": {"duration_min": 120, "complexity": "m", "confidence": 3},
            "leaf_key": "L1-stable-key" }
        ]
      }

    Output envelope (--format json):
      { "status": "ok", "data": { "root_id": "...", "emitted": [...],
        "research_id": "...", "baseline_ref": "...", "rejected": [...],
        "constitution_violations": [...], "dry_run": false } }
    """
    import json as _json
    from clawpm.emit_tree import parse_emit_document, emit_tree, EmitValidationError

    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # Read JSON from stdin
    try:
        raw_text = click.get_text_stream("stdin").read()
    except Exception as exc:
        output_error("stdin_read_error", f"Failed to read stdin: {exc}", fmt=fmt)
        sys.exit(1)

    if not raw_text or not raw_text.strip():
        output_error("empty_input", "No input document provided on stdin", fmt=fmt)
        sys.exit(1)

    try:
        raw_doc = _json.loads(raw_text)
    except _json.JSONDecodeError as exc:
        output_error("json_parse_error", f"stdin is not valid JSON: {exc}", fmt=fmt)
        sys.exit(1)

    if not isinstance(raw_doc, dict):
        output_error("invalid_input", "Input document must be a JSON object", fmt=fmt)
        sys.exit(1)

    # Override project from document if not set on CLI
    if not project_id:
        project_id = raw_doc.get("project")
    project_id, _ = require_project(ctx, project_id)

    # Phase 1 — parse + validate
    try:
        doc = parse_emit_document(raw_doc)
    except EmitValidationError as exc:
        output_error("validation_error", str(exc), fmt=fmt)
        sys.exit(1)

    # Use project from CLI preference over document
    doc_project = doc.project
    # (project_id already resolved; doc.project used only as fallback above)

    # Phases 2–4 — gate barrier + stage + promote
    try:
        result = emit_tree(
            config=config,
            project_id=project_id,
            doc=doc,
            dry_run=dry_run,
            strict=strict,
        )
    except EmitValidationError as exc:
        output_error("emit_error", str(exc), fmt=fmt)
        sys.exit(1)
    except Exception as exc:
        # emit-tree is a single transactional multi-op (stage → promote, which
        # may call split_task and thus raise LockTimeout). It intentionally
        # presents ONE error surface ("emit_error") for any internal failure
        # — including lock contention — rather than the per-command-specific
        # codes _mutation_errors emits, because a partial emit is reported as a
        # unit. This already maps to a clean error (no raw traceback), so it does
        # not use _mutation_errors (CLAWP-067 review).
        output_error("emit_error", f"Unexpected error during emission: {exc}", fmt=fmt)
        sys.exit(1)

    if dry_run:
        msg = (
            f"Dry-run complete for project '{project_id}': "
            f"{len(doc.leaves)} leaf(ves) would be emitted under {result.root_id}"
            + (f"; {len(result.rejected)} rejected" if result.rejected else "")
            + (f"; {len(result.constitution_violations)} constitution violation(s)" if result.constitution_violations else "")
            + ". No writes performed."
        )
    else:
        msg = (
            f"Emitted {len(result.emitted)} task(s) under {result.root_id}"
            + (f" [PRD: {result.research_id}]" if result.research_id else "")
            + (f"; {len(result.rejected)} leaf(ves) skipped (won't-do)" if result.rejected else "")
        )

    output_success(msg, data=result.to_dict(), fmt=fmt)


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

    with _mutation_errors(fmt, "split_failed"):
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
                    encoding="utf-8",  # CLAWP-046: UTF-8, not cp1252
                    errors="replace",
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

    # CLAWP-086 — a task-targeted log entry is the "log-attach" mutator: it
    # records activity against the task, so bump its `updated` stamp (best-
    # effort; the work-log entry above is the primary artefact).
    if task_id:
        touch_task_updated(config, project_id, task_id)

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
    from clawpm.models import WorkLogEntry
    from clawpm.worklog import get_worklog_path

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
                with open(worklog_path, encoding="utf-8", errors="replace") as f:
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
            # CLAWP-046: decode git's output as UTF-8, not the Windows locale
            # default (cp1252) — else a non-ASCII commit subject (em-dash etc.)
            # is mis-decoded and stored as mojibake in the work_log.
            encoding="utf-8",
            errors="replace",
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
                encoding="utf-8",  # CLAWP-046: UTF-8 filenames, not cp1252
                errors="replace",
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
# Judge subcommands — standalone judge primitives (CLAWP-044)
# ============================================================================


@main.group()
def judge() -> None:
    """Standalone judge primitives.

    Rubric pass/fail grading is wired through ``hook eval-stop``; this group
    exposes the judge primitives that are useful to call directly — currently
    the comparative-selection ``tournament``.
    """
    pass


@judge.command("tournament")
@click.option(
    "--rubric-file", "rubric_file",
    type=click.Path(exists=True, dir_okay=False), required=True,
    help="Path to the rubric / success-criteria file the candidates are judged against.",
)
@click.option(
    "--candidate", "candidate_files",
    type=click.Path(exists=True, dir_okay=False), multiple=True,
    help="Path to a candidate deliverable/transcript file. Repeat for each candidate. "
         "ORDER IS SEED ORDER — pass the strongest-prior candidate first; it wins ambiguous pairs.",
)
@click.option(
    "--label", "labels", multiple=True,
    help="Optional label per --candidate, in the same order. Defaults to each file's stem.",
)
@click.option(
    "--judge-cmd-override", "judge_cmd_override", default=None,
    help="Override the judge subprocess command (beats CLAWPM_JUDGE_CMD). Use a stub for offline testing.",
)
@click.pass_context
def judge_tournament(
    ctx: click.Context,
    rubric_file: str,
    candidate_files: tuple[str, ...],
    labels: tuple[str, ...],
    judge_cmd_override: str | None,
) -> None:
    """Pick the candidate that best satisfies the rubric via pairwise comparison.

    Comparative selection is more reliable than absolute scoring for choosing
    among attempts. The winner is SELECTED, not certified — feed it through
    ``hook eval-stop`` (optionally ``--confirm-close``) to verify it actually
    clears the rubric. Each pair is judged in both position orders to cancel
    position bias; ambiguous pairs keep the higher seed.
    """
    from clawpm.judges.tournament import Candidate, evaluate_tournament
    from clawpm.judges.stop_condition import make_judge_invoker

    fmt = get_format(ctx)
    if not candidate_files:
        output_error("no_candidates", "Provide at least one --candidate.", fmt=fmt)
        sys.exit(1)
    if labels and len(labels) != len(candidate_files):
        output_error(
            "label_mismatch",
            f"{len(labels)} --label(s) for {len(candidate_files)} --candidate(s); counts must match.",
            fmt=fmt,
        )
        sys.exit(1)

    # Robust reads (mirror `hook eval-stop`'s errors="replace"): a non-UTF-8 or
    # race-deleted file must surface as a structured error, not a raw traceback
    # that breaks the JSON contract callers rely on.
    try:
        rubric = Path(rubric_file).read_text(encoding="utf-8", errors="replace")
        candidates = []
        for i, cf in enumerate(candidate_files):
            path = Path(cf)
            label = labels[i] if labels else path.stem
            candidates.append(
                Candidate(
                    label=label,
                    transcript=path.read_text(encoding="utf-8", errors="replace"),
                )
            )
    except OSError as exc:
        output_error("read_failed", f"Failed to read an input file: {exc}", fmt=fmt)
        sys.exit(1)

    # An empty rubric means there is nothing to judge against — the model would
    # confidently pick a winner from noise. Refuse rather than emit a meaningless
    # selection that then seeds the close gate.
    if not rubric.strip():
        output_error(
            "empty_rubric",
            f"Rubric file {rubric_file!r} is empty; nothing to judge candidates against.",
            fmt=fmt,
        )
        sys.exit(1)

    invoker = make_judge_invoker(judge_cmd_override) if judge_cmd_override else None
    try:
        result = evaluate_tournament(rubric, candidates, invoker=invoker)
    except ValueError as exc:
        output_error("tournament_failed", str(exc), fmt=fmt)
        sys.exit(1)

    if fmt == OutputFormat.JSON:
        output_success("tournament complete", data=result.to_dict(), fmt=fmt)
    else:
        click.echo(f"Winner: {result.winner.label}")
        for c in result.comparisons:
            mark = "x" if c.degraded else ("=" if c.agreed else "~")
            click.echo(f"  {mark} {c.higher_seed} vs {c.lower_seed} -> {c.winner}")
        if result.is_degraded:
            click.echo(
                f"WARNING: {result.to_dict()['warning']}", err=True
            )


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
    from clawpm.dispatch import session_start_payload_path

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
@click.option("--confirm-close/--no-confirm-close", "confirm_close", default=None,
              help="CLAWP-041: run an adversarial refutation pass before letting the "
                   "rubric close the task (fires only on the ok=true transition; the "
                   "block path is unchanged). Default: env CLAWPM_CONFIRM_CLOSE, else off.")
@click.option("--refute-votes", "refute_votes", type=int, default=None,
              help="CLAWP-041: number of lens-varied refutation votes when --confirm-close "
                   "is active (>=half of refuters that ran overturn; ties overturn). Default: env CLAWPM_REFUTE_VOTES, else 1.")
@click.pass_context
def hook_eval_stop(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    transcript_file: str | None,
    rubric_file: str | None,
    confirm_close: bool | None,
    refute_votes: int | None,
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
    import os as _os_hook
    from clawpm.judges.stop_condition import (
        JudgeVerdict,
        evaluate_stop_condition,
        evaluate_stop_condition_confirmed,
        load_transcript_from_hook_input,
        map_verdict_to_hook_output,
    )
    from clawpm.rubric import render_rubric_markdown

    # CLAWP-041: resolve confirm-close gating. Flag wins; else env; else off.
    if confirm_close is None:
        confirm_close = _os_hook.environ.get(
            "CLAWPM_CONFIRM_CLOSE", ""
        ).strip().lower() in ("1", "true", "yes", "on")
    if refute_votes is None:
        try:
            refute_votes = int(_os_hook.environ.get("CLAWPM_REFUTE_VOTES", "1"))
        except ValueError:
            refute_votes = 1
    refute_votes = max(1, refute_votes)

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    # CLAWP-038 — best-effort agent_profile so the iteration events this
    # hook writes can be bucketed by profile in `reflect summarize`. Any
    # failure (task not found yet, parse error) degrades to None.
    _hook_agent_profile: str | None = None
    try:
        _ap_task = get_task(config, project_id, task_id)
        if _ap_task is not None:
            _hook_agent_profile = _ap_task.agent_profile
    except Exception:
        _hook_agent_profile = None

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
                    f"project {project_id} - fix dispatch config "
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
        if confirm_close:
            verdict = evaluate_stop_condition_confirmed(
                rubric=rubric, transcript=transcript, refute_votes=refute_votes
            )
        else:
            verdict = evaluate_stop_condition(rubric=rubric, transcript=transcript)
    except RuntimeError as exc:
        # Judge error = enforcement-layer down. Fail-open (continue=true)
        # is defensible because blocking forever on a broken judge is
        # worse, but we MUST leave a doctor signal so repeated judge
        # errors don't silently degrade clawpm to no-enforcement.
        try:
            from clawpm.reflect import write_iteration_event
            write_iteration_event(
                portfolio_root=config.portfolio_root,
                task_id=task_id,
                project_id=project_id,
                verdict_ok=False,
                verdict_reason=f"JUDGE_ERROR: {exc}",
                verdict_impossible=False,
                agent_profile=_hook_agent_profile,
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
                f"clawpm doctor - set CLAWPM_JUDGE_CMD or install Claude "
                f"Code if this keeps happening."
            ),
        }))
        return

    # CLAWP-019: capture the iteration event. This IS the calibration
    # spine — narrow exception so a real filesystem failure surfaces in
    # the systemMessage instead of silently nuking the iteration count.
    try:
        from clawpm.reflect import write_iteration_event
        write_iteration_event(
            portfolio_root=config.portfolio_root,
            task_id=task_id,
            project_id=project_id,
            verdict_ok=verdict.ok,
            verdict_reason=verdict.reason,
            verdict_impossible=verdict.impossible,
            agent_profile=_hook_agent_profile,
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

    # CLAWP-062: thrashing detection -- check AFTER writing the iteration event
    # so the count includes the iteration we just recorded.
    if not verdict.ok and not verdict.impossible:
        try:
            from clawpm.reflect import detect_thrashing, _DEFAULT_THRASH_THRESHOLD
            import os as _os_thr
            # Resolve effective threshold: per-task > env > module default.
            _thr_task = get_task(config, project_id, task_id)
            _thr_per_task = None
            if _thr_task is not None and _thr_task.predictions is not None:
                _thr_per_task = _thr_task.predictions.thrash_threshold
            if _thr_per_task is not None:
                _thr_effective = _thr_per_task
            else:
                _env_thr = _os_thr.environ.get("CLAWPM_THRASH_THRESHOLD", "").strip()
                if _env_thr:
                    try:
                        _thr_effective = int(_env_thr)
                    except ValueError:
                        _thr_effective = _DEFAULT_THRASH_THRESHOLD
                else:
                    _thr_effective = _DEFAULT_THRASH_THRESHOLD
            if detect_thrashing(
                config.portfolio_root, task_id, project_id,
                threshold=_thr_effective,
            ):
                _thrash_reason = (
                    "THRASHING detected on task " + task_id + ": "
                    + str(_thr_effective) + " consecutive not-ok iterations "
                    + "with no rubric progress. "
                    + "Last verdict: " + verdict.reason[:200] + ". "
                    + "Agent stopped; operator should triage."
                )
                verdict = JudgeVerdict(
                    ok=False,
                    reason=_thrash_reason,
                    stop_condition_tripped=True,
                )
        except Exception:
            # Best-effort: thrash detection failure must never block hook
            # output. Broader than OSError -- a malformed-record/parse path
            # must fail open too.
            pass

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
@click.option(
    "--confirm-close", "confirm_close", is_flag=True, default=False,
    help="CLAWP-041: run an adversarial refutation pass before accepting an "
         "ok=true verdict (single-shot dispatch grades once, so this is cheap).",
)
@click.option(
    "--refute-votes", "refute_votes", type=int, default=1,
    help="CLAWP-041: lens-varied refutation votes when --confirm-close is set "
         "(>=half of refuters that ran overturn; ties overturn). Default 1.",
)
@click.option(
    "--agent-profile", "agent_profile", default=None,
    help="Capability/skill profile for the dispatched subagent (CLAWP-038). "
         "Recorded on the subtask and in the reflection/iteration events so "
         "`reflect summarize` can segment predicted-vs-actual by profile.",
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
    confirm_close: bool,
    refute_votes: int,
    agent_profile: str | None,
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
    from clawpm.agent import AgentDispatchError, dispatch_agent

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)

    # NB: do NOT route dispatch through the broad _mutation_errors contract.
    # dispatch_agent's surface is far wider than a task-tree mutator — it creates
    # worktrees and runs git subprocesses — so a FileNotFoundError here can mean
    # "git not on PATH", NOT "task moved by a concurrent session". The broad
    # FileNotFoundError->not_found / FileExistsError->already_exists mapping would
    # mask a genuine environment failure (Codex review). Catch only the mutator
    # LockTimeout that genuinely propagates from add_task/change_task_state, plus
    # dispatch's own AgentDispatchError/ValueError; let anything else surface.
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
            confirm_close=confirm_close,
            refute_votes=refute_votes,
            agent_profile=agent_profile,
        )
    except LockTimeout as exc:
        output_error(
            "lock_timeout",
            f"Could not acquire the project lock (another session may be busy): {exc}",
            fmt=fmt,
        )
        sys.exit(1)
    except (AgentDispatchError, ValueError) as exc:
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
    from clawpm.mission import add_mission

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
    from clawpm.mission import list_missions

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
    from clawpm.mission import mission_status

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
    from clawpm.mission import mission_tasks

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
    from clawpm.mission import add_mission_mini_goal

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    with _mutation_errors(fmt, "mission_add_goal_failed"):
        mission = add_mission_mini_goal(
            config, project_id, mission_id, task_id, actor=actor
        )

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
    from clawpm.mission import set_mission_status

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    with _mutation_errors(fmt, "mission_state_failed"):
        mission = set_mission_status(config, project_id, mission_id, new_status)

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


# ============================================================================
# Lease commands (CLAWP-039) — crash-safe dispatch
# ============================================================================


@main.group("lease")
def lease_group() -> None:
    """Crash-safe dispatch leases: TTL + heartbeat + expiry → fallback.

    A dispatched subtask carries a lease; the holder heartbeats while alive
    (wired to the dispatch PostToolUse hook). If the holder goes silent past
    the TTL, a sweep (run by ``clawpm doctor`` and on the next ``tasks
    dispatch``) transitions the task per its fallback policy. No daemon —
    expiry is detected lazily on sweep.
    """
    pass


@lease_group.command("grant")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task the lease is granted on")
@click.option("--ttl", "ttl", type=int, required=True, help="Lease TTL in seconds (no heartbeat within → expired)")
@click.option("--fallback-policy", "fallback_policy", type=click.Choice(_FALLBACK_POLICIES), default="requeue", show_default=True)
@click.option("--holder", "holder_id", default=None, help="Optional holder identifier (e.g. worktree path / session id)")
@click.option("--target-dir", "target_dir", default=None, help="Dispatch target dir (torn down on requeue fallback)")
@click.pass_context
def lease_grant(ctx, project_id, task_id, ttl, fallback_policy, holder_id, target_dir):
    """Grant a lease on a dispatched task."""
    from clawpm.leases import FallbackPolicy, grant_lease

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    # Store an ABSOLUTE target dir (Codex P2) so a later sweep from a different
    # CWD tears down the right path — matching what `tasks dispatch` does.
    if target_dir:
        target_dir = Path(target_dir).resolve().as_posix()
    try:
        grant_lease(
            config.portfolio_root, task_id, project_id, ttl_seconds=ttl,
            fallback_policy=FallbackPolicy(fallback_policy),
            holder_id=holder_id, target_dir=target_dir,
        )
    except ValueError as exc:
        output_error("lease_grant_failed", str(exc), fmt=fmt)
        sys.exit(1)
    output_success(
        f"Lease granted on {task_id} (ttl {ttl}s, fallback {fallback_policy})",
        data={"task_id": task_id, "ttl_seconds": ttl, "fallback_policy": fallback_policy},
        fmt=fmt,
    )


@lease_group.command("heartbeat")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task whose lease to heartbeat")
@click.option("--holder", "holder_id", default=None)
@click.pass_context
def lease_heartbeat(ctx, project_id, task_id, holder_id):
    """Record a heartbeat — the holder is alive. (Called by the dispatch hook.)"""
    from clawpm.leases import heartbeat

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    heartbeat(config.portfolio_root, task_id, project_id, holder_id=holder_id)
    output_success(f"Heartbeat recorded for {task_id}", data={"task_id": task_id}, fmt=fmt)


@lease_group.command("release")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task whose lease to release (clean completion)")
@click.pass_context
def lease_release(ctx, project_id, task_id):
    """Release a lease — clean completion, no fallback on later sweeps."""
    from clawpm.leases import release_lease

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)
    release_lease(config.portfolio_root, task_id, project_id)
    output_success(f"Lease released for {task_id}", data={"task_id": task_id}, fmt=fmt)


@lease_group.command("list")
@click.option("--project", "-p", "project_id", help="Filter to a project (default: all)")
@click.pass_context
def lease_list(ctx, project_id):
    """List active leases with their expiry + fallback policy."""
    from datetime import datetime, timezone
    from clawpm.leases import active_leases

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    now = datetime.now(timezone.utc)
    rows = []
    for l in active_leases(config.portfolio_root):
        if project_id and l.project_id != project_id:
            continue
        rows.append({
            "task_id": l.task_id,
            "project_id": l.project_id,
            "holder_id": l.holder_id,
            "ttl_seconds": l.ttl_seconds,
            "last_heartbeat_at": l.last_heartbeat_at.isoformat().replace("+00:00", "Z"),
            "expires_at": l.expires_at().isoformat().replace("+00:00", "Z"),
            "expired": l.is_expired(now),
            "fallback_policy": l.fallback_policy.value,
        })
    output_success(f"{len(rows)} active lease(s)", data={"leases": rows}, fmt=fmt)


@lease_group.command("sweep")
@click.option("--dry-run", "dry_run", is_flag=True, default=False, help="Report expired leases without applying fallback.")
@click.pass_context
def lease_sweep(ctx, dry_run):
    """Detect expired leases and apply their fallback (the no-daemon expiry check)."""
    from datetime import datetime, timezone
    from clawpm.leases import expired_leases, sweep

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    now = datetime.now(timezone.utc)
    if dry_run:
        rows = [
            {"task_id": l.task_id, "project_id": l.project_id,
             "fallback_policy": l.fallback_policy.value}
            for l in expired_leases(config.portfolio_root, now)
        ]
        output_success(f"{len(rows)} expired lease(s) (dry-run)", data={"expired": rows}, fmt=fmt)
        return
    actions = sweep(config, config.portfolio_root, now=now)
    output_success(f"Swept {len(actions)} expired lease(s)", data={"actions": actions}, fmt=fmt)


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

    # CLAWP-032: cross-platform locked append. The previous non-atomic append
    # was the originating motivation for the concurrency audit — two parallel
    # `clawpm issues add` invocations on Windows could interleave JSON bytes
    # and silently corrupt `.agent/issues.jsonl`.
    from clawpm.concurrency import append_jsonl_line
    append_jsonl_line(issues_file, json.dumps(entry))

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
# Reflect command group — calibration capture + consumers (CLAWP-040)
# ============================================================================


@main.group()
def reflect() -> None:
    """Reflection layer — query predictions vs actuals and calibrate estimates."""
    pass


@reflect.command("summarize")
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.pass_context
def reflect_summarize(ctx: click.Context, project_id: str | None) -> None:
    """Summarize predicted-vs-actual duration calibration across done tasks (CLAWP-040).

    Aggregates the reflection corpus into duration ratios (actual/predicted)
    bucketed by complexity, confidence, and agent_profile. Rows without a
    usable actual are flagged (dirty) and excluded so they don't poison the
    ratio. Omit --project to span all projects. This is the measurement half
    of the calibration loop; `reflect suggest` applies it.
    """
    from clawpm.reflect import summarize_calibration
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    # CLAWP-040 codex round-3 P2 fix: honor the global --project flag from
    # the main group when the subcommand option wasn't passed. Both absent
    # = aggregate ALL projects.
    if project_id is None:
        project_id = ctx.obj.get("global_project")
    # By here `project_id` is the resolved scope: subcommand > global, with
    # None meaning aggregate ALL. The call below passes the resolved value,
    # NOT a raw default.
    summary = summarize_calibration(config.portfolio_root, project_id)
    output_success(
        f"Calibration summary ({summary['project_id']}): "
        f"{summary['with_usable_duration']}/{summary['total_done']} done tasks "
        f"with usable duration.",
        data=summary,
        fmt=fmt,
    )


@reflect.command("suggest")
@click.argument("task_id", required=False, default=None)
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="Complexity bucket to calibrate against (derived from the task when TASK_ID is given).")
@click.option("--predicted-duration", "predicted_duration", default=None, help="Gut estimate to calibrate: 90, 2h, 3d. Returned deflated by the learned ratio.")
@click.option("--confidence", type=int, default=None, help="Operator confidence 1-5 (recorded on the suggestion).")
@click.option("--agent-profile", "agent_profile", default=None, help="Agent profile (recorded on the suggestion).")
@click.option("--min-bucket", "min_bucket", type=int, default=5, help="Minimum samples for a complexity bucket before falling back to the global ratio.")
@click.pass_context
def reflect_suggest(
    ctx: click.Context,
    task_id: str | None,
    project_id: str | None,
    complexity: str | None,
    predicted_duration: str | None,
    confidence: int | None,
    agent_profile: str | None,
    min_bucket: int,
) -> None:
    """Suggest a calibrated duration from the corpus's learned ratio (CLAWP-040).

    Two modes:
      - ``reflect suggest <task_id>`` derives complexity / confidence /
        agent_profile / predicted-duration from the task, then deflates.
      - ``reflect suggest --complexity m --predicted-duration 6h`` calibrates
        a bare estimate against the complexity bucket.

    Deterministic — no model call. Falls back to the global ratio when the
    complexity bucket has fewer than --min-bucket samples.
    """
    from clawpm.reflect import parse_duration, suggest_duration
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # CLAWP-040 codex round-3 P2 fix: honor the global --project flag from
    # the main group when the subcommand option wasn't passed. require_project
    # below handles the task_id case (which always needs a concrete project);
    # the bare-bucket path inherits the global here, both-absent = ALL.
    if project_id is None:
        project_id = ctx.obj.get("global_project")

    predicted_min: int | None = None
    if task_id:
        project_id, _ = require_project(ctx, project_id)
        task_id = expand_task_id(task_id, project_id)
        t = get_task(config, project_id, task_id)
        if not t:
            output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
            sys.exit(1)
        # Codex round-6 P2: prefer t.predictions.complexity over t.complexity
        # because summarize_calibration buckets by predictions.complexity.
        # Using t.complexity would lookup the wrong bucket (or fall back to
        # global) when the predicted and actual/current complexity differ.
        complexity = complexity or (
            t.predictions.complexity.value if t.predictions.complexity
            else (t.complexity.value if t.complexity else None)
        )
        confidence = confidence if confidence is not None else t.predictions.confidence
        agent_profile = agent_profile or t.agent_profile
        predicted_min = t.predictions.duration_min

    if predicted_duration is not None:
        try:
            predicted_min = parse_duration(predicted_duration)
        except Exception as exc:
            output_error("bad_duration", str(exc), fmt=fmt)
            sys.exit(1)

    result = suggest_duration(
        config.portfolio_root,
        complexity=complexity,
        confidence=confidence,
        agent_profile=agent_profile,
        predicted_min=predicted_min,
        project_id=project_id,
        min_bucket=min_bucket,
    )
    output_success(f"Calibration suggestion (bucket: {result['bucket']})", data=result, fmt=fmt)


@reflect.command("history-import")
@click.option(
    "--source", "source_dir", default=None,
    envvar="CLAWPM_HISTORY_SOURCE",
    help="Path to history source directory (or set CLAWPM_HISTORY_SOURCE).",
)
@click.pass_context
def reflect_history_import(ctx: click.Context, source_dir: str | None) -> None:
    """Scan a directory of session transcripts / agent logs for task mentions.

    Walks ``<source_dir>`` (recursively) for ``.jsonl`` files, extracts every
    line that references a clawpm task ID (per ``clawpm.history.TASK_ID_RE``),
    and returns an aggregate report:

    .. code-block:: json

        {
          "status": "scanned" | "no_mentions" | "no_source" | "source_not_found",
          "source_dir": "<absolute path>",
          "files_scanned": <int>,
          "files_truncated": <bool>,
          "mentions_found": <int>,
          "unique_task_ids": <int>,
          "by_task": {"CLAWP-011": 12, "CLAWP-018": 3, ...},
          "mentions": [TaskMention, ...]
        }

    Source path resolution:
    - ``--source <dir>`` flag (highest precedence).
    - ``CLAWPM_HISTORY_SOURCE`` env var.
    - No hardcoded fallback. Static references to agent-runtime paths (e.g.
      ``~/.openclaw/``) were removed at commit a06a5b8 because they raised
      VirusTotal false positives and were an operational security smell.

    Implementation notes:
    - The importer module is lazy-imported below so the clawpm binary's
      static import graph stays free of suspicious-path patterns.
    - TASK_ID_RE accepts both single-segment (``CLAWP-011``) and multi-segment
      (``MY-PR-001``, ``A-B-C-123``) prefixes — matters for projects whose
      IDs normalise to embedded hyphens.

    Not yet implemented (Phase 3 work):
    - Writing reflection events back to ``~/clawpm/reflections/<task-id>.jsonl``
      (currently the function returns the mention report; the operator decides
      what to do with it).
    - Deduplication by ``task_id + occurred_at`` for safe re-runs.
    - Optional ``history_source`` key in ``portfolio.toml`` so ``clawpm setup``
      can prompt once instead of requiring the flag/env on every invocation.
    """
    import json as _json
    if not source_dir:
        click.echo(_json.dumps({
            "status": "no_source",
            "message": "Provide --source <dir> or set CLAWPM_HISTORY_SOURCE.",
        }, indent=2))
        return

    # Lazy import: keeps the suspicious-pattern code path out of the binary's
    # static import graph. See module docstring + design constraints above.
    from clawpm.history import import_history as _import_history

    source_path = Path(source_dir).expanduser()
    if not source_path.is_dir():
        click.echo(_json.dumps({
            "status": "source_not_found",
            "source": source_path.as_posix(),
            "message": "Source directory does not exist or is not a directory.",
        }, indent=2))
        return

    report = _import_history(source_path)
    report["status"] = "scanned" if report["mentions_found"] > 0 else "no_mentions"
    click.echo(_json.dumps(report, indent=2))


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

from clawpm.inbox import (  # noqa: E402 — local import to keep group self-contained
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

def _load_web_server():
    """Import the optional web-server deps (the ``web`` extra).

    Returns ``(create_app, uvicorn)``. Raises ``ImportError`` if fastapi /
    uvicorn aren't installed. Factored out so the graceful-degradation path
    is testable without uninstalling the deps.
    """
    import uvicorn
    from clawpm.serve import create_app

    return create_app, uvicorn


@main.command("serve")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, help="Port to bind to")
def serve(host: str, port: int) -> None:
    """Start the web UI server (read-only dashboard)."""
    try:
        create_app, uvicorn = _load_web_server()
    except ImportError:
        click.echo(
            "The ClawPM web UI requires the optional 'web' extra.\n"
            "Install it with:  pip install 'clawpm[web]'",
            err=True,
        )
        sys.exit(1)

    uvicorn.run(create_app(), host=host, port=port)


if __name__ == "__main__":
    main()






# ============================================================================
# Constitution commands (CLAWP-057)
# ============================================================================


@main.group("constitution")
def constitution_group() -> None:
    """Project constitution — named invariants that constrain emission."""
    pass


@constitution_group.command("add")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--name", "-n", required=True, help="Invariant name (unique per project)")
@click.option(
    "--kind", "-k", required=True,
    type=click.Choice([
        "require_success_criteria",
        "max_complexity",
        "require_scope",
        "advisory",
    ]),
    help="Invariant kind (code-checkable or advisory)",
)
@click.option("--description", "-d", default=None, help="Human description (optional)")
@click.option(
    "--param", "params_raw", multiple=True, metavar="KEY=VALUE",
    help="Kind-specific parameters (repeatable, e.g. --param max=l)",
)
@click.pass_context
def constitution_add(
    ctx: click.Context,
    project_id: str | None,
    name: str,
    kind: str,
    description: str | None,
    params_raw: tuple[str, ...],
) -> None:
    """Add (or replace) a named invariant in the project constitution."""
    from clawpm.constitution import add_invariant

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    # Parse --param KEY=VALUE pairs into a dict
    params: dict = {}
    for pair in params_raw:
        if "=" not in pair:
            output_error("invalid_param", f"--param must be KEY=VALUE, got {pair!r}", fmt=fmt)
            sys.exit(1)
        k, v = pair.split("=", 1)
        params[k.strip()] = v.strip()

    try:
        inv = add_invariant(
            config,
            project_id,
            name=name,
            kind=kind,
            description=description,
            params=params or None,
        )
    except Exception as exc:
        output_error("constitution_add_failed", str(exc), fmt=fmt)
        sys.exit(1)

    output_success(f"Invariant '{name}' added to constitution", data=inv, fmt=fmt)


@constitution_group.command("list")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.pass_context
def constitution_list(ctx: click.Context, project_id: str | None) -> None:
    """List invariants declared in the project constitution."""
    from clawpm.constitution import load_constitution

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    doc = load_constitution(config, project_id)
    invariants = doc.get("invariants", [])

    if fmt == OutputFormat.JSON:
        output_json(invariants)
    else:
        if not invariants:
            click.echo("No invariants declared.")
            return
        for inv in invariants:
            kind = inv.get("kind", "?")
            desc = inv.get("description", "")
            params = inv.get("params", {})
            line = f"  [{kind}] {inv['name']}"
            if params:
                line += f"  params={params}"
            if desc:
                line += f"  -- {desc}"
            click.echo(line)


@constitution_group.command("remove")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected)")
@click.option("--name", "-n", required=True, help="Invariant name to remove")
@click.pass_context
def constitution_remove(ctx: click.Context, project_id: str | None, name: str) -> None:
    """Remove a named invariant from the project constitution (idempotent)."""
    from clawpm.constitution import remove_invariant

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    try:
        removed = remove_invariant(config, project_id, name)
    except Exception as exc:
        output_error("constitution_remove_failed", str(exc), fmt=fmt)
        sys.exit(1)

    msg = f"Invariant '{name}' removed" if removed else f"Invariant '{name}' not found (no-op)"
    output_success(msg, data={"name": name, "removed": removed}, fmt=fmt)
