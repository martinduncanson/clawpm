"""Stdio MCP server exposing clawpm's core (CLAWP-068).

Any MCP host (Cursor, Windsurf, VS Code, Claude Code, Amazon Q, …) can drive
clawpm task / research / mission management through this server — not only the
Claude Code skill. It is launched by ``clawpm mcp`` over stdio.

Design:

- **Direct core calls, zero subprocess.** Every tool wraps the existing core
  functions (``clawpm.tasks`` / ``clawpm.research`` / ``clawpm.mission`` /
  ``clawpm.context``) and the CLAWP-077 service layer
  (``clawpm.services.tasks.transition``) directly. Nothing shells out to the
  ``clawpm`` CLI, so the whole cp1252 / spaced-path / ``UnicodeEncodeError``
  class is avoided and results are structured JSON natively.
- **Project discovery matches the CLI.** Each tool resolves the project via
  ``clawpm.context.resolve_project`` (explicit arg → cwd → ``clawpm use``
  context) and loads the portfolio via ``load_portfolio_config`` (which honours
  ``CLAWPM_PORTFOLIO`` / ``CLAWPM_PROJECT_ROOTS``). The per-project ``.mcp.json``
  registration pattern (documented in the README) launches the server with cwd
  inside the project, so cwd detection Just Works.
- **Tool-count discipline.** Exposed tools are gated by a min-tier tag +
  ``CLAWPM_MCP_TOOLS`` (``core`` | ``standard`` | ``all``, default ``core``) so a
  host's tool list stays lean. Future dispatch / agent tools slot into higher
  tiers without polluting ``core``.
- **No bespoke write-safety layer.** Write tools route through the same
  validated paths the CLI uses (``transition`` validates the surprise taxonomy,
  gates the parent rollup, normalises tags; ``add_task`` / ``edit_task`` /
  ``add_research`` carry their own contracts). Adding a second confirmation
  layer here would duplicate that, so writes rely on the service layer's
  existing guarantees.
"""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Tool-tier gating
# ---------------------------------------------------------------------------

TIERS: dict[str, int] = {"core": 0, "standard": 1, "all": 2}
DEFAULT_TIER = "core"
TOOLS_ENV_VAR = "CLAWPM_MCP_TOOLS"

SERVER_INSTRUCTIONS = (
    "clawpm task / research / mission management for the current project. "
    "Most tools auto-detect the project from the server's working directory; "
    "pass `project` to target a different one. Task ids accept short forms "
    "(e.g. '68' -> 'CLAWP-068'). State changes go through the same calibration-"
    "aware path as the CLI."
)


def resolve_tier(value: str | None) -> int:
    """Map a ``CLAWPM_MCP_TOOLS`` value to a numeric tier ceiling.

    Unknown / empty values fall back to ``core`` — the safe, lean default — so a
    typo can never silently expose a wider surface than intended.
    """
    if not value:
        return TIERS[DEFAULT_TIER]
    return TIERS.get(value.strip().lower(), TIERS[DEFAULT_TIER])


@dataclass(frozen=True)
class ToolSpec:
    name: str
    min_tier: str
    fn: Callable[..., Any]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_config():
    """Load the portfolio config fresh (honours CLAWPM_PORTFOLIO).

    Loaded per-call rather than cached at startup so a long-lived server picks
    up newly-registered projects without a restart. It is a cheap TOML read.
    """
    from clawpm.discovery import load_portfolio_config

    config = load_portfolio_config()
    if config is None:
        raise ValueError("No clawpm portfolio configured (CLAWPM_PORTFOLIO unset and no default found)")
    return config


def _resolve_project(explicit: str | None) -> tuple[str, str]:
    """Resolve the target project id, or raise a friendly usage error."""
    from clawpm.context import resolve_project

    project_id, source = resolve_project(explicit)
    if not project_id:
        raise ValueError(
            "No project specified or detected. Pass project=<id>, or launch the "
            "server with its working directory inside a clawpm project."
        )
    return project_id, source


