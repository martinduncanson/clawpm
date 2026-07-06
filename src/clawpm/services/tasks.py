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

import subprocess
from pathlib import Path

from clawpm.concurrency import LockTimeout
from clawpm.models import SURPRISE_TAXONOMY, TaskState, WorkLogAction
from clawpm.discovery import get_project
from clawpm.tasks import change_task_state, get_task
from clawpm.worklog import add_entry, filter_files_changed, read_entries
from clawpm.context import expand_task_id


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
