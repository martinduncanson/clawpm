"""Session-scoped worktree resolution for dispatched subagents (CLAWP-098).

BUG this closes: any ID-based mutator command (``tasks state <id>``,
``done <id>``, ``block <id>`` — and, transitively, every other command that
resolves a task's directory via ``discovery.get_project_dir``) finds its
project's filesystem location by scanning the GLOBAL portfolio registry
(``~/clawpm/portfolio.toml``'s ``project_roots``) for a directory matching
the project id. That scan is 100% independent of cwd. ``tasks dispatch
--worktree`` mints a git worktree under ``<repo>/.clawpm-worktrees/<task>/``
but never registers it as its own portfolio project — it's just a checkout.
So an ID-based mutator run with cwd inside that worktree still resolves via
the registry straight back to the MAIN checkout's ``repo_path``, and mutates
the task file THERE instead of in the worktree the agent is actually sitting
in. See ``.project/tasks/CLAWP-098.md`` for the full incident writeup.

FIX (adapted from agenticq's AgentCard identity model — a durable agent_id
plus a per-instance session_id, with all coordination state kept in a place
every instance can see): when ``tasks dispatch --worktree`` mints a worktree,
it also mints a ``session_id`` and appends a ``registered`` event here,
mapping that session_id to the worktree's actual filesystem path. This
mirrors the append-only JSONL ledger pattern already used for
``leases.jsonl`` / ``dispatches.jsonl`` (written through
``concurrency.append_jsonl_line`` for Windows append atomicity), replayed to
reconstruct current state rather than mutated in place.

``discovery.get_project_dir`` consults :func:`find_session_for_cwd` BEFORE
falling through to the portfolio-registry scan: when cwd is inside an active
session's registered worktree path for the project being resolved, it
short-circuits to that worktree's own ``.project/`` directory. When cwd
matches no active session (normal single-checkout usage — including every
other command that isn't running inside a dispatched worktree), the lookup
returns ``None`` and today's registry-based behaviour is completely
unaffected. This is also why ``tasks list`` / ``next`` / ``reflect`` (which
all resolve through the same ``get_project_dir`` chokepoint) are safe: they
only see session-scoped resolution when their cwd is actually inside a
registered worktree, which is exactly the case where that resolution is
correct.

SESSION LIFETIME (revised after Codex review, PR #55): a session is NOT
released when its dispatch's ``.claude/settings.local.json`` is torn down.
An earlier version did release there, and Codex caught the resulting
regression: dispatch-settings teardown and worktree lifetime are different
things. A bulk ``tasks state A B done`` run with cwd inside A's dispatched
worktree tears down A's settings — and, under the old design, released A's
session — mid-loop; task B, processed next in the SAME invocation with the
SAME cwd, would then find no active session for that path and silently fall
through to the portfolio registry (the main checkout) instead. Same hazard
for an operator who keeps working inside an already-torn-down worktree.

Instead, :func:`active_sessions` treats a session as active iff BOTH the
ledger says so (no ``released`` event — ``release_session`` /
``release_sessions_for_task`` remain available as library API for a future
caller that genuinely knows the worktree itself is gone, e.g. a
``worktree remove`` integration) AND its ``worktree_path`` still exists on
disk. Directory existence is the correct lifecycle boundary — it needs no
explicit ledger write to detect, is immune to the mid-invocation release
hazard above, and self-heals the moment ``git worktree remove`` actually
deletes the checkout. A crashed dispatch whose worktree is never removed
leaves its session record in the ledger forever, but harmlessly: the same
task_id always resolves to the same worktree path (``create_worktree``
scopes the path by task_id), so a stale-but-still-correct entry never
misdirects a different dispatch — the same "inert leftover, not corruption"
tradeoff ``dispatches.jsonl`` already accepts with no reaping of its own.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .concurrency import append_jsonl_line

logger = logging.getLogger(__name__)

SESSION_REGISTRY_FILENAME = "sessions.jsonl"

_REGISTERED = "registered"
_RELEASED = "released"


def _registry_path(portfolio_root: Path) -> Path:
    return portfolio_root / SESSION_REGISTRY_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass
class SessionRecord:
    """Reconstructed session state, replayed from the registry."""

    session_id: str
    task_id: str
    project_id: str
    worktree_path: Path
    active: bool


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def register_session(
    portfolio_root: Path,
    session_id: str,
    task_id: str,
    project_id: str,
    worktree_path: Path,
) -> None:
    """Append a ``registered`` event mapping *session_id* to *worktree_path*.

    Called by ``tasks dispatch --worktree`` right after the worktree is
    created. Idempotent-ish: re-dispatching the same task (worktree already
    exists, ``create_worktree`` short-circuits) simply appends another
    ``registered`` event for a fresh session_id pointing at the same path —
    harmless, since resolution only needs ANY active session whose path
    matches, not a unique one.
    """
    event = {
        "action": _REGISTERED,
        "session_id": session_id,
        "task_id": task_id,
        "project_id": project_id,
        "worktree_path": str(Path(worktree_path).resolve()),
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


def release_session(portfolio_root: Path, session_id: str) -> None:
    """Append a ``released`` event retiring *session_id*. Idempotent at the
    registry level — replaying multiple releases for the same id is
    harmless (the record is simply not-active either way)."""
    event = {
        "action": _RELEASED,
        "session_id": session_id,
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


def release_sessions_for_task(portfolio_root: Path, task_id: str, project_id: str) -> int:
    """Release every currently-active session for *(task_id, project_id)*.

    Called from dispatch teardown so a completed/torn-down dispatch's
    session pointer doesn't outlive the dispatch it belongs to. Returns the
    number of sessions released (0 if none were active — safe to call
    unconditionally from teardown paths that don't know whether a session
    was ever registered, e.g. non-worktree dispatches).
    """
    released = 0
    for record in _replay(portfolio_root).values():
        if record.active and record.task_id == task_id and record.project_id == project_id:
            release_session(portfolio_root, record.session_id)
            released += 1
    return released


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _replay(portfolio_root: Path) -> dict[str, SessionRecord]:
    """Reconstruct current session state per session_id from the log.

    Corrupted lines are skipped (defensive — a half-written line must not
    nuke resolution for every other registered session). A whole-file read
    failure falls back to "no active sessions", which is fail-OPEN (every
    caller of ``find_session_for_cwd`` treats an empty result as "fall
    through to the registry lookup") but must not be fail-SILENT (CLAWP-098
    review finding — this repo's own fail-open-needs-a-marker doctrine, see
    CLAWP-039/041): a silently swallowed read failure here means an ID-based
    mutator run from inside a dispatched worktree quietly regresses to the
    exact main-checkout corruption this module exists to prevent, with
    nothing in the logs to explain why. Log it.
    """
    path = _registry_path(portfolio_root)
    if not path.exists():
        return {}
    sessions: dict[str, SessionRecord] = {}
    try:
        # errors="replace" (antigravity review, PR #55), not "strict": a
        # single invalid byte ANYWHERE in the file must not take down every
        # OTHER session's replay — that would be strictly worse than the
        # per-line JSONDecodeError handling below, since one bad byte can
        # sit in the middle of an otherwise-healthy multi-KB append-only
        # file. A replaced byte lands inside whichever line it corrupted;
        # that one line then fails json.loads() below and is skipped same
        # as any other malformed line, while every other line still parses.
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        # error, not warning (Codex P1, PR #55): this is the fail-open path
        # that most directly recreates CLAWP-098's original corruption —
        # every ID-based mutator run from inside a dispatched worktree will
        # silently mutate the MAIN checkout again until this clears. A
        # genuinely hard fail-closed (raise out of get_project_dir) would
        # take down read-only commands too (tasks list/next/reflect share
        # this chokepoint) over what should be a rare, narrow corruption —
        # judged worse than a loud, high-severity log. Escalate here if that
        # tradeoff needs revisiting.
        logger.error(
            "Failed to read session registry %s: %s. Session-scoped project "
            "resolution is DISABLED until this clears — ID-based mutator "
            "commands run from inside a dispatched worktree will fall "
            "through to the portfolio registry (main-checkout) lookup.",
            path, exc,
        )
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            # A syntactically-valid JSON line that isn't an object (a bare
            # list/string/number/null) — same "corrupted line, skip only
            # this one" contract as the JSONDecodeError above, just a
            # different way a line can fail to be a real event (antigravity
            # review, PR #55: unguarded `.get()` would otherwise raise
            # AttributeError and abort replay for every OTHER session too).
            continue
        action = ev.get("action")
        session_id = ev.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            continue
        if action not in (_REGISTERED, _RELEASED):
            continue
        if action == _REGISTERED:
            task_id = ev.get("task_id")
            project_id = ev.get("project_id")
            worktree_path = ev.get("worktree_path")
            # Codex review, PR #55: a syntactically-valid line whose fields
            # have the WRONG TYPE (e.g. worktree_path: 42) previously passed
            # the truthiness checks below straight into Path(worktree_path),
            # which raises TypeError uncaught -- aborting replay for every
            # OTHER session, not just this malformed one. Require str for
            # every field (also covers empty-string, replacing the old
            # `not x` truthiness checks) before constructing the record.
            if (
                not isinstance(task_id, str) or not task_id
                or not isinstance(project_id, str) or not project_id
                or not isinstance(worktree_path, str) or not worktree_path
            ):
                continue
            sessions[session_id] = SessionRecord(
                session_id=session_id,
                task_id=task_id,
                project_id=project_id,
                worktree_path=Path(worktree_path),
                active=True,
            )
        elif action == _RELEASED:
            record = sessions.get(session_id)
            if record:
                record.active = False
    return sessions


def active_sessions(portfolio_root: Path) -> list[SessionRecord]:
    """Sessions whose latest event leaves them active AND whose worktree
    still exists on disk.

    The directory-existence check (Codex review, PR #55) is what makes
    session liveness track the worktree's actual lifetime instead of the
    dispatch-settings teardown moment — see the module docstring's "SESSION
    LIFETIME" section for why that distinction matters. A worktree removed
    out from under a still-``registered`` session (``git worktree remove``,
    a crashed dispatch's directory manually cleaned up, ...) naturally stops
    being returned here — no explicit ``released`` event needed.
    """
    result: list[SessionRecord] = []
    for s in _replay(portfolio_root).values():
        if not s.active:
            continue
        try:
            if not s.worktree_path.is_dir():
                continue
        except OSError:
            # Unreadable (permissions, transient FS hiccup): treat like "not
            # there" rather than raising out of every project resolution.
            continue
        result.append(s)
    return result


def find_session_for_cwd(
    portfolio_root: Path, cwd: Path, project_id: Optional[str] = None
) -> Optional[SessionRecord]:
    """Return the most specific active session whose worktree contains *cwd*.

    ``cwd`` may be the worktree root itself or any subdirectory beneath it.
    When ``project_id`` is given, only sessions for that project are
    considered — the caller already knows which project it's resolving and
    must never cross-match a different project's worktree just because cwd
    happens to be nested under it.

    Returns ``None`` when the registry is absent/empty or no active
    session's worktree path contains cwd — the normal case for every command
    NOT running inside a dispatched worktree, which is the overwhelming
    majority of usage. Callers must treat ``None`` as "fall through to the
    existing portfolio-registry lookup", never as an error.

    Path comparison case-normalises via ``os.path.normcase`` (grok review,
    PR #55) — the same normalisation ``concurrency.file_lock`` already
    applies for its lock-path keys. Windows filesystems are case-insensitive
    but ``pathlib.Path`` equality and ``in .parents`` are NOT, so a
    case-mismatched cwd (a different drive-letter spelling, a shell that
    preserves different casing than the one that minted the worktree) would
    otherwise silently miss an active session and fail open to exactly the
    main-checkout corruption this module exists to prevent. The
    case-normalised strings are then wrapped back in ``Path`` and compared
    via equality / ``in .parents`` (antigravity review, PR #55), not raw
    string ``.startswith(wt + os.sep)`` — a Windows drive root resolves with
    a trailing separator (``"W:\\"``), which made the string-concat version
    double up separators and silently miss every path nested under a
    worktree that happened to sit at a drive root.
    """
    try:
        resolved_cwd = Path(cwd).resolve()
    except OSError:
        # antigravity review, PR #55: an unresolvable cwd (permission error,
        # a network drive that dropped mid-call) must fall open to "no
        # session matched" like every other miss, not crash every caller
        # of get_project_dir — including read-only commands (tasks list/
        # next/reflect) that share this chokepoint.
        return None
    cwd_norm = Path(os.path.normcase(str(resolved_cwd)))
    best: Optional[SessionRecord] = None
    best_depth = -1
    for record in active_sessions(portfolio_root):
        if project_id is not None and record.project_id != project_id:
            continue
        try:
            wt = record.worktree_path.resolve()
        except OSError:
            continue
        wt_norm = Path(os.path.normcase(str(wt)))
        if cwd_norm != wt_norm and wt_norm not in cwd_norm.parents:
            continue
        depth = len(wt.parts)
        if depth > best_depth:
            best = record
            best_depth = depth
    return best