def _build_predictions(
    *,
    predict_duration: str | None,
    predict_complexity: str | None,
    predict_files_changed: int | None,
    predict_scope: list[str] | None,
    predict_frameworks: list[str] | None,
    predict_pitfalls: str | None,
    hypothesis: str | None,
    success_criteria: list[str] | None,
    predict_approach: str | None,
    unknowns: str | None,
    confidence: int | None,
    reference_tasks: list[str] | None,
    pre_mortem: str | None,
    predict_iterations: int | None,
):
    """Assemble a ``Predictions`` from tool params, or ``None`` if none supplied.

    Mirrors the CLI's ``tasks add`` / ``tasks edit`` prediction assembly so the
    calibration data captured through MCP is identical to the CLI's. Returns a
    validation error string via ValueError for a bad ``--confidence`` / duration.
    """
    from clawpm.models import Predictions, SuccessCriterion, TaskComplexity

    has_predictions = any([
        predict_duration is not None,
        predict_complexity is not None,
        predict_files_changed is not None,
        predict_scope,
        predict_frameworks,
        predict_pitfalls is not None,
        hypothesis is not None,
        success_criteria,
        predict_approach is not None,
        unknowns is not None,
        confidence is not None,
        reference_tasks,
        pre_mortem is not None,
        predict_iterations is not None,
    ])
    if not has_predictions:
        return None

    if confidence is not None and not (1 <= confidence <= 5):
        raise ValueError(f"confidence must be 1-5, got {confidence}")

    from click import BadParameter

    from clawpm.reflect import parse_duration as _parse_duration

    try:
        # parse_duration is a Click-option callback and raises click.BadParameter
        # (not ValueError) on a malformed string outside a CLI context — both
        # tasks_add and tasks_edit only catch ValueError, so re-raise as one
        # (CLAWP-068 review F2).
        parsed_duration = _parse_duration(predict_duration)
    except BadParameter as exc:
        raise ValueError(str(exc)) from exc
    return Predictions(
        duration_min=parsed_duration,
        complexity=TaskComplexity(predict_complexity) if predict_complexity else None,
        files_changed=predict_files_changed,
        files_scope=list(predict_scope or []),
        frameworks=list(predict_frameworks or []),
        pitfalls=predict_pitfalls,
        hypothesis=hypothesis,
        success_criteria=[SuccessCriterion.from_cli(s) for s in (success_criteria or [])],
        approach=predict_approach,
        unknowns=unknowns,
        confidence=confidence,
        reference_tasks=list(reference_tasks or []),
        pre_mortem=pre_mortem,
        predicted_iterations=predict_iterations,
    )


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def tasks_list(
    project: str | None = None,
    state: str | None = None,
    tag: str | None = None,
    limit: int | None = None,
) -> dict:
    """List tasks for a project.

    `state` filters by one of open|progress|blocked|done|rejected (omit for the
    active view: open/progress/blocked — matches the CLI's default, done and
    rejected tasks are excluded unless `state` asks for them explicitly).
    `tag` narrows to tasks carrying that workstream tag. `limit` caps the
    result count after filtering + sorting (default: unlimited). `project`
    auto-detects from the server's cwd if omitted.
    """
    from clawpm.models import TaskState
    from clawpm.tasks import list_tasks

    config = _load_config()
    project_id, source = _resolve_project(project)

    if state:
        try:
            state_filter = TaskState(state)
        except ValueError:
            return {"ok": False, "error": "bad_state", "message": f"invalid state '{state}'"}
        tasks = list_tasks(config, project_id, state_filter=state_filter)
    else:
        # Mirror the CLI's default (cli/tasks.py _collect_project_tasks): the
        # "active view" is open+progress+blocked, NOT an unfiltered scan — an
        # unfiltered list_tasks(state_filter=None) also walks the done/
        # directory (CLAWP-068 review F3).
        tasks = []
        for s in (TaskState.OPEN, TaskState.PROGRESS, TaskState.BLOCKED):
            tasks.extend(list_tasks(config, project_id, state_filter=s))
        tasks.sort(key=lambda t: (t.priority, t.id))

    if tag:
        tag_l = tag.strip().lower()
        tasks = [t for t in tasks if tag_l in [x.lower() for x in t.tags]]

    total = len(tasks)
    if limit is not None:
        tasks = tasks[:limit]

    return {
        "ok": True,
        "project": project_id,
        "source": source,
        "count": len(tasks),
        "total": total,
        "tasks": [t.to_dict() for t in tasks],
    }


