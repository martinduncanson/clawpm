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

Sessions are retired (a ``released`` event appended) by
``dispatch.teardown_dispatch_settings`` — the same moment a dispatch's
``.claude/settings.local.json`` is torn down, whether that happens via the
explicit ``tasks teardown-dispatch`` command or the automatic teardown that
runs when a dispatched task transitions to done/blocked. A crashed dispatch
that never tears down leaves its session ``active`` forever in the ledger;
that is a leftover append-only record, not filesystem corruption, and is the
same lifecycle tradeoff ``leases.jsonl`` already accepts (a stale lease is
reaped by ``doctor``/next-dispatch sweep; a stale session record is inert —
it only ever redirects resolution to a worktree path that either still
exists, in which case redirecting to it remains correct, or has been removed,
in which case ``get_tasks_dir`` finds no ``tasks/`` there and falls through
exactly like today's "no project" case).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .concurrency import append_jsonl_line

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
    nuke resolution for every other registered session).
    """
    path = _registry_path(portfolio_root)
    if not path.exists():
        return {}
    sessions: dict[str, SessionRecord] = {}
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = ev.get("action")
        session_id = ev.get("session_id")
        if not session_id or action not in (_REGISTERED, _RELEASED):
            continue
        if action == _REGISTERED:
            task_id = ev.get("task_id")
            project_id = ev.get("project_id")
            worktree_path = ev.get("worktree_path")
            if not task_id or not project_id or not worktree_path:
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
    """All sessions whose latest event leaves them active."""
    return [s for s in _replay(portfolio_root).values() if s.active]


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
    """
    resolved_cwd = Path(cwd).resolve()
    best: Optional[SessionRecord] = None
    best_depth = -1
    for record in active_sessions(portfolio_root):
        if project_id is not None and record.project_id != project_id:
            continue
        try:
            wt = record.worktree_path.resolve()
        except OSError:
            continue
        if resolved_cwd != wt and wt not in resolved_cwd.parents:
            continue
        depth = len(wt.parts)
        if depth > best_depth:
            best = record
            best_depth = depth
    return best
