"""Auto-remediation arms for ``clawpm doctor --apply`` (CLAWP-026).

Each class of warning surfaced by ``clawpm doctor`` either has a deterministic
auto-remediation arm here OR is explicitly documented as operator-judgment and
skipped.

Auto-applyable arms:
- ``drift_tasks`` half-rename — when both ``PROJ-001.md`` and
  ``PROJ-001.progress.md`` exist at the tasks root, the canonical state is
  "progress"; remove the bare ``.md`` and keep ``.progress.md``.
- ``drift_tasks`` state-field mismatch — frontmatter ``state:`` field disagrees
  with file location; file location wins, rewrite frontmatter.
- ``stale_blocked`` — call :func:`cascade_unblock_dependents` for each
  completed dependency to promote the still-blocked task.

NOT auto-applyable (documented & skipped):
- ``stale_tasks`` — needs operator judgment (was the task abandoned, blocked,
  or just neglected?).
- ``prefix_collisions`` — would require renaming a project, operator consent
  needed.
- ``unreadable_files`` — encoding fix needs operator review of the actual
  bytes (cp1252 vs latin-1 vs binary).
- ``commit_drift`` — operator must run ``clawpm log commit`` with intent;
  fabricating a log entry would be dishonest.
- ``missing_markers`` — agent docs may have hand-curated structure; running
  announce is a separate command the operator should invoke explicitly.
- ``codex_availability`` — network-backed observation, no local fix.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .frontmatter import FrontmatterError, split_frontmatter
from .tasks import cascade_unblock_dependents


# Sentinel values for the doctor JSON output's `apply_skipped[].reason`.
SKIP_REASONS = {
    "stale_tasks": (
        "operator-judgment: was the task abandoned, blocked, or just neglected? "
        "Move to done/blocked or update manually."
    ),
    "prefix_collisions": (
        "operator-judgment: renaming a project ID is a deliberate operator action; "
        "auto-rename could break cross-project references."
    ),
    "unreadable_files": (
        "operator-judgment: encoding issues (cp1252 vs latin-1 vs binary) need "
        "human review of the bytes before a safe transcode."
    ),
    "commit_drift": (
        "operator-action: run `clawpm log commit` to capture the work. "
        "Auto-generating log entries would fabricate intent."
    ),
    "missing_markers": (
        "operator-action: run `clawpm project announce --project <id>` from "
        "the repo so the marker block is placed deliberately in CLAUDE.md/AGENTS.md/README.md."
    ),
    "codex_availability": (
        "observation-only: no local fix; check the GitHub Codex app installation."
    ),
}


def _rewrite_frontmatter_state(file_path: Path, new_state: str) -> None:
    """Atomically rewrite the frontmatter ``state:`` field of ``file_path``.

    Preserves all other frontmatter fields, body content, and trailing whitespace.
    Uses the tmp + replace pattern from :func:`clawpm.tasks.add_task`.
    """
    text = file_path.read_text(encoding="utf-8")
    try:
        fm, body = split_frontmatter(text)
    except FrontmatterError as exc:
        if exc.reason == "absent":
            # No frontmatter to rewrite — synthesize a minimal one.
            new_text = f"---\nstate: {new_state}\n---\n\n{text}"
        elif exc.reason == "unterminated":
            # Malformed; refuse to silently break the file.
            raise ValueError(f"malformed frontmatter in {file_path}") from None
        elif exc.reason == "unparseable":
            # Preserve the raw yaml.YAMLError split attached as __cause__ (this
            # site never wrapped it); fall back to the FrontmatterError itself
            # if a caller ever constructed one without a chained cause.
            raise (exc.__cause__ or exc) from None
        else:
            raise
    else:
        fm["state"] = new_state
        new_fm_text = yaml.safe_dump(fm, default_flow_style=False, sort_keys=False)
        new_text = f"---\n{new_fm_text}---{body}"

    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    try:
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(file_path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def apply_drift(drift_entry: dict, *, dry_run: bool = False) -> dict:
    """Remediate one ``drift_tasks`` entry.

    Two sub-cases:

    1. ``issue == "half_rename"``: both ``PROJ-001.md`` and
       ``PROJ-001.progress.md`` exist. Delete the bare ``.md`` (progress state
       wins).
    2. ``issue == "state_mismatch"``: ``frontmatter_state`` != ``location_state``.
       Rewrite the frontmatter to match the location.
    """
    file_str = drift_entry.get("file")
    if not file_str:
        return {
            "class": "drift_tasks",
            "target": None,
            "result": "skipped: no file path in drift entry",
        }
    file_path = Path(file_str)
    issue = drift_entry.get("issue")

    if issue == "half_rename":
        # The `file` field already points at the bare .md (the doctor records
        # tasks_dir/half which is the non-progress sibling). Delete it.
        if not file_path.exists():
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": "skipped: file no longer exists (already remediated?)",
            }
        if dry_run:
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": "would-delete (half-rename; progress sibling wins)",
            }
        try:
            file_path.unlink()
        except OSError as exc:
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": f"error: {exc}",
            }
        return {
            "class": "drift_tasks",
            "target": file_path.as_posix(),
            "result": "deleted (half-rename; progress sibling kept)",
        }

    if issue == "state_mismatch":
        location_state = drift_entry.get("location_state")
        if not location_state:
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": "skipped: location_state missing from drift entry",
            }
        if not file_path.exists():
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": "skipped: file no longer exists",
            }
        if dry_run:
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": (
                    f"would-rewrite frontmatter state -> {location_state} "
                    f"(was {drift_entry.get('frontmatter_state')!r})"
                ),
            }
        try:
            _rewrite_frontmatter_state(file_path, location_state)
        except Exception as exc:
            return {
                "class": "drift_tasks",
                "target": file_path.as_posix(),
                "result": f"error: {type(exc).__name__}: {exc}",
            }
        return {
            "class": "drift_tasks",
            "target": file_path.as_posix(),
            "result": f"rewrote frontmatter state -> {location_state}",
        }

    return {
        "class": "drift_tasks",
        "target": file_path.as_posix(),
        "result": f"skipped: unknown drift issue {issue!r}",
    }


def apply_stale_blocked(stale_entry: dict, config: Any, *, dry_run: bool = False) -> dict:
    """Remediate one ``stale_blocked`` entry by running cascade for its deps.

    A task lands in ``stale_blocked`` when all its deps are DONE but it's still
    in ``blocked/``. The deterministic fix is the same one the live transition
    path runs: :func:`cascade_unblock_dependents` with each completed dep as
    trigger. Running it once per dep is idempotent — the second pass finds the
    task already promoted and no-ops.
    """
    task_id = stale_entry.get("task_id")
    project_id = stale_entry.get("project_id")
    deps = stale_entry.get("deps") or []

    if not task_id or not project_id:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": "skipped: missing task_id or project_id",
        }
    if not deps:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": "skipped: no deps recorded",
        }

    if dry_run:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": f"would-cascade via deps {deps} (project {project_id})",
        }

    transitions: list[dict] = []
    try:
        for dep_id in deps:
            transitions.extend(
                cascade_unblock_dependents(config, project_id, dep_id)
            )
    except Exception as exc:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": f"error: {type(exc).__name__}: {exc}",
        }

    promoted_ids = [t["task_id"] for t in transitions]
    if task_id in promoted_ids:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": f"promoted blocked -> open (trigger deps={deps})",
        }
    if transitions:
        return {
            "class": "stale_blocked",
            "target": task_id,
            "result": (
                f"cascade ran but did not promote {task_id}; "
                f"other transitions={promoted_ids}"
            ),
        }
    return {
        "class": "stale_blocked",
        "target": task_id,
        "result": "no-op: cascade found no eligible promotions (deps still unsatisfied?)",
    }


def run_apply_phase(
    *,
    config: Any,
    drift_tasks: list[dict],
    stale_blocked: list[dict],
    stale_tasks: list[dict],
    prefix_collisions: list[dict],
    unreadable_files: list[dict],
    commit_drift: list[dict],
    missing_markers: list[dict],
    codex_availability: list[dict],
    apply_drift_flag: bool = True,
    apply_cascade_flag: bool = True,
    apply_stale_blocked_flag: bool = True,
    apply_half_rename_flag: bool = True,
    dry_run: bool = False,
) -> tuple[list[dict], list[dict]]:
    """Run the apply phase over collected doctor warnings.

    Returns ``(applied, apply_skipped)``:

    - ``applied`` — one entry per remediation actually attempted, with the
      shape ``{class, target, result}``. Populated identically in dry-run mode
      except the filesystem is not touched and ``result`` is prefixed with
      ``would-``.
    - ``apply_skipped`` — one entry per warning that is **not** auto-applyable,
      with ``{class, target, reason}``. Surface so the operator can see what
      ``--apply`` consciously left untouched.

    The ``--no-apply-*`` flags map to the boolean kwargs above. Disabling
    ``apply_half_rename_flag`` filters half_rename drift entries but leaves
    state_mismatch entries alone (and vice versa for ``apply_drift_flag``,
    which governs state_mismatch). ``apply_cascade_flag`` and
    ``apply_stale_blocked_flag`` are siblings — either disables the
    cascade arm.
    """
    applied: list[dict] = []
    apply_skipped: list[dict] = []

    # --- drift_tasks ---
    for d in drift_tasks:
        issue = d.get("issue")
        if issue == "half_rename":
            if not apply_half_rename_flag:
                apply_skipped.append({
                    "class": "drift_tasks",
                    "target": d.get("file"),
                    "reason": "disabled by --no-apply-half-rename",
                })
                continue
            applied.append(apply_drift(d, dry_run=dry_run))
        elif issue == "state_mismatch":
            if not apply_drift_flag:
                apply_skipped.append({
                    "class": "drift_tasks",
                    "target": d.get("file"),
                    "reason": "disabled by --no-apply-drift",
                })
                continue
            applied.append(apply_drift(d, dry_run=dry_run))
        else:
            apply_skipped.append({
                "class": "drift_tasks",
                "target": d.get("file"),
                "reason": f"unknown drift issue {issue!r}",
            })

    # --- stale_blocked ---
    for sb in stale_blocked:
        if not (apply_cascade_flag and apply_stale_blocked_flag):
            apply_skipped.append({
                "class": "stale_blocked",
                "target": sb.get("task_id"),
                "reason": "disabled by --no-apply-cascade or --no-apply-stale-blocked",
            })
            continue
        applied.append(apply_stale_blocked(sb, config, dry_run=dry_run))

    # --- non-applyable classes ---
    for st in stale_tasks:
        apply_skipped.append({
            "class": "stale_tasks",
            "target": st.get("task_id"),
            "reason": SKIP_REASONS["stale_tasks"],
        })
    for pc in prefix_collisions:
        apply_skipped.append({
            "class": "prefix_collisions",
            "target": pc.get("prefix"),
            "reason": SKIP_REASONS["prefix_collisions"],
        })
    for uf in unreadable_files:
        apply_skipped.append({
            "class": "unreadable_files",
            "target": uf.get("file"),
            "reason": SKIP_REASONS["unreadable_files"],
        })
    for cd in commit_drift:
        apply_skipped.append({
            "class": "commit_drift",
            "target": cd.get("project_id"),
            "reason": SKIP_REASONS["commit_drift"],
        })
    for mm in missing_markers:
        apply_skipped.append({
            "class": "missing_markers",
            "target": mm.get("project_id"),
            "reason": SKIP_REASONS["missing_markers"],
        })
    for ca in codex_availability:
        apply_skipped.append({
            "class": "codex_availability",
            "target": ca.get("project_id"),
            "reason": SKIP_REASONS["codex_availability"],
        })

    return applied, apply_skipped