def tasks_get(task_id: str, project: str | None = None) -> dict:
    """Get one task's full detail by id (short forms like '68' are expanded)."""
    from clawpm.context import expand_task_id
    from clawpm.tasks import get_task

    config = _load_config()
    project_id, _ = _resolve_project(project)
    full_id = expand_task_id(task_id, project_id)
    task = get_task(config, project_id, full_id)
    if not task:
        return {"ok": False, "error": "not_found", "task_id": full_id,
                "message": f"No task '{full_id}' in project '{project_id}'"}
    return {"ok": True, "project": project_id, "task": task.to_dict()}


def context(project: str | None = None, log_limit: int = 5) -> dict:
    """Full agent-resume context: project, spec, in-progress/next/blocked tasks,
    open counts, recent work-log, git status, open issues. Everything needed to
    resume work on a project in one call."""
    config = _load_config()
    project_id, source = _resolve_project(project)
    from clawpm.context import build_agent_context

    ctx = build_agent_context(config, project_id, source=source, log_limit=log_limit)
    if ctx is None:
        return {"ok": False, "error": "not_found", "message": f"Project '{project_id}' not found"}
    ctx["ok"] = True
    return ctx


def next_task(project: str | None = None) -> dict:
    """Get the next task to work on (highest-priority open task with satisfied
    dependencies), or null if none is ready."""
    from clawpm.tasks import get_next_task

    config = _load_config()
    project_id, _ = _resolve_project(project)
    task = get_next_task(config, project_id)
    return {"ok": True, "project": project_id, "task": task.to_dict() if task else None}


def research_list(
    project: str | None = None,
    status: str | None = None,
    tag: str | None = None,
) -> dict:
    """List research entries for a project. `status` filters by
    open|in-progress|complete|stale; `tag` narrows to entries with that tag."""
    from clawpm.models import ResearchStatus
    from clawpm.research import list_research

    config = _load_config()
    project_id, _ = _resolve_project(project)

    status_filter = None
    if status:
        try:
            status_filter = ResearchStatus(status)
        except ValueError:
            return {"ok": False, "error": "bad_status", "message": f"invalid status '{status}'"}

    tags_filter = [tag] if tag else None
    items = list_research(config, project_id, status_filter=status_filter, tags_filter=tags_filter)
    return {
        "ok": True,
        "project": project_id,
        "count": len(items),
        "research": [r.to_dict() for r in items],
    }


def mission_list(project: str | None = None, status: str | None = None) -> dict:
    """List missions for a project (optionally filtered by a status string)."""
    from clawpm.mission import list_missions

    config = _load_config()
    project_id, _ = _resolve_project(project)
    missions = list_missions(config, project_id, status_filter=status)
    return {
        "ok": True,
        "project": project_id,
        "count": len(missions),
        "missions": [m.to_dict() for m in missions],
    }


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

