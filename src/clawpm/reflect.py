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
    # CLAWP-032: cross-platform locked append (Windows append is non-atomic).
    from .concurrency import append_jsonl_line
    append_jsonl_line(ref_file, json.dumps(record))
    return ref_file


def find_reference_tasks(
    portfolio_root: Path,
    *,
    project_id: str,
    complexity: "TaskComplexity | str | None" = None,
    files_scope: list[str] | None = None,
    frameworks: list[str] | None = None,
    success_criteria_text: list[str] | None = None,
    repo_path: Path | None = None,
    k: int = 3,
) -> list[dict]:
    """Find prior similar tasks for reference-class anchoring (CLAWP-023).

    Walks ``~/clawpm/reflections/*.jsonl`` looking for ``task_done`` events
    from the SAME project_id (cross-project isolation per the round-5-8
    sweep) and scores each by similarity to the proposed predictions:

      - +3 if complexity matches the proposed tier exactly
      - +2 per files_scope glob that overlaps the proposed scope (prefix-
        prefix overlap, same heuristic as ``cli._globs_overlap``)
      - +2 per framework intersection
      - +1 per success-criteria-text Jaccard-overlap step (tokenised on
        whitespace, lowercased, stop-words ignored)
      - +1 baseline if the candidate has actuals.duration_min set
        (otherwise its calibration value is zero — skip)

    Returns top-k results ordered by score desc, each as a dict carrying
    task_id, similarity_score, predicted vs actual duration, and the
    deltas the corpus already computed.

    The matching is intentionally simple — no embeddings, no LLM. The
    operator/agent gets fast O(reflections-file) suggestions at predict
    time without any subprocess or network. Phase 2 can swap in something
    smarter; the API surface stays the same.
    """
    ref_dir = _reflections_dir(portfolio_root)
    if not ref_dir.exists():
        return []

    # Normalise inputs
    target_complexity: str | None = None
    if complexity is not None:
        if hasattr(complexity, "value"):
            target_complexity = complexity.value
        elif isinstance(complexity, str):
            target_complexity = complexity
    target_scope = files_scope or []
    target_frameworks = {f.lower() for f in (frameworks or [])}
    target_sc_tokens = _tokenise_criteria(success_criteria_text or [])

    # CLAWP-030: resolve target symbols ONCE (not per-candidate) so
    # the subprocess call is amortised. When repo_path isn't given or
    # codegraph isn't installed/indexed, the symbol set is empty and
    # the scoring axis is a no-op (preserves the pre-CLAWP-030 scoring
    # exactly for callers that don't opt in).
    target_codegraph_symbols: set[str] = set()
    if repo_path is not None and target_scope:
        try:
            from .codegraph import search_symbols
            for glob in target_scope:
                target_codegraph_symbols |= search_symbols(glob, repo_path)
        except Exception:
            target_codegraph_symbols = set()

    candidates: list[dict] = []
    for ref_file in ref_dir.glob("*.jsonl"):
        try:
            text = ref_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Codex round-1 P2 fix: voided events MUST be excluded from
        # reference-class anchoring. `reflect void` is the operator's
        # explicit signal that a reflection is bad calibration data.
        # Surfacing it as a reference would degrade prediction quality
        # with examples the operator already flagged as untrustworthy.
        # We track void events keyed on (task_id, project_id) and skip
        # any task_done event matching a void record. Absent project_id
        # on the void = legacy unscoped void = matches any project (the
        # back-compat rule from the prior PR's round 8).
        done_record: dict | None = None
        voided = False
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            evt = rec.get("event")
            if evt == "void":
                # Void records match this project if their project_id
                # is absent (legacy unscoped) OR matches. Once voided
                # for this project, we don't surface the task as a
                # reference regardless of other event content.
                rec_proj = rec.get("project_id")
                if rec_proj is None or rec_proj == project_id:
                    voided = True
                continue
            if evt != "task_done":
                continue
            if rec.get("project_id") != project_id:
                continue
            # Keep the latest task_done event in the file (some tasks have
            # multiple if re-done after revert — rare but possible).
            done_record = rec
        if done_record is None or voided:
            continue

        actuals = done_record.get("actuals") or {}
        if actuals.get("duration_min") is None:
            # No real actuals = no calibration value
            continue

        predictions = done_record.get("predictions") or {}

        # CLAWP-030: resolve candidate symbols only when we have a
        # target symbol set to intersect with — skip the codegraph call
        # otherwise.
        candidate_codegraph_symbols: set[str] = set()
        if target_codegraph_symbols and repo_path is not None:
            cand_scope = predictions.get("files_scope") or []
            if cand_scope:
                try:
                    from .codegraph import search_symbols as _search
                    for glob in cand_scope:
                        candidate_codegraph_symbols |= _search(glob, repo_path)
                except Exception:
                    candidate_codegraph_symbols = set()

        score = _similarity_score(
            predictions=predictions,
            target_complexity=target_complexity,
            target_scope=target_scope,
            target_frameworks=target_frameworks,
            target_sc_tokens=target_sc_tokens,
            target_codegraph_symbols=target_codegraph_symbols or None,
            candidate_codegraph_symbols=candidate_codegraph_symbols or None,
        )
        if score <= 0:
            continue

        deltas = done_record.get("deltas") or {}
        candidates.append({
            "task_id": done_record.get("task_id", ref_file.stem),
            "similarity_score": score,
            "predicted_duration_min": predictions.get("duration_min"),
            "actual_duration_min": actuals.get("duration_min"),
            "duration_ratio": deltas.get("duration_ratio"),
            "complexity_predicted": predictions.get("complexity"),
            "complexity_actual": actuals.get("complexity"),
            "iterations_predicted": predictions.get("predicted_iterations"),
            "iterations_actual": actuals.get("iterations"),
            "process_lesson": done_record.get("process_lesson"),
            "surprise_taxonomy": done_record.get("surprise_taxonomy") or [],
        })

    candidates.sort(key=lambda c: c["similarity_score"], reverse=True)
    return candidates[:k]


