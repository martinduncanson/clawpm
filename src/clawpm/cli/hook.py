from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click

from clawpm.models import Task
from clawpm.tasks import get_task
from clawpm.context import expand_task_id
from clawpm.cli.base import main, get_format, require_portfolio, require_project

# ============================================================================
# Hook subcommands (called by Claude Code hooks; not for direct human use)
# ============================================================================


@main.group()
def hook() -> None:
    """Hook-callable subcommands for Claude Code integration.

    These commands are designed to be wired into ``.claude/settings.json``
    (or ``.claude/settings.local.json``) Stop / PostToolUse hooks. They
    read the standard hook stdin JSON and emit hook output JSON on stdout.
    """
    pass


@hook.command("session-start")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task ID whose SessionStart sidecar to emit")
@click.pass_context
def hook_session_start(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
) -> None:
    """Print the SessionStart additionalContext sidecar to stdout.

    Wired into Claude Code as a SessionStart command hook by
    `clawpm tasks dispatch`. Reads the sidecar JSON file co-located with
    settings.local.json and prints it verbatim — Claude Code's hook
    output schema accepts JSON on stdout. Cross-platform safe (no shell
    quoting, no embedded JSON in command strings).
    """
    import json as _json_ss
    from clawpm.dispatch import session_start_payload_path

    fmt = get_format(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    sidecar = session_start_payload_path(Path.cwd())
    if not sidecar.exists():
        # No sidecar = SessionStart was not configured for this dispatch
        # (or was torn down). Emit an empty hookSpecificOutput so the
        # hook is a no-op rather than a crash.
        click.echo(_json_ss.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }))
        return
    try:
        click.echo(sidecar.read_text(encoding="utf-8"))
    except OSError as exc:
        # Read failure must not crash the session start; emit a degraded
        # but valid hook output with the error surfaced.
        click.echo(_json_ss.dumps({
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": (
                    f"(clawpm: failed to read SessionStart sidecar: {exc})"
                ),
            }
        }))


@hook.command("eval-stop")
@click.option("--project", "-p", "project_id", help="Project ID (auto-detected if not specified)")
@click.option("--task", "task_id", required=True, help="Task ID whose rubric to evaluate against")
@click.option("--transcript-file", "transcript_file", type=click.Path(), default=None,
              help="Path to the transcript file. Overrides hook stdin's transcript_path.")
@click.option("--rubric-file", "rubric_file", type=click.Path(), default=None,
              help="Path to a pre-rendered rubric markdown file. Default: render from the task.")
@click.option("--confirm-close/--no-confirm-close", "confirm_close", default=None,
              help="CLAWP-041: run an adversarial refutation pass before letting the "
                   "rubric close the task (fires only on the ok=true transition; the "
                   "block path is unchanged). Default: env CLAWPM_CONFIRM_CLOSE, else off.")
@click.option("--refute-votes", "refute_votes", type=int, default=None,
              help="CLAWP-041: number of lens-varied refutation votes when --confirm-close "
                   "is active (>=half of refuters that ran overturn; ties overturn). Default: env CLAWPM_REFUTE_VOTES, else 1.")
