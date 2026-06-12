"""CLAWP-057 -- Constitution / governing-principles layer.

A thin, optional set of named, project-scoped INVARIANTS that constrain what
the emission API (CLAWP-056) persists.

Design contracts:
- validate() returns [] when no constitution file exists (exact no-op).
- CODE-CHECKABLE invariant kinds are enforced deterministically here.
- advisory invariants are STORED + returned as info-level (never auto-blocked).
- Any error loading or checking the constitution is fail-open (returns []).

Constitution file location: <project>/.project/constitution.yaml

File schema::

  invariants:
    - name: <str>            # unique identifier
      kind: <str>            # built-in kind or "advisory"
      description: <str>    # optional human description
      params: {}             # kind-specific params (optional)

Built-in code-checkable kinds
------------------------------
require_success_criteria
    Every leaf must have >=1 success_criterion.

max_complexity
    No leaf may exceed params.max complexity tier (s < m < l < xl).

require_scope
    Every leaf must declare >=1 scope glob.

Advisory invariants
-------------------
Stored + surfaced as level="advisory". Core never auto-blocks on them;
a separate planner skill is responsible for judging advisory conditions.

Suggested invariants (seed -- NOT active by default)
-----------------------------------------------------
The following are documented here for reference. Add them explicitly via
`clawpm constitution add` to activate in a project.

  name: root_cause_over_bandaid
  kind: advisory
  description: Fix root causes; do not paper over symptoms with bandaids.

  name: centralise_over_duplicate
  kind: advisory
  description: Centralise logic; do not duplicate code or configuration.

  name: extend_over_create
  kind: advisory
  description: Extend existing abstractions rather than creating new ones.

  name: architecture_first
  kind: advisory
  description: Design the architecture before writing implementation code.

  name: progressive_repo_conditioning
  kind: advisory
  description: Leave the repo cleaner after every change than you found it.

  name: no_unjustified_hardcoded_constants
  kind: advisory
  description: Every constant must have a named reason or be configurable.

  name: doc_freshness_is_part_of_done
  kind: advisory
  description: A task is not done until its documentation is up to date.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import PortfolioConfig, TaskComplexity

# Type-only imports to avoid circular import at runtime.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .emit_tree import EmitTreeDocument, LeafSpec


# ---------------------------------------------------------------------------
# Complexity tier ordering (s < m < l < xl)
# ---------------------------------------------------------------------------

_COMPLEXITY_ORDER: dict[str, int] = {
    TaskComplexity.S.value: 0,
    TaskComplexity.M.value: 1,
    TaskComplexity.L.value: 2,
    TaskComplexity.XL.value: 3,
}

# All recognised invariant kinds ("advisory" is not code-checkable).
_CODE_CHECKABLE_KINDS = frozenset({"require_success_criteria", "max_complexity", "require_scope"})
_ADVISORY_KIND = "advisory"
_ALL_KINDS = _CODE_CHECKABLE_KINDS | {_ADVISORY_KIND}


# ---------------------------------------------------------------------------
# Internal check helpers
# ---------------------------------------------------------------------------


def _check_require_success_criteria(
    inv: dict,
    leaves: "list[LeafSpec]",
) -> list[dict]:
    """Every leaf must have >=1 success_criterion."""
    violations: list[dict] = []
    for lf in leaves:
        if not lf.success_criteria:
            violations.append({
                "invariant": inv["name"],
                "leaf_ref": lf.ref,
                "reason": (
                    f"Leaf '{lf.ref}' has no success_criteria "
                    f"(invariant: {inv['name']!r})"
                ),
            })
    return violations


def _check_max_complexity(
    inv: dict,
    leaves: "list[LeafSpec]",
) -> list[dict]:
    """No leaf may exceed the configured maximum complexity tier."""
    params = inv.get("params") or {}
    max_label = params.get("max", "xl")
    max_ord = _COMPLEXITY_ORDER.get(max_label)
    if max_ord is None:
        # Unknown tier -- fail-open on a misconfigured invariant.
        return []

    violations: list[dict] = []
    for lf in leaves:
        if lf.predictions and lf.predictions.complexity is not None:
            leaf_label = lf.predictions.complexity.value
            leaf_ord = _COMPLEXITY_ORDER.get(leaf_label, -1)
            if leaf_ord > max_ord:
                violations.append({
                    "invariant": inv["name"],
                    "leaf_ref": lf.ref,
                    "reason": (
                        f"Leaf '{lf.ref}' has complexity '{leaf_label}' which exceeds "
                        f"the maximum allowed '{max_label}' (invariant: {inv['name']!r})"
                    ),
                })
    return violations


def _check_require_scope(
    inv: dict,
    leaves: "list[LeafSpec]",
) -> list[dict]:
    """Every leaf must declare >=1 scope glob."""
    violations: list[dict] = []
    for lf in leaves:
        if not lf.scope:
            violations.append({
                "invariant": inv["name"],
                "leaf_ref": lf.ref,
                "reason": (
                    f"Leaf '{lf.ref}' has no scope declared "
                    f"(invariant: {inv['name']!r})"
                ),
            })
    return violations


def _check_advisory(inv: dict) -> list[dict]:
    """Return a single info-level entry for each advisory invariant.

    Advisory invariants are stored + surfaced but NOT auto-judged by core.
    """
    return [{
        "invariant": inv["name"],
        "leaf_ref": None,
        "level": "advisory",
        "reason": (
            inv.get("description")
            or f"Advisory invariant '{inv['name']}' is active"
        ),
    }]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def validate(
    config: PortfolioConfig,
    project_id: str,
    doc: "EmitTreeDocument",
) -> list[dict]:
    """Check *doc* against the project constitution.

    Returns a list of violation dicts (empty = no violations / no constitution).
    Each dict has at minimum: invariant (str), leaf_ref (str | None), reason (str).
    Advisory violations also carry level="advisory".

    Always fail-open:
    - No constitution file       -> []
    - Malformed YAML / IO error  -> []
    - Unknown invariant kind     -> silently skipped (no block)
    - Any per-invariant error    -> silently skipped (no block)
    """
    try:
        invariants = _load_invariants(config, project_id)
    except Exception:
        return []

    if invariants is None:
        return []

    violations: list[dict] = []
    leaves = doc.leaves

    for inv in invariants:
        if not isinstance(inv, dict):
            continue
        kind = inv.get("kind", "")
        try:
            if kind == "require_success_criteria":
                violations.extend(_check_require_success_criteria(inv, leaves))
            elif kind == "max_complexity":
                violations.extend(_check_max_complexity(inv, leaves))
            elif kind == "require_scope":
                violations.extend(_check_require_scope(inv, leaves))
            elif kind == _ADVISORY_KIND:
                violations.extend(_check_advisory(inv))
            # else: unknown kind -- silently skip (fail-open)
        except Exception:
            # Per-invariant check error -- fail-open.
            continue

    return violations


# ---------------------------------------------------------------------------
# File I/O helpers (used by CLI)
# ---------------------------------------------------------------------------


def _load_invariants(config: PortfolioConfig, project_id: str) -> list[dict] | None:
    """Load the invariants list from .project/constitution.yaml for *project_id*.

    Returns None when the file does not exist.
    Returns [] when the file exists but declares no invariants.
    Raises on YAML parse failure or IO error (callers must handle).
    """
    from .discovery import get_project_dir

    project_dir = get_project_dir(config, project_id)
    if not project_dir:
        return None

    const_file = project_dir / "constitution.yaml"
    if not const_file.exists():
        return None

    data = yaml.safe_load(const_file.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    return data.get("invariants") or []


def get_constitution_file(config: PortfolioConfig, project_id: str) -> Path | None:
    """Return the path to the constitution.yaml file (may not exist)."""
    from .discovery import get_project_dir

    project_dir = get_project_dir(config, project_id)
    if not project_dir:
        return None
    return project_dir / "constitution.yaml"


def load_constitution(config: PortfolioConfig, project_id: str) -> dict:
    """Load the full constitution document, returning {invariants: []} when absent."""
    const_file = get_constitution_file(config, project_id)
    if not const_file or not const_file.exists():
        return {"invariants": []}
    try:
        data = yaml.safe_load(const_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"invariants": []}
        data.setdefault("invariants", [])
        return data
    except Exception:
        return {"invariants": []}


def save_constitution(config: PortfolioConfig, project_id: str, document: dict) -> None:
    """Persist the constitution document to .project/constitution.yaml."""
    const_file = get_constitution_file(config, project_id)
    if not const_file:
        raise ValueError(
            f"Cannot resolve constitution file path for project {project_id!r}"
        )
    const_file.write_text(
        yaml.dump(document, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def add_invariant(
    config: PortfolioConfig,
    project_id: str,
    *,
    name: str,
    kind: str,
    description: str | None = None,
    params: dict | None = None,
) -> dict:
    """Add (or replace) a named invariant in the project constitution.

    Idempotent: if an invariant with *name* already exists it is replaced;
    no duplicate is added.

    Returns the invariant dict that was written.
    """
    doc = load_constitution(config, project_id)
    inv: dict[str, Any] = {"name": name, "kind": kind}
    if description:
        inv["description"] = description
    if params:
        inv["params"] = params

    # Replace existing entry with same name, then append.
    existing = [i for i in doc["invariants"] if i.get("name") != name]
    existing.append(inv)
    doc["invariants"] = existing
    save_constitution(config, project_id, doc)
    return inv


def remove_invariant(
    config: PortfolioConfig,
    project_id: str,
    name: str,
) -> bool:
    """Remove the named invariant from the project constitution.

    Returns True if it was present, False if already absent (idempotent).
    """
    doc = load_constitution(config, project_id)
    before = len(doc["invariants"])
    doc["invariants"] = [i for i in doc["invariants"] if i.get("name") != name]
    removed = len(doc["invariants"]) < before
    save_constitution(config, project_id, doc)
    return removed
