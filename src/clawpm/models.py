"""Data models for ClawPM."""

from __future__ import annotations

import fnmatch
import sys
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .frontmatter import FrontmatterError, split_frontmatter


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class TaskState(str, Enum):
    OPEN = "open"
    PROGRESS = "progress"
    DONE = "done"
    BLOCKED = "blocked"
    # CLAWP-053 — won't-do ledger: considered and rejected with a rationale.
    # Terminal state (like DONE). Excluded from default listings.
    REJECTED = "rejected"


class TaskComplexity(str, Enum):
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"


class WorkLogAction(str, Enum):
    START = "start"
    PROGRESS = "progress"
    DONE = "done"
    BLOCKED = "blocked"
    UNBLOCK = "unblock"
    PAUSE = "pause"
    RESEARCH = "research"
    NOTE = "note"
    COMMIT = "commit"
    VOID = "void"
    CASCADE_UNBLOCK = "cascade_unblock"


# Fixed vocabulary for surprise taxonomy — one source of truth.
# Used by CLI validation and test assertions.
SURPRISE_TAXONOMY: frozenset[str] = frozenset({
    "unknown_unknown",
    "scope_drift",
    "dependency",
    "tooling_friction",
    "complexity_misread",
    "assumption_broke",
    "external_blocker",
})


class ResearchType(str, Enum):
    INVESTIGATION = "investigation"
    SPIKE = "spike"
    DECISION = "decision"
    REFERENCE = "reference"


class ResearchStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in-progress"
    COMPLETE = "complete"
    STALE = "stale"


