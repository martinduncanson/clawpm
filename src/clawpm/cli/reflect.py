from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from clawpm.output import output_error, output_json, output_success
from clawpm.tasks import get_task
from clawpm.context import expand_task_id
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Reflect command group — calibration capture + consumers (CLAWP-040)
# ============================================================================


@main.group()
def reflect() -> None:
    """Reflection layer — query predictions vs actuals and calibrate estimates."""
    pass


@reflect.command("summarize")
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.pass_context
def reflect_summarize(ctx: click.Context, project_id: str | None) -> None:
    """Summarize predicted-vs-actual duration calibration across done tasks (CLAWP-040).

    Aggregates the reflection corpus into duration ratios (actual/predicted)
    bucketed by complexity, confidence, and agent_profile. Rows without a
    usable actual are flagged (dirty) and excluded so they don't poison the
    ratio. Omit --project to span all projects. This is the measurement half
    of the calibration loop; `reflect suggest` applies it.
    """
    from clawpm.reflect import summarize_calibration
    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    # CLAWP-040 codex round-3 P2 fix: honor the global --project flag from
    # the main group when the subcommand option wasn't passed. Both absent
    # = aggregate ALL projects.
    if project_id is None:
        project_id = ctx.obj.get("global_project")
    # By here `project_id` is the resolved scope: subcommand > global, with
    # None meaning aggregate ALL. The call below passes the resolved value,
    # NOT a raw default.
    summary = summarize_calibration(config.portfolio_root, project_id)
    output_success(
        f"Calibration summary ({summary['project_id']}): "
        f"{summary['with_usable_duration']}/{summary['total_done']} done tasks "
        f"with usable duration.",
        data=summary,
        fmt=fmt,
    )


@reflect.command("suggest")
@click.argument("task_id", required=False, default=None)
@click.option("--project", "-p", "project_id", default=None, help="Project ID")
@click.option("--complexity", "-c", type=click.Choice(["s", "m", "l", "xl"]), default=None, help="Complexity bucket to calibrate against (derived from the task when TASK_ID is given).")
@click.option("--predicted-duration", "predicted_duration", default=None, help="Gut estimate to calibrate: 90, 2h, 3d. Returned deflated by the learned ratio.")
@click.option("--confidence", type=int, default=None, help="Operator confidence 1-5 (recorded on the suggestion).")
@click.option("--agent-profile", "agent_profile", default=None, help="Agent profile (recorded on the suggestion).")
@click.option("--min-bucket", "min_bucket", type=int, default=5, help="Minimum samples for a complexity bucket before falling back to the global ratio.")
@click.pass_context
def reflect_suggest(
    ctx: click.Context,
    task_id: str | None,
    project_id: str | None,
    complexity: str | None,
    predicted_duration: str | None,
    confidence: int | None,
    agent_profile: str | None,
    min_bucket: int,
) -> None:
    """Suggest a calibrated duration from the corpus's learned ratio (CLAWP-040).

    Two modes:
      - ``reflect suggest <task_id>`` derives complexity / confidence /
        agent_profile / predicted-duration from the task, then deflates.
      - ``reflect suggest --complexity m --predicted-duration 6h`` calibrates
        a bare estimate against the complexity bucket.

    Deterministic — no model call. Falls back to the global ratio when the
    complexity bucket has fewer than --min-bucket samples.
    """
    from clawpm.reflect import parse_duration, suggest_duration
    fmt = get_format(ctx)
    config = require_portfolio(ctx)

    # CLAWP-040 codex round-3 P2 fix: honor the global --project flag from
    # the main group when the subcommand option wasn't passed. require_project
    # below handles the task_id case (which always needs a concrete project);
    # the bare-bucket path inherits the global here, both-absent = ALL.
    if project_id is None:
        project_id = ctx.obj.get("global_project")

    predicted_min: int | None = None
    if task_id:
        project_id, _ = require_project(ctx, project_id)
        task_id = expand_task_id(task_id, project_id)
        t = get_task(config, project_id, task_id)
        if not t:
            output_error("task_not_found", f"No task with id '{task_id}' in project '{project_id}'", fmt=fmt)
            sys.exit(1)
        # Codex round-6 P2: prefer t.predictions.complexity over t.complexity
        # because summarize_calibration buckets by predictions.complexity.
        # Using t.complexity would lookup the wrong bucket (or fall back to
        # global) when the predicted and actual/current complexity differ.
        complexity = complexity or (
            t.predictions.complexity.value if t.predictions.complexity
            else (t.complexity.value if t.complexity else None)
        )
        confidence = confidence if confidence is not None else t.predictions.confidence
        agent_profile = agent_profile or t.agent_profile
        predicted_min = t.predictions.duration_min

    if predicted_duration is not None:
        try:
            predicted_min = parse_duration(predicted_duration)
        except Exception as exc:
            output_error("bad_duration", str(exc), fmt=fmt)
            sys.exit(1)

    result = suggest_duration(
        config.portfolio_root,
        complexity=complexity,
        confidence=confidence,
        agent_profile=agent_profile,
        predicted_min=predicted_min,
        project_id=project_id,
        min_bucket=min_bucket,
    )
    output_success(f"Calibration suggestion (bucket: {result['bucket']})", data=result, fmt=fmt)


