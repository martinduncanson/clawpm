"""Crash-safe dispatch leases (CLAWP-039).

When clawpm dispatches a subtask to a subagent (CLAWP-018/024), the holder
can die mid-task — a crashed session, a killed worktree, a network drop —
leaving the subtask stuck in ``progress`` forever with the parent silently
waiting. There is no daemon to notice.

This module borrows the **lease / heartbeat / expiry / fallback** model from
the 2026-05-27 agenticq assessment (design-donor, not an infra dependency —
see the ``agenticq_verdict`` memory) and implements it **file-local** over an
append-only registry, exactly like ``dispatch.dispatches.jsonl`` and the
reflection event streams:

  - A **lease** is granted at dispatch with a TTL and a fallback policy.
  - The holder **heartbeats** while alive (wired to the dispatch PostToolUse
    hook — every code-touching tool use is a liveness signal).
  - A **sweep** (run by ``clawpm doctor`` and opportunistically on the next
    ``tasks dispatch``) detects leases whose last heartbeat is older than the
    TTL and applies the **fallback policy**: requeue / route-secondary /
    escalate-to-human / fail.

No long-running process: expiry is detected lazily on sweep, never by a timer.
This preserves clawpm's local-first / no-daemon thesis. The registry is an
append-only JSONL replayed to reconstruct current lease state, written through
``concurrency.append_jsonl_line`` (Windows append atomicity, CLAWP-032).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from .concurrency import append_jsonl_line

LEASE_REGISTRY_FILENAME = "leases.jsonl"


class LeaseStatus(str, Enum):
    """Lifecycle status of a lease, derived from its latest event.

    Enum (not a bare string) so a typo in the replay state machine is a
    construction error, not a silent invariant break (Codex/type-review)."""

    ACTIVE = "active"        # latest event is granted or heartbeat
    RELEASED = "released"    # clean completion — retired, no fallback
    REASSIGNED = "reassigned"  # expiry detected, fallback applied (terminal)


class FallbackPolicy(str, Enum):
    """What to do with a subtask whose lease expired (holder went silent).

    Selectable per dispatch. ``str`` mixin so the value serialises cleanly to
    JSON and round-trips through the registry.
    """

    REQUEUE = "requeue"               # back to open for re-dispatch
    ROUTE_SECONDARY = "route-secondary"  # back to open, flagged for a secondary holder
    ESCALATE = "escalate-to-human"    # blocked, operator must triage
    FAIL = "fail"                     # blocked, terminal failure per policy

    @classmethod
    def from_str(cls, value: str) -> "FallbackPolicy":
        try:
            return cls(value)
        except ValueError as exc:
            allowed = ", ".join(p.value for p in cls)
            raise ValueError(
                f"unknown fallback policy {value!r}; allowed: {allowed}"
            ) from exc


# Event actions written to the registry.
_GRANTED = "granted"
_HEARTBEAT = "heartbeat"
_RELEASED = "released"      # clean completion — lease retired, no fallback
_REASSIGNED = "reassigned"  # expiry detected, fallback applied (terminal)

_ACTIVE_ACTIONS = {_GRANTED, _HEARTBEAT}


@dataclass
class Lease:
    """Reconstructed lease state for one (task_id, project_id) pair.

    ``status`` is derived from the latest event: ``active`` (granted/heartbeat),
    ``released`` (clean completion), or ``reassigned`` (fallback applied).
    """

    task_id: str
    project_id: str
    holder_id: Optional[str]
    granted_at: datetime
    ttl_seconds: int
    fallback_policy: FallbackPolicy
    last_heartbeat_at: datetime
    status: LeaseStatus
    target_dir: Optional[str] = None

    def __post_init__(self) -> None:
        # Boundary check: replay is the only constructor, so these guard the
        # event-stream → snapshot projection rather than trusting it blindly.
        if self.ttl_seconds <= 0:
            raise ValueError(f"Lease ttl_seconds must be positive, got {self.ttl_seconds}")
        if self.last_heartbeat_at < self.granted_at:
            raise ValueError("Lease last_heartbeat_at cannot precede granted_at")

    @property
    def active(self) -> bool:
        return self.status is LeaseStatus.ACTIVE

    def is_expired(self, now: datetime) -> bool:
        """True iff active AND no heartbeat within the TTL window.

        Boundary: ``age == ttl`` is NOT expired (strict ``>``)."""
        if not self.active:
            return False
        age = (now - self.last_heartbeat_at).total_seconds()
        return age > self.ttl_seconds

    def expires_at(self) -> datetime:
        return self.last_heartbeat_at + timedelta(seconds=self.ttl_seconds)


def _registry_path(portfolio_root: Path) -> Path:
    return portfolio_root / LEASE_REGISTRY_FILENAME


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, tolerating the trailing ``Z`` clawpm writes
    and naive strings (assumed UTC)."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def grant_lease(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
    ttl_seconds: int,
    fallback_policy: FallbackPolicy,
    holder_id: Optional[str] = None,
    target_dir: Optional[str] = None,
) -> None:
    """Append a ``granted`` event. The granted timestamp doubles as the first
    heartbeat, so a lease is alive from the moment it is granted."""
    if ttl_seconds <= 0:
        raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
    policy = (
        fallback_policy
        if isinstance(fallback_policy, FallbackPolicy)
        else FallbackPolicy.from_str(fallback_policy)
    )
    event = {
        "action": _GRANTED,
        "task_id": task_id,
        "project_id": project_id,
        "holder_id": holder_id,
        "ttl_seconds": int(ttl_seconds),
        "fallback_policy": policy.value,
        "target_dir": target_dir,
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


def heartbeat(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
    holder_id: Optional[str] = None,
) -> None:
    """Append a ``heartbeat`` event — the holder is alive."""
    event = {
        "action": _HEARTBEAT,
        "task_id": task_id,
        "project_id": project_id,
        "holder_id": holder_id,
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


def release_lease(portfolio_root: Path, task_id: str, project_id: str) -> None:
    """Append a ``released`` event — clean completion, no fallback. Idempotent
    at the registry level (replaying multiple releases is harmless)."""
    event = {
        "action": _RELEASED,
        "task_id": task_id,
        "project_id": project_id,
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


def _record_reassigned(
    portfolio_root: Path,
    task_id: str,
    project_id: str,
    fallback_policy: FallbackPolicy,
    resulting_state: str,
    last_heartbeat_at: datetime,
) -> None:
    event = {
        "action": _REASSIGNED,
        "task_id": task_id,
        "project_id": project_id,
        "fallback_policy": fallback_policy.value,
        "resulting_state": resulting_state,
        "last_heartbeat_at": last_heartbeat_at.isoformat().replace("+00:00", "Z"),
        "ts": _now_iso(),
    }
    append_jsonl_line(_registry_path(portfolio_root), json.dumps(event, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------


def _replay(portfolio_root: Path) -> dict[tuple[str, str], Lease]:
    """Reconstruct current lease state per (task_id, project_id) from the log.

    A ``granted`` event (re)starts a lease; ``heartbeat`` advances its liveness
    timestamp; ``released`` / ``reassigned`` make it terminal. Corrupted lines
    are skipped (defensive — a half-written line must not nuke the whole sweep).
    """
    path = _registry_path(portfolio_root)
    if not path.exists():
        return {}
    leases: dict[tuple[str, str], Lease] = {}
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
        task_id = ev.get("task_id")
        project_id = ev.get("project_id")
        if not task_id or not project_id or action not in (
            _GRANTED, _HEARTBEAT, _RELEASED, _REASSIGNED
        ):
            continue
        key = (task_id, project_id)
        ts_raw = ev.get("ts")
        try:
            ts = _parse_ts(ts_raw) if ts_raw else None
        except (ValueError, TypeError):
            ts = None

        if action == _GRANTED:
            if ts is None:
                continue
            try:
                policy = FallbackPolicy.from_str(ev.get("fallback_policy", ""))
            except ValueError:
                continue
            leases[key] = Lease(
                task_id=task_id,
                project_id=project_id,
                holder_id=ev.get("holder_id"),
                granted_at=ts,
                ttl_seconds=int(ev.get("ttl_seconds", 0)) or 1,
                fallback_policy=policy,
                last_heartbeat_at=ts,
                status=LeaseStatus.ACTIVE,
                target_dir=ev.get("target_dir"),
            )
        elif action == _HEARTBEAT:
            lease = leases.get(key)
            if lease and lease.active and ts is not None and ts > lease.last_heartbeat_at:
                lease.last_heartbeat_at = ts
        elif action == _RELEASED:
            lease = leases.get(key)
            if lease:
                lease.status = LeaseStatus.RELEASED
        elif action == _REASSIGNED:
            lease = leases.get(key)
            if lease:
                lease.status = LeaseStatus.REASSIGNED
    return leases


def active_leases(portfolio_root: Path) -> list[Lease]:
    """All leases whose latest event leaves them active."""
    return [l for l in _replay(portfolio_root).values() if l.active]


def get_lease(
    portfolio_root: Path, task_id: str, project_id: str
) -> Optional[Lease]:
    return _replay(portfolio_root).get((task_id, project_id))


def expired_leases(
    portfolio_root: Path, now: datetime, project_id: Optional[str] = None
) -> list[Lease]:
    """Active leases whose last heartbeat is older than their TTL.

    ``project_id`` scopes the result — pass it so a project-scoped doctor run
    never reaps another project's leases (cross-project isolation)."""
    return [
        l for l in active_leases(portfolio_root)
        if l.is_expired(now) and (project_id is None or l.project_id == project_id)
    ]


