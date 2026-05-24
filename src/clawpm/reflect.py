"""Reflection layer — Phase 1: compute and store predictions vs actuals.

Phase 2 stubs (summarize / suggest / history-import) live in cli.py under the
``clawpm reflect`` command group.
"""

from __future__ import annotations

import fnmatch
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import click

from .models import Actuals, Predictions, TaskComplexity, WorkLogAction, WorkLogEntry


def parse_duration(value: "str | int | None") -> "int | None":
    """Parse a human-friendly duration string into an integer number of minutes.

    Accepted formats:
      - ``45`` or ``45m``  → 45 minutes
      - ``2h``             → 120 minutes
      - ``3d``             → 4320 minutes  (24 h/day — wall-clock, not 8-hour workday)
      - ``1w``             → 10080 minutes (7 × 24 h)

    Wall-clock days/weeks are intentional: calibration compares predicted elapsed
    time against actual elapsed time, not scheduled working hours.

    Raises :class:`click.BadParameter` for unrecognised input.
    """
    if value is None:
        return None
    if isinstance(value, int):
        return value
    s = str(value).strip().lower()
    match = re.fullmatch(r"(\d+)([mhdw]?)", s)
    if not match:
        raise click.BadParameter(
            f"Bad duration: {value!r}. Use 90, 90m, 2h, 1d, or 1w."
        )
    n, unit = int(match.group(1)), match.group(2) or "m"
    multiplier = {"m": 1, "h": 60, "d": 60 * 24, "w": 60 * 24 * 7}[unit]
    return n * multiplier


def _reflections_dir(portfolio_root: Path) -> Path:
    """Return (and create if needed) ~/clawpm/reflections/."""
    d = portfolio_root / "reflections"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compute_actuals(
    task_id: str,
    task_complexity: TaskComplexity | None,
    log_entries: list[WorkLogEntry],
    portfolio_root: Path | None = None,
    project_id: str | None = None,
) -> Actuals:
    # Inner name for the cross-project filter below.
    _project_id_hint = project_id
    """Derive Actuals from work_log entries for a given task.

    - duration_min: elapsed minutes between the first ``start`` log entry and now
    - complexity: the task's current complexity field (if set)
    - files_changed: deduplicated count of unique files across all log entries
    - files_touched: sorted deduplicated list of those files

    IMPORTANT: filtering uses an EXACT ``task_id`` match.  A subtask
    (``PROJ-042-003``) will NOT inherit ``start`` events logged against its
    parent (``PROJ-042``).  If no ``start`` event exists for the subtask's own
    ID, ``duration_min`` is ``None`` rather than a nonsensical inherited value.
    """
    task_entries = [e for e in log_entries if e.task == task_id]  # exact match only

    # Duration: first start → now
    start_entries = sorted(
        [e for e in task_entries if e.action == WorkLogAction.START],
        key=lambda e: e.ts,
    )
    duration_min: int | None = None
    if start_entries:
        first_start = start_entries[0].ts
        now = datetime.now(timezone.utc)
        # Ensure both are timezone-aware for the subtraction
        if first_start.tzinfo is None:
            first_start = first_start.replace(tzinfo=timezone.utc)
        delta = now - first_start
        duration_min = max(0, int(delta.total_seconds() / 60))

    # Files: deduplicated union from all log entries' files_changed
    all_files: set[str] = set()
    for e in task_entries:
        if e.files_changed:
            for f in e.files_changed:
                # strip git status prefix (e.g. "M\tpath/to/file") if present
                clean = f.split("\t")[-1].strip() if "\t" in f else f.strip()
                if clean:
                    all_files.add(clean)

    files_touched = sorted(all_files)
    files_changed_count = len(files_touched) if files_touched else None

    iterations: int | None = None
    if portfolio_root is not None:
        # Populate iterations from the JSONL — even 0 is a valid signal
        # (means dispatch happened but no Stop-hook ever fired).
        # Pass project_id when available (added via the project_id kwarg
        # below) so cross-project task_id collisions don't pollute the
        # count. Legacy callers without project_id get the old behaviour.
        ic = count_iterations_for_task(
            portfolio_root, task_id, project_id=_project_id_hint
        )
        iterations = ic if ic > 0 else None

    return Actuals(
        duration_min=duration_min,
        complexity=task_complexity,
        files_changed=files_changed_count,
        files_touched=files_touched,
        iterations=iterations,
    )


