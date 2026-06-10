"""Runtime next-action hints (CLAWP-050).

clawpm should actively STEER the acting agent to the right next primitive, not
just return data. Each hint is a terse, heuristic, **code-derived** suggestion
(no LLM call — see Deterministic-First) keyed off the task/result state.

Hints are advisory and non-blocking: they ride in a structured ``hints`` field
of the JSON output (so an agent can read them) and are fully suppressible via
``--no-hints`` or ``CLAWPM_NO_HINTS``. They never change exit codes or block.
"""

from __future__ import annotations

import os

_TRUTHY = {"1", "true", "yes", "on"}


def hints_enabled(ctx=None) -> bool:
    """Hints are on unless ``--no-hints`` (ctx.obj) or ``CLAWPM_NO_HINTS`` is set."""
    if ctx is not None:
        obj = getattr(ctx, "obj", None)
        # Click allows ctx.obj to be any object; guard before .get().
        if isinstance(obj, dict) and obj.get("no_hints"):
            return False
    return os.environ.get("CLAWPM_NO_HINTS", "").strip().lower() not in _TRUTHY


def _success_criteria(task) -> list:
    preds = getattr(task, "predictions", None)
    return list(getattr(preds, "success_criteria", None) or [])


def _complexity_value(task) -> str | None:
    cx = getattr(task, "complexity", None)
    return getattr(cx, "value", cx)


def hints_for_added_task(task) -> list[str]:
    """Steer right after ``tasks add``."""
    hints: list[str] = []
    if _complexity_value(task) in {"l", "xl"}:
        hints.append(
            f"complexity {_complexity_value(task)}: if this splits into independent "
            f"sub-pieces, `clawpm tasks decompose {task.id}` into child subtasks."
        )
    if _success_criteria(task):
        hints.append(
            "verifiable goal set: dispatch a subagent with these success_criteria "
            "and verify the deliverable with the `subagent-judge` skill — or "
            f"`clawpm tasks dispatch {task.id}` for a separate-process worktree run "
            "(see the Two dispatch modes section of the clawpm skill)."
        )
    if getattr(task, "parallel_group", None) is not None:
        hints.append(
            "in a parallel_group: `clawpm next --batch` dispatches the group "
            "together — run `clawpm conflicts` first to check file-scope overlap."
        )
    return hints


def hints_for_next_task(task) -> list[str]:
    """Steer after ``clawpm next`` returns a single task."""
    hints: list[str] = []
    if getattr(task, "parallel_group", None) is not None:
        hints.append(
            "this task is in a parallel_group: `clawpm next --batch` returns the "
            "whole dispatchable group instead of one task."
        )
    if _success_criteria(task):
        hints.append(
            "has success_criteria: dispatchable as a verifiable goal "
            "(`tasks dispatch` for a worktree run, or success_criteria + `subagent-judge`)."
        )
    return hints


def hints_for_shown_task(task) -> list[str]:
    """Steer after ``tasks show`` — same surface as a freshly added task."""
    return hints_for_added_task(task)


def attach_hints(ctx, data: dict, hints: list[str]) -> dict:
    """Fold ``hints`` into a result dict when enabled and non-empty. No-op
    otherwise, so off-path commands and ``--no-hints`` stay clean."""
    if hints and hints_enabled(ctx):
        data["hints"] = hints
    return data
