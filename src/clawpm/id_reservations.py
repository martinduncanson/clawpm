"""Portfolio-level task-id reservation ledger (CLAWP-092).

Next-id allocation used to scan only the tasks dir of the checkout it ran in.
Two worktrees (or unmerged branches) of one project cannot see each other's
uncommitted task files, so both minted the same id -- and the clash was only
noticed after the id had spread into branch names, commits and PR titles.

This ledger sits under ``<portfolio_root>/id_reservations.jsonl`` -- outside
every git-tracked tree, so every allocator on the machine sees every other's
reservation the moment it is written. The next ordinal is
``max(on-disk scan, ledger high-water mark for the key) + 1``.

``key`` is the root prefix for a top-level id (``CLAWP``) and the parent id
for a subtask id (``CLAWP-092``). Callers record while holding
``tasks.portfolio_prefix_lock`` so allocate-and-record is atomic.

Scope: per machine. Cross-machine clashes remain and surface as a git add/add
conflict on the same task filename. Numbering gaps from abandoned branches are
accepted.

Fail-open, never fail-silent: a missing ledger changes nothing; an unreadable
file or corrupt line is skipped with a ``logging.warning`` and allocation falls
back to the scan.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from .concurrency import append_jsonl_line, retry_transient

logger = logging.getLogger(__name__)

LEDGER_FILENAME = "id_reservations.jsonl"

# `<key>-<ordinal>` / legacy `<key>--<ordinal>`; non-greedy so a hyphenated
# prefix (`ARB-P-007`) and a subtask id (`CLAWP-092-003`) both split at the
# LAST ordinal.
_ID_RE = re.compile(r"^(.+?)-{1,2}(\d+)$")


def _ledger_path(portfolio_root: Path) -> Path:
    return Path(portfolio_root) / LEDGER_FILENAME


def normalize_key(key: str) -> str:
    """Canonical ledger key: trailing separators stripped (a kept-legacy ``CODE-``
    prefix, CLAWP-113, shares ``CODE``'s key) and case folded to upper, the repo's
    id convention (``expand_task_id``). Every write and lookup goes through here,
    so ``clawp-007`` and ``CLAWP-007`` reserve the same slot (Windows filenames
    collide, too)."""
    return key.rstrip("-").upper()


def split_task_id(task_id: str) -> tuple[str, int] | None:
    """``(key, ordinal)`` for an id ending in ``-NNN``, else ``None``."""
    m = _ID_RE.match(task_id)
    if not m:
        return None
    return normalize_key(m.group(1)), int(m.group(2))


def reserved_high_water(
    portfolio_root: Path, key: str, project_id: str | None = None
) -> int | None:
    """Highest ordinal ever reserved for ``key`` (by ``project_id``, when given:
    two projects may deliberately share an explicit prefix and must not
    inflate each other), or ``None`` (no ledger, or
    nothing reserved under that key)."""
    path = _ledger_path(portfolio_root)
    try:
        raw = retry_transient(lambda: path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(
            "Cannot read id reservation ledger %s (%s: %s); allocating from the "
            "on-disk scan only -- ids reserved by other worktrees are invisible "
            "until this clears.",
            path, type(exc).__name__, exc,
        )
        return None

    want = normalize_key(key)
    high: int | None = None
    for lineno, line in enumerate(raw.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            rec = None
        ordinal = rec.get("ordinal") if isinstance(rec, dict) else None
        rec_key = rec.get("key") if isinstance(rec, dict) else None
        if (
            not isinstance(ordinal, int)
            or isinstance(ordinal, bool)
            or not isinstance(rec_key, str)
        ):
            logger.warning(
                "Skipping corrupt line %d in id reservation ledger %s.", lineno, path
            )
            continue
        if project_id is not None and rec.get("project_id") != project_id:
            continue
        if normalize_key(rec_key) == want and (high is None or ordinal > high):
            high = ordinal
    return high


def record_reservation(
    portfolio_root: Path,
    key: str,
    ordinal: int,
    task_id: str,
    project_id: str,
) -> None:
    """Append one reservation. Fail-open: an unwritable ledger is logged and
    the caller's mint proceeds (it still has the on-disk scan)."""
    path = _ledger_path(portfolio_root)
    event = {
        "key": normalize_key(key),
        "ordinal": ordinal,
        "task_id": task_id,
        "project_id": project_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        append_jsonl_line(path, json.dumps(event, ensure_ascii=False))
    except OSError as exc:
        logger.warning(
            "Cannot write id reservation for %s to %s (%s: %s); the id is "
            "reserved on disk only, so a sibling worktree may mint it again.",
            task_id, path, type(exc).__name__, exc,
        )


def record_task_id(portfolio_root: Path, task_id: str, project_id: str) -> None:
    """Reserve ``task_id`` under the key its own shape implies (explicit ids)."""
    parts = split_task_id(task_id)
    if parts is None:
        return
    record_reservation(portfolio_root, parts[0], parts[1], task_id, project_id)