@dataclass
class PortfolioConfig:
    """Portfolio configuration from portfolio.toml."""

    portfolio_root: Path
    project_roots: list[Path]
    default_status: ProjectStatus = ProjectStatus.ACTIVE
    openclaw_workspace: Path | None = None

    @classmethod
    def load(cls, path: Path) -> PortfolioConfig:
        """Load portfolio config from TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)

        portfolio_root = Path(data.get("portfolio_root", path.parent)).expanduser()
        project_roots = [
            Path(p).expanduser() for p in data.get("project_roots", [])
        ]

        defaults = data.get("defaults", {})
        default_status = ProjectStatus(defaults.get("status", "active"))

        openclaw = data.get("openclaw", {})
        openclaw_workspace = None
        if ws := openclaw.get("workspace"):
            openclaw_workspace = Path(ws).expanduser()

        return cls(
            portfolio_root=portfolio_root,
            project_roots=project_roots,
            default_status=default_status,
            openclaw_workspace=openclaw_workspace,
        )


@dataclass
class ProjectSettings:
    """Project settings from .project/settings.toml."""

    id: str
    name: str
    status: ProjectStatus = ProjectStatus.ACTIVE
    priority: int = 5
    repo_path: Path | None = None
    labels: list[str] = field(default_factory=list)
    project_dir: Path | None = None  # Set after loading
    # CLAWP-048 — explicit task-ID prefix. When unset, the prefix is inferred
    # from existing tasks (stability) or derived collision-free from the id.
    # Set this to disambiguate two projects whose ids share a short prefix.
    task_prefix: str | None = None

    @classmethod
    def load(cls, path: Path) -> ProjectSettings:
        """Load project settings from TOML file."""
        with open(path, "rb") as f:
            data = tomllib.load(f)

        repo_path = None
        if rp := data.get("repo_path"):
            repo_path = Path(rp).expanduser()

        settings = cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            status=ProjectStatus(data.get("status", "active")),
            priority=data.get("priority", 5),
            repo_path=repo_path,
            labels=data.get("labels", []),
            task_prefix=data.get("task_prefix"),
        )
        settings.project_dir = path.parent.parent
        return settings

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        return {
            "id": self.id,
            "name": self.name,
            "status": self.status.value,
            "priority": self.priority,
            "repo_path": str(self.repo_path) if self.repo_path else None,
            "labels": self.labels,
            "project_dir": str(self.project_dir) if self.project_dir else None,
            "task_prefix": self.task_prefix,
        }


@dataclass(eq=False)
class SuccessCriterion:
    """A structured success criterion suitable for both clawpm reflection
    and an Anthropic `user.define_outcome` rubric.

    A bare-string criterion (``"P95 latency <200ms"``) parses into
    ``SuccessCriterion(criterion="P95 latency <200ms")`` with no signal or
    comparator. Structured form adds:

    - ``gradeable_signal`` — what evidence proves the criterion held
    - ``comparator`` — a parseable pass-condition (free text; future Phase 2
      may add a tiny DSL for ``lt:200ms`` / ``gte:0.95`` etc.)

    Equality is intentionally loose: an SC equals a plain string when their
    criterion texts match. This preserves the existing assertion style
    ``predictions.success_criteria == ["P95 latency <200ms"]`` from the
    pre-structured-criteria test corpus.
    """

    criterion: str
    gradeable_signal: str | None = None
    comparator: str | None = None

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return self.criterion == other
        if isinstance(other, SuccessCriterion):
            return (
                self.criterion == other.criterion
                and self.gradeable_signal == other.gradeable_signal
                and self.comparator == other.comparator
            )
        return NotImplemented

    def __hash__(self) -> int:
        # MUST be consistent with __eq__ which can match a plain str on
        # the criterion alone. Hashing on `criterion` only preserves the
        # Python data-model invariant (a == b → hash(a) == hash(b)) for
        # the str-equality bridge. Structured variants sharing the same
        # criterion text will collide in dicts/sets — acceptable because
        # equality on structured form already disambiguates by signal +
        # comparator, so set/dict semantics remain correct.
        return hash(self.criterion)

    def is_structured(self) -> bool:
        return self.gradeable_signal is not None or self.comparator is not None

    def to_yaml(self) -> str | dict[str, str]:
        """Serialize back to YAML — bare string when no structure is set."""
        if not self.is_structured():
            return self.criterion
        d: dict[str, str] = {"criterion": self.criterion}
        if self.gradeable_signal:
            d["gradeable_signal"] = self.gradeable_signal
        if self.comparator:
            d["comparator"] = self.comparator
        return d

    @classmethod
    def from_cli(cls, value: str) -> SuccessCriterion:
        """Parse a value from the ``--success-criteria`` CLI flag.

        Accepts either a plain string (treated as ``criterion`` only) or a
        JSON object string of shape ``{"criterion": "...", "gradeable_signal":
        "...", "comparator": "..."}``. JSON detection is conservative — only
        triggered when the string starts with ``{`` to keep ``--success-criteria
        '{count} > 0'`` working as a plain string for plausible-but-unusual
        criteria phrasings.
        """
        import json as _json
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = _json.loads(stripped)
            except _json.JSONDecodeError:
                # Wasn't actually JSON; treat as plain string.
                return cls(criterion=value)
            if isinstance(parsed, dict):
                return cls.from_yaml(parsed)
        return cls(criterion=value)

    @classmethod
    def from_yaml(cls, value: Any) -> SuccessCriterion:
        if isinstance(value, str):
            return cls(criterion=value)
        if isinstance(value, dict):
            crit = value.get("criterion")
            if not crit:
                raise ValueError(
                    f"success_criterion dict missing 'criterion' key: {value!r}"
                )
            return cls(
                criterion=crit,
                gradeable_signal=value.get("gradeable_signal"),
                comparator=value.get("comparator"),
            )
        if isinstance(value, cls):
            return value
        raise ValueError(f"Bad success_criterion: {value!r}")


@dataclass
class Predictions:
    """Operator predictions captured at task creation or edit time.

    Stored under a ``predictions:`` block in YAML frontmatter so they don't
    pollute the top-level keys.  All fields are optional — no prediction is fine.
    """

    duration_min: int | None = None
    complexity: TaskComplexity | None = None
    files_changed: int | None = None
    files_scope: list[str] = field(default_factory=list)
    frameworks: list[str] = field(default_factory=list)
    pitfalls: str | None = None
    hypothesis: str | None = None
    # Phase 1.5 — applied-science framing
    # Each entry is a SuccessCriterion. Bare strings (legacy) and dicts (new
    # structured form) are both accepted at construction and normalised via
    # ``__post_init__``. SuccessCriterion equality matches plain strings on
    # the ``criterion`` field so existing assertions keep working.
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    approach: str | None = None
    unknowns: str | None = None
    confidence: int | None = None  # 1–5; None = not set
    reference_tasks: list[str] = field(default_factory=list)
    pre_mortem: str | None = None
    # CLAWP-019 — predicted iteration count for iterate→grade→revise loops.
    # Default None (no expectation); 1 means "expected to land in one pass".
    # Compared against iterations_actual (count of Stop-hook eval cycles)
    # at done-time to surface revision-count calibration.
    predicted_iterations: int | None = None
    # Phase 1.6 — attribution: who filled in these predictions?
    filled_by: str | None = None  # "agent" | "operator" | "operator-edited" | "retroactive" | None
    # CLAWP-062 -- per-task thrashing threshold. None = use global env/default.
    thrash_threshold: int | None = None

    def __post_init__(self) -> None:
        # Normalise success_criteria — accept str | dict | SuccessCriterion
        # so callers passing the legacy ``list[str]`` form Just Work.
        normalised: list[SuccessCriterion] = []
        for item in self.success_criteria:
            if isinstance(item, SuccessCriterion):
                normalised.append(item)
            else:
                normalised.append(SuccessCriterion.from_yaml(item))
        self.success_criteria = normalised

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_min": self.duration_min,
            "complexity": self.complexity.value if self.complexity else None,
            "files_changed": self.files_changed,
            "files_scope": self.files_scope,
            "frameworks": self.frameworks,
            "pitfalls": self.pitfalls,
            "hypothesis": self.hypothesis,
            "success_criteria": [sc.to_yaml() for sc in self.success_criteria],
            "approach": self.approach,
            "unknowns": self.unknowns,
            "confidence": self.confidence,
            "reference_tasks": self.reference_tasks,
            "pre_mortem": self.pre_mortem,
            "predicted_iterations": self.predicted_iterations,
            "filled_by": self.filled_by,
            "thrash_threshold": self.thrash_threshold,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Predictions:
        complexity = None
        if c := data.get("complexity"):
            try:
                complexity = TaskComplexity(c)
            except ValueError:
                pass
        return cls(
            duration_min=data.get("duration_min"),
            complexity=complexity,
            files_changed=data.get("files_changed"),
            files_scope=data.get("files_scope") or [],
            frameworks=data.get("frameworks") or [],
            pitfalls=data.get("pitfalls"),
            hypothesis=data.get("hypothesis"),
            success_criteria=[
                SuccessCriterion.from_yaml(v)
                for v in (data.get("success_criteria") or [])
            ],
            approach=data.get("approach"),
            unknowns=data.get("unknowns"),
            confidence=data.get("confidence"),
            reference_tasks=data.get("reference_tasks") or [],
            pre_mortem=data.get("pre_mortem"),
            predicted_iterations=data.get("predicted_iterations"),
            filled_by=data.get("filled_by"),
            thrash_threshold=data.get("thrash_threshold"),
        )

    def is_empty(self) -> bool:
        """True when no predictions have been set."""
        return (
            self.duration_min is None
            and self.complexity is None
            and self.files_changed is None
            and not self.files_scope
            and not self.frameworks
            and self.pitfalls is None
            and self.hypothesis is None
            and not self.success_criteria
            and self.approach is None
            and self.unknowns is None
            and self.confidence is None
            and not self.reference_tasks
            and self.pre_mortem is None
            and self.predicted_iterations is None
        )


@dataclass
class Actuals:
    """Actual outcomes computed at task completion (done/blocked).

    Derived from work_log entries for the task — not stored in the task file
    itself, only in the reflection event JSONL.
    """

    duration_min: int | None = None
    complexity: TaskComplexity | None = None
    files_changed: int | None = None
    files_touched: list[str] = field(default_factory=list)
    # CLAWP-019 — count of Stop-hook eval cycles observed before terminal
    # event. Populated by _compute_actuals from iteration_event lines in
    # the reflection JSONL. None = no iterations were captured.
    iterations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_min": self.duration_min,
            "complexity": self.complexity.value if self.complexity else None,
            "files_changed": self.files_changed,
            "files_touched": self.files_touched,
            "iterations": self.iterations,
        }


@dataclass
class Task:
    """A task with frontmatter and content."""

    id: str
    title: str
    state: TaskState
    priority: int = 5
    complexity: TaskComplexity | None = None
    depends: list[str] = field(default_factory=list)
    scope: list[str] = field(default_factory=list)
    parent: str | None = None
    children: list[str] = field(default_factory=list)  # Populated by discovery
    created: str | None = None
    content: str = ""
    file_path: Path | None = None
    predictions: Predictions = field(default_factory=Predictions)
    # CLAWP-021 — batch dispatch ordinal. Tasks sharing a parallel_group are
    # dispatchable together (provided scopes don't overlap). Group N+1 only
    # becomes eligible once every group-N task is DONE. Absent field =
    # excluded from --batch (sequential by default).
    parallel_group: int | None = None
    # CLAWP-022 Mission Control: a task can belong to a mission (the macro
    # binary-outcome layer above tasks) and carry an actor tag.
    # actor: "agent" (default, dispatchable to a subagent) or "human"
    # (operator-only, e.g. on-camera recording, physical signature).
    # parent_mission: ID of the mission this task is a mini-goal of.
    actor: str | None = None  # None = legacy/unspecified, defaults to "agent" semantics
    parent_mission: str | None = None
    # CLAWP-038 — agent_profile: a capability/skill hint (shape modeled on
    # agenticq's AgentCard.capability) recorded on the task and propagated
    # into reflection/iteration events so calibration can segment
    # predicted-vs-actual by profile. None = unspecified = generic dispatch.
    agent_profile: str | None = None
    # CLAWP-053 — won't-do ledger fields. Only meaningful when state==REJECTED.
    # rationale: required free-text reason the idea was rejected.
    # supersedes: optional task-id link (another task that replaces this one).
    rationale: str | None = None
    supersedes: str | None = None
    # CLAWP-054 — dispatch contract fields.
    # out_of_scope: explicit boundary — file globs or named topics the executor
    #   must NOT touch. Rendered verbatim in the agent preamble/rubric.
    # stop_conditions: escape-hatch conditions — free-text triggers that, when
    #   tripped by the executor, should cause a STOP+report-back.
    # delegability: who may execute this leaf.
    #   "agent"  — auto-dispatchable to a subagent
    #   "human"  — operator-only; dispatch MUST refuse auto-dispatch
    #   "either" — either is acceptable (default; back-compat)
    out_of_scope: list[str] = field(default_factory=list)
    stop_conditions: list[str] = field(default_factory=list)
    delegability: str = "either"  # "agent" | "human" | "either"
    # CLAWP-055 — per-task baseline marker. Stamped at task-creation time.
    # Opaque string: git short-SHA when the project is a git repo, else a
    # "ts:<ISO8601-UTC>" timestamp. None for legacy tasks (backward-compat).
    baseline_ref: str | None = None

    @property
    def is_parent(self) -> bool:
        """True if this task has subtasks or is stored as a directory."""
        if self.children:
            return True
        # Check if stored as directory (has _task.md)
        if self.file_path and self.file_path.name == "_task.md":
            return True
        return False

    @classmethod
    def from_file(cls, path: Path) -> Task:
        """Load task from markdown file with YAML frontmatter."""
        text = path.read_text(encoding="utf-8")

        # Determine state from filename/location
        # Check path components for done/blocked/rejected (handles both regular files and task directories)
        path_parts = path.parts
        if "done" in path_parts:
            state = TaskState.DONE
        elif "blocked" in path_parts:
            state = TaskState.BLOCKED
        elif "rejected" in path_parts:
            state = TaskState.REJECTED
        elif ".progress" in path.name:
            state = TaskState.PROGRESS
        else:
            state = TaskState.OPEN

        # Parse frontmatter (lenient: any malformation -> {} + full text as
        # content, matching the pre-CLAWP-079 hand-rolled behaviour).
        frontmatter: dict[str, Any]
        try:
            frontmatter, body = split_frontmatter(text)
            content = body.strip()
        except FrontmatterError:
            frontmatter = {}
            content = text

        # Extract title from first heading
        title = frontmatter.get("id", path.stem)
        for line in content.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

        complexity = None
        if c := frontmatter.get("complexity"):
            try:
                complexity = TaskComplexity(c)
            except ValueError:
                pass

        predictions = Predictions()
        if pred_raw := frontmatter.get("predictions"):
            if isinstance(pred_raw, dict):
                predictions = Predictions.from_dict(pred_raw)

        pg_raw = frontmatter.get("parallel_group")
        parallel_group: int | None = None
        if pg_raw is not None:
            try:
                parallel_group = int(pg_raw)
            except (TypeError, ValueError):
                parallel_group = None

        actor_raw = frontmatter.get("actor")
        actor: str | None = None
        if isinstance(actor_raw, str) and actor_raw in ("agent", "human"):
            actor = actor_raw

        # CLAWP-038 — agent_profile is a free-form capability string. Absent
        # or non-string (legacy task files) → None, preserving back-compat.
        ap_raw = frontmatter.get("agent_profile")
        agent_profile: str | None = (
            ap_raw if isinstance(ap_raw, str) and ap_raw.strip() else None
        )

        # CLAWP-037 (codex round-1 fix) — persist children on the parent so
        # the rollup gate sees the full set even after children migrate out
        # of the parent directory into done/ or blocked/. Without this, a
        # dir-scan-only children list silently shrinks as work completes,
        # defeating the missing/dangling-child = UNSATISFIED rule.
        # list_tasks' parent-linking still appends any newly-discovered
        # children (idempotent), so legacy directory-only parents keep
        # working unchanged.
        children_raw = frontmatter.get("children") or []
        children = (
            [c for c in children_raw if isinstance(c, str)]
            if isinstance(children_raw, list) else []
        )

        # CLAWP-053 — rationale and supersedes are only meaningful for REJECTED
        # tasks but are stored as plain frontmatter strings so any task file
        # can carry them without a migration step.
        rationale_raw = frontmatter.get("rationale")
        rationale: str | None = (
            rationale_raw if isinstance(rationale_raw, str) and rationale_raw.strip()
            else None
        )
        supersedes_raw = frontmatter.get("supersedes")
        supersedes: str | None = (
            supersedes_raw if isinstance(supersedes_raw, str) and supersedes_raw.strip()
            else None
        )

        # CLAWP-054 — out_of_scope, stop_conditions: lists of strings
        oos_raw = frontmatter.get("out_of_scope")
        out_of_scope: list[str] = (
            [s for s in oos_raw if isinstance(s, str)]
            if isinstance(oos_raw, list) else []
        )
        sc_raw = frontmatter.get("stop_conditions")
        stop_conditions: list[str] = (
            [s for s in sc_raw if isinstance(s, str)]
            if isinstance(sc_raw, list) else []
        )
        deleg_raw = frontmatter.get("delegability")
        delegability: str = (
            deleg_raw if deleg_raw in ("agent", "human", "either") else "either"
        )

        # CLAWP-055 — baseline_ref: opaque string stamped at task creation.
        # None for legacy tasks — backward-compat default.
        baseline_ref_raw = frontmatter.get("baseline_ref")
        baseline_ref: str | None = (
            baseline_ref_raw
            if isinstance(baseline_ref_raw, str) and baseline_ref_raw.strip()
            else None
        )

        return cls(
            id=frontmatter.get("id", path.stem.replace(".progress", "")),
            title=title,
            state=state,
            priority=frontmatter.get("priority", 5),
            complexity=complexity,
            depends=frontmatter.get("depends", []),
            scope=frontmatter.get("scope", []),
            parent=frontmatter.get("parent"),
            children=children,
            created=frontmatter.get("created"),
            content=content,
            file_path=path,
            predictions=predictions,
            parallel_group=parallel_group,
            actor=actor,
            parent_mission=frontmatter.get("parent_mission"),
            agent_profile=agent_profile,
            rationale=rationale,
            supersedes=supersedes,
            out_of_scope=out_of_scope,
            stop_conditions=stop_conditions,
            delegability=delegability,
            baseline_ref=baseline_ref,
        )

    @property
    def body(self) -> str | None:
        """Extract body text (between title and first ## section)."""
        if not self.content:
            return None
        lines = self.content.split("\n")
        title_idx = None
        section_idx = None
        for i, line in enumerate(lines):
            if line.startswith("# ") and title_idx is None:
                title_idx = i
            elif line.startswith("## ") and title_idx is not None:
                section_idx = i
                break
        if title_idx is None:
            return None
        start = title_idx + 1
        end = section_idx if section_idx is not None else len(lines)
        body = "\n".join(lines[start:end]).strip()
        return body if body else None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        result = {
            "id": self.id,
            "title": self.title,
            "state": self.state.value,
            "priority": self.priority,
            "complexity": self.complexity.value if self.complexity else None,
            "depends": self.depends,
            "scope": self.scope,
            "parent": self.parent,
            "children": self.children,
            "is_parent": self.is_parent,
            "created": self.created,
            "file_path": str(self.file_path) if self.file_path else None,
            "parallel_group": self.parallel_group,
            "actor": self.actor,
            "parent_mission": self.parent_mission,
            "agent_profile": self.agent_profile,
            # CLAWP-053 — won't-do ledger. rationale/supersedes are None for
            # non-rejected tasks; included unconditionally so the schema is
            # stable and agents can introspect without conditional logic.
            "rationale": self.rationale,
            "supersedes": self.supersedes,
            # CLAWP-054 — contract fields
            "out_of_scope": self.out_of_scope,
            "stop_conditions": self.stop_conditions,
            "delegability": self.delegability,
            # CLAWP-055 — baseline ref (opaque; None for legacy tasks)
            "baseline_ref": self.baseline_ref,
        }
        body = self.body
        if body:
            result["body"] = body
        # Always include predictions in output when any field is set;
        # include the block even when empty so agents can see the schema.
        result["predictions"] = self.predictions.to_dict()
        return result


@dataclass
class WorkLogEntry:
    """A work log entry."""

    ts: datetime
    project: str
    action: WorkLogAction
    agent: str = "main"
    session_key: str | None = None
    task: str | None = None
    summary: str | None = None
    next: str | None = None
    files_changed: list[str] | None = None
    blockers: str | None = None
    auto: bool = False  # True for auto-generated entries (state changes)
    commit_hash: str | None = None  # Git commit hash (for action=commit)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        result = {
            "ts": self.ts.isoformat() + "Z" if self.ts.tzinfo is None else self.ts.isoformat(),
            "project": self.project,
            "task": self.task,
            "action": self.action.value,
            "agent": self.agent,
            "session_key": self.session_key,
            "summary": self.summary,
            "next": self.next,
            "files_changed": self.files_changed,
            "blockers": self.blockers,
        }
        if self.auto:
            result["auto"] = True
        if self.commit_hash:
            result["commit_hash"] = self.commit_hash
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WorkLogEntry:
        """Create from dictionary."""
        ts = data["ts"]
        if isinstance(ts, str):
            # Handle ISO format with Z suffix
            ts = ts.rstrip("Z")
            ts = datetime.fromisoformat(ts)

        return cls(
            ts=ts,
            project=data["project"],
            action=WorkLogAction(data["action"]),
            agent=data.get("agent", "main"),
            session_key=data.get("session_key"),
            task=data.get("task"),
            summary=data.get("summary"),
            next=data.get("next"),
            files_changed=data.get("files_changed"),
            blockers=data.get("blockers"),
            auto=data.get("auto", False),
            commit_hash=data.get("commit_hash"),
        )


@dataclass
class Research:
    """A research item."""

    id: str
    title: str
    type: ResearchType
    status: ResearchStatus = ResearchStatus.OPEN
    tags: list[str] = field(default_factory=list)
    created: str | None = None
    content: str = ""
    openclaw: dict[str, Any] | None = None
    file_path: Path | None = None

    @classmethod
    def from_file(cls, path: Path) -> Research:
        """Load research from markdown file with YAML frontmatter."""
        text = path.read_text(encoding="utf-8")

        # Parse frontmatter (lenient: any malformation -> {} + full text as
        # content, matching the pre-CLAWP-079 hand-rolled behaviour).
        frontmatter: dict[str, Any]
        try:
            frontmatter, body = split_frontmatter(text)
            content = body.strip()
        except FrontmatterError:
            frontmatter = {}
            content = text

        # Extract title from first heading
        title = frontmatter.get("id", path.stem)
        for line in content.split("\n"):
            if line.startswith("# "):
                title = line[2:].strip()
                break

        return cls(
            id=frontmatter.get("id", path.stem),
            title=title,
            type=ResearchType(frontmatter.get("type", "investigation")),
            status=ResearchStatus(frontmatter.get("status", "open")),
            tags=frontmatter.get("tags", []),
            created=frontmatter.get("created"),
            content=content,
            openclaw=frontmatter.get("openclaw"),
            file_path=path,
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON output."""
        return {
            "id": self.id,
            "title": self.title,
            "type": self.type.value,
            "status": self.status.value,
            "tags": self.tags,
            "created": self.created,
            "openclaw": self.openclaw,
            "file_path": str(self.file_path) if self.file_path else None,
        }

