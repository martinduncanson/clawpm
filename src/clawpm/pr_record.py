"""Locked, surgical writer for a task's ``pr_verification`` record (CLAWP-151).

``record_pr_verification`` stores the outcome of a merged-mode ``verify_pr`` on
the task file as ONE top-level frontmatter line::

    pr_verification: {"pr": 7, "head": "<40 hex>", ...}

The JSON flow mapping is valid YAML, so ``Task.from_file`` can read it back.
Like ``tasks._set_updated_line`` this is a byte-level line splice, not a YAML
round-trip: comments, key order, body, BOM and CRLF/LF endings outside the one
line are preserved exactly.  It refuses -- writing nothing -- when the task is
missing or its frontmatter cannot be proven to be a mapping.
"""

from __future__ import annotations

import json
import re
from typing import Any

import yaml

from clawpm.concurrency import file_lock, retry_transient
from clawpm.models import PortfolioConfig
from clawpm.sessions import Scope
from clawpm.tasks import _candidate_task_paths, get_tasks_dir

RECORD_KEYS = ("pr", "head", "merge_commit", "verified_at", "remote", "repo")
_KEY = "pr_verification"
_KEY_LINE_RE = re.compile(r"^pr_verification\s*:")
_BOM = "﻿"


class PrRecordError(Exception):
    """The record was NOT written (``code`` says why); the task file is untouched."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


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
    try:
        parsed = yaml.safe_load("\n".join(ln.rstrip("\r") for ln in lines[1:close_idx]))
    except yaml.YAMLError as exc:
        raise PrRecordError("bad_frontmatter", f"task frontmatter is not parseable YAML: {exc}")
    if not isinstance(parsed, dict):
        raise PrRecordError("bad_frontmatter", "task frontmatter is not a mapping")

    eol = "\r" if lines[0].endswith("\r") else ""
    new_line = f"{_KEY}: " + json.dumps(record, sort_keys=True, separators=(", ", ": ")) + eol

    hits = [i for i in range(1, close_idx) if _KEY_LINE_RE.match(lines[i])]
    if len(hits) > 1:
        raise PrRecordError("bad_frontmatter", f"task frontmatter has {len(hits)} '{_KEY}' keys")
    if hits:
        start = hits[0]
        end = start + 1
        while end < close_idx and (not lines[end].strip() or lines[end][:1] in (" ", "\t")):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        lines[start:end] = [new_line]
    else:
        lines.insert(close_idx, new_line)
    out = "\n".join(lines)

    # Read the value back before anything is committed to disk.
    check_lines = out.split("\n")
    check_close = next(i for i in range(1, len(check_lines)) if check_lines[i].strip() == "---")
    try:
        check = yaml.safe_load("\n".join(ln.rstrip("\r") for ln in check_lines[1:check_close]))
    except yaml.YAMLError as exc:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not parse: {exc}")
    if not isinstance(check, dict) or check.get(_KEY) != record:
        raise PrRecordError("verify_failed", "spliced pr_verification did not read back as the record")
    return bom + out


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
    :class:`PrRecordError` (file untouched) on a bad record, a missing task, or
    frontmatter that is absent, non-mapping or unparseable.  Holds the project's
    ``.clawpm-tasks.lock`` across the read-splice-replace.
    """
    if set(record) != set(RECORD_KEYS):
        raise PrRecordError(
            "bad_record", f"record keys must be exactly {sorted(RECORD_KEYS)}, got {sorted(record)}"
        )
    tasks_dir = (
        get_tasks_dir(config, project_id)
        if scope is None
        else get_tasks_dir(config, project_id, scope=scope)
    )
    if tasks_dir is None:
        raise PrRecordError("task_not_found", f"no tasks directory for project {project_id!r}")
    with file_lock(tasks_dir / ".clawpm-tasks.lock"):
        path = next((p for p in _candidate_task_paths(tasks_dir, task_id) if p.exists()), None)
        if path is None:
            raise PrRecordError("task_not_found", f"task {task_id!r} not found in project {project_id!r}")
        # Bytes in, bytes out: text mode would fold/expand line endings.
        text = retry_transient(lambda: path.read_bytes()).decode("utf-8")
        new_text = _splice(text, record)
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp.write_bytes(new_text.encode("utf-8"))
            retry_transient(tmp.replace, path)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
    return path
