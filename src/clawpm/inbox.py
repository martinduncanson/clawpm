"""Inter-agent inbox messaging for ClawPM.

Filesystem-first, append-only, no-daemon messaging between agents.
Each agent has its own JSONL file under ~/clawpm/inbox/<agent-id>.jsonl.
Events are never rewritten or deleted — acks are events too.
"""

from __future__ import annotations

import json
import re
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


# An agent id becomes a filename (`<inbox>/<id>.jsonl`) and ids arrive from other
# agents (message `from`, `to`). Anything carrying a path separator, a drive or
# stream colon, a `..` segment or a trailing dot is refused so no caller can
# steer a write outside the inbox directory.
#
# Matched with ``fullmatch``: a ``$`` anchor also matches before a trailing
# newline, so ``"a\n"`` would pass. Windows reserves the DOS device names
# (``NUL``, ``CON``, ``COM1``...) even with an extension (``NUL.jsonl``,
# ``CON.foo``), so a reply to such an id would vanish or fail; they are refused
# case-insensitively on every platform to keep the portfolio portable.
_AGENT_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._@-]{0,62}[A-Za-z0-9_@-])?")
_WIN_DEVICE_RE = re.compile(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", re.IGNORECASE)


def validate_agent_id(agent_id: object) -> str:
    """Return ``agent_id`` if it is a safe inbox filename stem, else raise ValueError."""
    if (
        not isinstance(agent_id, str)
        or not _AGENT_ID_RE.fullmatch(agent_id)
        or _WIN_DEVICE_RE.fullmatch(agent_id.split(".", 1)[0])
    ):
        raise ValueError(
            f"invalid agent id {agent_id!r}: use 1-64 letters, digits or . _ @ -"
            " (not a reserved device name such as NUL or CON)"
        )
    return agent_id


def _inbox_file(portfolio_root: Path, agent_id: str) -> Path:
    """Return the JSONL path for an agent's inbox (validated: stays inside the inbox dir)."""
    return _inbox_dir(portfolio_root) / f"{validate_agent_id(agent_id)}.jsonl"


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
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Valid JSON that is not an object (`[1]`, `"x"`, `42`) would crash
            # every `ev.get(...)` caller; treat it like any other malformed line.
            if isinstance(ev, dict):
                events.append(ev)
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
    payload: dict | None = None,
) -> dict:
    """Append a message event to the recipient's inbox. Returns the event dict.

    ``payload`` (CLAWP-101) is an optional structured dict. It is additive: the
    key is omitted entirely when ``None``, so plain-text events are byte-for-byte
    what they were before and older readers ignore it.
    """
    validate_agent_id(from_agent)
    inbox_path = _inbox_file(portfolio_root, to)
    _ensure_inbox_dir(portfolio_root)

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
    if payload is not None:
        event["payload"] = payload
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
                if isinstance(ref, str) and ref:
                    acked_ids.add(ref)

    # Apply filters
    result: list[dict] = []
    for msg in messages:
        if unacked_only and isinstance(msg.get("msg_id"), str) and msg["msg_id"] in acked_ids:
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
            if isinstance(mid, str) and mid:
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
        if isinstance(mid, str) and mid and mid not in seen:
            seen.add(mid)
            unique_messages.append(msg)

    # Index by msg_id for fast lookup
    by_id: dict[str, dict] = {m["msg_id"]: m for m in unique_messages}

    if msg_id not in by_id:
        return []

    # Walk ancestors (follow in_reply_to chain up)
    thread_ids: set[str] = {msg_id}
    cursor = msg_id
    while True:
        parent_id = by_id.get(cursor, {}).get("in_reply_to")
        if not isinstance(parent_id, str) or not parent_id or parent_id in thread_ids:
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
            if isinstance(msg.get("in_reply_to"), str) and msg["in_reply_to"] in frontier:
                thread_ids.add(mid)
                next_frontier.add(mid)
        frontier = next_frontier

    thread = [by_id[mid] for mid in thread_ids if mid in by_id]
    # Primary: timestamp (ISO 8601, lexicographic = chronological for UTC)
    # Secondary: msg_id (deterministic tie-break within the same second)
    thread.sort(key=lambda m: (m.get("ts", ""), m.get("msg_id", "")))
    return thread