@reflect.command("history-import")
@click.option(
    "--source", "source_dir", default=None,
    envvar="CLAWPM_HISTORY_SOURCE",
    help="Path to history source directory (or set CLAWPM_HISTORY_SOURCE).",
)
@click.pass_context
def reflect_history_import(ctx: click.Context, source_dir: str | None) -> None:
    """Scan a directory of session transcripts / agent logs for task mentions.

    Walks ``<source_dir>`` (recursively) for ``.jsonl`` files, extracts every
    line that references a clawpm task ID (per ``clawpm.history.TASK_ID_RE``),
    and returns an aggregate report:

    .. code-block:: json

        {
          "status": "scanned" | "no_mentions" | "no_source" | "source_not_found",
          "source_dir": "<absolute path>",
          "files_scanned": <int>,
          "files_truncated": <bool>,
          "mentions_found": <int>,
          "unique_task_ids": <int>,
          "by_task": {"CLAWP-011": 12, "CLAWP-018": 3, ...},
          "mentions": [TaskMention, ...]
        }

    Source path resolution:
    - ``--source <dir>`` flag (highest precedence).
    - ``CLAWPM_HISTORY_SOURCE`` env var.
    - No hardcoded fallback. Static references to agent-runtime paths (e.g.
      ``~/.openclaw/``) were removed at commit a06a5b8 because they raised
      VirusTotal false positives and were an operational security smell.

    Implementation notes:
    - The importer module is lazy-imported below so the clawpm binary's
      static import graph stays free of suspicious-path patterns.
    - TASK_ID_RE accepts both single-segment (``CLAWP-011``) and multi-segment
      (``MY-PR-001``, ``A-B-C-123``) prefixes — matters for projects whose
      IDs normalise to embedded hyphens.

    Not yet implemented (Phase 3 work):
    - Writing reflection events back to ``~/clawpm/reflections/<task-id>.jsonl``
      (currently the function returns the mention report; the operator decides
      what to do with it).
    - Deduplication by ``task_id + occurred_at`` for safe re-runs.
    - Optional ``history_source`` key in ``portfolio.toml`` so ``clawpm setup``
      can prompt once instead of requiring the flag/env on every invocation.
    """
    import json as _json
    if not source_dir:
        click.echo(_json.dumps({
            "status": "no_source",
            "message": "Provide --source <dir> or set CLAWPM_HISTORY_SOURCE.",
        }, indent=2))
        return

    # Lazy import: keeps the suspicious-pattern code path out of the binary's
    # static import graph. See module docstring + design constraints above.
    from clawpm.history import import_history as _import_history

    source_path = Path(source_dir).expanduser()
    if not source_path.is_dir():
        click.echo(_json.dumps({
            "status": "source_not_found",
            "source": source_path.as_posix(),
            "message": "Source directory does not exist or is not a directory.",
        }, indent=2))
        return

    report = _import_history(source_path)
    report["status"] = "scanned" if report["mentions_found"] > 0 else "no_mentions"
    click.echo(_json.dumps(report, indent=2))