def write_iteration_event(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
    verdict_ok: bool,
    verdict_reason: str,
    verdict_impossible: bool = False,
) -> Path:
    """Append a single iteration_event line to the task's reflection JSONL.

    Called from the Stop-hook condition evaluator (CLAWP-017) on every
    invocation. Each iteration represents one grader cycle in an
    iterate→grade→revise loop; counts roll up into ``actuals.iterations``
    at terminal-event time so the operator can see calibration delta on
    "how many revisions did this task need".

    Returns the path of the JSONL file.

    The event is recorded even if verdict_ok=True (the final iteration
    counts too) — the consumer that computes ``iterations_actual`` reads
    all iteration_event lines and reports the count.
    """
    record = {
        "event": "iteration_event",
        "task_id": task_id,
        "project_id": project_id,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "verdict": {
            "ok": verdict_ok,
            "reason": verdict_reason,
            "impossible": verdict_impossible,
        },
    }
    ref_dir = _reflections_dir(portfolio_root)
    ref_file = ref_dir / f"{task_id}.jsonl"
    with open(ref_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")
    return ref_file


def count_iterations_for_task(
    portfolio_root: Path,
    task_id: str,
    project_id: str | None = None,
) -> int:
    """Count iteration_event lines for a task. Used to populate actuals.iterations.

    Voided events ARE counted — voiding marks a reflection event as bad
    data for calibration, but the iteration still happened. A separate
    decision can exclude voided iterations later if Phase 2 calibration
    demands it.

    **Cross-project isolation** (Codex round-6 P2 fix): the reflection
    JSONL filename is keyed by ``task_id`` alone, so two projects
    sharing a task_id write to the same file. When ``project_id`` is
    provided, this function filters events by project_id to prevent
    one project's iteration cycles being counted into the other's
    actuals. ``project_id=None`` preserves the legacy "count everything
    in this file" behaviour for callers that haven't been threaded yet.
    """
    ref_file = _reflections_dir(portfolio_root) / f"{task_id}.jsonl"
    if not ref_file.exists():
        return 0
    count = 0
    for line in ref_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") != "iteration_event":
            continue
        if project_id is not None and rec.get("project_id") != project_id:
            continue
        count += 1
    return count


def _compute_deltas(
    predictions: Predictions,
    actuals: Actuals,
) -> dict[str, Any]:
    """Compute prediction vs actual deltas.

    Returns a dict with all delta keys; values are None when a comparison is
    not possible (e.g. prediction or actual was not set).
    """
    deltas: dict[str, Any] = {}

    # Duration ratio (actual / predicted)
    if predictions.duration_min and actuals.duration_min is not None:
        deltas["duration_ratio"] = round(
            actuals.duration_min / predictions.duration_min, 4
        )
    else:
        deltas["duration_ratio"] = None

    # Files-changed ratio (actual / predicted)
    if predictions.files_changed and actuals.files_changed is not None:
        deltas["files_changed_ratio"] = round(
            actuals.files_changed / predictions.files_changed, 4
        )
    else:
        deltas["files_changed_ratio"] = None

    # Scope: which files_touched were NOT covered by any predicted glob
    if predictions.files_scope:
        covered = {
            f
            for f in actuals.files_touched
            for g in predictions.files_scope
            if fnmatch.fnmatch(f, g)
        }
        overrun = sorted(set(actuals.files_touched) - covered)
        unused_globs = [
            g
            for g in predictions.files_scope
            if not any(fnmatch.fnmatch(f, g) for f in actuals.files_touched)
        ]
    else:
        overrun = []
        unused_globs = []

    deltas["files_scope_overrun"] = overrun
    deltas["files_scope_unused"] = unused_globs

    # Complexity match
    pred_c = predictions.complexity.value if predictions.complexity else None
    actual_c = actuals.complexity.value if actuals.complexity else None
    if pred_c is not None and actual_c is not None:
        deltas["complexity_match"] = pred_c == actual_c
        deltas["complexity_predicted"] = pred_c
        deltas["complexity_actual"] = actual_c
    else:
        deltas["complexity_match"] = None
        deltas["complexity_predicted"] = pred_c
        deltas["complexity_actual"] = actual_c

    # Iterations ratio (CLAWP-019): predicted vs grader-cycle count.
    # Only meaningful when both sides set; missing data → None.
    if predictions.predicted_iterations and actuals.iterations is not None:
        deltas["iterations_ratio"] = round(
            actuals.iterations / predictions.predicted_iterations, 4
        )
    else:
        deltas["iterations_ratio"] = None
    deltas["iterations_predicted"] = predictions.predicted_iterations
    deltas["iterations_actual"] = actuals.iterations

    return deltas


def write_reflection_event(
    portfolio_root: Path,
    event: str,
    task_id: str,
    project_id: str,
    predictions: Predictions,
    actuals: Actuals,
    note: str | None = None,
    meta_reflection: str | None = None,
    process_lesson: str | None = None,
    surprise_taxonomy: list[str] | None = None,
) -> Path:
    """Compute deltas and append one JSON line to ~/clawpm/reflections/<task-id>.jsonl.

    Returns the path of the reflection file written.

    Phase 1.5 adds two recursive meta-reflection fields:
    - ``process_lesson``: what update to the prediction *process* would have
      caught the surprise?  Accumulates into a personal calibration manual.
    - ``surprise_taxonomy``: multi-pick tags from the fixed vocabulary in
      ``SURPRISE_TAXONOMY`` (models.py).  Validated before calling this function
      — pass an empty list rather than None when no surprise is provided.
    """
    deltas = _compute_deltas(predictions, actuals)

    record: dict[str, Any] = {
        "event": event,
        "task_id": task_id,
        "project_id": project_id,
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "predictions": predictions.to_dict(),
        "actuals": actuals.to_dict(),
        "deltas": deltas,
        "note": note,
        "meta_reflection": meta_reflection,
        "process_lesson": process_lesson,
        "surprise_taxonomy": surprise_taxonomy if surprise_taxonomy is not None else [],
    }

    ref_dir = _reflections_dir(portfolio_root)
    ref_file = ref_dir / f"{task_id}.jsonl"
    with open(ref_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return ref_file
