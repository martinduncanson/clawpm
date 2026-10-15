from __future__ import annotations

import json
import os
import sys

import click

from clawpm.models import ProjectStatus, TaskState, WorkLogAction
from clawpm.output import OutputFormat, output_context, output_error, output_json, output_task_detail
from clawpm.discovery import discover_projects, get_project
from clawpm.tasks import get_next_task, get_task, list_tasks
from clawpm.worklog import add_entry
from clawpm.context import expand_task_id
from clawpm.cli.tasks import _render_state_results, render_frontier, tasks_add, tasks_state
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
@click.option("--actual-complexity", "actual_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None,
              help="Independent post-hoc complexity assessment (CLAWP-112-002). Without this, "
                   "actuals.complexity is null and complexity_match is null.")
# CLAWP-111 — decision-kind tasks: required to complete a kind: decision task.
@click.option("--resolution", "resolution", default=None,
              help="Outcome text. Required to complete (done) a kind: decision task; optional (stored) for build tasks.")
# CLAWP-112-003 — root cause of a missed prediction (single pick, validated).
@click.option("--miss-category", "miss_category", default=None,
              help="Why the prediction method failed: wrong_signal, wrong_weight, missed_variable, "
                   "operator_override, noise, structural. 'noise' = inside expected variance, no lesson.")
@click.pass_context
def quick_done(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, force: bool, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...], actual_complexity: str | None = None, resolution: str | None = None, miss_category: str | None = None) -> None:
    """Mark one or many tasks as done (alias for 'tasks state <ids...> done')."""
    ctx.invoke(tasks_state, project_id=project_id, task_ids=task_ids, new_state="done", note=note, force=force, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags, actual_complexity=actual_complexity, resolution=resolution, miss_category=miss_category)


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
@click.option("--actual-complexity", "actual_complexity", type=click.Choice(["s", "m", "l", "xl"]), default=None,
              help="Independent post-hoc complexity assessment (CLAWP-112-002). Without this, "
                   "actuals.complexity is null and complexity_match is null.")
# CLAWP-154 — awaiting-human decision record.
@click.option("--question", "question", default=None,
              help="Decision the block waits on a human for (recorded on the task; list with 'tasks list --needs-human'). Single task only.")
@click.option("--option", "option_labels", multiple=True, help="An answer option for --question (repeatable).")
@click.option("--recommend", "recommend", default=None, help="Recommended option label (must match an --option).")
@click.pass_context
def quick_block(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, reflect_note: str | None, meta_reflect: str | None, process_lesson: str | None, surprise_tags: tuple[str, ...], actual_complexity: str | None = None, question: str | None = None, option_labels: tuple[str, ...] = (), recommend: str | None = None) -> None:
    """Mark one or many tasks as blocked (alias for 'tasks state <ids...> blocked').

    With --question the task is blocked on a human decision: the question,
    --option labels and --recommend are recorded on the task and in the work
    log, and the task appears in 'tasks list --needs-human'.
    """
    if question is None:
        if option_labels or recommend is not None:
            raise click.UsageError("--option/--recommend require --question.")
    else:
        if len(task_ids) > 1:
            raise click.UsageError("--question applies to exactly one task id.")
        from clawpm.tasks import normalize_human_options, record_human_question
        try:
            opts = normalize_human_options(list(option_labels))
            if not question.strip():
                raise ValueError("A non-empty question is required.")
            if recommend is not None and recommend.strip() not in {o["label"] for o in opts}:
                raise ValueError(f"--recommend {recommend!r} is not one of the --option labels.")
        except ValueError as exc:
            raise click.UsageError(str(exc))
        parts = [f"Question: {question.strip()}"]
        if opts:
            parts.append("Options: " + " / ".join(o["label"] for o in opts))
        if recommend:
            parts.append(f"Recommendation: {recommend.strip()}")
        decision = " | ".join(parts)
        note = f"{note} | {decision}" if note else decision
        fmt = get_format(ctx)
        config = require_portfolio(ctx)
        resolved_project, _ = require_project(ctx, project_id)
        full_id = expand_task_id(task_ids[0], resolved_project)
        r = transition_isolated(
            False, config,
            project_id=resolved_project, task_id=full_id, new_state="blocked",
            note=note, reflect_note=reflect_note, meta_reflect=meta_reflect,
            process_lesson=process_lesson, surprise_tags=surprise_tags,
            actual_complexity=actual_complexity,
        )
        if r.get("ok"):
            try:
                record_human_question(
                    config, resolved_project, full_id, question, opts, recommend,
                )
            except (ValueError, OSError) as exc:
                # The block is already durable; say so rather than rolling back.
                r = {
                    "ok": False, "task_id": full_id,
                    "error": "decision_record_failed", "state_changed": True,
                    "message": f"Task blocked but the decision was not recorded: {exc}",
                }
        _render_state_results([r], "blocked", resolved_project, fmt, batch=False)
        return
    ctx.invoke(tasks_state, project_id=project_id, task_ids=task_ids, new_state="blocked", note=note, reflect_note=reflect_note, meta_reflect=meta_reflect, process_lesson=process_lesson, surprise_tags=surprise_tags, actual_complexity=actual_complexity)


@main.command("unblock")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.argument("task_ids", nargs=-1, required=True)
@click.option("--note", "-n", help="Reason the blocker was resolved (applies to ALL listed tasks)")
@click.option("--start", "also_start", is_flag=True, help="Also transition to in-progress (blocked → progress)")
@click.option("--answer", "answer", default=None, help="Human's answer to the recorded question (CLAWP-154). Single task only.")
@click.pass_context
def quick_unblock(ctx: click.Context, project_id: str | None, task_ids: tuple[str, ...], note: str | None, also_start: bool, answer: str | None = None) -> None:
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

    if answer is not None:
        if len(task_ids) > 1:
            raise click.UsageError("--answer applies to exactly one task id.")
        if not answer.strip():
            raise click.UsageError("--answer must be non-empty.")
        note = f"{note}. Answer: {answer.strip()}" if note else f"Answer: {answer.strip()}"

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
        if r.get("ok") and answer is not None:
            from clawpm.tasks import record_human_answer
            try:
                record_human_answer(config, project_id, full_task_id, answer)
            except (ValueError, OSError) as exc:
                r = {
                    "ok": False, "task_id": full_task_id,
                    "error": "decision_record_failed", "state_changed": True,
                    "message": f"Task unblocked but the answer was not recorded: {exc}",
                }
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
@click.option(
    "--frontier", "frontier_root", default=None,
    help="Show the actionable frontier under this root task (CLAWP-111-004): ready, blocked (waiting_on) and claimed (lease holder).",
)
@click.pass_context
def quick_next(ctx: click.Context, project_id: str | None, batch_mode: bool, frontier_root: str | None = None) -> None:
    """Get the next task to work on, the next parallel batch with --batch, or a root's frontier with --frontier."""
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if frontier_root is not None:
        if batch_mode:
            raise click.UsageError("--frontier cannot be combined with --batch.")
        # Read-only query: never auto-init a .project/ from an untracked repo.
        project_id, _ = require_project(ctx, project_id, auto_init=False)
        render_frontier(config, project_id, frontier_root, fmt)
        return

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
    
    from clawpm.context import build_agent_context

    context = build_agent_context(config, resolved_id, source=source, log_limit=log_limit)
    if context is None:
        output_error("project_not_found", f"Project '{resolved_id}' not found", fmt=fmt)
        sys.exit(1)

    output_context(context, fmt=fmt)
