"""Inter-agent inbox messaging for ClawPM.

Filesystem-first, append-only, no-daemon messaging between agents.
Each agent has its own JSONL file under ~/clawpm/inbox/<agent-id>.jsonl.
Events are never rewritten or deleted — acks are events too.
"""

from __future__ import annotations

import json
import secrets
import warnings
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


def _inbox_dir(portfolio_root: Path) -> Path:
    """Return the inbox directory path (does NOT create it)."""
    return portfolio_root / "inbox"


def _inbox_file(portfolio_root: Path, agent_id: str) -> Path:
    """Return the JSONL path for an agent's inbox."""
    return _inbox_dir(portfolio_root) / f"{agent_id}.jsonl"


def _ensure_inbox_dir(portfolio_root: Path) -> Path:
    """Create inbox dir if absent. Returns the dir path."""
    d = _inbox_dir(portfolio_root)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _all_inbox_files(portfolio_root: Path) -> list[Path]:
    """Return all .jsonl files in the inbox directory."""
    d = _inbox_dir(portfolio_root)
    if not d.exists():
        return []
    return sorted(d.glob("*.jsonl"))


def _generate_msg_id() -> str:
    """Generate INBOX-<YYYYMMDD>-<4-hex-chars>."""
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(2)  # 4 hex chars
    return f"INBOX-{date_str}-{suffix}"


def _now_iso() -> str:
    """Current UTC timestamp as ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def _append_event(path: Path, event: dict) -> None:
    """Append a single JSON event line to a JSONL file.

    CLAWP-032: routed through `concurrency.append_jsonl_line` for cross-platform
    locked append. Windows `open(p, "a")` is NOT atomic across processes.
    """
    from .concurrency import append_jsonl_line
    append_jsonl_line(path, json.dumps(event, ensure_ascii=False))


def _read_events(path: Path) -> list[dict]:
    """Read all events from a JSONL file. Skips malformed lines."""
    if not path.exists():
        return []
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------


def send_message(
    portfolio_root: Path,
    to: str,
    message: str,
    from_agent: str = "main",
    in_reply_to: str | None = None,
    project: str | None = None,
    task: str | None = None,
) -> dict:
    """Append a message event to the recipient's inbox. Returns the event dict."""
    _ensure_inbox_dir(portfolio_root)
    inbox_path = _inbox_file(portfolio_root, to)

    ts = _now_iso()
    event: dict = {
        "event": "message",
        "msg_id": _generate_msg_id(),
        "ts": ts,
        "from": from_agent,
        "to": to,
        "in_reply_to": in_reply_to,
        "project": project,
        "task": task,
        "message": message,
    }
    _append_event(inbox_path, event)
    return event


def read_inbox(
    portfolio_root: Path,
    agent_id: str,
    unacked_only: bool = True,
    since: str | None = None,
    from_filter: str | None = None,
) -> list[dict]:
    """Read message events from an agent's inbox.

    Parameters
    ----------
    agent_id:
        Whose inbox to read.
    unacked_only:
        When True (default), omit messages that have a corresponding ack by agent_id.
    since:
        ISO timestamp or YYYY-MM-DD string — only return messages at or after this time.
    from_filter:
        Only return messages sent by this agent.
    """
    inbox_path = _inbox_file(portfolio_root, agent_id)
    events = _read_events(inbox_path)

    # Partition messages and acks
    messages: list[dict] = []
    acked_ids: set[str] = set()

    for ev in events:
        if ev.get("event") == "message":
            messages.append(ev)
        elif ev.get("event") == "ack":
            if ev.get("acked_by") == agent_id:
                ref = ev.get("msg_id_ref")
                if ref:
                    acked_ids.add(ref)

    # Apply filters
    result: list[dict] = []
    for msg in messages:
        if unacked_only and msg.get("msg_id") in acked_ids:
            continue
        if since is not None and not _ts_at_or_after(msg.get("ts", ""), since):
            continue
        if from_filter is not None and msg.get("from") != from_filter:
            continue
        result.append(msg)

    return result


