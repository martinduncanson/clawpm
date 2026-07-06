from __future__ import annotations

import subprocess
import sys

import click

from clawpm.concurrency import LockTimeout
from clawpm.models import SuccessCriterion
from clawpm.output import output_error, output_success
from clawpm.tasks import add_task, change_task_state
from clawpm.context import expand_task_id
from clawpm.cli.base import main, _mutation_errors, get_format, require_portfolio, require_project

# ============================================================================
# Agent dispatch wrapper (CLAWP-024)
# ============================================================================


@main.group("agent")
def agent_group() -> None:
    """Parent-spawned subagent dispatch with Stop-hook judge integration.

    ``clawpm tasks dispatch`` (CLAWP-018) writes the per-target settings
    so a hand-launched subagent's Stop / PostToolUse / SessionStart hooks
    fire. ``clawpm agent dispatch`` (CLAWP-024) wraps the full cycle in
    one command — task create, dispatch settings write, subagent invoke,
    judge grade, state transition — so the rubric is enforced on every
    parent-spawned subagent without the parent needing to remember the
    six-step manual sequence.
    """
    pass


@agent_group.command("dispatch")
@click.option(
    "--project", "-p", "project_id",
    help="Project ID (auto-detected if not specified)",
)
@click.option(
    "--prompt", "prompt", required=True,
    help="The subagent prompt — becomes the subtask body AND is fed on stdin to the judge CLI.",
)
@click.option(
    "--parent", "parent_id", default=None,
    help="Optional parent task ID. Recorded in the reflection event for traceability.",
)
@click.option(
    "--rubric-criteria", "rubric_criteria", multiple=True,
    help="Success criterion (repeatable). Plain string OR JSON object "
         "{'criterion':'...','gradeable_signal':'...','comparator':'...'} — "
         "parsed via SuccessCriterion.from_cli.",
)
@click.option(
    "--title", "title", default=None,
    help="Optional subtask title. Defaults to a truncated prompt preview.",
)
@click.option(
    "--judge-cmd-override", "judge_cmd_override", default=None,
    help="Override the judge subprocess command (highest priority — beats "
         "CLAWPM_JUDGE_CMD env var). Use a stub here for offline testing.",
)
@click.option(
    "--no-codegraph", "no_codegraph", is_flag=True, default=False,
    help="Skip codegraph init+index inside the worktree (CLAWP-029). "
         "Default: init when codegraph is on PATH. Use this for batches "
         "where per-dispatch index cost dominates.",
)
@click.option(
    "--confirm-close", "confirm_close", is_flag=True, default=False,
    help="CLAWP-041: run an adversarial refutation pass before accepting an "
         "ok=true verdict (single-shot dispatch grades once, so this is cheap).",
)
@click.option(
    "--refute-votes", "refute_votes", type=int, default=1,
    help="CLAWP-041: lens-varied refutation votes when --confirm-close is set "
         "(>=half of refuters that ran overturn; ties overturn). Default 1.",
)
@click.option(
    "--agent-profile", "agent_profile", default=None,
    help="Capability/skill profile for the dispatched subagent (CLAWP-038). "
         "Recorded on the subtask and in the reflection/iteration events so "
         "`reflect summarize` can segment predicted-vs-actual by profile.",
)
@click.pass_context
def agent_dispatch(
    ctx: click.Context,
    project_id: str | None,
    prompt: str,
    parent_id: str | None,
    rubric_criteria: tuple[str, ...],
    title: str | None,
    judge_cmd_override: str | None,
    no_codegraph: bool,
    confirm_close: bool,
    refute_votes: int,
    agent_profile: str | None,
) -> None:
    """Spawn a subagent, grade its output against the rubric, persist the verdict.

    Flow:
      1. ``add_task`` with prompt as body + rubric_criteria as
         ``predictions.success_criteria``.
      2. ``create_worktree`` under ``<repo>/.clawpm-worktrees/<subtask-id>/``.
      3. ``write_dispatch_settings`` into the worktree (Stop / PostToolUse /
         SessionStart hooks).
      4. Subprocess to ``claude --print`` (or ``--judge-cmd-override``)
         with the prompt on stdin; capture stdout as the transcript.
      5. ``evaluate_stop_condition(rubric, transcript)``.
      6. ``ok=True``  → mark subtask DONE + write reflection event.
         ``ok=False`` → mark subtask BLOCKED + write iteration event.

    The wrapper is testable without a real ``claude`` CLI by passing a
    ``--judge-cmd-override`` that points to a stub command, or by setting
    ``CLAWPM_JUDGE_CMD`` (legacy env var from CLAWP-017).
    """
    from clawpm.agent import AgentDispatchError, dispatch_agent

    fmt = get_format(ctx)
    config = require_portfolio(ctx)
    project_id, _ = require_project(ctx, project_id)

    if parent_id:
        parent_id = expand_task_id(parent_id, project_id)

    # NB: do NOT route dispatch through the broad _mutation_errors contract.
    # dispatch_agent's surface is far wider than a task-tree mutator — it creates
    # worktrees and runs git subprocesses — so a FileNotFoundError here can mean
    # "git not on PATH", NOT "task moved by a concurrent session". The broad
    # FileNotFoundError->not_found / FileExistsError->already_exists mapping would
    # mask a genuine environment failure (Codex review). Catch only the mutator
    # LockTimeout that genuinely propagates from add_task/change_task_state, plus
    # dispatch's own AgentDispatchError/ValueError; let anything else surface.
    try:
        result = dispatch_agent(
            config=config,
            project_id=project_id,
            prompt=prompt,
            success_criteria=list(rubric_criteria),
            parent_id=parent_id,
            judge_cmd_override=judge_cmd_override,
            title=title,
            init_codegraph=not no_codegraph,
            confirm_close=confirm_close,
            refute_votes=refute_votes,
            agent_profile=agent_profile,
        )
    except LockTimeout as exc:
        output_error(
            "lock_timeout",
            f"Could not acquire the project lock (another session may be busy): {exc}",
            fmt=fmt,
        )
        sys.exit(1)
    except (AgentDispatchError, ValueError) as exc:
        output_error("agent_dispatch_failed", str(exc), fmt=fmt)
        sys.exit(1)

    # Surface the verdict-derived headline in the success message so
    # text-mode operators see at-a-glance whether the dispatch passed
    # without parsing JSON.
    verdict = result["verdict"]
    if verdict["ok"]:
        headline = f"Agent dispatch ok ({result['subtask_id']}): {verdict['reason'][:120]}"
    elif verdict["impossible"]:
        headline = (
            f"Agent dispatch IMPOSSIBLE ({result['subtask_id']}): "
            f"{verdict['reason'][:120]} — subtask marked blocked"
        )
    else:
        headline = (
            f"Agent dispatch failed ({result['subtask_id']}): "
            f"{verdict['reason'][:120]} — subtask marked blocked"
        )

    output_success(headline, data=result, fmt=fmt)