# Stopwords filtered when tokenising success criteria — common English
# function words that don't carry domain signal. Conservative list;
# Phase 2 could swap in a real tokeniser if precision matters.
_SC_STOPWORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "of", "in", "on", "at", "to", "for", "with", "from", "by",
    "and", "or", "not", "no", "yes", "if", "when", "then", "than",
    "this", "that", "these", "those", "it", "its", "as",
})


def _tokenise_criteria(criteria: list) -> set[str]:
    """Tokenise success_criteria text for Jaccard scoring.

    Accepts either bare strings or {criterion, ...} dicts (clawpm's
    structured form per CLAWP-016).
    """
    tokens: set[str] = set()
    for c in criteria:
        if isinstance(c, str):
            text = c
        elif isinstance(c, dict):
            text = c.get("criterion", "")
        else:
            text = str(c)
        for tok in text.lower().split():
            # Strip common punctuation
            tok = tok.strip(".,;:!?\"'()[]{}<>")
            if not tok or tok in _SC_STOPWORDS:
                continue
            tokens.add(tok)
    return tokens


def _similarity_score(
    *,
    predictions: dict,
    target_complexity: str | None,
    target_scope: list[str],
    target_frameworks: set[str],
    target_sc_tokens: set[str],
    target_codegraph_symbols: set[str] | None = None,
    candidate_codegraph_symbols: set[str] | None = None,
) -> int:
    """Compute a simple additive similarity score. Higher = more similar.

    Codex round-7 may flag this — note: the CodeGraph axis (CLAWP-030)
    is opt-in via ``target_codegraph_symbols`` + per-candidate
    ``candidate_codegraph_symbols``. Callers without a CodeGraph index
    omit both and the scoring is unchanged.
    """
    score = 0

    if target_complexity is not None:
        pc = predictions.get("complexity")
        if pc == target_complexity:
            score += 3

    # Scope-glob overlap (prefix-prefix heuristic, same as _globs_overlap)
    pred_scope = predictions.get("files_scope") or []
    if target_scope and pred_scope:
        for a in target_scope:
            for b in pred_scope:
                if _scope_overlap_simple(a, b):
                    score += 2
                    break  # one match per target glob

    # Framework intersection
    pred_frameworks = {
        f.lower() for f in (predictions.get("frameworks") or [])
    }
    if target_frameworks and pred_frameworks:
        score += 2 * len(target_frameworks & pred_frameworks)

    # Success-criteria token Jaccard step-score: every 3 shared tokens
    # adds 1, capped at 4 to prevent a single text-heavy criterion from
    # dominating the ranking.
    if target_sc_tokens:
        pred_sc_tokens = _tokenise_criteria(
            predictions.get("success_criteria") or []
        )
        shared = len(target_sc_tokens & pred_sc_tokens)
        score += min(4, shared // 3)

    # CLAWP-030: CodeGraph semantic-symbol overlap. Catches "same
    # subsystem" relevance even when files_scope strings don't share a
    # glob prefix (e.g. one task touches `src/auth/middleware.py`, the
    # candidate touched `src/api/login.py` — different scope globs but
    # both reference the `authenticate_user` symbol). +1 per shared
    # symbol, capped at +4 to match the sc-token cap.
    if target_codegraph_symbols and candidate_codegraph_symbols:
        shared_symbols = len(
            target_codegraph_symbols & candidate_codegraph_symbols
        )
        score += min(4, shared_symbols)

    # Baseline: candidate has actuals (guaranteed by caller filtering,
    # but the +1 documents that real actuals beat empty ones).
    score += 1

    return score


def _scope_overlap_simple(a: str, b: str) -> bool:
    """Prefix-prefix heuristic — same shape as cli._globs_overlap but
    importable from reflect.py without the circular dep."""
    if a == b:
        return True
    # Strip glob metas to literal prefix
    def _prefix(p: str) -> str:
        for ch in ("*", "?", "["):
            i = p.find(ch)
            if i != -1:
                p = p[:i]
        return p.rstrip("/")
    pa = _prefix(a)
    pb = _prefix(b)
    if pa == "" or pb == "":
        return True
    return pa.startswith(pb) or pb.startswith(pa)


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
