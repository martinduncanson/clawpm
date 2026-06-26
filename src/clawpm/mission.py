"""Mission Control — macro binary-outcome layer above tasks (CLAWP-022).

A mission is a multi-week binary outcome decomposed into 4-10 **mini-goals**
(regular clawpm tasks tagged with ``parent_mission`` + ``actor``). Adapts
the Claude ``/goal`` long-form Mission Control pattern from the
feature-mining research, but without the dashboard POST — clawpm stays
filesystem-first.

Why this layer exists:

  - Tasks are micro-experiments (single-session, ~hours). Missions are
    macro-experiments (multi-week, 4-10 mini-goals each).
  - Mini-goals split across **actors**: ``agent`` (an agent runs this in
    a session, dispatchable via ``clawpm tasks dispatch``) and ``human``
    (the operator does it — on-camera recording, physical signing,
    waiting on a third party). Both progress count toward the mission.
  - ``binary_outcome`` is YES/NO at the deadline. The mission either
    shipped or it didn't.

Storage:

  - Missions live at ``<project>/.project/missions/<MISSION-ID>.md`` with
    YAML frontmatter (title, binary_outcome, deadline_days, status,
    created, mini_goals: list of {id, actor}).
  - Mini-goals are regular tasks at ``<project>/.project/tasks/``
    carrying ``parent_mission: <MISSION-ID>`` and ``actor: agent|human``
    in their frontmatter. They're discoverable both via
    ``clawpm tasks list`` (with the rest of the backlog) AND via
    ``clawpm mission tasks <id>``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .discovery import get_project_dir
from .models import PortfolioConfig, Task, TaskState
from .tasks import list_tasks


# Codex round-5 P1: the prefix is project_id.upper()[:5], so project IDs
# like "proj1" → "PROJ1" or "my-app" → "MY-AP" yield prefixes that
# contain digits or hyphens. Accept one-or-more [A-Z0-9] groups joined
# by single hyphens, then -MISSION- + 3 digits. This still forbids
# leading/trailing/double hyphens, `..`, path separators, etc. —
# everything that would let an attacker escape the missions/ directory.
MISSION_ID_PATTERN = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)*-MISSION-\d{3}$")


def _assert_safe_mission_id(value: str) -> None:
    """Raise ValueError unless value matches the strict mission-ID shape.

    Codex round-3 P1 fix: mission_id flows into file-path joins
    (``missions_dir / f"{mission_id}.md"``). Anything other than the
    canonical ``<PREFIX>-MISSION-NNN`` shape risks path traversal —
    ``../../../etc/passwd``, absolute paths on Windows
    (``C:\\foo``), backslashes, etc.

    The pattern allows uppercase letters + ``-MISSION-`` + exactly
    three digits. That's the exact shape ``add_mission`` generates
    when no ID is supplied; rejecting anything else here means
    explicit ``--id`` overrides must conform to the same shape.
    """
    if not isinstance(value, str) or not MISSION_ID_PATTERN.match(value):
        raise ValueError(
            f"Refusing to use unsafe mission_id {value!r} — must match "
            f"{MISSION_ID_PATTERN.pattern} (e.g. 'CLAWP-MISSION-007'). "
            f"This guards against path traversal in the missions/ "
            f"directory."
        )


@dataclass
class MissionMiniGoal:
    """Reference to a task that is a mini-goal of this mission."""

    id: str
    actor: str  # "agent" | "human"

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "actor": self.actor}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MissionMiniGoal":
        actor = data.get("actor", "agent")
        if actor not in ("agent", "human"):
            actor = "agent"
        return cls(id=data["id"], actor=actor)


@dataclass
class Mission:
    """A mission: binary outcome at deadline_days, decomposed into mini-goals."""

    id: str
    title: str
    binary_outcome: str
    deadline_days: int
    status: str = "active"  # active | complete | failed | cancelled
    created: str | None = None
    mini_goals: list[MissionMiniGoal] = field(default_factory=list)
    content: str = ""
    file_path: Path | None = None

    @property
    def deadline_date(self) -> date | None:
        """Computed: created date + deadline_days."""
        if not self.created:
            return None
        try:
            created = date.fromisoformat(self.created)
        except (ValueError, TypeError):
            return None
        return created + timedelta(days=self.deadline_days)

    @classmethod
    def from_file(cls, path: Path) -> "Mission":
        text = path.read_text(encoding="utf-8")
        frontmatter: dict[str, Any] = {}
        content = text
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                try:
                    frontmatter = yaml.safe_load(parts[1]) or {}
                    content = parts[2].strip()
                except yaml.YAMLError:
                    pass
        title = frontmatter.get("title", path.stem)
        for line in content.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break
        mini_goals = [
            MissionMiniGoal.from_dict(g)
            for g in (frontmatter.get("mini_goals") or [])
        ]
        return cls(
            id=frontmatter.get("id", path.stem),
            title=title,
            binary_outcome=frontmatter.get("binary_outcome", ""),
            deadline_days=int(frontmatter.get("deadline_days", 28)),
            status=frontmatter.get("status", "active"),
            created=frontmatter.get("created"),
            mini_goals=mini_goals,
            content=content,
            file_path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        dd = self.deadline_date
        return {
            "id": self.id,
            "title": self.title,
            "binary_outcome": self.binary_outcome,
            "deadline_days": self.deadline_days,
            "status": self.status,
            "created": self.created,
            "deadline_date": dd.isoformat() if dd else None,
            "mini_goals": [g.to_dict() for g in self.mini_goals],
            "file_path": str(self.file_path) if self.file_path else None,
        }


def _missions_dir(config: PortfolioConfig, project_id: str) -> Path | None:
    project_dir = get_project_dir(config, project_id)
    if not project_dir:
        return None
    md = project_dir / "missions"
    return md


def _ensure_missions_dir(config: PortfolioConfig, project_id: str) -> Path:
    project_dir = get_project_dir(config, project_id)
    if not project_dir:
        raise ValueError(f"Project not found: {project_id}")
    md = project_dir / "missions"
    md.mkdir(parents=True, exist_ok=True)
    return md


def list_missions(
    config: PortfolioConfig,
    project_id: str,
    status_filter: str | None = None,
) -> list[Mission]:
    md = _missions_dir(config, project_id)
    if md is None or not md.exists():
        return []
    missions: list[Mission] = []
    for f in sorted(md.glob("*.md")):
        try:
            mission = Mission.from_file(f)
            if status_filter is None or mission.status == status_filter:
                missions.append(mission)
        except Exception:
            # Defer to doctor for surfacing — don't crash list on one bad file
            continue
    return missions


def get_mission(
    config: PortfolioConfig, project_id: str, mission_id: str
) -> Mission | None:
    """Look up a mission by ID; returns None when not found OR when the ID
    is malformed (defense-in-depth path-traversal guard).

    Codex round-4 P1: mutating callers (``set_mission_status``,
    ``add_mission_mini_goal``) flow through ``get_mission`` and then
    ``_rewrite_mission`` writes back to ``mission.file_path``. A
    traversal-shaped ID like ``../../foo`` could otherwise overwrite an
    existing markdown file outside the missions directory whenever such
    a file is parseable as Mission frontmatter. Validating here is the
    single point of truth — every mutating flow benefits without each
    caller needing to remember.
    """
    md = _missions_dir(config, project_id)
    if md is None:
        return None
    try:
        _assert_safe_mission_id(mission_id)
    except ValueError:
        # Treat malformed input as "not found" — callers already handle
        # None. This is the fail-safe shape: never compose an
        # attacker-controlled path.
        return None
    path = md / f"{mission_id}.md"
    if not path.exists():
        return None
    try:
        return Mission.from_file(path)
    except Exception:
        return None


def add_mission(
    config: PortfolioConfig,
    project_id: str,
    title: str,
    binary_outcome: str,
    deadline_days: int = 28,
    description: str = "",
    mission_id: str | None = None,
    force: bool = False,
) -> Mission:
    """Create a new mission file. Does NOT create mini-goal tasks — caller
    is responsible for invoking ``add_mission_mini_goal`` per goal so the
    operator can populate actor + task content explicitly.

    Codex round-2 P2 fix: if an explicit ``mission_id`` is reused, refuse
    to overwrite the existing mission unless ``force=True``. Silent
    overwrite via ``--id`` would destroy mini_goals + status state with
    no recovery path.
    """
    if deadline_days < 7 or deadline_days > 42:
        raise ValueError(
            f"deadline_days must be in [7, 42], got {deadline_days}. "
            "Shorter = use a single task; longer = strategic planning, "
            "not a mission."
        )
    md = _ensure_missions_dir(config, project_id)

    if not mission_id:
        # Codex round-6 P1: project_id can contain characters that
        # `_assert_safe_mission_id` (rightly) rejects — dots, underscores,
        # unicode, etc. (`my.app` → `MY.AP`; `foo_bar` → `FOO_B`; etc.).
        # Without sanitisation, add_mission succeeds (writes the file
        # with the unsanitised ID) but every subsequent get_mission /
        # set_mission_status / add_mission_mini_goal fails with
        # not-found because the validator rejects the same ID.
        # Sanitise: replace any non-[A-Z0-9] with `-`, collapse runs of
        # dashes, strip leading/trailing dashes. Fall back to `PROJ`
        # when the result is empty.
        raw_prefix = project_id.upper()[:5]
        sanitised = re.sub(r"[^A-Z0-9]+", "-", raw_prefix).strip("-")
        prefix = sanitised or "PROJ"
        existing = []
        for f in md.glob("*.md"):
            m = re.match(rf"^{re.escape(prefix)}-MISSION-(\d+)$", f.stem)
            if m:
                existing.append(int(m.group(1)))
        next_num = max(existing, default=-1) + 1
        mission_id = f"{prefix}-MISSION-{next_num:03d}"
    else:
        # Codex round-3 P1 fix: caller-supplied ID flows into a file
        # path. Without validation, `--id "../../etc/passwd"` would
        # escape the missions directory and turn add_mission into a
        # path-traversal write primitive (combined with --force it
        # could overwrite arbitrary .md files). Enforce the strict ID
        # shape upstream of any path composition.
        _assert_safe_mission_id(mission_id)
        existing_path = md / f"{mission_id}.md"
        if existing_path.exists() and not force:
            raise ValueError(
                f"Mission {mission_id!r} already exists at "
                f"{existing_path.as_posix()}. Pass force=True to "
                f"overwrite (destructive), or pick a different ID."
            )

    frontmatter = {
        "id": mission_id,
        "title": title,
        "binary_outcome": binary_outcome,
        "deadline_days": deadline_days,
        "status": "active",
        "created": date.today().isoformat(),
        "mini_goals": [],
    }
    content = f"""---
{yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True, sort_keys=False).strip()}
---
# {title}

