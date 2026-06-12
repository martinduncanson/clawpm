"""CLAWP-055 — Per-task baseline_ref stamping and pre-dispatch drift detection.

Provides a small provider abstraction for resolving an opaque baseline marker:
  - git_short_sha(repo_path)  → 7-char hex SHA, or None if not a git repo
  - timestamp_marker()        → "ts:<ISO8601-UTC>" fallback for non-git repos
  - resolve_baseline_ref(repo_path) → picks the right provider; always returns str

And the scope-drift check used by the dispatch gate:
  - detect_scope_drift(repo_path, scope, baseline_ref)
    → {"status": "clean"|"drifted"|"skipped", ...}

Design notes:
  - The baseline_ref is OPAQUE to clawpm itself.  The git provider stores the
    short SHA; the non-git provider stores a UTC timestamp string.  Both are
    stored verbatim in task frontmatter as a plain string.
  - drift detection is always against the PROJECT repo (repo_path from
    ProjectSettings), never against clawpm's own repo.
  - reuses the same subprocess.run / git plumbing style as doctor's
    commit-drift check so the patterns are consistent and testable.
"""

from __future__ import annotations

import fnmatch
import subprocess
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Provider: git short SHA
# ---------------------------------------------------------------------------

def git_short_sha(repo_path: Path) -> str | None:
    """Return the current HEAD short SHA for ``repo_path``, or None if not a git repo.

    Uses ``git rev-parse --short HEAD`` — the same idiom doctor uses for
    commit counting.  A non-zero return code (detached HEAD, empty repo, no
    .git) silently returns None so callers can fall through to the timestamp
    fallback.
    """
    if not repo_path or not repo_path.exists():
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha if sha else None


# ---------------------------------------------------------------------------
# Provider: timestamp marker (non-git fallback)
# ---------------------------------------------------------------------------

def timestamp_marker() -> str:
    """Return a UTC timestamp marker: ``ts:<ISO8601>``."""
    now = datetime.now(timezone.utc)
    return f"ts:{now.isoformat()}"


# ---------------------------------------------------------------------------
# Resolver: pick the right provider
# ---------------------------------------------------------------------------

def resolve_baseline_ref(repo_path: Path | None) -> str:
    """Return a baseline_ref string for the given repo.

    - If ``repo_path`` is a git repo: returns the HEAD short SHA.
    - Otherwise (no repo_path, not a git dir, empty repo): returns a
      ``ts:<ISO>`` timestamp marker.

    Always returns a non-empty string — callers can store it unconditionally.
    """
    if repo_path is not None:
        sha = git_short_sha(repo_path)
        if sha:
            return sha
    return timestamp_marker()


# ---------------------------------------------------------------------------
# Drift detector
# ---------------------------------------------------------------------------

def detect_scope_drift(
    repo_path: Path | None,
    scope: list[str],
    baseline_ref: str | None,
) -> dict:
    """Check whether any in-scope files have changed since ``baseline_ref``.

    Returns a dict with:
      ``status``:        "clean" | "drifted" | "skipped"
      ``reason``:        human-readable explanation (present when "skipped")
      ``changed_files``: list[str] of matched changed files (when "drifted")
      ``baseline_ref``:  the ref that was checked against

    Skipped when:
      - scope is empty (no paths to check — cannot determine drift)
      - baseline_ref is None (legacy task, no baseline recorded)
      - baseline_ref starts with "ts:" (timestamp marker, non-git baseline —
        we can't diff a timestamp against git history reliably)
      - repo_path is not a git repo (no git history to diff)

    On any subprocess failure the check degrades to "skipped" (fail-open)
    so a git outage never blocks dispatch.
    """
    # Guard: no scope defined
    if not scope:
        return {
            "status": "skipped",
            "skip_class": "expected",
            "reason": "no scope defined — cannot determine path drift",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    # Guard: no baseline recorded (legacy task)
    if not baseline_ref:
        return {
            "status": "skipped",
            "skip_class": "expected",
            "reason": "no baseline_ref on task (legacy task — skipping drift check)",
            "baseline_ref": None,
            "changed_files": [],
        }

    # Guard: timestamp-only baseline — cannot git-diff a timestamp
    if baseline_ref.startswith("ts:"):
        return {
            "status": "skipped",
            "skip_class": "expected",
            "reason": "baseline_ref is a timestamp marker (non-git project) — skipping git diff",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    # Guard: repo_path must be a git repo
    if not repo_path or not repo_path.exists():
        return {
            "status": "skipped",
            "skip_class": "expected",
            "reason": "repo_path is absent — cannot check drift",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    if not (repo_path / ".git").exists():
        return {
            "status": "skipped",
            "skip_class": "expected",
            "reason": "repo_path is not a git repository — skipping drift check",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    # Validate that baseline_ref resolves in the repo before running the diff.
    # An unknown ref (e.g. force-pushed away) degrades to "skipped" rather
    # than crashing — classified as ERROR because the check wanted to run.
    try:
        chk = subprocess.run(
            ["git", "rev-parse", "--verify", baseline_ref],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "skipped",
            "skip_class": "error",
            "reason": f"git rev-parse failed for baseline_ref={baseline_ref!r}",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }
    if chk.returncode != 0:
        return {
            "status": "skipped",
            "skip_class": "error",
            "reason": f"baseline_ref={baseline_ref!r} not found in repo — may have been force-pushed away",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    # Get the list of files changed since baseline_ref.
    # ``git diff --name-only <ref> HEAD`` is the canonical diff-range for
    # "what changed since this commit" — same approach as doctor's commit-drift
    # check but requesting file names rather than a count.
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", baseline_ref, "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {
            "status": "skipped",
            "skip_class": "error",
            "reason": "git diff subprocess failed — degrading to skipped (fail-open)",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }
    if result.returncode != 0:
        return {
            "status": "skipped",
            "skip_class": "error",
            "reason": f"git diff returned exit code {result.returncode} — skipping",
            "baseline_ref": baseline_ref,
            "changed_files": [],
        }

    all_changed = [line.strip() for line in result.stdout.splitlines() if line.strip()]

    # Filter to files that match any of the scope globs.
    # fnmatch.fnmatch matches against the filename only for single-level globs
    # and uses the full relative path for path-shaped globs — consistent with
    # how clawpm's scope field is documented (file globs).
    matched: list[str] = []
    for fpath in all_changed:
        fname = Path(fpath).name
        for glob in scope:
            if fnmatch.fnmatch(fpath, glob) or fnmatch.fnmatch(fname, glob):
                matched.append(fpath)
                break  # one glob match is enough

    if matched:
        return {
            "status": "drifted",
            "baseline_ref": baseline_ref,
            "changed_files": matched,
            "all_changed_files": all_changed,
        }

    return {
        "status": "clean",
        "baseline_ref": baseline_ref,
        "changed_files": [],
    }
