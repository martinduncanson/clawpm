"""CLAWP-056 — Emission API: one-shot persist a fully-contracted task-tree.

Deterministic sink — ZERO LLM calls in this path. A planner (a separate skill)
supplies a fully-specified tree; CORE persists it atomically or not at all.

Four-phase model (per the design spec):
  1. Parse + validate (in-memory, no writes)
  2. Gate barrier (read-only checks — collision, reject-match, constitution,
     baseline resolution)
  3. Stage (render files into a same-FS dot-prefixed staging dir)
  4. Promote (atomic rename into live store)

JSONL work_log is appended LAST, after promotion succeeds.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .models import (
    Predictions,
    ResearchType,
    ResearchStatus,
    SuccessCriterion,
    Task,
    TaskComplexity,
    TaskState,
    PortfolioConfig,
)

# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1
ALLOWED_TOP_KEYS = frozenset(
    {"schema_version", "project", "root", "prd", "leaves"}
)
ALLOWED_LEAF_KEYS = frozenset(
    {
        "ref",
        "parent_ref",
        "title",
        "success_criteria",
        "scope",
        "out_of_scope",
        "stop_conditions",
        "delegability",
        "predictions",
        "agent_profile",
        "parallel_group",
        "leaf_key",
    }
)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class LeafSpec:
    """In-memory representation of one leaf from the input document."""

    ref: str
    parent_ref: str | None
    title: str
    success_criteria: list[SuccessCriterion]
    scope: list[str]
    out_of_scope: list[str]
    stop_conditions: list[str]
    delegability: str
    predictions: Predictions
    agent_profile: str | None
    parallel_group: int | None
    leaf_key: str  # stable idempotency key supplied by the caller


@dataclass
class PrdSpec:
    """In-memory representation of the optional PRD block."""

    title: str
    type: ResearchType
    tags: list[str]
    body_markdown: str


@dataclass
class RootSpec:
    """Root node — either a new task or an attach target."""

    attach_to: str | None  # task ID to attach children under (None = create new root)
    title: str | None
    predictions: Predictions | None


@dataclass
class EmitTreeDocument:
    """Parsed + validated input document."""

    schema_version: int
    project: str | None
    root: RootSpec
    prd: PrdSpec | None
    leaves: list[LeafSpec]


@dataclass
class EmitResult:
    """Return value from emit_tree()."""

    root_id: str
    emitted: list[dict]          # task dicts for every created task
    research_id: str | None      # ID of the created PRD research entry, if any
    baseline_ref: str            # the baseline stamped on every leaf
    rejected: list[dict]         # leaves rejected by won't-do gate (report-back)
    constitution_violations: list[dict]  # constitution violations (report-back)
    dry_run: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "emitted": self.emitted,
            "research_id": self.research_id,
            "baseline_ref": self.baseline_ref,
            "rejected": self.rejected,
            "constitution_violations": self.constitution_violations,
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# Parse & validate (Phase 1)
# ---------------------------------------------------------------------------


class EmitValidationError(ValueError):
    """Raised when the input document fails schema validation."""


def _parse_leaf(raw: Any, idx: int) -> LeafSpec:
    """Parse and validate a single leaf dict from the input document."""
    if not isinstance(raw, dict):
        raise EmitValidationError(f"leaves[{idx}] must be a JSON object, got {type(raw).__name__}")

    # Unknown top-level keys are a hard error (fail-closed — a typo'd
    # success_criterai must not silently drop a contract).
    unknown = set(raw.keys()) - ALLOWED_LEAF_KEYS
    if unknown:
        raise EmitValidationError(
            f"leaves[{idx}] has unknown keys: {sorted(unknown)!r}. "
            "Check for typos — unknown keys on a leaf spec are rejected to prevent "
            "silently-dropped contracts."
        )

    ref = raw.get("ref")
    if not ref or not isinstance(ref, str):
        raise EmitValidationError(f"leaves[{idx}] missing required 'ref' (string)")

    title = raw.get("title")
    if not title or not isinstance(title, str):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) missing required 'title' (string)")

    # success_criteria — validate each criterion
    raw_sc = raw.get("success_criteria") or []
    if not isinstance(raw_sc, list):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) success_criteria must be a list")
    success_criteria: list[SuccessCriterion] = []
    for sc_idx, sc_raw in enumerate(raw_sc):
        try:
            success_criteria.append(SuccessCriterion.from_yaml(sc_raw))
        except ValueError as exc:
            raise EmitValidationError(
                f"leaves[{idx}] (ref={ref!r}) success_criteria[{sc_idx}]: {exc}"
            ) from exc

    scope = raw.get("scope") or []
    if not isinstance(scope, list) or not all(isinstance(s, str) for s in scope):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) scope must be a list of strings")

    out_of_scope = raw.get("out_of_scope") or []
    if not isinstance(out_of_scope, list) or not all(isinstance(s, str) for s in out_of_scope):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) out_of_scope must be a list of strings")

    stop_conditions = raw.get("stop_conditions") or []
    if not isinstance(stop_conditions, list) or not all(isinstance(s, str) for s in stop_conditions):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) stop_conditions must be a list of strings")

    delegability = raw.get("delegability", "either")
    if delegability not in ("agent", "human", "either"):
        raise EmitValidationError(
            f"leaves[{idx}] (ref={ref!r}) delegability must be 'agent', 'human', or 'either', got {delegability!r}"
        )

    raw_preds = raw.get("predictions") or {}
    if not isinstance(raw_preds, dict):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) predictions must be a JSON object")
    predictions = Predictions.from_dict(raw_preds)

    agent_profile = raw.get("agent_profile")
    if agent_profile is not None and not isinstance(agent_profile, str):
        raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) agent_profile must be a string")

    pg = raw.get("parallel_group")
    if pg is not None:
        try:
            pg = int(pg)
        except (TypeError, ValueError):
            raise EmitValidationError(f"leaves[{idx}] (ref={ref!r}) parallel_group must be an integer")

    leaf_key = raw.get("leaf_key")
    if not leaf_key or not isinstance(leaf_key, str):
        # Generate a stable key from ref if not provided — callers SHOULD supply one
        leaf_key = ref

    # parent_ref: when non-null, this leaf is a child of another leaf in the
    # same document (CLAWP-064 in-document nesting). Validation that parent_ref
    # resolves to an existing sibling ref is done in parse_emit_document after
    # all leaves are collected.
    parent_ref = raw.get("parent_ref")
    if parent_ref is not None and not isinstance(parent_ref, str):
        raise EmitValidationError(
            f"leaves[{idx}] (ref={ref!r}) parent_ref must be a string or null"
        )

    return LeafSpec(
        ref=ref,
        parent_ref=parent_ref,
        title=title,
        success_criteria=success_criteria,
        scope=scope,
        out_of_scope=out_of_scope,
        stop_conditions=stop_conditions,
        delegability=delegability,
        predictions=predictions,
        agent_profile=agent_profile if isinstance(agent_profile, str) else None,
        parallel_group=pg,
        leaf_key=leaf_key,
    )


def _parse_prd(raw: Any) -> PrdSpec:
    """Parse the optional prd block."""
    if not isinstance(raw, dict):
        raise EmitValidationError("prd must be a JSON object")
    title = raw.get("title")
    if not title or not isinstance(title, str):
        raise EmitValidationError("prd.title is required (string)")
    type_str = raw.get("type", "spike")
    try:
        prd_type = ResearchType(type_str)
    except ValueError:
        valid = [t.value for t in ResearchType]
        raise EmitValidationError(f"prd.type {type_str!r} is not valid; expected one of {valid}")
    tags = raw.get("tags") or []
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        raise EmitValidationError("prd.tags must be a list of strings")
    body_markdown = raw.get("body_markdown", "")
    if not isinstance(body_markdown, str):
        raise EmitValidationError("prd.body_markdown must be a string")
    return PrdSpec(title=title, type=prd_type, tags=tags, body_markdown=body_markdown)


def parse_emit_document(raw: dict[str, Any]) -> EmitTreeDocument:
    """Parse and validate the full tree document.

    Raises EmitValidationError on any structural issue.
    """
    # Unknown top-level keys — fail-closed
    unknown = set(raw.keys()) - ALLOWED_TOP_KEYS
    if unknown:
        raise EmitValidationError(
            f"Document has unknown top-level keys: {sorted(unknown)!r}. "
            "Check for typos — unknown top-level keys are rejected fail-closed."
        )

    # schema_version is mandatory
    sv = raw.get("schema_version")
    if sv is None:
        raise EmitValidationError("'schema_version' is required")
    if not isinstance(sv, int) or sv != CURRENT_SCHEMA_VERSION:
        raise EmitValidationError(
            f"schema_version must be {CURRENT_SCHEMA_VERSION}, got {sv!r}"
        )

    # root block — exactly one of attach_to or title
    raw_root = raw.get("root") or {}
    if not isinstance(raw_root, dict):
        raise EmitValidationError("'root' must be a JSON object")
    attach_to = raw_root.get("attach_to")
    root_title = raw_root.get("title")
    if attach_to and root_title:
        raise EmitValidationError(
            "'root' must have exactly one of 'attach_to' or 'title', not both"
        )
    if not attach_to and not root_title:
        raise EmitValidationError(
            "'root' must have either 'attach_to' (existing task ID) or 'title' (new root task)"
        )
    raw_root_preds = raw_root.get("predictions") or {}
    root_preds = Predictions.from_dict(raw_root_preds) if isinstance(raw_root_preds, dict) else None

    root = RootSpec(
        attach_to=attach_to if isinstance(attach_to, str) else None,
        title=root_title if isinstance(root_title, str) else None,
        predictions=root_preds,
    )

    # prd
    raw_prd = raw.get("prd")
    prd = _parse_prd(raw_prd) if raw_prd is not None else None

    # leaves — must have ≥1
    raw_leaves = raw.get("leaves")
    if not raw_leaves or not isinstance(raw_leaves, list):
        raise EmitValidationError("'leaves' must be a non-empty list")

    leaves: list[LeafSpec] = []
    for idx, raw_leaf in enumerate(raw_leaves):
        leaves.append(_parse_leaf(raw_leaf, idx))

    # Unique refs
    refs = [lf.ref for lf in leaves]
    if len(refs) != len(set(refs)):
        dupes = [r for r in refs if refs.count(r) > 1]
        raise EmitValidationError(f"Duplicate leaf refs: {sorted(set(dupes))!r}")

    # parent_ref resolution and cycle detection (CLAWP-064).
    # Every non-null parent_ref must match another leaf's ref in the document.
    ref_set = set(refs)
    for lf in leaves:
        if lf.parent_ref is not None:
            if lf.parent_ref == lf.ref:
                raise EmitValidationError(
                    f"Leaf ref={lf.ref!r} has a self-cycle: parent_ref points to itself"
                )
            if lf.parent_ref not in ref_set:
                raise EmitValidationError(
                    f"Leaf ref={lf.ref!r} has parent_ref={lf.parent_ref!r} "
                    "which does not match any other leaf's ref in this document"
                )

    # Cycle detection: topological sort via Kahn's algorithm.
    # Build adjacency: parent_ref -> child.
    in_degree = {lf.ref: 0 for lf in leaves}
    children_of: dict[str, list[str]] = {lf.ref: [] for lf in leaves}
    for lf in leaves:
        if lf.parent_ref is not None:
            in_degree[lf.ref] += 1
            children_of[lf.parent_ref].append(lf.ref)

    queue = [r for r, d in in_degree.items() if d == 0]
    processed = 0
    while queue:
        node = queue.pop(0)
        processed += 1
        for child in children_of[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                queue.append(child)

    if processed != len(leaves):
        raise EmitValidationError(
            "Cycle detected in parent_ref graph: all leaves in a document must form "
            "a DAG (directed acyclic graph) rooted at leaves with parent_ref=null"
        )

    return EmitTreeDocument(
        schema_version=sv,
        project=raw.get("project"),
        root=root,
        prd=prd,
        leaves=leaves,
    )


# ---------------------------------------------------------------------------
# Gate barrier helpers (Phase 2 — read-only)
# ---------------------------------------------------------------------------


def _check_reject_match(
    leaves: list[LeafSpec],
    config: PortfolioConfig,
    project_id: str,
) -> list[dict]:
    """Check each leaf against the CLAWP-053 won't-do ledger.

    Returns a list of rejection dicts (empty = no rejections).
    Graceful no-op if the reject ledger is not available.
    """
    try:
        from .tasks import get_task, list_tasks

        rejected_tasks = list_tasks(config, project_id, state_filter=TaskState.REJECTED)
    except Exception:
        # Fail-open: if the ledger isn't readable, don't block emission
        return []

    rejected: list[dict] = []
    for lf in leaves:
        for rejected_task in rejected_tasks:
            # EXACT case-insensitive match only — core is deterministic. Fuzzy /
            # "resembling" matching is the planner skill's job (CLAWP-053), not
            # core's; a prefix match here would false-positive on short rejected
            # titles and silently drop a legitimate leaf.
            if lf.title.lower() == rejected_task.title.lower():
                rejected.append({
                    "leaf_ref": lf.ref,
                    "leaf_title": lf.title,
                    "matched_rejected_id": rejected_task.id,
                    "matched_rejected_title": rejected_task.title,
                    "rationale": rejected_task.rationale,
                })
                break
    return rejected


def _check_constitution(
    doc: EmitTreeDocument,
    config: PortfolioConfig,
    project_id: str,
) -> list[dict]:
    """Check the tree against the project constitution (CLAWP-057).

    Graceful no-op if the constitution layer is not built yet — returns [].
    """
    try:
        # CLAWP-057 may not exist yet — graceful no-op
        from . import constitution as _const  # type: ignore[attr-defined]
        return _const.validate(config, project_id, doc)
    except (ImportError, AttributeError):
        # Constitution module not yet built — graceful no-op as per spec
        return []
    except Exception:
        # Any other constitution check error — fail-open
        return []


def _existing_child_nums(tasks_dir: Path, parent_id: str) -> set[int]:
    """Union-scan the existing child ordinals for ``parent_id``.

    Mirrors add_subtask's id-generator union: parent dir glob + done/ + blocked/
    + the parent's persisted ``children:`` frontmatter list. Read-only; shared by
    the collision pre-check (phase 2) and the staging id-mint (phase 3) so the
    two can never disagree.
    """
    existing_nums: set[int] = set()

    def _record(tid: str) -> None:
        try:
            num_str = tid.split("-")[-1].replace(".progress", "")
            existing_nums.add(int(num_str))
        except (IndexError, ValueError):
            pass

    parent_dir = tasks_dir / parent_id
    if parent_dir.exists():
        for f in parent_dir.glob(f"{parent_id}-*.md"):
            _record(f.stem)
    for state_dir in (tasks_dir / "done", tasks_dir / "blocked"):
        if state_dir.exists():
            for f in state_dir.glob(f"{parent_id}-*.md"):
                _record(f.stem)

    # Also union the parent task's persisted frontmatter children list.
    for pf in (tasks_dir / f"{parent_id}.md", tasks_dir / parent_id / "_task.md"):
        if pf.exists():
            try:
                text = pf.read_text(encoding="utf-8")
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        for cid in (fm.get("children") or []):
                            if isinstance(cid, str) and cid.startswith(parent_id + "-"):
                                _record(cid)
            except Exception:
                pass

    return existing_nums


def _check_id_collisions(
    config: PortfolioConfig,
    project_id: str,
    parent_id: str,
    leaves: list[LeafSpec],
) -> list[dict]:
    """Predict the subtask IDs that would be minted and check for collisions.

    Returns list of collision dicts. Empty = no collisions.
    Read-only — mirrors add_subtask's union-scan logic but does not write.
    """
    from .tasks import get_tasks_dir

    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return []

    existing_nums = _existing_child_nums(tasks_dir, parent_id)

    # Predict IDs for leaves in order
    collisions: list[dict] = []
    next_num = (max(existing_nums) if existing_nums else 0) + 1
    for lf in leaves:
        pred_id = f"{parent_id}-{next_num:03d}"
        # Check if this predicted ID already exists anywhere
        for state_dir in (tasks_dir, tasks_dir / "done", tasks_dir / "blocked", tasks_dir / "rejected"):
            conflict_file = state_dir / f"{pred_id}.md"
            conflict_dir = state_dir / pred_id / "_task.md"
            if conflict_file.exists() or conflict_dir.exists():
                collisions.append({
                    "leaf_ref": lf.ref,
                    "predicted_id": pred_id,
                    "reason": f"Task {pred_id} already exists on disk",
                })
        next_num += 1

    return collisions


def _resolve_idempotency(
    config: PortfolioConfig,
    project_id: str,
    parent_id: str,
    leaves: list[LeafSpec],
) -> list[str]:
    """Return leaf_keys of leaves that already exist (idempotent re-emit).

    Scans all children of parent_id for matching leaf_key frontmatter.
    Returns the list of already-emitted leaf_keys.
    """
    from .tasks import get_tasks_dir

    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        return []

    leaf_keys = {lf.leaf_key for lf in leaves}
    already_emitted: list[str] = []

    parent_dir = tasks_dir / parent_id
    if parent_dir.exists():
        for f in parent_dir.glob(f"{parent_id}-*.md"):
            try:
                text = f.read_text(encoding="utf-8")
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        fm = yaml.safe_load(parts[1]) or {}
                        lk = fm.get("leaf_key")
                        if lk and lk in leaf_keys:
                            already_emitted.append(lk)
            except Exception:
                pass

    return already_emitted


# ---------------------------------------------------------------------------
# Staging helpers (Phase 3)
# ---------------------------------------------------------------------------


def _build_predictions_block(
    predictions: Predictions | None,
    success_criteria: list[SuccessCriterion] | None = None,
) -> dict[str, Any] | None:
    """Render a cleaned predictions frontmatter block, or None if empty.

    Shared by leaf and new-root rendering. ``success_criteria`` (when given)
    is attached onto the predictions dict — the leaf carries its rubric
    separately from ``predictions``; the root carries it inline.
    """
    sc = success_criteria or []
    if predictions is None or (predictions.is_empty() and not sc):
        return None
    pred_dict = (predictions or Predictions()).to_dict()
    if sc:
        pred_dict["success_criteria"] = [c.to_yaml() for c in sc]
    cleaned = {k: v for k, v in pred_dict.items() if v is not None and v != []}
    return cleaned or None


def _render_task_content(
    task_id: str,
    title: str,
    parent_id: str | None,
    leaf: LeafSpec | None,
    baseline_ref: str,
    prd_ref: str | None = None,
    children: list[str] | None = None,
    predictions: Predictions | None = None,
) -> str:
    """Render the markdown content for a single task file.

    Mirrors exactly what add_task / add_subtask write, extended with
    CLAWP-054 and CLAWP-055 fields.

    ``leaf`` carries the per-leaf contract (when staging a leaf). ``predictions``
    is used for the new-root task (which has no leaf) so root predictions are
    not silently lost.
    """
    frontmatter: dict[str, Any] = {
        "id": task_id,
        "priority": 5,
        "created": date.today().isoformat(),
        "baseline_ref": baseline_ref,
    }

    if parent_id:
        frontmatter["parent"] = parent_id

    if children:
        frontmatter["children"] = children

    if prd_ref:
        frontmatter["prd_ref"] = prd_ref

    if leaf is not None:
        # CLAWP-054 contract fields
        if leaf.scope:
            frontmatter["scope"] = leaf.scope
        if leaf.out_of_scope:
            frontmatter["out_of_scope"] = leaf.out_of_scope
        if leaf.stop_conditions:
            frontmatter["stop_conditions"] = leaf.stop_conditions
        if leaf.delegability != "either":
            frontmatter["delegability"] = leaf.delegability
        if leaf.agent_profile:
            frontmatter["agent_profile"] = leaf.agent_profile
        if leaf.parallel_group is not None:
            frontmatter["parallel_group"] = leaf.parallel_group
        # leaf_key for idempotent re-emit
        frontmatter["leaf_key"] = leaf.leaf_key
        # predictions including success_criteria
        pred_block = _build_predictions_block(leaf.predictions, leaf.success_criteria)
        if pred_block:
            frontmatter["predictions"] = pred_block
    else:
        # New-root task — thread its predictions through so they persist.
        pred_block = _build_predictions_block(predictions)
        if pred_block:
            frontmatter["predictions"] = pred_block

    fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()
    content = (
        f"---\n{fm_yaml}\n---\n# {title}\n\n## Notes\n\n"
    )
    return content


def _render_research_content(
    research_id: str,
    prd: PrdSpec,
    linked_task_tree: str,
) -> str:
    """Render the markdown content for the PRD research file."""
    frontmatter: dict[str, Any] = {
        "id": research_id,
        "type": prd.type.value,
        "status": ResearchStatus.OPEN.value,
        "created": date.today().isoformat(),
        "linked_task_tree": linked_task_tree,
    }
    if prd.tags:
        frontmatter["tags"] = prd.tags

    fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True).strip()
    body = prd.body_markdown or "(To be filled in as research progresses)"
    content = f"---\n{fm_yaml}\n---\n# {prd.title}\n\n{body}\n"
    return content


def _predict_parent_id(
    doc: EmitTreeDocument,
    config: PortfolioConfig,
    project_id: str,
) -> str:
    """Predict the task ID for the root/parent task.

    - attach_to: returns the existing task ID directly.
    - new root: predicts what add_task would mint.
    """
    if doc.root.attach_to:
        return doc.root.attach_to

    # New root — predict the next ID add_task would generate
    from .tasks import get_tasks_dir, assign_task_prefix
    from .discovery import get_project
    import re

    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        raise EmitValidationError(f"Cannot locate tasks directory for project {project_id!r}")

    _settings = get_project(config, project_id)
    prefix = assign_task_prefix(
        project_id,
        tasks_dir,
        config,
        explicit_prefix=getattr(_settings, "task_prefix", None) if _settings else None,
    )

    _dir_pat = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    _file_pat = re.compile(rf"^{re.escape(prefix)}-(\d+)(?:\.progress)?$")
    existing_nums = []

    for scan_dir in [tasks_dir, tasks_dir / "done", tasks_dir / "blocked"]:
        if not scan_dir.exists():
            continue
        for f in scan_dir.glob(f"{prefix}-*.md"):
            m = _file_pat.match(f.stem)
            if m:
                existing_nums.append(int(m.group(1)))
        for entry in scan_dir.iterdir():
            if entry.is_dir():
                m = _dir_pat.match(entry.name)
                if m:
                    existing_nums.append(int(m.group(1)))

    next_num = max(existing_nums, default=-1) + 1
    return f"{prefix}-{next_num:03d}"


# ---------------------------------------------------------------------------
# Core emission function
# ---------------------------------------------------------------------------


def emit_tree(
    config: PortfolioConfig,
    project_id: str,
    doc: EmitTreeDocument,
    dry_run: bool = False,
    strict: bool = False,
) -> EmitResult:
    """Persist a fully-contracted task-tree atomically.

    Phases:
      1. (Already done) — parse/validate (parse_emit_document).
      2. Gate barrier (read-only checks, all before first write).
      3. Stage (render into .emit-<uuid>/ staging directory).
      4. Promote (atomic rename into live store).

    dry_run=True: runs phases 1-2 and reports what would be written, no writes.
    strict=True: hard-fail on reject-match / constitution violations instead of
                 report-back (the default).

    Returns EmitResult — the caller is responsible for logging.
    """
    from .tasks import get_tasks_dir, add_task, split_task, get_task
    from .tasks import _append_child_to_parent_frontmatter
    from .baseline import resolve_baseline_ref
    from .discovery import get_project
    from .worklog import add_entry
    from .models import WorkLogAction

    tasks_dir = get_tasks_dir(config, project_id)
    if not tasks_dir:
        raise EmitValidationError(f"No tasks directory for project {project_id!r}")

    # -----------------------------------------------------------------------
    # Phase 2 — Gate barrier (all read-only)
    # -----------------------------------------------------------------------

    # Resolve baseline once for the whole tree (planning baseline).
    _settings = get_project(config, project_id)
    _repo_path = getattr(_settings, "repo_path", None) if _settings else None
    baseline_ref = resolve_baseline_ref(_repo_path)

    # Predict parent/root task ID (before writing anything)
    parent_id = _predict_parent_id(doc, config, project_id)

    # Idempotent re-emit: leaves whose leaf_key already exists are skipped
    already_emitted_keys = _resolve_idempotency(
        config, project_id, parent_id, doc.leaves
    )
    leaves_to_emit = [lf for lf in doc.leaves if lf.leaf_key not in already_emitted_keys]

    # Won't-do reject-match (CLAWP-053)
    rejected = _check_reject_match(leaves_to_emit, config, project_id)
    if rejected and strict:
        raise EmitValidationError(
            f"Emission aborted (--strict): {len(rejected)} leaf(ves) matched the won't-do ledger: "
            + ", ".join(r["leaf_ref"] for r in rejected)
        )

    # Filter out rejected leaves for report-back mode
    rejected_refs = {r["leaf_ref"] for r in rejected}
    leaves_to_emit = [lf for lf in leaves_to_emit if lf.ref not in rejected_refs]

    # Constitution check (CLAWP-057 - graceful no-op if not built yet).
    # Advisory invariants (level="advisory") are surfaced for report-back but
    # must NEVER block emission - exclude them from the strict-mode blocking set.
    constitution_violations = _check_constitution(doc, config, project_id)
    blocking_violations = [
        v for v in constitution_violations if v.get("level") != "advisory"
    ]
    if blocking_violations and strict:
        raise EmitValidationError(
            f"Emission aborted (--strict): constitution violations: "
            + str(blocking_violations)
        )

    # ID collision pre-check (only needed for root-level children;
    # inner-node children start fresh so no collision risk from existing tasks).
    # Only check root-level leaves (those with parent_ref=None or whose
    # parent_ref points to another leaf — the root-level check is sufficient
    # because inner nodes are newly created in this same emit).
    root_level_leaves = [lf for lf in leaves_to_emit if lf.parent_ref is None]
    collisions = _check_id_collisions(config, project_id, parent_id, root_level_leaves)
    if collisions:
        raise EmitValidationError(
            f"Emission aborted: ID collisions detected: "
            + ", ".join(c["predicted_id"] for c in collisions)
        )

    # Dry-run exits here — all gates have fired, no writes performed
    if dry_run:
        return EmitResult(
            root_id=parent_id,
            emitted=[],
            research_id=None,
            baseline_ref=baseline_ref,
            rejected=rejected,
            constitution_violations=constitution_violations,
            dry_run=True,
        )

    # -----------------------------------------------------------------------
    # Phase 3 — Stage
    # -----------------------------------------------------------------------

    # Generate PRD research ID before staging
    research_id: str | None = None
    if doc.prd:
        prd_title_slug = doc.prd.title.lower()
        prd_title_slug = "".join(c if c.isalnum() else "-" for c in prd_title_slug)
        prd_title_slug = "-".join(filter(None, prd_title_slug.split("-")))[:40]
        research_id = f"{project_id}-research-prd-{prd_title_slug}"

    # -----------------------------------------------------------------------
    # CLAWP-064: Topological ID minting (top-down, parents before children).
    #
    # We need to know which refs have children in the emit set, so we can
    # promote those leaves to directory tasks.  We also need to mint IDs
    # level-by-level so that a child's ID can be derived from its parent's
    # already-minted ID (matching clawpm's existing PARENT-NNN-MMM convention).
    #
    # Algorithm:
    #   1. Build a map: ref -> list of children (from leaves_to_emit only)
    #   2. Topological sort via Kahn's (same as parse validation, but now using
    #      the filtered leaves_to_emit set)
    #   3. Assign task IDs in topo order: root-level leaves get IDs under
    #      parent_id; inner-leaf children get IDs under their parent's minted ID.
    # -----------------------------------------------------------------------

    # Build parent→children map within the filtered leaves_to_emit set.
    emit_refs = {lf.ref for lf in leaves_to_emit}
    children_by_ref: dict[str, list[str]] = {lf.ref: [] for lf in leaves_to_emit}
    for lf in leaves_to_emit:
        if lf.parent_ref is not None and lf.parent_ref in emit_refs:
            children_by_ref[lf.parent_ref].append(lf.ref)

    # Which refs have at least one child in the emit set (must become dir tasks)
    has_children: set[str] = {ref for ref, kids in children_by_ref.items() if kids}

    # Topological sort of leaves_to_emit
    in_degree_emit: dict[str, int] = {}
    for lf in leaves_to_emit:
        effective_parent = lf.parent_ref if lf.parent_ref in emit_refs else None
        in_degree_emit[lf.ref] = 1 if effective_parent is not None else 0

    topo_queue = [lf.ref for lf in leaves_to_emit if in_degree_emit[lf.ref] == 0]
    topo_order: list[str] = []
    while topo_queue:
        node = topo_queue.pop(0)
        topo_order.append(node)
        for child_ref in children_by_ref[node]:
            in_degree_emit[child_ref] -= 1
            if in_degree_emit[child_ref] == 0:
                topo_queue.append(child_ref)

    # ref → LeafSpec lookup
    leaf_by_ref: dict[str, LeafSpec] = {lf.ref: lf for lf in leaves_to_emit}

    # Mint IDs top-down.  For each leaf in topo order:
    #   - root-level (parent_ref=None or parent_ref not in emit set): child of parent_id
    #   - nested (parent_ref in emit set): child of parent's minted ID
    # Track per-node next ordinal separately (each new ID space starts fresh).
    leaf_id_map: dict[str, str] = {}  # ref -> minted task ID
    next_ordinal: dict[str, int] = {}  # minted-parent-id -> next ordinal

    # Seed root-level ordinal from existing children on disk.
    existing_nums = _existing_child_nums(tasks_dir, parent_id)
    next_ordinal[parent_id] = (max(existing_nums) if existing_nums else 0) + 1

    for ref in topo_order:
        lf = leaf_by_ref[ref]
        effective_parent_id = (
            leaf_id_map[lf.parent_ref]
            if lf.parent_ref is not None and lf.parent_ref in emit_refs
            else parent_id
        )
        if effective_parent_id not in next_ordinal:
            # Newly-created inner node: ordinal starts at 1 (no existing children)
            next_ordinal[effective_parent_id] = 1
        ordinal = next_ordinal[effective_parent_id]
        next_ordinal[effective_parent_id] = ordinal + 1
        leaf_id_map[ref] = f"{effective_parent_id}-{ordinal:03d}"

    # Direct children of parent_id (for root's children list + attach_to update)
    child_ids: list[str] = [
        leaf_id_map[lf.ref]
        for lf in leaves_to_emit
        if (lf.parent_ref is None or lf.parent_ref not in emit_refs)
    ]

    # Build staging directory on same FS as tasks_dir
    staging_uuid = uuid.uuid4().hex[:12]
    staging_dir = tasks_dir / f".emit-{staging_uuid}"

    try:
        staging_dir.mkdir(parents=True, exist_ok=False)

        # Stage PRD research file
        if doc.prd and research_id:
            research_dir = tasks_dir.parent / "research"
            research_dir.mkdir(parents=True, exist_ok=True)
            staging_research_dir = staging_dir / "_research"
            staging_research_dir.mkdir()
            today = date.today().isoformat()
            prd_slug = research_id.replace(f"{project_id}-research-", "")
            research_filename = f"{today}_{prd_slug}.md"
            research_content = _render_research_content(
                research_id, doc.prd, linked_task_tree=parent_id
            )
            staging_research_file = staging_research_dir / research_filename
            _atomic_write(staging_research_file, research_content)

        # Stage new root task (if not attach_to)
        if not doc.root.attach_to:
            # Stage root as a directory task (it will have children)
            staging_parent_dir = staging_dir / parent_id
            staging_parent_dir.mkdir()
            root_content = _render_task_content(
                task_id=parent_id,
                title=doc.root.title or parent_id,
                parent_id=None,
                leaf=None,
                baseline_ref=baseline_ref,
                prd_ref=research_id,
                children=child_ids,
                predictions=doc.root.predictions,
            )
            staging_parent_task = staging_parent_dir / "_task.md"
            _atomic_write(staging_parent_task, root_content)

        # -----------------------------------------------------------------------
        # Stage all leaf task files (multi-level, topo order).
        #
        # For new-root:
        #   staging_dir/<parent_id>/                   ← root dir (already staged)
        #   staging_dir/<parent_id>/<child>.md         ← direct leaf child
        #   staging_dir/<parent_id>/<child>/           ← inner-node child (has kids)
        #   staging_dir/<parent_id>/<child>/_task.md
        #   staging_dir/<parent_id>/<child>/<grandchild>.md
        #   ... and so on recursively
        #
        # For attach_to:
        #   staging_dir/_attach/<child>.md
        #   staging_dir/_attach/<child>/               ← if inner node
        #   staging_dir/_attach/<child>/_task.md
        #   staging_dir/_attach/<child>/<grandchild>.md
        # -----------------------------------------------------------------------

        # Determine the base dir inside the staging root where direct children land.
        if not doc.root.attach_to:
            staging_base = staging_dir / parent_id
        else:
            staging_base = staging_dir / "_attach"
            staging_base.mkdir()

        # Stage each leaf in topo order.  We need to know each leaf's children
        # to decide whether it becomes a directory task.
        for ref in topo_order:
            lf = leaf_by_ref[ref]
            task_id = leaf_id_map[ref]
            effective_parent_id = (
                leaf_id_map[lf.parent_ref]
                if lf.parent_ref is not None and lf.parent_ref in emit_refs
                else parent_id
            )

            # Where does this leaf's file land in the staging tree?
            # Root-level leaves land in staging_base.
            # Nested leaves land inside their parent's staging sub-directory.
            if lf.parent_ref is None or lf.parent_ref not in emit_refs:
                staging_parent_subdir = staging_base
            else:
                # Parent is another leaf — its staging dir is inside staging_base
                # (or deeper). We need to locate it by walking up.
                staging_parent_subdir = _staging_dir_for_task(
                    staging_base, leaf_id_map[lf.parent_ref], parent_id
                )

            leaf_direct_children = [
                leaf_id_map[child_ref] for child_ref in children_by_ref[ref]
            ]
            is_inner = ref in has_children

            if is_inner:
                # This leaf becomes a directory task (_task.md inside a subdir)
                leaf_subdir = staging_parent_subdir / task_id
                leaf_subdir.mkdir(parents=True, exist_ok=True)
                leaf_content = _render_task_content(
                    task_id=task_id,
                    title=lf.title,
                    parent_id=effective_parent_id,
                    leaf=lf,
                    baseline_ref=baseline_ref,
                    children=leaf_direct_children,
                )
                _atomic_write(leaf_subdir / "_task.md", leaf_content)
            else:
                # Leaf node — plain .md file
                leaf_content = _render_task_content(
                    task_id=task_id,
                    title=lf.title,
                    parent_id=effective_parent_id,
                    leaf=lf,
                    baseline_ref=baseline_ref,
                )
                staging_parent_subdir.mkdir(parents=True, exist_ok=True)
                _atomic_write(staging_parent_subdir / f"{task_id}.md", leaf_content)

        # -----------------------------------------------------------------------
        # Phase 4 — Promote (atomic rename)
        # -----------------------------------------------------------------------

        emitted_tasks: list[Task] = []

        if not doc.root.attach_to:
            # New-root: rename the whole staging parent dir into tasks_dir in one shot.
            # All levels of the hierarchy are inside staging_dir/<parent_id>/ and
            # move atomically as a single rename.
            live_parent_dir = tasks_dir / parent_id
            if live_parent_dir.exists():
                raise EmitValidationError(
                    f"Cannot promote: {live_parent_dir} already exists. "
                    "Use attach_to to add leaves to an existing task."
                )
            (staging_dir / parent_id).rename(live_parent_dir)

            # Read the promoted task files (depth-first collect) — includes root
            # _task.md and all children at every level.
            _collect_emitted_tasks(live_parent_dir, parent_id, emitted_tasks)

        else:
            # attach_to: ensure parent is a directory task.
            parent_task = get_task(config, project_id, parent_id)
            if not parent_task:
                raise EmitValidationError(
                    f"attach_to target {parent_id!r} not found in project {project_id!r}"
                )

            # Split parent to directory if not already
            if parent_task.file_path and parent_task.file_path.name != "_task.md":
                parent_task = split_task(config, project_id, parent_id)
                if not parent_task:
                    raise EmitValidationError(
                        f"Failed to split parent task {parent_id!r} to directory"
                    )

            live_parent_dir = parent_task.file_path.parent  # type: ignore[union-attr]

            # Promote each top-level child subtree atomically (file or dir rename).
            # Children land before the parent child-list is updated — crash-safe
            # ordering preserved from v1.
            for cid in child_ids:
                src_file = staging_base / f"{cid}.md"
                src_dir = staging_base / cid
                if src_dir.exists():
                    # Inner node — rename entire subtree directory
                    dst_dir = live_parent_dir / cid
                    src_dir.rename(dst_dir)
                    _collect_emitted_tasks(dst_dir, cid, emitted_tasks)
                elif src_file.exists():
                    # Leaf node — rename single file
                    dst_file = live_parent_dir / f"{cid}.md"
                    src_file.rename(dst_file)
                    from .models import Task as _Task4
                    try:
                        emitted_tasks.append(_Task4.from_file(dst_file))
                    except Exception:
                        pass

            # Last: update parent's children list (after files are in place)
            if parent_task.file_path:
                for cid in child_ids:
                    _append_child_to_parent_frontmatter(parent_task.file_path, cid)

                # Also stamp prd_ref on parent if we have one
                if research_id:
                    _stamp_prd_ref(parent_task.file_path, research_id)

        # Promote PRD research file (inside the atomic set, after task files)
        promoted_research_id: str | None = None
        if doc.prd and research_id:
            research_dir = tasks_dir.parent / "research"
            research_dir.mkdir(parents=True, exist_ok=True)
            today = date.today().isoformat()
            prd_slug = research_id.replace(f"{project_id}-research-", "")
            research_filename = f"{today}_{prd_slug}.md"
            staging_rf = staging_dir / "_research" / research_filename
            live_rf = research_dir / research_filename
            if staging_rf.exists():
                # Use replace() (not rename()) so re-emits overwrite on Windows
                # (rename raises FileExistsError when target exists on Windows)
                staging_rf.replace(live_rf)
                promoted_research_id = research_id

        # Cleanup staging dir (may still have empty dirs)
        shutil.rmtree(staging_dir, ignore_errors=True)

    except Exception:
        # Cleanup staging dir on any failure
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # -----------------------------------------------------------------------
    # Post-promotion: JSONL work_log (intentionally outside the atomic set)
    # -----------------------------------------------------------------------
    try:
        leaf_titles = ", ".join(lf.title for lf in leaves_to_emit[:3])
        if len(leaves_to_emit) > 3:
            leaf_titles += f" (+{len(leaves_to_emit) - 3} more)"
        add_entry(
            config,
            project_id,
            action=WorkLogAction.NOTE,
            task=parent_id,
            summary=(
                f"emit-tree: emitted {len(leaves_to_emit)} leaf(ves) under {parent_id}"
                + (f" — {leaf_titles}" if leaf_titles else "")
                + (f"; PRD: {promoted_research_id}" if promoted_research_id else "")
            ),
        )
    except Exception:
        # Work-log failure never blocks the result
        pass

    # Build result — emitted_tasks was populated by _collect_emitted_tasks
    # (new-root path) or by individual file reads (attach_to path).
    # new_root_task is set only for new-root; it was already added to emitted_tasks
    # via _collect_emitted_tasks, so we do NOT re-add it here.
    emitted_dicts = [t.to_dict() for t in emitted_tasks]

    return EmitResult(
        root_id=parent_id,
        emitted=emitted_dicts,
        research_id=promoted_research_id,
        baseline_ref=baseline_ref,
        rejected=rejected,
        constitution_violations=constitution_violations,
        dry_run=False,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _staging_dir_for_task(staging_base: Path, task_id: str, root_parent_id: str) -> Path:
    """Locate the staging directory for a given task_id inside staging_base.

    Used during multi-level staging to find where a nested leaf's parent
    directory lives inside the staging tree. The task_id is always of the form
    <root>-NNN or <root>-NNN-MMM[-...], so we can reconstruct the path by
    walking the ID segments and resolving each directory level inside
    staging_base.

    Examples (staging_base = .emit-xxx/<root_parent_id>/):
      task_id = ROOT-001       -> staging_base/ROOT-001/
      task_id = ROOT-001-002   -> staging_base/ROOT-001/ROOT-001-002/
    """
    # The task_id is relative to root_parent_id. All intermediate directories
    # nest directly — e.g. ROOT-001-002 lives at staging_base/ROOT-001/ROOT-001-002/.
    # We derive the path by stripping successively shorter suffixes.
    segments = task_id.split("-")
    # root_parent_id itself is NOT inside staging_base as a directory —
    # staging_base IS the root_parent_id directory.
    # We need to figure out: how many levels deep is task_id from staging_base?
    # strategy: walk the ancestor chain from task_id up to (but not including) root_parent_id.
    ancestors: list[str] = []
    current = task_id
    while True:
        # Strip the last ordinal segment to get the parent ID
        parts = current.rsplit("-", 1)
        if len(parts) < 2:
            break
        parent = parts[0]
        if parent == root_parent_id:
            # task_id is a direct child of root_parent_id
            break
        ancestors.append(parent)
        current = parent

    # ancestors is in child-to-parent order; reverse to get root-to-child
    ancestors.reverse()
    # Build the path: staging_base / ancestor1 / ancestor2 / ... / task_id
    path = staging_base
    for anc in ancestors:
        path = path / anc
    return path / task_id


def _collect_emitted_tasks(live_dir: Path, task_id: str, out: list) -> None:
    """Recursively collect all Task objects from a promoted directory tree.

    Reads _task.md files (for directory tasks) and *.md leaf files (excluding
    _task.md) at every level beneath live_dir, appending Task objects to out.
    """
    from .models import Task as _TaskC

    # Read the _task.md at this level if present
    task_file = live_dir / "_task.md"
    if task_file.exists():
        try:
            out.append(_TaskC.from_file(task_file))
        except Exception:
            pass

    # Recurse into subdirectories (direct children that became dir tasks)
    for entry in live_dir.iterdir():
        if entry.is_dir():
            _collect_emitted_tasks(entry, entry.name, out)
        elif entry.is_file() and entry.name != "_task.md" and entry.suffix == ".md":
            try:
                out.append(_TaskC.from_file(entry))
            except Exception:
                pass


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path via atomic tmp → rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def _stamp_prd_ref(task_file: Path, prd_ref: str) -> None:
    """Atomically add prd_ref to an existing task's frontmatter."""
    if not task_file.exists():
        return
    text = task_file.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return
    parts = text.split("---", 2)
    if len(parts) < 3:
        return
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return
    if fm.get("prd_ref") == prd_ref:
        return  # idempotent
    fm["prd_ref"] = prd_ref
    body = parts[2].lstrip("\n")
    new_text = (
        "---\n"
        + yaml.dump(fm, default_flow_style=False, allow_unicode=True).strip()
        + "\n---\n"
        + body
    )
    _atomic_write(task_file, new_text)
