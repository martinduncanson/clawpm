"""Parent-spawned subagent dispatch wrapper (CLAWP-024).

`clawpm tasks dispatch` (CLAWP-018) handles the case where an operator
hand-rolls a subagent invocation: it writes a `.claude/settings.local.json`
into a target dir so the subagent's Stop / PostToolUse / SessionStart hooks
fire. But the common reality is that **parents spawn subagents directly via
the Task tool**, bypassing the dispatch settings entirely. The Stop-hook
condition evaluator (CLAWP-017) then has no rubric to enforce, so
subagent-driven work goes through unreviewed.

This module closes that gap. ``clawpm agent dispatch`` is the
single-command wrapper that:

  1. Auto-creates a subtask with the supplied prompt as the body and the
     supplied criteria as ``predictions.success_criteria``.
  2. Builds a per-dispatch worktree under
     ``<repo>/.clawpm-worktrees/<subtask-id>/`` and seeds it with the same
     hook-wired ``settings.local.json`` ``tasks dispatch`` produces.
  3. Invokes the judge CLI (``claude --print`` by default, overridable via
     ``CLAWPM_JUDGE_CMD`` or ``--judge-cmd-override``) with the prompt on
     stdin, capturing stdout as the "transcript".
  4. Pipes that transcript through the Stop-hook condition evaluator
     against the rubric rendered from the new subtask.
  5. Marks the subtask DONE on ``ok=true`` (writing a reflection event)
     or BLOCKED on ``ok=false`` / ``impossible`` (writing an iteration
     event so the calibration loop captures the failure mode).

The transcript and reflection-event paths are returned to the caller so
downstream tooling can attach the artefacts to the parent task.

Design tradeoffs:

  - **Worktree per dispatch.** Each subagent gets an isolated
    ``.clawpm-worktrees/<subtask-id>/`` to avoid colliding on a single
    ``.claude/settings.local.json``. This mirrors the ``--worktree`` flag
    on ``tasks dispatch``; here it's the default because parent-spawned
    subagents are routinely fan-out.
  - **Judge injectable via callable, env var, OR flag.** The Python entry
    point ``dispatch_agent`` accepts a ``judge_invoker`` callable for
    tests; the CLI exposes ``--judge-cmd-override`` for ad-hoc swaps; the
    existing ``CLAWPM_JUDGE_CMD`` env var works unchanged. Three levels
    of override keep the wrapper testable without a real ``claude`` CLI
    and let operators inject a stub mid-pipeline.
  - **Failure ≠ exception.** A judge subprocess failure is surfaced as a
    BLOCKED subtask with the error in the iteration event's reason,
    NOT a Python exception escaping to the caller. The parent agent's
    own Stop hook should pick that up; raising here would orphan the
    subtask in PROGRESS state.
"""

from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .discovery import get_project
from .dispatch import create_worktree, write_dispatch_settings
from .judges.stop_condition import (
    JudgeVerdict,
    evaluate_stop_condition,
    evaluate_stop_condition_confirmed,
)
from .models import (
    Actuals,
    Predictions,
    SuccessCriterion,
    TaskState,
)
from .reflect import write_iteration_event, write_reflection_event
from .rubric import render_rubric_markdown
from .tasks import add_task, change_task_state


# Default judge — matches judges.stop_condition.DEFAULT_JUDGE_CMD so the
# wrapper and the Stop hook stay consistent.
DEFAULT_JUDGE_CMD = ["claude", "--print", "--model", "claude-haiku-4-5"]

JudgeInvoker = Callable[[str], str]


class AgentDispatchError(Exception):
    """Raised for caller-facing errors that prevent dispatch entirely.

    Distinct from "judge ran and returned ok=false": this signals the
    wrapper couldn't even get to the judge call (missing repo_path,
    add_task failure, worktree creation failure). Caller turns this into
    a CLI error; the subtask is NOT marked BLOCKED because there's no
    subtask to mark.
    """