# ---------------------------------------------------------------------------
# Structured task requests (CLAWP-101)
#
# Single-writer discipline: the sender (a1) only ever appends to the inbox,
# which lives at the portfolio root, outside every project's git tree. The
# RECIPIENT (a2) turns the request into a task through `add_task`, from its own
# context. Nothing here gives a1 a path into `.project/tasks/`.
# ---------------------------------------------------------------------------

TASK_REQUEST = "task_request"
TASK_REQUEST_RESULT = "task_request_result"
TASK_REQUEST_REJECTED = "task_request_rejected"

_COMPLEXITIES = ("s", "m", "l", "xl")
_LIST_FIELDS = ("success_criteria", "scope", "depends", "tags")
_STR_FIELDS = ("title", "description", "predict_duration", "predict_complexity",
               "predict_approach", "pre_mortem")


class TaskRequestError(ValueError):
    """A task_request payload is malformed."""


def build_task_request_payload(
    *,
    title: str | None,
    priority: int | None = None,
    complexity: str | None = None,
    description: str | None = None,
    depends: list[str] | tuple[str, ...] | None = None,
    scope: list[str] | tuple[str, ...] | None = None,
    tags: list[str] | tuple[str, ...] | None = None,
    success_criteria: list[str] | tuple[str, ...] | None = None,
    predict_duration: str | None = None,
    predict_complexity: str | None = None,
    predict_approach: str | None = None,
    confidence: int | None = None,
    pre_mortem: str | None = None,
) -> dict:
    """Build and validate a ``task_request`` payload. Omits unset fields."""
    payload: dict = {
        "type": TASK_REQUEST,
        "title": title,
        "priority": priority,
        "complexity": complexity,
        "description": description,
        "depends": list(depends) if depends else None,
        "scope": list(scope) if scope else None,
        "tags": list(tags) if tags else None,
        "success_criteria": list(success_criteria) if success_criteria else None,
        "predict_duration": predict_duration,
        "predict_complexity": predict_complexity,
        "predict_approach": predict_approach,
        "confidence": confidence,
        "pre_mortem": pre_mortem,
    }
    payload = {k: v for k, v in payload.items() if v is not None}
    _validate_task_request(payload)
    return payload


_SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # used with fullmatch
_MAX_TITLE = 300
_MAX_ITEM = 500
_CTRL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")  # tab allowed, newlines/NUL/ESC not
_DRIVE_RE = re.compile(r"^[A-Za-z]:")


def _is_safe_id(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID_RE.fullmatch(value))


def _safe_scope_entry(entry: str) -> bool:
    """A claimed file glob must be relative and stay inside the repo."""
    if not entry or _CTRL_RE.search(entry) or len(entry) > _MAX_ITEM:
        return False
    if entry.startswith(("/", "\\", "~")) or _DRIVE_RE.match(entry):
        return False
    return ".." not in re.split(r"[\\/]", entry)


def _validate_task_request(payload: dict) -> None:
    """Raise TaskRequestError if ``payload`` is not a usable task_request.

    Every field is untrusted (it comes from another agent), so this also covers
    what would later reach a path, a heading or the criteria parser.
    """
    title = payload.get("title")
    if not isinstance(title, str) or not title.strip():
        raise TaskRequestError("task_request needs a non-empty title")
    if len(title) > _MAX_TITLE or _CTRL_RE.search(title):
        raise TaskRequestError(
            f"title must be a single line of at most {_MAX_TITLE} characters")
    for key in _STR_FIELDS:
        v = payload.get(key)
        if v is not None and not isinstance(v, str):
            raise TaskRequestError(f"{key} must be a string")
    for key in _LIST_FIELDS:
        v = payload.get(key)
        if v is not None and not (isinstance(v, list) and all(isinstance(i, str) for i in v)):
            raise TaskRequestError(f"{key} must be a list of strings")
    for key, lo, hi in (("priority", 1, 10), ("confidence", 1, 5)):
        v = payload.get(key)
        if v is not None and (isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi):
            raise TaskRequestError(f"{key} must be an integer {lo}-{hi}")
    for key in ("complexity", "predict_complexity"):
        v = payload.get(key)
        if v is not None and v not in _COMPLEXITIES:
            raise TaskRequestError(f"{key} must be one of {'|'.join(_COMPLEXITIES)}")
    if payload.get("predict_duration") is not None:
        from .reflect import parse_duration

        try:
            parse_duration(payload["predict_duration"])
        except Exception as exc:  # click.BadParameter et al.
            raise TaskRequestError(f"bad predict_duration: {exc}") from exc
    for dep in payload.get("depends") or []:
        if not _is_safe_id(dep):
            raise TaskRequestError(f"depends entry is not a task id: {dep!r}")
    for entry in payload.get("scope") or []:
        if not _safe_scope_entry(entry):
            raise TaskRequestError(f"scope entry must be a relative path inside the repo: {entry!r}")
    for tag in payload.get("tags") or []:
        if not tag.strip() or len(tag) > 64 or _CTRL_RE.search(tag):
            raise TaskRequestError(f"bad tag: {tag!r}")
    if payload.get("success_criteria"):
        from .models import SuccessCriterion

        for crit in payload["success_criteria"]:
            try:
                SuccessCriterion.from_cli(crit)
            except Exception as exc:
                raise TaskRequestError(f"bad success_criteria entry {crit!r}: {exc}") from exc


