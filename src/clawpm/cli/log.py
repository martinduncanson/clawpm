from __future__ import annotations

import json
import subprocess
import sys

import click

from clawpm.models import Task, WorkLogAction
from clawpm.output import OutputFormat, output_error, output_json, output_success, output_worklog_entries
from clawpm.discovery import get_project
from clawpm.tasks import touch_task_updated
from clawpm.worklog import add_entry, get_last_entry, get_logged_commit_hashes, tail_entries
from clawpm.context import expand_task_id
from clawpm.cli.base import main, get_format, require_portfolio, require_project

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
