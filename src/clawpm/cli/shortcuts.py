from __future__ import annotations

import json
import os
import subprocess
import sys

import click

from clawpm.models import ProjectStatus, Task, TaskState, WorkLogAction
from clawpm.output import OutputFormat, output_context, output_error, output_json, output_task_detail
from clawpm.discovery import discover_projects, get_project
from clawpm.tasks import get_next_task, get_task, list_tasks
from clawpm.worklog import add_entry, tail_entries
from clawpm.context import expand_task_id
from clawpm.cli.tasks import _render_state_results, tasks_add, tasks_state
from clawpm.services.tasks import transition_isolated
from clawpm.cli.projects import projects_next
from clawpm.cli.base import main, get_format, require_portfolio, require_project

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

        r = transition_isolated(
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