def tasks_add(
    title: str,
    project: str | None = None,
    priority: int = 5,
    complexity: str = "m",
    depends: list[str] | None = None,
    scope: list[str] | None = None,
    tags: list[str] | None = None,
    description: str = "",
    parallel_group: int | None = None,
    agent_profile: str | None = None,
    out_of_scope: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    delegability: str | None = None,
    success_criteria: list[str] | None = None,
    predict_duration: str | None = None,
    predict_complexity: str | None = None,
    predict_files_changed: int | None = None,
    confidence: int | None = None,
    pre_mortem: str | None = None,
    predict_approach: str | None = None,
    reference_tasks: list[str] | None = None,
    hypothesis: str | None = None,
) -> dict:
    """Create a task. Prefer verifiable goals: pass `success_criteria` (each a
    plain string or a JSON object `{"criterion","gradeable_signal","comparator"}`)
    plus predictions (`predict_duration` like '4h', `confidence` 1-5,
    `pre_mortem`, `predict_approach`, `reference_tasks`) so the task is gradeable
    and feeds calibration. `delegability` is agent|human|either. Returns the
    created task."""
    from clawpm.models import TaskComplexity
    from clawpm.tasks import add_task

    config = _load_config()
    project_id, _ = _resolve_project(project)

    try:
        cmplx = TaskComplexity(complexity) if complexity else None
    except ValueError:
        return {"ok": False, "error": "bad_complexity", "message": f"invalid complexity '{complexity}' (s|m|l|xl)"}

    if delegability is not None and delegability not in ("agent", "human", "either"):
        return {"ok": False, "error": "bad_delegability",
                "message": f"invalid delegability '{delegability}' (agent|human|either)"}

    try:
        predictions = _build_predictions(
            predict_duration=predict_duration,
            predict_complexity=predict_complexity,
            predict_files_changed=predict_files_changed,
            predict_scope=None,
            predict_frameworks=None,
            predict_pitfalls=None,
            hypothesis=hypothesis,
            success_criteria=success_criteria,
            predict_approach=predict_approach,
            unknowns=None,
            confidence=confidence,
            reference_tasks=reference_tasks,
            pre_mortem=pre_mortem,
            predict_iterations=None,
        )
    except ValueError as exc:
        return {"ok": False, "error": "bad_predictions", "message": str(exc)}

    task = add_task(
        config,
        project_id,
        title=title,
        priority=priority,
        complexity=cmplx,
        depends=list(depends) if depends else None,
        scope=list(scope) if scope else None,
        tags=list(tags) if tags else None,
        description=description,
        predictions=predictions,
        parallel_group=parallel_group,
        agent_profile=agent_profile,
        out_of_scope=list(out_of_scope) if out_of_scope else None,
        stop_conditions=list(stop_conditions) if stop_conditions else None,
        delegability=delegability,
    )
    if not task:
        return {"ok": False, "error": "add_failed",
                "message": f"Could not create task in project '{project_id}' (project has no tasks dir?)"}
    return {"ok": True, "project": project_id, "task": task.to_dict()}


def tasks_state(
    task_id: str,
    new_state: str,
    project: str | None = None,
    note: str | None = None,
    force: bool = False,
    reflect_note: str | None = None,
    surprise_tags: list[str] | None = None,
    rationale: str | None = None,
    supersedes: str | None = None,
) -> dict:
    """Transition a task to a new state (open|progress|done|blocked|rejected).

    Routes through the same calibration-aware service path as the CLI: it gates
    parent rollup (pass `force=True` to complete over incomplete subtasks),
    appends the work-log, runs the dependency cascade, and writes the reflection
    event on done/blocked. `surprise_tags` (validated against the fixed
    taxonomy) and `reflect_note` enrich that calibration event. `rationale` /
    `supersedes` document a `rejected` won't-do decision. Returns the updated
    task plus any cascade/teardown side-effects."""
    from clawpm.context import expand_task_id
    from clawpm.models import TaskState
    from clawpm.services.tasks import transition

    config = _load_config()
    project_id, _ = _resolve_project(project)
    full_id = expand_task_id(task_id, project_id)

    try:
        TaskState(new_state)
    except ValueError:
        return {"ok": False, "task_id": full_id, "error": "bad_state",
                "message": f"invalid state '{new_state}' (open|progress|done|blocked|rejected)"}

    try:
        return transition(
            config,
            project_id=project_id,
            task_id=full_id,
            new_state=new_state,
            note=note,
            force=force,
            reflect_note=reflect_note,
            surprise_tags=tuple(surprise_tags or ()),
            rationale=rationale,
            supersedes=supersedes,
        )
    except ValueError as exc:
        # transition validates the surprise taxonomy up front and raises
        # ValueError for an out-of-vocab tag (part of the mutator contract).
        return {"ok": False, "task_id": full_id, "error": "invalid_argument", "message": str(exc)}


