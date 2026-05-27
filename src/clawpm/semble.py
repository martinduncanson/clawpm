"""Semble content-shape advisory (CLAWP-036).

CodeGraph (clawpm's edit-loop default) indexes code symbols and call
graphs; it is **blind to prose**. For repos with substantial documentation
— research repos, ops/infra context stores, knowledge bases, docs-heavy
monorepos — semantic retrieval over prose + config is better served by
semble (``MinishLab/semble``), which exposes ``--content docs`` (markdown/
prose), ``--content config`` (yaml/toml), and ``--content all`` modes that
CodeGraph has no equivalent for.

This module mirrors :func:`clawpm.codegraph.count_code_files`: a cheap,
bounded file census the ``doctor`` advisory uses to decide whether a
project is doc-heavy enough that semble would add value.

The semble advisory is **independent** of the CodeGraph advisory — a mixed
repo with lots of code *and* lots of docs should surface *both*: CodeGraph
for the call graph, semble for the prose. They are complementary, not
mutually exclusive, so neither suppresses the other.

Safety: pure read-only filesystem census. Never raises to the caller —
every function degrades to a conservative default (0 / False) on error so
the doctor command can't be taken down by an unreadable tree.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_log = logging.getLogger(__name__)

# Name of the clawpm-convention semble index. Used to make the advisory
# idempotent: once the operator has indexed the repo here, stop nagging.
SEMBLE_INDEX_NAME = ".clawpm-semble"

# Prose / documentation extensions semble's ``--content docs`` covers well.
# Deliberately disjoint from codegraph._CODE_EXTENSIONS so the two censuses
# measure different things; config files (.toml/.yml) are intentionally
# excluded here — they're a weaker signal and inflate counts on every repo.
_DOC_EXTENSIONS = frozenset({
    ".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc", ".org",
})


def _on_walk_error(err: OSError) -> None:
    """``os.walk`` onerror hook: log and CONTINUE past an unreadable subtree.

    A bare ``try/except`` around the whole walk would abort the census at
    the first OSError (a permission-denied subtree, a broken Windows
    junction) and return a partial under-count — which, because the
    advisory only fires at ``doc_count >= threshold``, silently *suppresses*
    the advisory on exactly the "unreadable tree" case this module exists
    to tolerate. ``onerror`` lets the walk skip the bad directory and keep
    counting the rest, while leaving a debug breadcrumb so the degradation
    is observable rather than invisible.
    """
    _log.debug("count_doc_files: skipping unreadable path during walk: %s", err)


def count_doc_files(repo_path: Path, *, max_walk: int = 5000) -> int:
    """Count documentation/prose files under ``repo_path`` (bounded walk).

    Mirrors :func:`clawpm.codegraph.count_code_files` — same skip-list and
    scanned-entry cap — but counts prose extensions. The cap is applied to
    SCANNED ENTRIES (not matched files) so a code-heavy repo with few docs
    still terminates promptly. An unreadable subtree is skipped (via
    ``onerror``) rather than aborting the whole census.
    """
    if not repo_path.exists():
        return 0
    count = 0
    scanned = 0
    for _root, dirs, files in os.walk(repo_path, onerror=_on_walk_error):
        # Skip vendored / generated / hidden trees that would inflate the
        # count without representing project documentation.
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".")
            and d not in (
                "node_modules", "venv", ".venv", "__pycache__",
                "dist", "build", "target", "vendor",
            )
        ]
        for f in files:
            scanned += 1
            if Path(f).suffix.lower() in _DOC_EXTENSIONS:
                count += 1
            if scanned >= max_walk:
                return count
    return count


def is_doc_indexed(repo_path: Path) -> bool:
    """True iff a clawpm-convention semble index already exists in the repo.

    Used by the doctor advisory to avoid re-advising a project the operator
    has already indexed. Fails toward ``False`` (advisory fires — a harmless
    nudge) rather than raising, with a debug breadcrumb on the rare probe
    error.
    """
    try:
        return (repo_path / SEMBLE_INDEX_NAME).exists()
    except OSError as err:
        _log.debug("is_doc_indexed: probe failed for %s: %s", repo_path, err)
        return False