@click.pass_context
def hook_eval_stop(
    ctx: click.Context,
    project_id: str | None,
    task_id: str,
    transcript_file: str | None,
    rubric_file: str | None,
    confirm_close: bool | None,
    refute_votes: int | None,
) -> None:
    """Stop-hook condition evaluator (CLAWP-017).

    Reads the Claude Code Stop-hook input from stdin (JSON), extracts the
    transcript path, renders the task's success-criteria rubric, dispatches
    a Haiku-class judge, and emits a hook-output JSON deciding whether the
    subagent may stop.

    Local emulation of Anthropic Managed Agents' Outcomes evaluator — no
    paid API required; uses the operator's existing Claude Code subscription
    via subprocess to ``claude --print``. Override the judge with
    ``CLAWPM_JUDGE_CMD`` env var.
    """
    import json as _json_hook
    import os as _os_hook
    from clawpm.judges.stop_condition import (
        JudgeVerdict,
        evaluate_stop_condition,
        evaluate_stop_condition_confirmed,
        load_transcript_from_hook_input,
        map_verdict_to_hook_output,
    )
    from clawpm.rubric import render_rubric_markdown

    # CLAWP-041: resolve confirm-close gating. Flag wins; else env; else off.
    if confirm_close is None:
        confirm_close = _os_hook.environ.get(
            "CLAWPM_CONFIRM_CLOSE", ""
        ).strip().lower() in ("1", "true", "yes", "on")
    if refute_votes is None:
        try:
            refute_votes = int(_os_hook.environ.get("CLAWPM_REFUTE_VOTES", "1"))
        except ValueError:
            refute_votes = 1
    refute_votes = max(1, refute_votes)

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)
    task_id = expand_task_id(task_id, project_id)

    # CLAWP-038 — best-effort agent_profile so the iteration events this
    # hook writes can be bucketed by profile in `reflect summarize`. Any
    # failure (task not found yet, parse error) degrades to None.
    _hook_agent_profile: str | None = None
    try:
        _ap_task = get_task(config, project_id, task_id)
        if _ap_task is not None:
            _hook_agent_profile = _ap_task.agent_profile
    except Exception:
        _hook_agent_profile = None

    # 1. Load the rubric — from file if given, else render from the task.
    rubric: str
    if rubric_file:
        rubric = Path(rubric_file).read_text(encoding="utf-8")
    else:
        task = get_task(config, project_id, task_id)
        if not task:
            # Task-not-found is a dispatch-config bug, NOT a soft fail.
            # Block the Stop event so the operator sees the problem in
            # the transcript rather than discovering it after the subagent
            # has already finished gated work. Codex round-2 P1: use
            # `decision: "block"` + `reason` (forces agent to keep
            # working), NOT `continue: false` (which halts the entire
            # pipeline / terminates the agent).
            click.echo(_json_hook.dumps({
                "decision": "block",
                "reason": (
                    f"clawpm eval-stop: task {task_id} not found in "
                    f"project {project_id} - fix dispatch config "
                    f"(check `clawpm tasks dispatch --task-id`) before "
                    f"continuing."
                ),
            }))
            return
        rubric = render_rubric_markdown(task)

    # 2. Load the transcript — from --transcript-file, or from hook stdin.
    transcript: str
    if transcript_file:
        transcript = Path(transcript_file).read_text(encoding="utf-8", errors="replace")
    else:
        # Stop-hook input comes in on stdin.
        try:
            stdin_raw = sys.stdin.read()
        except OSError:
            stdin_raw = ""
        if stdin_raw.strip():
            try:
                hook_input = _json_hook.loads(stdin_raw)
            except _json_hook.JSONDecodeError:
                hook_input = {}
            try:
                transcript = load_transcript_from_hook_input(hook_input)
            except (ValueError, FileNotFoundError) as exc:
                # Can't find the transcript — surface to operator but don't
                # block the stop (would loop forever).
                click.echo(_json_hook.dumps({
                    "continue": True,
                    "systemMessage": f"clawpm eval-stop: transcript unavailable ({exc}); rubric not enforced",
                }))
                return
        else:
            click.echo(_json_hook.dumps({
                "continue": True,
                "systemMessage": "clawpm eval-stop: no stdin and no --transcript-file; rubric not enforced",
            }))
            return

    # 3. Dispatch the judge. Errors here are unexpected — surface them
    # in a way that's visible to doctor, not silently swallowed.
    try:
        if confirm_close:
            verdict = evaluate_stop_condition_confirmed(
                rubric=rubric, transcript=transcript, refute_votes=refute_votes
            )
        else:
            verdict = evaluate_stop_condition(rubric=rubric, transcript=transcript)
    except RuntimeError as exc:
        # Judge error = enforcement-layer down. Fail-open (continue=true)
        # is defensible because blocking forever on a broken judge is
        # worse, but we MUST leave a doctor signal so repeated judge
        # errors don't silently degrade clawpm to no-enforcement.
        try:
            from clawpm.reflect import write_iteration_event
            write_iteration_event(
                portfolio_root=config.portfolio_root,
                task_id=task_id,
                project_id=project_id,
                verdict_ok=False,
                verdict_reason=f"JUDGE_ERROR: {exc}",
                verdict_impossible=False,
                agent_profile=_hook_agent_profile,
            )
        except OSError:
            # Writing the doctor signal failed too — last resort is the
            # systemMessage. Don't pile silent failures.
            pass
        click.echo(_json_hook.dumps({
            "continue": True,
            "systemMessage": (
                f"clawpm eval-stop: judge error ({exc}); rubric not "
                f"enforced. Consecutive judge errors will be flagged by "
                f"clawpm doctor - set CLAWPM_JUDGE_CMD or install Claude "
                f"Code if this keeps happening."
            ),
        }))
        return

    # CLAWP-019: capture the iteration event. This IS the calibration
    # spine — narrow exception so a real filesystem failure surfaces in
    # the systemMessage instead of silently nuking the iteration count.
    try:
        from clawpm.reflect import write_iteration_event
        write_iteration_event(
            portfolio_root=config.portfolio_root,
            task_id=task_id,
            project_id=project_id,
            verdict_ok=verdict.ok,
            verdict_reason=verdict.reason,
            verdict_impossible=verdict.impossible,
            agent_profile=_hook_agent_profile,
        )
    except OSError as exc:
        # Disk full / permission / encoding errors. Surface in the
        # hook output's systemMessage so the operator sees it in the
        # next transcript update.
        output = map_verdict_to_hook_output(verdict)
        # Preserve continue/block decision; just decorate systemMessage.
        existing_msg = output.get("systemMessage", "")
        output["systemMessage"] = (
            f"clawpm eval-stop: iteration event WRITE FAILED ({exc}); "
            f"calibration data lost for this cycle. {existing_msg}".strip()
        )
        click.echo(_json_hook.dumps(output))
        return

    # CLAWP-062: thrashing detection -- check AFTER writing the iteration event
    # so the count includes the iteration we just recorded.
    if not verdict.ok and not verdict.impossible:
        try:
            from clawpm.reflect import detect_thrashing, _DEFAULT_THRASH_THRESHOLD
            import os as _os_thr
            # Resolve effective threshold: per-task > env > module default.
            _thr_task = get_task(config, project_id, task_id)
            _thr_per_task = None
            if _thr_task is not None and _thr_task.predictions is not None:
                _thr_per_task = _thr_task.predictions.thrash_threshold
            if _thr_per_task is not None:
                _thr_effective = _thr_per_task
            else:
                _env_thr = _os_thr.environ.get("CLAWPM_THRASH_THRESHOLD", "").strip()
                if _env_thr:
                    try:
                        _thr_effective = int(_env_thr)
                    except ValueError:
                        _thr_effective = _DEFAULT_THRASH_THRESHOLD
                else:
                    _thr_effective = _DEFAULT_THRASH_THRESHOLD
            if detect_thrashing(
                config.portfolio_root, task_id, project_id,
                threshold=_thr_effective,
            ):
                _thrash_reason = (
                    "THRASHING detected on task " + task_id + ": "
                    + str(_thr_effective) + " consecutive not-ok iterations "
                    + "with no rubric progress. "
                    + "Last verdict: " + verdict.reason[:200] + ". "
                    + "Agent stopped; operator should triage."
                )
                verdict = JudgeVerdict(
                    ok=False,
                    reason=_thrash_reason,
                    stop_condition_tripped=True,
                )
        except Exception:
            # Best-effort: thrash detection failure must never block hook
            # output. Broader than OSError -- a malformed-record/parse path
            # must fail open too.
            pass

    output = map_verdict_to_hook_output(verdict)
    click.echo(_json_hook.dumps(output))
