"""Locked, surgical writer for a task's ``pr_verification`` record (CLAWP-151).

``record_pr_verification`` stores the outcome of a merged-mode ``verify_pr`` on
the task file as ONE top-level frontmatter line::

    pr_verification: {"pr": 7, "head": "<40 hex>", ...}

The JSON flow mapping is valid YAML, so ``Task.from_file`` can read it back.
Like ``tasks._set_updated_line`` this is a byte-level line splice, not a YAML
round-trip: comments, key order, body, BOM and CRLF/LF endings outside the
replaced value are preserved exactly.  The value's extent comes from the YAML
parser's event marks (never from line heuristics), and the writer refuses --
writing nothing -- whenever the frontmatter cannot be proven to be a plain
block mapping it can splice without disturbing any other key.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from clawpm.concurrency import file_lock, retry_transient
from clawpm.discovery import get_project_dir
from clawpm.models import PortfolioConfig
from clawpm.sessions import Scope
from clawpm.tasks import _candidate_task_paths, get_tasks_dir

RECORD_KEYS = ("pr", "head", "merge_commit", "verified_at", "remote", "repo")
_KEY = "pr_verification"
# The only raw spellings of the key this writer will replace (column 0).
_KEY_SPELLINGS = (_KEY, f'"{_KEY}"', f"'{_KEY}'")
# A task reference is a bare id (PREFIX-NNN[-NNN]); never a path.
_TASK_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)*-\d+(?:-\d+)?")
_BOM = "﻿"
# Line breaks YAML honours beyond "\n": the parser's line numbers would no longer
# match a split on "\n", so frontmatter containing them is refused.
_ODD_BREAKS = ("\r", "\x85", " ", " ")


class PrRecordError(Exception):
    """The record was NOT written (``code`` says why); the task file is untouched."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_task_ref(task_id: str) -> None:
    """Raise ``bad_task_ref`` unless ``task_id`` is a bare task id (no separators, ``..`` or drive)."""
    if not isinstance(task_id, str) or ".." in task_id or not _TASK_REF_RE.fullmatch(task_id):
        raise PrRecordError("bad_task_ref", f"{task_id!r} is not a task id (expected PREFIX-NNN)")


def _refuse(message: str) -> PrRecordError:
    return PrRecordError("bad_frontmatter", message)