@reflect.command("void")
@click.argument("task_id", required=False, default=None)
@click.option("--project", "-p", "project_id", default=None, help="Project ID (auto-detected if not specified). Stamped on the void event for cross-project isolation.")
@click.option("--reason", required=True, help="Why this reflection is bad data (required)")
@click.option(
    "--all-empty-actuals", "all_empty_actuals", is_flag=True,
    help="Void all reflections across the corpus where actuals.duration_min is null",
)
@click.pass_context
def reflect_void(
    ctx: click.Context,
    task_id: str | None,
    project_id: str | None,
    reason: str,
    all_empty_actuals: bool,
) -> None:
    """Mark a reflection event void without deleting it (event-source discipline).

    Appends a void event to the task's .jsonl file.  Does NOT modify or delete
    the original event — consumers skip events with a matching void entry.

    Examples::

        clawpm reflect void PROJ-007 --reason "actuals were wrong — pre-bugfix"
        clawpm reflect void --all-empty-actuals --reason "Phase 1 corpus cleanup"
    """
    import json as _json
    from datetime import datetime, timezone

    config = require_portfolio(ctx)
    fmt = get_format(ctx)
    ref_dir = config.portfolio_root / "reflections"

    voided: list[dict] = []
    errors: list[dict] = []

    def _void_task_reflection(tid: str, project_hint: str | None = None) -> None:
        ref_file = ref_dir / f"{tid}.jsonl"
        if not ref_file.exists():
            errors.append({"task_id": tid, "error": "no_reflection_file"})
            return

        # Read existing events to check whether void already applied
        lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        existing_records = []
        for line in lines:
            try:
                existing_records.append(_json.loads(line))
            except _json.JSONDecodeError:
                pass

        # Cross-project isolation (round-7 audit + round-8 P1 follow-up):
        # the JSONL filename is keyed by task_id alone, so two projects
        # sharing a task_id share a file. Stamp the void event with
        # project_id from the EXPLICIT command-line context only — do
        # not infer from prior file events, because in a shared file
        # the first record could belong to a different project than
        # the operator's `--project` context. When no command-line
        # project is given, fall through to legacy unscoped (matches
        # back-compat for older voids; consumers must treat absent
        # project_id as wildcard).
        resolved_project: str | None = project_hint

        # Build the void entry
        void_entry: dict = {
            "event": "void",
            "task_id": tid,
            "reason": reason,
            "voided_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if resolved_project is not None:
            void_entry["project_id"] = resolved_project

        # Append (atomic: write to .tmp then replace)
        tmp_file = ref_file.with_suffix(".jsonl.tmp")
        try:
            # Copy existing content + new void line
            existing_text = ref_file.read_text(encoding="utf-8")
            tmp_file.write_text(
                existing_text.rstrip("\n") + "\n" + _json.dumps(void_entry) + "\n",
                encoding="utf-8",
            )
            tmp_file.replace(ref_file)
            voided.append({"task_id": tid, "voided_at": void_entry["voided_at"]})
        except OSError as exc:
            errors.append({"task_id": tid, "error": str(exc)})

    # Resolve project_id context for the single-task path. ONLY use
    # the explicit --project flag, never auto-detect from CWD —
    # auto-detect can stamp the void with the dev environment's
    # project instead of the task's real project, which is worse than
    # stamping unscoped (legacy consumers wildcard on absent
    # project_id, scoped consumers in shared-task-id files get
    # mis-attribution). When --project is omitted we fall through to
    # legacy unscoped behaviour, which is back-compat-safe.
    cli_project_id: str | None = project_id  # explicit only

    if all_empty_actuals:
        if not ref_dir.exists():
            output_json({"voided": [], "errors": [], "message": "No reflections directory found"})
            return
        for ref_file in sorted(ref_dir.glob("*.jsonl")):
            derived_tid = ref_file.stem
            lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
            has_empty_actuals = False
            for line in lines:
                try:
                    rec = _json.loads(line)
                    if rec.get("event") in ("task_done", "task_blocked"):
                        actuals = rec.get("actuals", {})
                        if actuals.get("duration_min") is None:
                            has_empty_actuals = True
                            break
                except _json.JSONDecodeError:
                    pass
            if has_empty_actuals:
                # Corpus sweep emits unscoped voids (cross-project-safe
                # only because the consumer wildcards on absent
                # project_id). Don't try to infer project from prior
                # records — same hazard Codex flagged.
                _void_task_reflection(derived_tid)
    elif task_id:
        _void_task_reflection(task_id, project_hint=cli_project_id)
    else:
        output_error(
            "missing_target",
            "Provide a TASK_ID or use --all-empty-actuals",
            fmt=fmt,
        )
        sys.exit(1)

    output_json({
        "voided": voided,
        "errors": errors,
        "count": len(voided),
    })