# ---------------------------------------------------------------------------
# Fallback application + sweep
# ---------------------------------------------------------------------------

# (FallbackPolicy -> (TaskState, note-verb)). Imported lazily in apply_fallback
# to avoid a models import at module load where it isn't needed.
_POLICY_PLAN = {
    FallbackPolicy.REQUEUE: ("OPEN", "requeued for re-dispatch"),
    FallbackPolicy.ROUTE_SECONDARY: ("OPEN", "requeued, flagged for a secondary holder"),
    FallbackPolicy.ESCALATE: ("BLOCKED", "escalated to human for triage"),
    FallbackPolicy.FAIL: ("BLOCKED", "failed per fallback policy"),
}


def apply_fallback(config, portfolio_root: Path, lease: Lease, now: datetime) -> dict:
    """Transition an expired lease's task per its fallback policy and record a
    terminal ``reassigned`` event so the lease is no longer swept.

    Returns a summary dict ``{task_id, project_id, policy, resulting_state,
    transitioned}``. ``transitioned`` is False if the task could not be moved
    (e.g. already gone) — the lease is still retired so a missing task doesn't
    loop forever in the sweep.
    """
    from .models import TaskState
    from .tasks import change_task_state, get_task

    # Crash-safety guard: a fallback only makes sense for a task still being
    # worked (PROGRESS). If the holder actually FINISHED (task already done) or
    # the task is already blocked/gone — e.g. it completed but crashed before
    # releasing the lease — retire the lease WITHOUT moving the task. Otherwise
    # a stale lease would yank a completed task back out of done.
    current = get_task(config, lease.project_id, lease.task_id)
    if current is None or current.state is not TaskState.PROGRESS:
        release_lease(portfolio_root, lease.task_id, lease.project_id)
        return {
            "task_id": lease.task_id,
            "project_id": lease.project_id,
            "policy": lease.fallback_policy.value,
            "resulting_state": current.state.value if current else "missing",
            "transitioned": False,
            "retired_without_fallback": True,
        }

    state_name, verb = _POLICY_PLAN[lease.fallback_policy]
    target_state = TaskState[state_name]
    age = int((now - lease.last_heartbeat_at).total_seconds())
    note = (
        f"clawpm lease expired ({verb}): no heartbeat for {age}s "
        f"(TTL {lease.ttl_seconds}s, last beat "
        f"{lease.last_heartbeat_at.isoformat().replace('+00:00', 'Z')})"
    )

    transitioned = False
    transition_error: Optional[str] = None
    try:
        # Only genuine I/O (the file move) raises out of change_task_state; the
        # soft-fail paths (rollup gate, missing dir) return None. Catch ONLY
        # OSError — a programming bug (KeyError, AttributeError) must propagate,
        # not masquerade as a stuck holder.
        result = change_task_state(
            config, lease.project_id, lease.task_id, target_state, note=note
        )
        transitioned = result is not None
    except OSError as exc:
        transition_error = f"{type(exc).__name__}: {exc}"

    if not transitioned:
        # CRITICAL (Codex/silent-failure): do NOT retire the lease as a success
        # when the task did not actually move — that would orphan it in PROGRESS
        # forever (lease terminal, never re-swept). Leave the lease ACTIVE so the
        # next sweep retries, and surface the failure so it isn't silent.
        return {
            "task_id": lease.task_id,
            "project_id": lease.project_id,
            "policy": lease.fallback_policy.value,
            "resulting_state": (current.state.value if current else "missing"),
            "transitioned": False,
            "transition_error": transition_error or "change_task_state returned None",
        }

    # REQUEUE/ROUTE_SECONDARY: best-effort teardown of stale dispatch settings
    # so the task can be cleanly re-dispatched. Never fatal — but the failure is
    # captured in the summary so a persistent leak is observable.
    teardown_failed: Optional[str] = None
    if lease.fallback_policy in (FallbackPolicy.REQUEUE, FallbackPolicy.ROUTE_SECONDARY) and lease.target_dir:
        try:
            from .dispatch import teardown_dispatch_settings

            teardown_dispatch_settings(
                Path(lease.target_dir),
                task_id=lease.task_id,
                portfolio_root=portfolio_root,
                project_id=lease.project_id,
            )
        except Exception as exc:
            teardown_failed = f"{type(exc).__name__}: {exc}"

    _record_reassigned(
        portfolio_root,
        lease.task_id,
        lease.project_id,
        lease.fallback_policy,
        state_name.lower(),
        lease.last_heartbeat_at,
    )
    summary = {
        "task_id": lease.task_id,
        "project_id": lease.project_id,
        "policy": lease.fallback_policy.value,
        "resulting_state": state_name.lower(),
        "transitioned": True,
    }
    if teardown_failed:
        summary["teardown_failed"] = teardown_failed
    return summary


def sweep(
    config,
    portfolio_root: Path,
    now: Optional[datetime] = None,
    project_id: Optional[str] = None,
) -> list[dict]:
    """Detect every expired lease and apply its fallback. Returns one summary
    dict per lease acted on (empty if nothing expired). The no-daemon expiry
    detector: call from ``doctor`` and opportunistically on ``tasks dispatch``.

    ``project_id`` scopes the sweep — pass it from a project-scoped ``doctor``
    run so the remediation matches the detection and never reaps another
    project's leases (cross-project isolation).
    """
    if now is None:
        now = datetime.now(timezone.utc)
    actions: list[dict] = []
    for lease in expired_leases(portfolio_root, now, project_id=project_id):
        actions.append(apply_fallback(config, portfolio_root, lease, now))
    return actions