**Binary outcome:** {binary_outcome}

**Deadline:** {deadline_days} days from creation

{description}

## Mini-goals

_(Add via `clawpm mission add-goal {mission_id} --task <task-id>`. Tasks become mini-goals by carrying `parent_mission: {mission_id}` in their frontmatter.)_
"""
    path = md / f"{mission_id}.md"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return Mission.from_file(path)


def add_mission_mini_goal(
    config: PortfolioConfig,
    project_id: str,
    mission_id: str,
    task_id: str,
    actor: str = "agent",
) -> Mission:
    """Link an existing task to a mission as a mini-goal.

    Sets ``parent_mission`` + ``actor`` on the task's frontmatter AND
    appends to the mission's ``mini_goals`` list. Enforces the 10-mini-goal
    soft cap (Claude /goal Mission Control rule).
    """
    if actor not in ("agent", "human"):
        raise ValueError(f"actor must be 'agent' or 'human', got {actor!r}")

    from .tasks import get_task, get_tasks_dir
    from .concurrency import file_lock, retry_transient
    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        raise ValueError(f"Project {project_id} has no tasks dir")

    # CLAWP-066 / Codex review: run the ENTIRE read→validate→write→rewrite under
    # the per-project lock. A task-only lock left two races open: (a) two links
    # to the SAME mission read a stale mini_goals list and the later
    # _rewrite_mission drops the earlier mini-goal; (b) two links of the SAME
    # task to DIFFERENT missions both pass the cross-mission check before either
    # write. Serialising mission read + cap + ownership check + task write +
    # mission rewrite together closes both. get_mission/get_task/_rewrite_mission
    # take no lock, so this single critical section is safe.
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        mission = get_mission(config, project_id, mission_id)
        if mission is None:
            raise ValueError(f"Mission not found: {mission_id}")

        task = get_task(config, project_id, task_id)
        if task is None:
            raise ValueError(f"Task not found: {task_id}")

        # Idempotency check MUST precede the cap: re-running add-goal for a task
        # already linked to THIS mission is a no-op, not a cap violation.
        if any(g.id == task_id for g in mission.mini_goals):
            return mission

        if len(mission.mini_goals) >= 10:
            raise ValueError(
                f"Mission {mission_id} already has 10 mini-goals (hard cap). "
                "Split into a follow-up mission instead."
            )

        # Refuse cross-mission relink — checked here against the freshly-read
        # task UNDER the lock, so two concurrent links of the same task to
        # different missions can't both pass (Codex review).
        if task.parent_mission and task.parent_mission != mission_id:
            raise ValueError(
                f"Task {task_id} is already a mini-goal of mission "
                f"{task.parent_mission!r}. Unlink it from there first "
                f"(edit the task frontmatter or remove the mini_goals entry "
                f"on the prior mission file) before re-linking to "
                f"{mission_id!r}."
            )

        # Update the task's frontmatter (re-resolved under the lock so the write
        # can't land on a path a concurrent change_task_state vacated).
        if task.file_path is None or not task.file_path.exists():
            raise ValueError(
                f"Task {task_id} vanished — it may have been moved by a "
                "concurrent session; retry the mini-goal link."
            )
        text = task.file_path.read_text(encoding="utf-8")
        if not text.startswith("---"):
            raise ValueError(f"Task {task_id} has no frontmatter")
        parts = text.split("---", 2)
        if len(parts) < 3:
            raise ValueError(f"Task {task_id} frontmatter malformed")
        try:
            fm = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Task {task_id} frontmatter unparseable: {exc}") from exc
        fm["parent_mission"] = mission_id
        fm["actor"] = actor
        new_text = (
            "---\n"
            + yaml.dump(fm, default_flow_style=False, allow_unicode=True).rstrip()
            + "\n---"
            + parts[2]
        )
        tmp = task.file_path.with_suffix(".tmp")
        try:
            tmp.write_text(new_text, encoding="utf-8")
            retry_transient(tmp.replace, task.file_path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise

        # Append to the mission + rewrite INSIDE the lock so the mini_goals list
        # we persist reflects every concurrent add (no lost update — Codex).
        mission.mini_goals.append(MissionMiniGoal(id=task_id, actor=actor))
        _rewrite_mission(mission)
        return mission


def _rewrite_mission(mission: Mission) -> None:
    """Persist a mission's mini_goals + status back to its file."""
    if mission.file_path is None:
        raise ValueError("Mission has no file path")
    text = mission.file_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError("Mission file has no frontmatter")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Mission frontmatter malformed")
    fm = yaml.safe_load(parts[1]) or {}
    fm["status"] = mission.status
    fm["mini_goals"] = [g.to_dict() for g in mission.mini_goals]
    new_text = (
        "---\n"
        + yaml.dump(fm, default_flow_style=False, allow_unicode=True, sort_keys=False).rstrip()
        + "\n---"
        + parts[2]
    )
    from .concurrency import retry_transient
    tmp = mission.file_path.with_suffix(".tmp")
    try:
        tmp.write_text(new_text, encoding="utf-8")
        # Retry transient sharing/access faults + clean up the tmp on failure,
        # matching the task-side write hardening (Grok review).
        retry_transient(tmp.replace, mission.file_path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def mission_status(
    config: PortfolioConfig, project_id: str, mission_id: str
) -> dict[str, Any]:
    """Compute progress + outcome state for a mission.

    Walks the mini-goals, counts done/in-progress/blocked/open per actor,
    computes deadline drift. Returns a flat JSON-friendly dict.
    """
    mission = get_mission(config, project_id, mission_id)
    if mission is None:
        return {"error": f"Mission not found: {mission_id}", "id": mission_id}

    all_tasks = {t.id: t for t in list_tasks(config, project_id)}

    agent_counts = {"done": 0, "progress": 0, "blocked": 0, "open": 0, "missing": 0}
    human_counts = {"done": 0, "progress": 0, "blocked": 0, "open": 0, "missing": 0}
    missing_refs: list[str] = []
    by_state: dict[str, list[str]] = {"done": [], "progress": [], "blocked": [], "open": []}

    for g in mission.mini_goals:
        task = all_tasks.get(g.id)
        bucket = agent_counts if g.actor == "agent" else human_counts
        if task is None:
            bucket["missing"] += 1
            missing_refs.append(g.id)
            continue
        s = task.state.value
        bucket[s] += 1
        by_state[s].append(g.id)

    total = len(mission.mini_goals)
    done = agent_counts["done"] + human_counts["done"]
    pct_complete = round((done / total) * 100, 1) if total > 0 else 0.0

    # Deadline math
    days_remaining = None
    overdue = False
    dd = mission.deadline_date
    if dd is not None:
        today = date.today()
        delta = (dd - today).days
        days_remaining = delta
        overdue = delta < 0 and done < total

    outcome_status: str
    if mission.status in ("complete", "failed", "cancelled"):
        outcome_status = mission.status
    elif total == 0:
        outcome_status = "empty"  # no mini-goals = ill-defined mission
    elif done == total:
        outcome_status = "ready_to_close"  # all mini-goals done; operator confirms binary outcome
    elif overdue:
        outcome_status = "overdue"
    else:
        outcome_status = "in_progress"

    return {
        "id": mission.id,
        "title": mission.title,
        "binary_outcome": mission.binary_outcome,
        "outcome_status": outcome_status,
        "status": mission.status,
        "complete_count": done,
        "total_count": total,
        "pct_complete": pct_complete,
        "deadline_date": dd.isoformat() if dd else None,
        "days_remaining": days_remaining,
        "overdue": overdue,
        "agent_counts": agent_counts,
        "human_counts": human_counts,
        "by_state": by_state,
        "missing_refs": missing_refs,
    }


def mission_tasks(
    config: PortfolioConfig,
    project_id: str,
    mission_id: str,
    actor_filter: str | None = None,
) -> list[Task]:
    """List tasks that are mini-goals of this mission.

    ``actor_filter`` restricts to agent or human mini-goals; None = both.
    """
    mission = get_mission(config, project_id, mission_id)
    if mission is None:
        return []
    if actor_filter not in (None, "agent", "human"):
        raise ValueError(f"actor_filter must be agent|human|None, got {actor_filter!r}")
    all_tasks = {t.id: t for t in list_tasks(config, project_id)}
    out: list[Task] = []
    for g in mission.mini_goals:
        if actor_filter and g.actor != actor_filter:
            continue
        task = all_tasks.get(g.id)
        if task is not None:
            out.append(task)
    return out


def set_mission_status(
    config: PortfolioConfig,
    project_id: str,
    mission_id: str,
    new_status: str,
) -> Mission:
    """Transition a mission to complete / failed / cancelled / active."""
    if new_status not in ("active", "complete", "failed", "cancelled"):
        raise ValueError(
            f"new_status must be active|complete|failed|cancelled, got {new_status!r}"
        )
    mission = get_mission(config, project_id, mission_id)
    if mission is None:
        raise ValueError(f"Mission not found: {mission_id}")
    mission.status = new_status
    _rewrite_mission(mission)
    return mission