def _make_default_invoker(
    judge_cmd_override: Optional[str],
    cwd: Optional[Path] = None,
) -> JudgeInvoker:
    """Build a judge invoker that subprocesses to a `claude --print`-like CLI.

    Resolution order (highest priority first):
      1. ``judge_cmd_override`` (explicit ``--judge-cmd-override`` flag)
      2. ``CLAWPM_JUDGE_CMD`` env var (legacy from CLAWP-017)
      3. ``DEFAULT_JUDGE_CMD``

    The prompt is fed on stdin so long rubrics + transcripts don't hit
    shell argument limits. 60s ceiling matches the Stop-hook judge —
    same model class, same UX expectation.

    ``cwd`` (Codex round-1 P1 fix): the subprocess is invoked from this
    directory so the per-dispatch ``.claude/settings.local.json`` and
    hooks pick up the worktree-scoped context. Without this, the judge
    runs from the parent process's CWD and the worktree isolation is
    defeated. Defaults to current CWD when None (back-compat for
    callers that don't pass it).
    """
    import os as _os

    if judge_cmd_override:
        cmd = shlex.split(judge_cmd_override)
    else:
        env_cmd = _os.environ.get("CLAWPM_JUDGE_CMD")
        cmd = shlex.split(env_cmd) if env_cmd else list(DEFAULT_JUDGE_CMD)

    def _invoke(prompt: str) -> str:
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                cwd=str(cwd) if cwd is not None else None,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"Judge command not found: {cmd[0]!r}. Install Claude "
                f"Code or set CLAWPM_JUDGE_CMD / pass "
                f"--judge-cmd-override. Error: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Judge timed out after {exc.timeout}s; prompt or "
                "transcript may be too large for a single call"
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(
                f"Judge exited {result.returncode}: {result.stderr[:500]}"
            )
        return result.stdout

    return _invoke


def _run_subagent(
    invoker: JudgeInvoker, prompt: str
) -> tuple[str, Optional[str]]:
    """Invoke the subagent and return ``(transcript, error_reason)``.

    The subagent is treated as the SAME process the judge will later
    inspect: same CLI, same stdin contract. A judge invoker error is
    captured into ``error_reason`` rather than raised — the caller turns
    that into a BLOCKED iteration event so the calibration loop sees
    the failure mode instead of losing the subtask.
    """
    try:
        transcript = invoker(prompt)
        return transcript, None
    except RuntimeError as exc:
        return "", str(exc)


def _write_transcript(target_dir: Path, transcript: str) -> Path:
    """Persist the transcript alongside the dispatch settings.

    Co-locating with `.claude/` keeps the entire dispatch surface in one
    directory tree — easy to ``ls .claude/`` and see everything clawpm
    wrote, no scavenging across the repo.
    """
    path = target_dir / ".claude" / "clawpm-transcript.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(transcript, encoding="utf-8")
    return path


