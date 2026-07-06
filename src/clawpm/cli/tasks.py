from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from clawpm.concurrency import LockTimeout
from clawpm.models import Predictions, SURPRISE_TAXONOMY, SuccessCriterion, Task, TaskComplexity, TaskState, WorkLogAction
from clawpm.output import OutputFormat, output_error, output_json, output_success, output_task_detail, output_tasks_list
from clawpm.discovery import get_project
from clawpm.tasks import add_subtask, add_task, change_task_state, distinct_tags, edit_task, get_task, list_tasks, split_task
from clawpm.worklog import add_entry, filter_files_changed, read_entries
from clawpm.context import expand_task_id
from clawpm.cli.base import main, _mutation_errors, get_format, require_portfolio, require_project, _read_patterns_file, _FALLBACK_POLICIES

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
