"""Task state-change orchestration — the domain service layer (CLAWP-077).

``transition`` owns the full state-change orchestration: parent-rollup gating,
the mutator-contract mapping, the work-log append, the dependency cascade,
crash-lease release, dispatch teardown, and reflection-event capture. It is
deliberately free of any click / CLI dependency — it takes a portfolio
``config`` and plain kwargs, returns a structured result dict, and raises
nothing outside the known mutator contract (which it maps to failure results).

This is the seam the MCP server (CLAWP-068) consumes directly, without going
through the click command layer or a subprocess. The CLI handlers in
``clawpm.cli.tasks`` / ``clawpm.cli.shortcuts`` call ``transition_isolated`` and
render the result; the ``_mutation_errors`` presentation wrapper stays at the
CLI boundary.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from clawpm.concurrency import LockTimeout
from clawpm.models import MISS_CATEGORIES, SURPRISE_TAXONOMY, TaskComplexity, TaskState, WorkLogAction
from clawpm.discovery import get_project, get_repo_path
from clawpm.tasks import change_task_state, get_task
from clawpm.worklog import add_entry, filter_files_changed, read_entries
from clawpm.context import expand_task_id

logger = logging.getLogger(__name__)


def transition(
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
    actual_complexity: str | None = None,
    resolution: str | None = None,
    miss_category: str | None = None,
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

    # Validate surprise-taxonomy tags at the service boundary (CLAWP-077 Codex
    # review). This seam is the MCP entry point (CLAWP-068), and
    # write_reflection_event's contract requires callers to pre-validate — an
    # out-of-vocabulary tag would permanently write a bad value into the fixed
    # calibration taxonomy in the reflection JSONL. The CLI (tasks_state) also
    # validates for a friendlier flag-level error and never reaches here with a
    # bad tag; this is the backstop for every non-CLI caller. ValueError is part
    # of the mutator contract the callers already map.
    invalid_tags = [t for t in surprise_tags if t not in SURPRISE_TAXONOMY]
    if invalid_tags:
        raise ValueError(
            f"Unknown surprise tag(s): {invalid_tags}. "
            f"Valid values: {sorted(SURPRISE_TAXONOMY)}"
        )

    # CLAWP-112-003 — miss_category is a closed vocabulary; same backstop as the
    # surprise tags so a non-CLI caller cannot write a free-text root cause.
    if miss_category is not None and miss_category not in MISS_CATEGORIES:
        raise ValueError(
            f"Unknown miss_category {miss_category!r}. "
            f"Valid values: {sorted(MISS_CATEGORIES)}"
        )

    # CLAWP-112-002 — --actual-complexity is the ONLY source of
    # actuals.complexity (honest actuals: no passthrough of the prediction).
    # Validated here too so any non-CLI caller (MCP) gets the same guarantee
    # the CLI's click.Choice gives interactively.
    actual_complexity_value: TaskComplexity | None = None
    if actual_complexity is not None:
        try:
            actual_complexity_value = TaskComplexity(actual_complexity)
        except ValueError:
            raise ValueError(
                f"Unknown actual_complexity {actual_complexity!r}. "
                f"Valid values: {[c.value for c in TaskComplexity]}"
            )

    # CLAWP-037 — parent rollup gate. Compute readiness up front so we can
    # either block (no --force) or proceed-and-log (--force). A missing
    # child ref counts as unsatisfied (see parent_rollup_status).
    #
    # Codex round-4 fix: do NOT short-circuit on task.children being empty —
    # parent_rollup_status's belt-and-braces parent-ref scan handles
    # manually-created subtasks that bypassed the persistence path. Tasks
    # with no children at all return ready=True from the scan immediately.
    rollup_incomplete: list[str] = []
    # Capture task predictions before state transition (needed for reflection)
    pre_transition_task = get_task(config, project_id, task_id)
    if state == TaskState.DONE and pre_transition_task is not None:
        # CLAWP-147-002 -- ONE validator for every refusal that precedes the
        # move (rollup, decision resolution, blank resolution, done/
        # destination). `tasks land` calls the same function in its pre-check,
        # so the two cannot drift. CLAWP-111: this is the SHARED choke point --
        # shortcuts.done, `tasks state` and the MCP server all reach it, and
        # change_task_state only re-checks as defence in depth. Checked before
        # the mutator runs so a refusal leaves the task untouched.
        from clawpm.tasks import (
            gather_done_destination,
            parent_rollup_status,
            validate_done_transition,
        )
        _status = parent_rollup_status(config, project_id, pre_transition_task)
        rollup_incomplete = (
            [f"{c['id']} [{c['state']}]" for c in _status["incomplete"]]
            + [f"{m} [missing]" for m in _status["missing"]]
        )
        _refusal = validate_done_transition(
            task_id,
            pre_transition_task.kind,
            resolution,
            force,
            _status,
            gather_done_destination(config, project_id, pre_transition_task),
        )
        if _refusal is not None:
            return {
                "ok": False,
                "task_id": task_id,
                "error": (
                    "already_exists"
                    if _refusal.code.startswith("done_destination")
                    else _refusal.code
                ),
                "message": _refusal.message,
            }

    # Map the mutator contract to isolated failure results so one bad task does
    # not abort a bulk run (CLAWP-083). Anything OUTSIDE the contract (an
    # unexpected OSError, a genuine bug) is deliberately NOT caught — it should
    # surface as a traceback rather than be masked behind a "failed" result.
    try:
        task = change_task_state(
            config, project_id, task_id, state,
            note=note, force=force,
            rationale=rationale, supersedes=supersedes,
            resolution=resolution,
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
    warnings: list[dict] = []
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
        # Auto-detect git files changed.
        #
        # CLAWP-098 (Codex review, PR #55): resolve the checkout through
        # get_repo_path, NOT get_project(...).repo_path. The task mutation
        # above is session-scoped — run from a dispatched worktree it edits
        # that worktree's task file — but this secondary enrichment used the
        # cwd-independent registry path, so it diffed the MAIN checkout: the
        # entry either omitted every file the agent had actually touched, or
        # recorded unrelated main-checkout edits against this task. Both
        # steps must follow the same checkout.
        files_changed = None
        repo_path = get_repo_path(config, project_id)
        if repo_path and repo_path.exists():
            try:
                result = subprocess.run(
                    ["git", "diff", "--name-only", "HEAD"],
                    cwd=repo_path,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",  # CLAWP-046: UTF-8, not cp1252
                    errors="replace",
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    raw_files = [f for f in result.stdout.strip().split('\n') if f]
                    files_changed = filter_files_changed(raw_files, repo_path)
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

        # CLAWP-094: a frontmatter-sync failure is nested in the per-task
        # record; lift it into cascade_errors (the marker the CLI reports as
        # "degraded") and log it, so the durable-but-degraded unblock is visible.
        for cr in cascade_results:
            if cr.get("state_sync_error"):
                logger.warning(
                    "cascade unblock of %s: frontmatter state not synced: %s",
                    cr["task_id"], cr["state_sync_error"],
                )
                cascade_errors.append({
                    "error_class": "StateSyncError",
                    "task_id": cr["task_id"],
                    "message": cr["state_sync_error"],
                })

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
            from clawpm.reflect import (
                write_reflection_event, _compute_actuals, _compute_deltas, resolve_axes,
            )
            all_log_entries = read_entries(config, project=project_id)
            actuals = _compute_actuals(
                task_id,
                all_log_entries,
                portfolio_root=config.portfolio_root,
                project_id=project_id,
                actual_complexity=actual_complexity_value,
            )
            event_name = "task_done" if state == TaskState.DONE else "task_blocked"
            # CLAWP-112-003 — nudge, never block: a missed prediction with no
            # root cause gets a one-line reminder; the close proceeds regardless.
            if state == TaskState.DONE and miss_category is None:
                _preds = pre_transition_task.predictions
                _axes = resolve_axes(
                    _preds, actuals, _compute_deltas(_preds, actuals),
                    kind=pre_transition_task.kind,
                )
                if _axes["held"] == "incorrect":
                    warnings.append({
                        "code": "miss_category_missing",
                        "message": (
                            "Prediction missed and no --miss-category given; consider: "
                            + ", ".join(sorted(MISS_CATEGORIES))
                        ),
                    })
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
                kind=pre_transition_task.kind,
                miss_category=miss_category,
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
    if warnings:
        data["warnings"] = warnings
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


def transition_isolated(batch: bool, config, **kwargs) -> dict:
    """Call :func:`transition`, isolating an UNEXPECTED exception in bulk
    mode (CLAWP-083, Grok review).

    ``transition`` already maps the known mutator contract to failure
    results, but a truly unexpected exception (a genuine bug, a new OSError
    subclass, a corrupt-file read in ``get_task``) would otherwise unwind the
    whole batch loop and discard every result collected so far. In BATCH mode we
    convert it to a visible failure result (``error="unexpected_error"`` +
    class + message) so the batch still renders an honest summary and non-zero
    exit — fail-open WITH a marker, not fail-silent. In SINGLE mode we re-raise
    to preserve the traceback for a genuine bug (fail-open != fail-silent).
    """
    try:
        return transition(config, **kwargs)
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


# ---------------------------------------------------------------------------
# Human-decision record (CLAWP-154 / CLAWP-157)
#
# ONE write path for the awaiting-human record: the CLI shortcuts (block
# --question, unblock) and the MCP tasks_state tool both call these, so the
# two surfaces cannot drift.
# ---------------------------------------------------------------------------

def block_with_question(
    config,
    *,
    project_id: str,
    task_id: str,
    question: str,
    options=None,
    recommendation: str | None = None,
    note: str | None = None,
    reflect_note: str | None = None,
    meta_reflect: str | None = None,
    process_lesson: str | None = None,
    surprise_tags: tuple[str, ...] = (),
    actual_complexity: str | None = None,
) -> dict:
    """Block ONE task on a human decision and record the question.

    Validation (blank question, bad options, a recommendation that is not an
    option label) raises ``ValueError`` BEFORE any move, so a refused call
    leaves the task untouched. The block transition runs first; the record is
    written after it. A record failure on an already-blocked task returns a
    ``decision_record_failed`` result with ``state_changed`` set rather than
    rolling the block back.
    """
    from clawpm.tasks import normalize_human_options, record_human_question

    opts = normalize_human_options(options)
    if not isinstance(question, str) or not question.strip():
        raise ValueError("A non-empty question is required.")
    if recommendation is not None and recommendation.strip() not in {o["label"] for o in opts}:
        raise ValueError(f"--recommend {recommendation!r} is not one of the --option labels.")
    parts = [f"Question: {question.strip()}"]
    if opts:
        parts.append("Options: " + " / ".join(o["label"] for o in opts))
    if recommendation:
        parts.append(f"Recommendation: {recommendation.strip()}")
    decision = " | ".join(parts)
    note = f"{note} | {decision}" if note else decision
    task_id = expand_task_id(task_id, project_id)
    r = transition_isolated(
        False, config,
        project_id=project_id, task_id=task_id, new_state="blocked",
        note=note, reflect_note=reflect_note, meta_reflect=meta_reflect,
        process_lesson=process_lesson, surprise_tags=surprise_tags,
        actual_complexity=actual_complexity,
    )
    if r.get("ok"):
        try:
            record_human_question(
                config, project_id, task_id, question, opts, recommendation,
            )
        except (ValueError, OSError) as exc:
            # The block is already durable; say so rather than rolling back.
            r = {
                "ok": False, "task_id": task_id,
                "error": "decision_record_failed", "state_changed": True,
                "message": f"Task blocked but the decision was not recorded: {exc}",
            }
    return r


def unblock_task(
    config,
    *,
    project_id: str,
    task_id: str,
    also_start: bool = False,
    note: str | None = None,
    answer: str | None = None,
    batch: bool = False,
) -> dict:
    """Unblock ONE task (blocked -> open, or -> progress with ``also_start``).

    With ``answer`` the human's answer is recorded on the task (question,
    options and recommendation are kept); without it the now-dead question is
    cleared so a later plain re-block cannot resurrect it. The ``unblock``
    work-log entry is written only when the transition lands. Per-task
    isolation: a missing or not-blocked task returns a failure result.
    """
    from clawpm.tasks import clear_stale_human_decision, record_human_answer

    if answer is not None:
        if not answer.strip():
            raise ValueError("--answer must be non-empty.")
        note = f"{note}. Answer: {answer.strip()}" if note else f"Answer: {answer.strip()}"

    new_state_str = "progress" if also_start else "open"
    full_task_id = expand_task_id(task_id, project_id)

    # Verify the task is actually blocked (per-task, isolated).
    task = get_task(config, project_id, full_task_id)
    if not task:
        return {
            "ok": False, "task_id": full_task_id, "error": "task_not_found",
            "message": f"No task with id '{full_task_id}' in project '{project_id}'",
        }
    if task.state != TaskState.BLOCKED:
        return {
            "ok": False, "task_id": full_task_id, "error": "not_blocked",
            "message": (
                f"Task {full_task_id} is in state '{task.state.value}', not 'blocked'. "
                "Use 'clawpm tasks state <id> open' to change state directly."
            ),
        }

    r = transition_isolated(
        batch, config,
        project_id=project_id, task_id=full_task_id, new_state=new_state_str,
        note=note,
    )
    if r.get("ok") and answer is not None:
        try:
            record_human_answer(config, project_id, full_task_id, answer)
        except (ValueError, OSError) as exc:
            r = {
                "ok": False, "task_id": full_task_id,
                "error": "decision_record_failed", "state_changed": True,
                "message": f"Task unblocked but the answer was not recorded: {exc}",
            }
    clear_failure: dict | None = None
    if r.get("ok") and answer is None:
        # CLAWP-154 -- unblocked unanswered: the question is no longer live.
        try:
            clear_stale_human_decision(config, project_id, full_task_id)
        except (ValueError, OSError) as exc:
            # Loud (non-zero exit), applied after the work-log append below.
            clear_failure = {
                "ok": False, "task_id": full_task_id,
                "error": "stale_decision_not_cleared", "state_changed": True,
                "message": (
                    f"Task {full_task_id} is unblocked but its stale decision "
                    f"record was not cleared: {exc}"
                ),
            }
    if r.get("ok"):
        # Log the explicit unblock action only when the transition landed.
        # Best-effort: the transition is already durable, so a work-log
        # append failure must not abort the batch -- surface it as a marker.
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
    return clear_failure if clear_failure is not None else r