def dispatch_agent(
    config,
    project_id: str,
    prompt: str,
    success_criteria: list[str],
    parent_id: Optional[str] = None,
    judge_invoker: Optional[JudgeInvoker] = None,
    judge_cmd_override: Optional[str] = None,
    title: Optional[str] = None,
    init_codegraph: bool = True,
    confirm_close: bool = False,
    refute_votes: int = 1,
) -> dict:
    """Run a parent-spawned subagent through the full clawpm enforcement loop.

    Returns a dict shaped for JSON output:

      ``subtask_id``, ``verdict`` (ok/reason/impossible), ``transcript_path``,
      ``reflection_event_path``, ``target_dir``, ``settings_path``.

    Raises ``AgentDispatchError`` only when dispatch can't proceed
    (missing repo_path, add_task failed, worktree creation failed). Judge
    failures and rubric not-satisfied verdicts are reported via the
    returned dict — the subtask is marked BLOCKED but the function
    returns normally so the caller can surface the verdict.
    """
    project = get_project(config, project_id)
    if not project or not project.repo_path or not project.repo_path.exists():
        raise AgentDispatchError(
            f"Project {project_id!r} has no usable repo_path "
            f"(got {project.repo_path if project else None!r}); "
            f"agent dispatch requires a repo to create the per-dispatch "
            f"worktree under .clawpm-worktrees/."
        )

    # 1. Auto-create the subtask. Prompt becomes the body; criteria flow
    # into predictions.success_criteria via SuccessCriterion.from_cli so
    # structured-JSON criteria (`{"criterion":"...","gradeable_signal":"..."}`)
    # parse cleanly alongside plain-string criteria.
    parsed_criteria = [SuccessCriterion.from_cli(c) for c in success_criteria]
    predictions = Predictions(
        success_criteria=parsed_criteria,
        filled_by="agent" if parsed_criteria else None,
    )
    # Title defaults to a truncated prompt preview — keeps `clawpm tasks
    # list` readable when many agent-dispatch subtasks land in a project.
    if not title:
        first_line = prompt.strip().splitlines()[0] if prompt.strip() else "agent dispatch"
        title = (first_line[:80] + "...") if len(first_line) > 80 else first_line

    task = add_task(
        config,
        project_id,
        title=title,
        description=prompt,
        predictions=predictions,
    )
    if not task:
        raise AgentDispatchError(
            f"add_task returned None for project {project_id!r}; check "
            "portfolio settings.toml (repo_path must use forward slashes "
            "on Windows)."
        )
    subtask_id = task.id

    # If a parent task was specified, record it in the iteration event
    # stream — we don't restructure the on-disk task hierarchy because
    # add_task already wrote the file at top level. The parent linkage
    # is informational; the operator can promote later with `tasks split`.
    parent_link = parent_id

    # 2. Create a per-dispatch worktree under <repo>/.clawpm-worktrees/<id>.
    # create_worktree validates the task_id for shell-injection / path-
    # traversal safety (CLAWP-018 round-1 hardening).
    #
    # Codex round-1 P2 fix: if worktree creation fails AFTER the subtask
    # was created, the subtask becomes an orphan — open in the backlog
    # with no dispatch artifacts, and a retry would create a duplicate.
    # Mark the orphan BLOCKED with a clear reason before re-raising so
    # the operator can triage instead of finding stray opens later.
    try:
        target_dir = create_worktree(project.repo_path, subtask_id)
    except subprocess.CalledProcessError as exc:
        error_detail = (exc.stderr or exc.stdout or "").strip() or repr(exc)
        try:
            change_task_state(
                config, project_id, subtask_id, TaskState.BLOCKED,
                note=f"clawpm agent dispatch: worktree creation failed — "
                     f"{error_detail}",
            )
        except Exception:
            # Best-effort cleanup; do not mask the original error.
            pass
        raise AgentDispatchError(
            f"git worktree add failed: {error_detail}"
        ) from exc

    # CLAWP-029: initialise CodeGraph in the worktree so the subagent
    # has the index from turn one. Best-effort — failure (codegraph not
    # installed, indexing timeout) silently degrades; the dispatch
    # proceeds without the index.
    codegraph_initialized = False
    if init_codegraph:
        try:
            from .codegraph import init_in_worktree
            codegraph_initialized = init_in_worktree(target_dir)
        except Exception:
            codegraph_initialized = False

    rubric_markdown = render_rubric_markdown(task)

    # 3. Write the dispatch settings — same Stop / PostToolUse /
    # SessionStart hooks `tasks dispatch` writes, so this command is the
    # programmatic equivalent of the manual dispatch+launch pair.
    #
    # Codex round-8 P2: write_dispatch_settings can raise
    # FileExistsError / ValueError (marker conflicts) or OSError (disk
    # full / permissions). Same orphan-cleanup pattern as the worktree-
    # creation guard above: mark the subtask BLOCKED with a clear reason
    # before re-raising AgentDispatchError. Otherwise the command
    # crashes and leaves the subtask OPEN with no dispatch artifacts —
    # retries create duplicates.
    try:
        settings_path = write_dispatch_settings(
            target_dir=target_dir,
            task_id=subtask_id,
            project_id=project_id,
            rubric_markdown=rubric_markdown,
            portfolio_root=config.portfolio_root,
        )
    except (FileExistsError, ValueError, OSError) as exc:
        error_detail = str(exc)
        try:
            change_task_state(
                config, project_id, subtask_id, TaskState.BLOCKED,
                note=f"clawpm agent dispatch: write_dispatch_settings "
                     f"failed — {error_detail}",
            )
        except Exception:
            # Best-effort cleanup; don't mask the original error.
            pass
        raise AgentDispatchError(
            f"write_dispatch_settings failed: {error_detail}"
        ) from exc

    # 4. Invoke the subagent. Tests pass `judge_invoker`; the CLI passes
    # `judge_cmd_override` or falls through to CLAWPM_JUDGE_CMD /
    # DEFAULT_JUDGE_CMD via _make_default_invoker.
    #
    # Codex round-1 P1 fix: pass `cwd=target_dir` so the subprocess runs
    # inside the worktree — picks up the per-dispatch
    # .claude/settings.local.json hooks AND any file edits land in the
    # isolated tree, not the parent checkout. Without this the worktree
    # isolation is theatre.
    invoker = judge_invoker or _make_default_invoker(
        judge_cmd_override, cwd=target_dir
    )
    transcript, subagent_error = _run_subagent(invoker, prompt)
    transcript_path = _write_transcript(target_dir, transcript)

    # 5. Evaluate the transcript against the rubric. If the subagent
    # invocation itself errored, short-circuit to a not-ok verdict — the
    # judge has nothing to grade, and we want the failure mode captured
    # in the iteration_event stream.
    reflection_event_path: Optional[Path] = None
    if subagent_error is not None:
        verdict = JudgeVerdict(
            ok=False,
            reason=f"SUBAGENT_ERROR: {subagent_error}",
            impossible=False,
        )
    else:
        # Both phases (subagent + judge) reuse the same `invoker`
        # callable. ``evaluate_stop_condition`` composes the
        # rubric+transcript prompt via ``build_judge_prompt`` and hands
        # it to the invoker; we get JudgeVerdict.parse() for free.
        # Note (CLAWP-041): in this single-shot path a base-judge RuntimeError
        # fails CLOSED (ok=False, below) — intentional, unlike the CLI Stop
        # hook which fails open. A *refuter* error inside
        # evaluate_stop_condition_confirmed abstains (fails open relative to
        # the refutation pass) to avoid an infinite block loop. The mixed
        # stance is deliberate; don't "reconcile" it without re-reading both.
        try:
            if confirm_close:
                verdict = evaluate_stop_condition_confirmed(
                    rubric=rubric_markdown,
                    transcript=transcript,
                    invoker=invoker,
                    refute_votes=max(1, refute_votes),
                )
            else:
                verdict = evaluate_stop_condition(
                    rubric=rubric_markdown,
                    transcript=transcript,
                    invoker=invoker,
                )
        except RuntimeError as exc:
            verdict = JudgeVerdict(
                ok=False,
                reason=f"JUDGE_ERROR: {exc}",
                impossible=False,
            )

    # 6. Persist verdict → state transition + calibration event.
    if verdict.ok:
        # DONE path: terminal reflection event captures the (empty for
        # now) deltas. Iterations counter rolls up via
        # count_iterations_for_task on the reflection-event read path.
        change_task_state(
            config,
            project_id,
            subtask_id,
            TaskState.DONE,
            note=f"agent dispatch verdict ok: {verdict.reason[:200]}",
        )
        # Build minimal Actuals — no git diff or duration tracking here;
        # this is a single-shot subagent dispatch, not a long task. The
        # reflection event still captures the success_criteria predictions
        # so calibration aggregates can include agent-dispatch outcomes.
        actuals = Actuals()
        reflection_event_path = write_reflection_event(
            portfolio_root=config.portfolio_root,
            event="agent_dispatch_done",
            task_id=subtask_id,
            project_id=project_id,
            predictions=predictions,
            actuals=actuals,
            note=(
                f"agent dispatch verdict ok; parent={parent_link!r}; "
                f"reason: {verdict.reason[:300]}"
            ),
        )
    else:
        # BLOCKED path: iteration_event so the calibration loop sees the
        # failure mode. The subtask sits in `tasks/blocked/<id>.md` for
        # the operator to triage — `clawpm tasks list --state blocked`
        # will surface it.
        change_task_state(
            config,
            project_id,
            subtask_id,
            TaskState.BLOCKED,
            note=(
                f"agent dispatch verdict not-ok: {verdict.reason[:200]} "
                f"(impossible={verdict.impossible})"
            ),
        )
        reflection_event_path = write_iteration_event(
            portfolio_root=config.portfolio_root,
            task_id=subtask_id,
            project_id=project_id,
            verdict_ok=False,
            verdict_reason=(
                f"{verdict.reason} (parent={parent_link!r})"
                if parent_link
                else verdict.reason
            ),
            verdict_impossible=verdict.impossible,
        )

    return {
        "subtask_id": subtask_id,
        "parent_id": parent_link,
        "verdict": {
            "ok": verdict.ok,
            "reason": verdict.reason,
            "impossible": verdict.impossible,
        },
        "transcript_path": str(transcript_path),
        "reflection_event_path": (
            str(reflection_event_path) if reflection_event_path else None
        ),
        "target_dir": str(target_dir),
        "settings_path": str(settings_path),
        "codegraph_initialized": codegraph_initialized,
        "rubric_markdown": rubric_markdown,
        "dispatched_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"
        ),
    }