def _has_result_reply(portfolio_root: Path, agent_id: str, msg_id: str) -> dict | None:
    """Return a prior result reply from ``agent_id`` to ``msg_id`` (resume guard)."""
    for f in _all_inbox_files(portfolio_root):
        for ev in _read_events(f):
            if (
                ev.get("event") == "message"
                and ev.get("in_reply_to") == msg_id
                and ev.get("from") == agent_id
                and isinstance(ev.get("payload"), dict)
                and ev["payload"].get("type") == TASK_REQUEST_RESULT
            ):
                return ev
    return None


def materialize_task_requests(
    config,
    agent_id: str,
    default_project: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Turn pending ``task_request`` messages for ``agent_id`` into real tasks.

    Runs in the RECEIVER's context via ``add_task``. Every field of a request is
    untrusted. Per message:

    - invalid payload, or malformed sender / project / msg id -> reply
      ``task_request_rejected`` (when the sender is a usable inbox id) + ack
      (permanent; retrying cannot help) -> ``rejected``
    - project well-formed but not registered -> left unacked, no reply ->
      ``failed`` (retryable)
    - ok -> ``add_task`` (stamping ``source_request: <msg_id>`` in the task's own
      frontmatter), reply ``task_request_result`` (in_reply_to), ack ->
      ``materialized``
    - anything unexpected -> ``failed`` for that message only; later requests
      still run.

    Crash window: if the process dies after ``add_task`` but before the reply, a
    re-run finds the task by its ``source_request`` key and only replies + acks
    (``resumed``) instead of creating a duplicate. Once the reply exists, a
    re-run only acks. The pre-checks above are not locked, but ``add_task``
    rechecks the ``source_request`` key inside its creation lock, so a CONCURRENT
    second materializer for the same agent reuses the first one's task
    (``resumed``) instead of creating a duplicate. Both may then send the result
    reply (same ``task_id``); the unlocked reply check cannot prevent that.

    ``dry_run`` writes nothing at all (no tasks, replies or acks).
    """
    from .discovery import discover_projects
    from .models import Predictions, SuccessCriterion, TaskComplexity
    from .reflect import parse_duration
    from .tasks import add_task_with_status, find_task_by_source_request

    validate_agent_id(agent_id)
    root = config.portfolio_root
    result: dict = {
        "agent": agent_id,
        "dry_run": dry_run,
        "materialized": [],
        "would_create": [],
        "rejected": [],
        "failed": [],
    }
    registered = {p.id for p in discover_projects(config)}

    def reject(msg_id, sender, project, reason: str) -> None:
        """Loud, permanent refusal: record, reply when possible, ack when possible."""
        result["rejected"].append({"msg_id": msg_id, "reason": reason})
        if dry_run or not isinstance(msg_id, str) or not msg_id:
            return
        if sender is not None:
            send_message(
                root, to=sender, from_agent=agent_id, in_reply_to=msg_id,
                project=project if _is_safe_id(project) else None,
                message=f"task_request {msg_id} rejected: {reason}",
                payload={"type": TASK_REQUEST_REJECTED, "reason": reason},
            )
        ack_messages(root, [msg_id], acked_by=agent_id)

    def reply_and_ack(msg_id, sender, project_id, task_id) -> None:
        send_message(
            root, to=sender, from_agent=agent_id, in_reply_to=msg_id,
            project=project_id, task=task_id,
            message=f"task_request {msg_id} materialized as {task_id} in {project_id}",
            payload={"type": TASK_REQUEST_RESULT, "task_id": task_id, "project": project_id},
        )
        ack_messages(root, [msg_id], acked_by=agent_id)

    def handle(msg: dict, payload: dict) -> None:
        msg_id = msg.get("msg_id")
        if not _is_safe_id(msg_id):
            # Nothing trustworthy to reply to or reference; report and move on.
            reject(msg_id if isinstance(msg_id, str) else None, None, None,
                   f"malformed msg_id {msg_id!r}")
            return

        raw_sender = msg.get("from") or "main"
        try:
            sender = validate_agent_id(raw_sender)
        except ValueError:
            # No safe inbox to reply to: refuse + ack (so it cannot wedge the drain).
            reject(msg_id, None, None, f"malformed sender {raw_sender!r}")
            return

        try:
            _validate_task_request(payload)
        except TaskRequestError as exc:
            reject(msg_id, sender, msg.get("project"), str(exc))
            return

        # Project: from the (untrusted) message/payload it must be a well-formed
        # registry id; the operator-supplied default is only ever retryable.
        claimed = msg.get("project") or payload.get("project")
        if claimed:
            if not _is_safe_id(claimed):
                reject(msg_id, sender, None, f"malformed project {claimed!r}")
                return
            project_id = claimed
        else:
            project_id = default_project
        if not _is_safe_id(project_id) or project_id not in registered:
            result["failed"].append({
                "msg_id": msg_id,
                "reason": f"project not resolvable: {project_id!r}",
            })
            return

        if dry_run:
            result["would_create"].append({
                "msg_id": msg_id, "project": project_id, "title": payload["title"],
                "from": sender,
            })
            return

        prior = _has_result_reply(root, agent_id, msg_id)
        if prior is not None:
            ack_messages(root, [msg_id], acked_by=agent_id)
            result["materialized"].append({
                "msg_id": msg_id, "project": project_id,
                "task_id": (prior.get("payload") or {}).get("task_id"),
                "resumed": True,
            })
            return

        # Crash window guard: a task already stamped with this request id means a
        # previous run created it but died before replying.
        existing = find_task_by_source_request(config, project_id, msg_id)
        if existing is not None:
            reply_and_ack(msg_id, sender, project_id, existing.id)
            result["materialized"].append({
                "msg_id": msg_id, "project": project_id, "task_id": existing.id,
                "title": existing.title, "resumed": True,
            })
            return

        has_pred = any(payload.get(k) is not None for k in (
            "predict_duration", "predict_complexity", "predict_approach",
            "confidence", "pre_mortem", "success_criteria"))
        predictions = None
        if has_pred:
            predictions = Predictions(
                duration_min=parse_duration(payload.get("predict_duration")),
                complexity=(TaskComplexity(payload["predict_complexity"])
                            if payload.get("predict_complexity") else None),
                success_criteria=[SuccessCriterion.from_cli(s)
                                  for s in payload.get("success_criteria") or []],
                approach=payload.get("predict_approach"),
                confidence=payload.get("confidence"),
                pre_mortem=payload.get("pre_mortem"),
                filled_by="agent",
            )

        try:
            task, created = add_task_with_status(
                config,
                project_id,
                payload["title"].strip(),
                priority=payload.get("priority") or 5,
                complexity=(TaskComplexity(payload["complexity"])
                            if payload.get("complexity") else None),
                depends=payload.get("depends") or None,
                scope=payload.get("scope") or None,
                tags=payload.get("tags") or None,
                description=payload.get("description") or "",
                predictions=predictions,
                source_request=msg_id,
            )
        except ValueError as exc:
            task, created, err = None, False, str(exc)
        else:
            err = f"could not create task in project {project_id!r}"
        if not task:
            result["failed"].append({"msg_id": msg_id, "reason": err})
            return

        reply_and_ack(msg_id, sender, project_id, task.id)
        rec = {
            "msg_id": msg_id, "project": project_id, "task_id": task.id,
            "title": task.title,
        }
        if not created:  # lost a race: add_task found the winner's task under its lock
            rec["resumed"] = True
        result["materialized"].append(rec)

    for msg in read_inbox(root, agent_id, unacked_only=True):
        payload = msg.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != TASK_REQUEST:
            continue
        try:
            handle(msg, payload)
        except Exception as exc:  # one bad request must never block the rest
            mid = msg.get("msg_id")
            result["failed"].append({
                "msg_id": mid if isinstance(mid, str) else None,
                "reason": f"{type(exc).__name__}: {exc}",
            })

    return result


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
