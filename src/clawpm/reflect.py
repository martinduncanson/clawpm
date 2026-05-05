"""Reflection layer — Phase 1: compute and store predictions vs actuals.

Phase 2 stubs (summarize / suggest / history-import) live in cli.py under the
``clawpm reflect`` command group.
"""

from __future__ import annotations

import fnmatch
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Actuals, Predictions, TaskComplexity, WorkLogAction, WorkLogEntry


def _reflections_dir(portfolio_root: Path) -> Path:
    """Return (and create if needed) ~/clawpm/reflections/."""
    d = portfolio_root / "reflections"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _compute_actuals(
    task_id: str,
    task_complexity: TaskComplexity | None,
    log_entries: list[WorkLogEntry],
) -> Actuals:
    """Derive Actuals from work_log entries for a given task.

    - duration_min: elapsed minutes between the first ``start`` log entry and now
    - complexity: the task's current complexity field (if set)
    - files_changed: deduplicated count of unique files across all log entries
    - files_touched: sorted deduplicated list of those files
    """
    task_entries = [e for e in log_entries if e.task == task_id]

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

    return Actuals(
        duration_min=duration_min,
        complexity=task_complexity,
        files_changed=files_changed_count,
        files_touched=files_touched,
    )


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
) -> Path:
    """Compute deltas and append one JSON line to ~/clawpm/reflections/<task-id>.jsonl.

    Returns the path of the reflection file written.
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
    }

    ref_dir = _reflections_dir(portfolio_root)
    ref_file = ref_dir / f"{task_id}.jsonl"
    with open(ref_file, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    return ref_file
