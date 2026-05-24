"""CodeGraph integration helpers (CLAWP-027..031).

Thin wrapper around the `codegraph` CLI (https://github.com/colbymchenry/codegraph)
for clawpm's integration points. Every helper degrades gracefully when
codegraph isn't installed or the project isn't indexed — clawpm continues
to work, the operator just doesn't get the augmentation.

Module-level guarantees:
  - No exceptions propagate out of the public helpers; on any failure
    they return empty/None and let the caller carry on.
  - Subprocess timeout caps every call so a hung codegraph can't lock
    up a clawpm command. Default ceiling 5s.
  - All paths are validated before composition: codegraph commands take
    a project path argument, and we feed it ``str(project_path)`` only
    after resolving — never raw operator input.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional


DEFAULT_TIMEOUT = 5  # seconds; codegraph operations should be fast


def is_codegraph_available() -> bool:
    """True iff the ``codegraph`` binary is resolvable on PATH or via the
    standard Windows install location (``%LOCALAPPDATA%/codegraph/current/bin``).

    Returns False rather than raising — every caller treats codegraph
    augmentation as optional.
    """
    if shutil.which("codegraph") is not None:
        return True
    if shutil.which("codegraph.cmd") is not None:
        return True
    # Windows fallback: the PowerShell installer puts the binary in
    # %LOCALAPPDATA%/codegraph/current/bin/codegraph.cmd. PATH update
    # requires a terminal restart, so resolve directly.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "codegraph" / "current" / "bin" / "codegraph.cmd"
        if candidate.exists():
            return True
    return False


def _resolve_codegraph_cmd() -> Optional[list[str]]:
    """Return the argv-form invocation for `codegraph`, or None if absent."""
    for name in ("codegraph", "codegraph.cmd"):
        path = shutil.which(name)
        if path:
            return [path]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidate = Path(local) / "codegraph" / "current" / "bin" / "codegraph.cmd"
        if candidate.exists():
            return [str(candidate)]
    return None


def is_project_indexed(project_path: Path) -> bool:
    """True iff the project has a ``.codegraph/`` directory."""
    try:
        return (project_path / ".codegraph").exists()
    except OSError:
        return False


def _run_codegraph(
    args: list[str],
    cwd: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Run a codegraph subcommand, return stdout or None on any failure.

    Pure best-effort: timeouts, missing binary, non-zero exits all map
    to None. Callers decide what to do with empty output.
    """
    cmd = _resolve_codegraph_cmd()
    if cmd is None:
        return None
    try:
        result = subprocess.run(
            cmd + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def suggest_scope_from_text(
    text: str,
    repo_path: Path,
    *,
    max_globs: int = 5,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[str]:
    """Suggest files_scope globs for a task description (CLAWP-027).

    Strategy: run ``codegraph context <text>`` and parse the file paths
    it surfaces. Convert directory hits to ``<dir>/**`` globs, dedupe,
    and cap at ``max_globs``. Returns [] on any failure.

    The repo_path argument scopes the subprocess to the right project;
    text is passed as a single positional argument (codegraph's
    ``context`` command takes a free-text task).
    """
    if not is_project_indexed(repo_path):
        return []
    out = _run_codegraph(["context", text], cwd=repo_path, timeout=timeout)
    if not out:
        return []
    return _parse_file_paths_to_globs(out, repo_path, max_globs)


def search_symbols(
    query: str,
    repo_path: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> set[str]:
    """Resolve a symbol set for a query via ``codegraph query`` (CLAWP-030).

    Returns the set of symbol names codegraph reports. Used by
    reference-task scoring as a semantic-overlap signal.
    """
    if not is_project_indexed(repo_path):
        return set()
    out = _run_codegraph(["query", query], cwd=repo_path, timeout=timeout)
    if not out:
        return set()
    return _parse_symbol_names(out)


def context_brief(
    task_or_scope: str,
    repo_path: Path,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_chars: int = 2000,
) -> str:
    """Return the markdown context block for a task or scope (CLAWP-028).

    Used by ``clawpm resume`` to prepend code-level orientation to the
    session briefing. Truncated to ``max_chars`` so we don't blow the
    judge's prompt budget.
    """
    if not is_project_indexed(repo_path):
        return ""
    out = _run_codegraph(["context", task_or_scope], cwd=repo_path, timeout=timeout)
    if not out:
        return ""
    return out[:max_chars]


def init_in_worktree(worktree_path: Path, *, timeout: int = 60) -> bool:
    """Run ``codegraph init`` + ``codegraph index`` in a fresh worktree
    so a dispatched subagent has the index from turn one (CLAWP-029).

    Returns True on success, False on any failure (graceful skip).
    Longer timeout because indexing scales with file count.
    """
    if not is_codegraph_available():
        return False
    init_out = _run_codegraph(["init"], cwd=worktree_path, timeout=timeout)
    if init_out is None:
        return False
    index_out = _run_codegraph(["index"], cwd=worktree_path, timeout=timeout)
    return index_out is not None


# ---------------------------------------------------------------------------
# Parsing helpers — codegraph output is markdown with embedded file paths
# and symbol names. We don't try to be clever; conservative regex
# extraction is enough for the integration use cases.
# ---------------------------------------------------------------------------


# Codex PR#9 round-1 P1 fix: the prior regex started matching at the
# first `src|lib|...` token anywhere in the string, so a monorepo path
# like `apps/web/src/main.ts` got truncated to `src/main.ts`. The new
# pattern anchors at a non-path-character boundary (whitespace,
# backtick, punctuation, line start) and walks the FULL relative path
# — 1-6 directory segments ending in a known code extension. The
# extension whitelist constrains arbitrary operator-input strings.
_PATH_RE = re.compile(
    r"(?<![\w/.-])"
    r"((?:[\w.-]+/){1,6}"
    r"[\w.-]+\.(?:py|ts|tsx|js|jsx|go|rs|java|cs|rb|php|cpp|hpp|c|h|swift|kt|dart|lua|svelte|liquid|pas))"
    r"(?!\w)"
)

_SYMBOL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")


def _parse_file_paths_to_globs(
    text: str, repo_path: Path, max_globs: int
) -> list[str]:
    """Extract file paths from codegraph output and convert to globs.

    Single-file hits become ``dir/file.ext`` (exact). Multiple files in
    the same directory roll up to ``dir/**`` to reduce glob count.

    Codex PR#9 round-1 P2 fix: dedupe via ``dict.fromkeys`` rather than
    ``set`` so iteration order is deterministic (insertion order from
    the regex pass). This makes ``suggested_scope`` stable across runs
    and ensures ``max_globs`` truncation drops the SAME globs each
    invocation — important for caches and snapshot tests.
    """
    # Preserve first-match order; dedupe by path string.
    paths = list(dict.fromkeys(_PATH_RE.findall(text)))
    if not paths:
        return []

    # Group by parent dir, preserving the order parents first appeared.
    by_parent: dict[str, list[str]] = {}
    for p in paths:
        parent = "/".join(p.split("/")[:-1])
        by_parent.setdefault(parent, []).append(p)

    globs: list[str] = []
    for parent, files in by_parent.items():
        if len(files) >= 2:
            globs.append(f"{parent}/**")
        else:
            globs.append(files[0])

    # Dedupe + truncate; preserve insertion order (deterministic for tests)
    seen: set[str] = set()
    ordered: list[str] = []
    for g in globs:
        if g not in seen:
            seen.add(g)
            ordered.append(g)
        if len(ordered) >= max_globs:
            break
    return ordered


def _parse_symbol_names(text: str) -> set[str]:
    """Extract symbol names (backtick-quoted in codegraph output)."""
    return set(_SYMBOL_RE.findall(text))


# ---------------------------------------------------------------------------
# Doctor advisory helper (CLAWP-031)
# ---------------------------------------------------------------------------


# Source-code extensions that count toward the "this is a code project"
# threshold. Conservative — excludes docs (.md), config (.toml/.yml),
# tests-only (no separate type), images, etc.
_CODE_EXTENSIONS = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".cs",
    ".rb", ".php", ".cpp", ".hpp", ".c", ".h", ".swift", ".kt", ".dart",
    ".lua", ".svelte", ".liquid", ".pas",
})


def count_code_files(repo_path: Path, *, max_walk: int = 5000) -> int:
    """Count code files under ``repo_path`` (capped to bound walk time).

    Used by the doctor advisory: projects with >50 code files but no
    ``.codegraph/`` are surfaced as candidates for ``codegraph init``.

    Walks lazily and stops at ``max_walk`` to keep doctor fast on
    massive repos; the threshold check (>50) is well below that ceiling
    so the cap doesn't affect the advisory.
    """
    if not repo_path.exists():
        return 0
    count = 0
    try:
        for root, dirs, files in os.walk(repo_path):
            # Skip vendored / generated / hidden trees that would inflate
            # the count without representing project source.
            dirs[:] = [
                d for d in dirs
                if not d.startswith(".")
                and d not in (
                    "node_modules", "venv", ".venv", "__pycache__",
                    "dist", "build", "target", "vendor",
                )
            ]
            for f in files:
                if Path(f).suffix.lower() in _CODE_EXTENSIONS:
                    count += 1
                    if count >= max_walk:
                        return count
    except OSError:
        return count
    return count