def tasks_edit(
    task_id: str,
    project: str | None = None,
    title: str | None = None,
    priority: int | None = None,
    complexity: str | None = None,
    body: str | None = None,
    scope: list[str] | None = None,
    tags: list[str] | None = None,
    clear_tags: bool = False,
    parallel_group: int | None = None,
    clear_parallel_group: bool = False,
    out_of_scope: list[str] | None = None,
    stop_conditions: list[str] | None = None,
    delegability: str | None = None,
    success_criteria: list[str] | None = None,
    predict_duration: str | None = None,
    predict_complexity: str | None = None,
    predict_files_changed: int | None = None,
    predict_scope: list[str] | None = None,
    predict_frameworks: list[str] | None = None,
    predict_pitfalls: str | None = None,
    unknowns: str | None = None,
    predict_iterations: int | None = None,
    confidence: int | None = None,
    pre_mortem: str | None = None,
    predict_approach: str | None = None,
    reference_tasks: list[str] | None = None,
    hypothesis: str | None = None,
) -> dict:
    """Edit an existing task's metadata (title, priority, complexity, body,
    scope, tags, dispatch-contract fields, predictions). Only the fields you
    pass are changed. `edit_task` REPLACES the whole predictions block whenever
    ANY predict_*/success_criteria/confidence/pre_mortem argument is supplied —
    so to change one prediction field without erasing the others (pitfalls,
    unknowns, scope, frameworks, iterations), pass the existing values for the
    rest too (fetch them via `tasks_get` first). Returns the updated task."""
    from clawpm.context import expand_task_id
    from clawpm.models import TaskComplexity
    from clawpm.tasks import edit_task

    config = _load_config()
    project_id, _ = _resolve_project(project)
    full_id = expand_task_id(task_id, project_id)

    try:
        cmplx = TaskComplexity(complexity) if complexity else None
    except ValueError:
        return {"ok": False, "error": "bad_complexity", "message": f"invalid complexity '{complexity}' (s|m|l|xl)"}

    if delegability is not None and delegability not in ("agent", "human", "either"):
        return {"ok": False, "error": "bad_delegability",
                "message": f"invalid delegability '{delegability}' (agent|human|either)"}

    try:
        predictions = _build_predictions(
            predict_duration=predict_duration,
            predict_complexity=predict_complexity,
            predict_files_changed=predict_files_changed,
            predict_scope=predict_scope,
            predict_frameworks=predict_frameworks,
            predict_pitfalls=predict_pitfalls,
            hypothesis=hypothesis,
            success_criteria=success_criteria,
            predict_approach=predict_approach,
            unknowns=unknowns,
            confidence=confidence,
            reference_tasks=reference_tasks,
            pre_mortem=pre_mortem,
            predict_iterations=predict_iterations,
        )
    except ValueError as exc:
        return {"ok": False, "error": "bad_predictions", "message": str(exc)}

    task = edit_task(
        config,
        project_id,
        full_id,
        title=title,
        priority=priority,
        complexity=cmplx,
        scope=list(scope) if scope else None,
        tags=list(tags) if tags else None,
        clear_tags=clear_tags,
        body=body,
        predictions=predictions,
        parallel_group=parallel_group,
        clear_parallel_group=clear_parallel_group,
        out_of_scope=list(out_of_scope) if out_of_scope else None,
        stop_conditions=list(stop_conditions) if stop_conditions else None,
        delegability=delegability,
    )
    if not task:
        return {"ok": False, "error": "not_found", "task_id": full_id,
                "message": f"No task '{full_id}' in project '{project_id}'"}
    return {"ok": True, "project": project_id, "task": task.to_dict()}


