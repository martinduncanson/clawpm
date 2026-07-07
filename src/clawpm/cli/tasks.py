from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from clawpm.concurrency import LockTimeout
from clawpm.models import PortfolioConfig, Predictions, ProjectStatus, SURPRISE_TAXONOMY, SuccessCriterion, Task, TaskComplexity, TaskState, WorkLogAction
from clawpm.output import OutputFormat, output_error, output_json, output_success, output_task_detail, output_tasks_list
from clawpm.discovery import discover_projects, get_project
from clawpm.tasks import add_subtask, add_task, archive_done_tasks, change_task_state, distinct_tags, edit_task, get_task, list_tasks, split_task
from clawpm.worklog import add_entry, filter_files_changed, read_entries
from clawpm.context import expand_task_id
from clawpm.cli.base import main, _mutation_errors, get_format, require_portfolio, require_project, _read_patterns_file, _FALLBACK_POLICIES
from clawpm.services.tasks import transition_isolated

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
@click.option("--all-projects", "all_projects", is_flag=True, default=False, help="List tasks across every ACTIVE project (CLAWP-084). Each row carries its project_id; filters compose per-project. Mutually exclusive with --project.")
@click.option(
    "--include-archived", "include_archived", is_flag=True, default=False,
    help="Fold archived done tasks (done/archive/) back into the listing (CLAWP-085). "
         "Only affects 'done' and 'all' scans.",
)
@click.pass_context
def tasks_list(ctx: click.Context, project_id: str | None, state: str | None, flat: bool, tags: tuple[str, ...], all_tags: bool, text: str | None, use_regex: bool, priority: str | None, complexities: tuple[str, ...], parent: str | None, linked: str | None, limit: int | None, all_projects: bool, include_archived: bool) -> None:
    """List tasks for a project (default: open+progress+blocked, use -s all for everything).

    ``--all-projects`` (CLAWP-084) spans every ACTIVE project instead of one.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    if all_projects:
        # CLAWP-084 — portfolio-wide view. --project scopes to a single project,
        # so combining it with --all-projects is contradictory; refuse rather
        # than silently pick one.
        if project_id is not None:
            raise click.UsageError("--all-projects cannot be combined with --project.")
        active = discover_projects(config, status_filter=ProjectStatus.ACTIVE)
        found_tasks = []
        proj_priority: dict[str, int] = {}
        for proj in active:
            proj_priority[proj.id] = proj.priority
            proj_tasks = _collect_project_tasks(
                config, proj.id, state, tags, all_tags, text, use_regex,
                priority, complexities, parent, linked, include_archived,
            )
            # Stamp the owning project on each row so cross-project ids stay
            # unambiguous (two same-numeric-id tasks in different projects must
            # never be conflated — cross-project id-isolation class).
            for t in proj_tasks:
                t.project_id = proj.id
            found_tasks.extend(proj_tasks)
        # Portfolio ordering mirrors `projects next`: project priority first,
        # then task priority, then id for a stable total order across projects.
        found_tasks.sort(key=lambda t: (proj_priority.get(t.project_id, 10**9), t.priority, t.id))
    else:
        project_id, _ = require_project(ctx, project_id)
        found_tasks = _collect_project_tasks(
            config, project_id, state, tags, all_tags, text, use_regex,
            priority, complexities, parent, linked, include_archived,
        )

    if limit is not None and limit >= 0:
        found_tasks = found_tasks[:limit]

    # CLAWP-084 — in the cross-project view force a flat, project-scoped render:
    # parent/child hierarchy is per-project and the merged task_map could
    # otherwise conflate identical ids from different projects.
    output_tasks_list(found_tasks, fmt=fmt, flat=flat, show_project=all_projects)


def _collect_project_tasks(
    config: PortfolioConfig,
    project_id: str,
    state: str | None,
    tags: tuple[str, ...],
    all_tags: bool,
    text: str | None,
    use_regex: bool,
    priority: str | None,
    complexities: tuple[str, ...],
    parent: str | None,
    linked: str | None,
    include_archived: bool = False,
) -> list["Task"]:
    """Gather + filter one project's tasks (CLAWP-084 extraction).

    Shared by the single-project and ``--all-projects`` list paths so the
    state-gather + composable-filter pass (CLAWP-069/082) behaves identically in
    both. Link resolution stays per-project — a ``[[wiki-link]]`` resolves
    within its own project — which is exactly why the filter pass runs inside
    the per-project loop rather than globally. No limit is applied here; the
    caller applies it (globally, after the cross-project merge + sort).
    """
    if state == "all":
        found_tasks = list_tasks(config, project_id, state_filter=None, include_archived=include_archived)
    elif state is None:
        # Default: show everything except done
        found_tasks = []
        for s in (TaskState.OPEN, TaskState.PROGRESS, TaskState.BLOCKED):
            found_tasks.extend(list_tasks(config, project_id, state_filter=s))
        found_tasks.sort(key=lambda t: (t.priority, t.id))
    else:
        found_tasks = list_tasks(
            config, project_id, state_filter=TaskState(state), include_archived=include_archived
        )

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
    # CLAWP-084 — resolve this project's ACTUAL task-ID prefix (explicit
    # task_prefix -> inferred-from-tasks) so a short ref like `1` expands to the
    # real minted id even when the prefix diverges from the naive project_id[:5]
    # (Codex P2: --all-projects over a project with task_prefix="SAME" stored
    # children under SAME-001 but expanded --parent 1 to ALPHA-001 -> no match).
    # Also corrects the single-project path for divergent-prefix projects.
    resolved_prefix = None
    if parent or linked:
        from clawpm.tasks import resolve_existing_prefix
        _settings = get_project(config, project_id)
        resolved_prefix = resolve_existing_prefix(_settings) if _settings else None
    if parent:
        filter_list.append(by_parent(expand_task_id(parent, project_id, resolved_prefix)))
    if linked:
        from clawpm.links import build_link_index
        index = build_link_index(config, project_id)
        # Resolve both the expanded (task-style) id and the raw ref so --linked
        # works for research/mission ids that expand_task_id would leave alone.
        refs: set[str] = set()
        for target in {expand_task_id(linked, project_id, resolved_prefix), linked}:
            refs |= index.referencing_ids(target)
        filter_list.append(by_linked(refs))

    if filter_list:
        found_tasks = apply_filters(found_tasks, filter_list)

    return found_tasks


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

    # CLAWP-085: flag archived done tasks so a resolved-from-archive task is
    # visibly distinguished from a live done task. is_archived_path matches the
    # specific done/archive/ silo, not any "archive" path segment.
    from clawpm.tasks import is_archived_path
    is_archived = is_archived_path(task.file_path)

    if fmt == OutputFormat.JSON:
        task_dict = task.to_dict()
        task_dict["reflections_voided"] = reflections_voided
        task_dict["linked_from"] = _linked_from
        task_dict["archived"] = is_archived
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
        if is_archived:
            click.echo("[archived: true]")
        if reflections_voided:
            click.echo("[reflections_voided: true]")


@tasks.command("archive")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option(
    "--older-than", "older_than", default="90d",
    help="Archive done tasks whose file has not been touched in this window "
         "(e.g. 90d, 12w, 2160h). Default: 90d.",
)
@click.option(
    "--dry-run", "dry_run", is_flag=True, default=False,
    help="List what would be archived without moving anything.",
)
@click.pass_context
def tasks_archive(ctx: click.Context, project_id: str | None, older_than: str, dry_run: bool) -> None:
    """Move stale done tasks into done/archive/ to keep the hot path cheap (CLAWP-085).

    Move-not-delete: nothing is ever removed. Archived tasks stay resolvable via
    'tasks show' and can be re-listed with 'tasks list -s done --include-archived'.
    """
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    from clawpm.reflect import parse_duration
    try:
        minutes = parse_duration(older_than)
    except click.BadParameter:
        minutes = None
    if minutes is None:
        output_error(
            "bad_older_than",
            f"Invalid --older-than {older_than!r}. Use forms like 90d, 12w, 2160h.",
            fmt=fmt,
        )
        sys.exit(1)
    older_than_days = minutes / (60 * 24)

    with _mutation_errors(fmt, "archive_failed"):
        results = archive_done_tasks(
            config, project_id, older_than_days=older_than_days, dry_run=dry_run,
        )

    # Partition the per-candidate records: clean moves/plans vs. skipped vs.
    # errored (stat failures surfaced, not swallowed).
    errored = [r for r in results if r.get("error")]
    skipped = [r for r in results if r.get("skipped")]
    archived = [r for r in results if not r.get("error") and not r.get("skipped")]

    if fmt == OutputFormat.JSON:
        output_json({
            "success": True,
            "project": project_id,
            "dry_run": dry_run,
            "older_than": older_than,
            "count": len(archived),
            "archived": archived,
            "skipped": skipped,
            "errors": errored,
        })
    else:
        verb = "Would archive" if dry_run else "Archived"
        if not archived:
            click.echo(f"No done tasks older than {older_than} to archive.")
        else:
            click.echo(f"{verb} {len(archived)} task(s) older than {older_than}:")
            for rec in archived:
                click.echo(f"  {rec['id']} -> {rec['to']}")
        for rec in skipped:
            click.echo(f"  [skipped: {rec['skipped']}] {rec['id']}")
        for rec in errored:
            click.echo(f"  [error: {rec['error']}] {rec['id']}")


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
            transition_isolated(
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