def _top_level_pairs(fm_text: str) -> list[tuple[Any, list[Any]]]:
    """Parse ``fm_text`` into ``[(key_event, value_events), ...]`` for the root mapping.

    Raises ``bad_frontmatter`` unless the document is exactly one block-style root
    mapping whose top-level keys are plain scalars with no duplicate (compared
    after the parser decodes escapes and resolves quoting).  Works on parser
    events, which keep anchors/aliases/tags visible (the composer hides them).
    """
    try:
        events = list(yaml.parse(fm_text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise _refuse(f"task frontmatter is not parseable YAML: {exc}")
    starts = [e for e in events if isinstance(e, yaml.DocumentStartEvent)]
    if len(starts) != 1:
        raise _refuse("task frontmatter is not a mapping")
    if starts[0].explicit or any(
        isinstance(e, yaml.DocumentEndEvent) and e.explicit for e in events
    ):
        raise _refuse("task frontmatter contains a YAML document marker")
    root_idx = events.index(starts[0]) + 1
    root = events[root_idx]
    if not isinstance(root, yaml.MappingStartEvent):
        raise _refuse("task frontmatter is not a mapping")
    if root.flow_style:
        raise _refuse("task frontmatter is a flow mapping; only a block mapping can be spliced")

    items: list[list[Any]] = []
    cur: list[Any] = []
    depth = 0
    for ev in events[root_idx + 1:]:
        if isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            cur.append(ev)
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            if depth == 0:
                break
            depth -= 1
            cur.append(ev)
            if depth == 0:
                items.append(cur)
                cur = []
        else:
            cur.append(ev)
            if depth == 0:
                items.append(cur)
                cur = []
    if len(items) % 2:
        raise _refuse("task frontmatter has a key without a value")

    pairs: list[tuple[Any, list[Any]]] = []
    seen: set[str] = set()
    for key_item, value_item in zip(items[0::2], items[1::2]):
        if len(key_item) != 1 or not isinstance(key_item[0], yaml.ScalarEvent):
            raise _refuse("task frontmatter has a top-level key that is not a plain scalar (alias or collection)")
        name = key_item[0].value
        if name in seen:
            raise _refuse(f"task frontmatter has more than one top-level '{name}' key")
        seen.add(name)
        pairs.append((key_item[0], value_item))
    return pairs


def _value_extent(key_ev: Any, value_events: list[Any], fm_text: str, fm_lines: list[str]) -> tuple[int, int, str]:
    """Return ``(first_line, last_line, tail)`` of the existing key+value, 0-based within the frontmatter.

    ``tail`` is whatever follows the value on its last line (a trailing comment),
    kept so only the key and value are rewritten.  Refuses layouts it cannot
    replace exactly.
    """
    if key_ev.start_mark.column != 0:
        raise _refuse(f"'{_KEY}' key is indented; refusing to splice it")
    raw_key = fm_text[key_ev.start_mark.index:key_ev.end_mark.index]
    if raw_key not in _KEY_SPELLINGS:
        raise _refuse(f"task frontmatter holds a '{_KEY}' key in a spelling this writer cannot replace")
    flow_stack: list[bool] = []
    last: tuple[int, int] = (key_ev.end_mark.line, key_ev.end_mark.column)
    for ev in value_events:
        if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None) or getattr(ev, "tag", None):
            raise _refuse(f"'{_KEY}' value uses an anchor, alias or tag; refusing to splice it")
        if isinstance(ev, yaml.ScalarEvent):
            if ev.style in ("|", ">"):
                raise _refuse(f"'{_KEY}' value contains a block scalar; refusing to splice it")
            last = max(last, (ev.end_mark.line, ev.end_mark.column))
        elif isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            flow_stack.append(bool(ev.flow_style))
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            if flow_stack.pop():
                last = max(last, (ev.end_mark.line, ev.end_mark.column))
    last_line, last_col = last
    tail = fm_lines[last_line][last_col:]
    return key_ev.start_mark.line, last_line, tail if tail.strip() else ""


def _splice(text: str, record: dict[str, Any]) -> str:
    """Return ``text`` with the ``pr_verification`` line set, or raise PrRecordError."""
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    lines = body.split("\n")
    if not body.startswith("---") or lines[0].strip() != "---":
        raise PrRecordError("no_frontmatter", "task file has no leading '---' frontmatter fence")
    close_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close_idx is None:
        raise PrRecordError("no_frontmatter", "task file frontmatter fence is not terminated")
    fm_lines = [ln.rstrip("\r") for ln in lines[1:close_idx]]
    fm_text = "\n".join(fm_lines)
    if any(ch in fm_text for ch in _ODD_BREAKS):
        raise _refuse("task frontmatter contains a line separator other than LF/CRLF")
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise _refuse(f"task frontmatter is not parseable YAML: {exc}")
    if not isinstance(parsed, dict):
        raise _refuse("task frontmatter is not a mapping")
    pairs = _top_level_pairs(fm_text)

    eol = "\r" if lines[0].endswith("\r") else ""
    new_value = f"{_KEY}: " + json.dumps(record, sort_keys=True, separators=(", ", ": "))

    existing = next(((k, v) for k, v in pairs if k.value == _KEY), None)
    if existing is not None:
        first, last, tail = _value_extent(existing[0], existing[1], fm_text, fm_lines)
        lines[1 + first:1 + last + 1] = [new_value + tail + eol]
    else:
        lines.insert(close_idx, new_value + eol)
    out = "\n".join(lines)

    # Re-parse before anything is committed to disk: exactly one key, the record
    # reads back, and no OTHER top-level key changed.
    check_lines = out.split("\n")
    check_close = next(i for i in range(1, len(check_lines)) if check_lines[i].strip() == "---")
    check_text = "\n".join(ln.rstrip("\r") for ln in check_lines[1:check_close])
    try:
        check = yaml.safe_load(check_text)
        check_pairs = _top_level_pairs(check_text)
    except (yaml.YAMLError, PrRecordError) as exc:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not parse cleanly: {exc}")
    if sum(1 for k, _ in check_pairs if k.value == _KEY) != 1:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not hold exactly one '{_KEY}' key")
    if not isinstance(check, dict) or check.get(_KEY) != record:
        raise PrRecordError("verify_failed", "spliced pr_verification did not read back as the record")
    if {k: v for k, v in check.items() if k != _KEY} != {k: v for k, v in parsed.items() if k != _KEY}:
        raise PrRecordError("verify_failed", "splice changed a frontmatter key other than pr_verification")
    return bom + out


def _verified_tasks_dir(config: PortfolioConfig, project_id: str, scope: Scope | None) -> Path:
    """Return the tasks dir, proven to be the REAL ``<verified project dir>/tasks``.

    Both sides of the comparison derive from the verified project dir, never
    from the tasks dir itself, so a ``tasks`` symlink/junction that points at
    another project's store (whose real path "contains" itself) is caught.
    """
    kw = {} if scope is None else {"scope": scope}
    tasks_dir = get_tasks_dir(config, project_id, **kw)
    if tasks_dir is None:
        raise PrRecordError("task_not_found", f"no tasks directory for project {project_id!r}")
    project_dir = get_project_dir(config, project_id, **kw)
    if project_dir is None:
        raise PrRecordError("unsafe_store", f"cannot establish the verified project dir for {project_id!r}")
    expected = os.path.normcase(str(Path(project_dir).resolve() / "tasks"))
    actual = os.path.normcase(str(Path(tasks_dir).resolve()))
    if actual != expected or Path(tasks_dir).is_symlink():
        raise PrRecordError(
            "unsafe_store",
            f"tasks directory {str(tasks_dir)!r} is not the real 'tasks' dir of project {project_id!r} "
            f"(resolves to {actual!r}); refusing to write through a link",
        )
    return Path(tasks_dir)


def _require_regular(path: Path) -> os.stat_result:
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        raise PrRecordError("bad_task_ref", f"{str(path)!r} is a link, not a regular task file")
    return st


def _replace_file(path: Path, data: bytes) -> None:
    """Atomically replace regular file ``path`` with ``data`` via an exclusively created temp file."""
    st = _require_regular(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        _require_regular(path)  # re-check right before the swap
        retry_transient(os.replace, tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def record_pr_verification(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    record: dict[str, Any],
    *,
    scope: Scope | None = None,
):
    """Write ``record`` as the task's ``pr_verification`` line; return the task file Path.

    ``record`` must carry exactly :data:`RECORD_KEYS`.  Raises
    :class:`PrRecordError` (file untouched) on a bad record, a missing task, an
    unsafe (linked) store, or frontmatter that is absent, non-mapping,
    unparseable or not a layout the splice supports.  Holds the project's
    ``.clawpm-tasks.lock`` across the read-splice-replace.
    """
    validate_task_ref(task_id)
    if set(record) != set(RECORD_KEYS):
        raise PrRecordError(
            "bad_record", f"record keys must be exactly {sorted(RECORD_KEYS)}, got {sorted(record)}"
        )
    tasks_dir = _verified_tasks_dir(config, project_id, scope)
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        path = next((p for p in _candidate_task_paths(tasks_dir, task_id) if p.exists()), None)
        if path is None:
            raise PrRecordError("task_not_found", f"task {task_id!r} not found in project {project_id!r}")
        # Defence in depth: a symlinked file or state dir must not lead outside the store.
        if not path.resolve().is_relative_to(tasks_dir.resolve()):
            raise PrRecordError("bad_task_ref", f"task {task_id!r} resolves outside the project's tasks directory")
        _require_regular(path)
        # Bytes in, bytes out: text mode would fold/expand line endings.
        text = retry_transient(lambda: path.read_bytes()).decode("utf-8")
        _replace_file(path, _splice(text, record).encode("utf-8"))
    return path