def research_add(
    title: str,
    project: str | None = None,
    research_type: str = "investigation",
    tags: list[str] | None = None,
    question: str = "",
    summary: str = "",
    findings: list[str] | None = None,
    conclusion: str = "",
    research_id: str | None = None,
) -> dict:
    """Add a research entry. `research_type` is investigation|spike|decision|
    reference. Supplying `summary`/`findings`/`conclusion` records a single-shot
    verdict; omitting them creates a progressive (to-fill-in) template. Returns
    the created entry."""
    from clawpm.models import ResearchType
    from clawpm.research import add_research

    config = _load_config()
    project_id, _ = _resolve_project(project)

    try:
        rtype = ResearchType(research_type)
    except ValueError:
        return {"ok": False, "error": "bad_type",
                "message": f"invalid research_type '{research_type}' (investigation|spike|decision|reference)"}

    item = add_research(
        config,
        project_id,
        title=title,
        research_type=rtype,
        research_id=research_id,
        tags=list(tags) if tags else None,
        question=question,
        summary=summary,
        findings=list(findings) if findings else None,
        conclusion=conclusion,
    )
    if not item:
        return {"ok": False, "error": "add_failed",
                "message": f"Could not create research in project '{project_id}' (project not found?)"}
    return {"ok": True, "project": project_id, "research": item.to_dict()}


# ---------------------------------------------------------------------------
# Registry + server construction
# ---------------------------------------------------------------------------

# All 10 tools are `core` for v1 (the operator-specified initial set). The tier
# tag is the seam for later expansion: dispatch / agent-spawning tools will be
# added at `standard` / `all` so they never bloat a default host's tool list.
TOOL_SPECS: list[ToolSpec] = [
    ToolSpec("tasks_list", "core", tasks_list),
    ToolSpec("tasks_get", "core", tasks_get),
    ToolSpec("context", "core", context),
    ToolSpec("next", "core", next_task),
    ToolSpec("research_list", "core", research_list),
    ToolSpec("mission_list", "core", mission_list),
    ToolSpec("tasks_add", "core", tasks_add),
    ToolSpec("tasks_state", "core", tasks_state),
    ToolSpec("tasks_edit", "core", tasks_edit),
    ToolSpec("research_add", "core", research_add),
]


def _catch_unhandled(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap a tool so an exception it didn't already catch still returns the
    documented ``{"ok": false, ...}`` JSON shape instead of an MCP ``isError``
    text blob.

    Every tool already catches its OWN known validation errors (bad_state,
    bad_complexity, ...) and returns a dict. But several paths can still raise
    past that — ``_resolve_project`` when no project is detected, ``edit_task``
    on a corrupted task file, etc. Left unwrapped, those exceptions escape to
    the MCP SDK's generic handler, which returns a plain-text ``isError``
    result — a structurally different, unparseable shape next to every other
    error this server returns (CLAWP-068 review F11). ``functools.wraps``
    preserves ``__wrapped__``, which ``inspect.signature`` follows by default —
    FastMCP's schema generation still sees the original signature/docstring.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except ValueError as exc:
            # ValueError is this module's convention for "bad input/state"
            # (see tasks_state's own except ValueError below) — give it the
            # same error code a caller would get from a caught one.
            return {"ok": False, "error": "invalid_argument", "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - deliberate tool-boundary catch-all
            return {"ok": False, "error": "internal_error", "message": str(exc)}

    return wrapper


def specs_for_tier(tools_tier: str | None) -> list[ToolSpec]:
    """The subset of tool specs exposed at the requested tier ceiling."""
    ceiling = resolve_tier(tools_tier)
    return [s for s in TOOL_SPECS if TIERS[s.min_tier] <= ceiling]


def build_server(tools_tier: str | None = None, *, name: str = "clawpm"):
    """Build the FastMCP server with the tier-appropriate tool set.

    `tools_tier` defaults to the ``CLAWPM_MCP_TOOLS`` env var, then ``core``.
    The heavy ``mcp`` import is deferred to here so importing this module (e.g.
    for the tool functions, or in tests) doesn't require the SDK.
    """
    from mcp.server.fastmcp import FastMCP

    if tools_tier is None:
        tools_tier = os.environ.get(TOOLS_ENV_VAR)

    server = FastMCP(name, instructions=SERVER_INSTRUCTIONS)
    for spec in specs_for_tier(tools_tier):
        server.add_tool(_catch_unhandled(spec.fn), name=spec.name)
    return server


def run_stdio(tools_tier: str | None = None) -> None:
    """Launch the stdio MCP server (blocks until the host disconnects)."""
    build_server(tools_tier).run()