def ack_messages(
    portfolio_root: Path,
    msg_ids: list[str],
    acked_by: str = "main",
) -> dict:
    """Append ack events for each msg_id. Returns summary dict.

    The ack is appended to the inbox file of ``acked_by`` (i.e. the agent
    reading the message owns its ack record). If acked_by's inbox file does
    not yet exist it is created; if the msg_id is not found in any inbox a
    warning is emitted but no error is raised.
    """
    _ensure_inbox_dir(portfolio_root)

    # Build set of known msg_ids across all inboxes for validation
    known_ids: set[str] = set()
    for inbox_file in _all_inbox_files(portfolio_root):
        for ev in _read_events(inbox_file):
            mid = ev.get("msg_id")
            if mid:
                known_ids.add(mid)

    ts = _now_iso()
    acked: list[str] = []

    for msg_id in msg_ids:
        if msg_id not in known_ids:
            warnings.warn(
                f"inbox ack: msg_id '{msg_id}' not found in any inbox; ack recorded anyway.",
                stacklevel=2,
            )
        ack_event: dict = {
            "event": "ack",
            "msg_id_ref": msg_id,
            "acked_by": acked_by,
            "ts": ts,
        }
        inbox_path = _inbox_file(portfolio_root, acked_by)
        _append_event(inbox_path, ack_event)
        acked.append(msg_id)

    return {"acked": acked, "ts": ts}


def get_thread(portfolio_root: Path, msg_id: str) -> list[dict]:
    """Return all messages in the thread containing msg_id, sorted by timestamp.

    Walks the in_reply_to chain (ancestors) and finds any messages whose
    in_reply_to is any id in the chain (descendants). Search spans all
    inbox files since cross-agent replies are common.
    """
    # Collect all message events across all inboxes
    all_messages: list[dict] = []
    for inbox_file in _all_inbox_files(portfolio_root):
        for ev in _read_events(inbox_file):
            if ev.get("event") == "message":
                all_messages.append(ev)

    # Deduplicate by msg_id (same message could appear if sender and recipient
    # are both local agents)
    seen: set[str] = set()
    unique_messages: list[dict] = []
    for msg in all_messages:
        mid = msg.get("msg_id", "")
        if mid and mid not in seen:
            seen.add(mid)
            unique_messages.append(msg)

    # Index by msg_id for fast lookup
    by_id: dict[str, dict] = {m["msg_id"]: m for m in unique_messages if "msg_id" in m}

    if msg_id not in by_id:
        return []

    # Walk ancestors (follow in_reply_to chain up)
    thread_ids: set[str] = {msg_id}
    cursor = msg_id
    while True:
        parent_id = by_id.get(cursor, {}).get("in_reply_to")
        if not parent_id or parent_id in thread_ids:
            break
        thread_ids.add(parent_id)
        cursor = parent_id

    # Find all messages that are direct replies to anything in the thread
    # (BFS over descendants)
    frontier = set(thread_ids)
    while frontier:
        next_frontier: set[str] = set()
        for msg in unique_messages:
            mid = msg.get("msg_id", "")
            if mid in thread_ids:
                continue
            if msg.get("in_reply_to") in frontier:
                thread_ids.add(mid)
                next_frontier.add(mid)
        frontier = next_frontier

    thread = [by_id[mid] for mid in thread_ids if mid in by_id]
    # Primary: timestamp (ISO 8601, lexicographic = chronological for UTC)
    # Secondary: msg_id (deterministic tie-break within the same second)
    thread.sort(key=lambda m: (m.get("ts", ""), m.get("msg_id", "")))
    return thread


# ---------------------------------------------------------------------------
# Timestamp helper
# ---------------------------------------------------------------------------


def _ts_at_or_after(ts: str, since: str) -> bool:
    """Return True if ts >= since (both as ISO strings or YYYY-MM-DD)."""
    # Normalise: if since is YYYY-MM-DD, append T00:00:00+00:00 for comparison
    try:
        if len(since) == 10:
            since = since + "T00:00:00+00:00"
        # Truncate to comparable prefix — simple string comparison works for
        # well-formed ISO 8601 timestamps with the same offset (UTC).
        return ts >= since
    except Exception:
        return True
