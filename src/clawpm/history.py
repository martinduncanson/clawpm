"""Synthesise reflection events from historical agent log files.

Generic JSONL log scanner. Walks a user-supplied source directory and extracts
entries that reference clawpm task IDs (the `PREFIX-NNN` pattern). No hardcoded
paths — the source comes from `--source <dir>` or `CLAWPM_HISTORY_SOURCE`.

This module is lazy-imported by the CLI command in `clawpm.cli` to keep the
binary's static import graph minimal — see `reflect history-import` in cli.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_OUTPUT_LIMIT = 500
"""Truncate per-entry text snippets to N chars to bound output size."""

TASK_ID_RE = re.compile(
    r"\b("
    r"(?:"
    # Multi-segment: each segment >=1 letter, 2-5 segments total, each
    # capped at 10 chars. Catches multi-hyphen prefixes like MY-PR or
    # A-B-C produced by project-id normalisation.
    r"[A-Z][A-Z0-9_]{0,9}(?:-[A-Z][A-Z0-9_]{0,9}){1,4}"
    r"|"
    # Single-segment: 2-10 chars (original shape, retained for back-compat).
    r"[A-Z][A-Z0-9_]{1,9}"
    r")"
    r"-\d{1,5}"
    r")\b"
)
"""Match clawpm task IDs. Examples: CLAWP-011, ALPHA-001, MY-PR-001, A-B-C-123.

Two prefix shapes covered:
  - Single-segment: ``CLAWP-011`` — 2-10 uppercase chars (letters/digits/_)
  - Multi-segment: ``MY-PR-001``, ``A-B-C-123`` — 2-5 segments joined by
    hyphens, each segment starts with an uppercase letter and is up to
    10 chars long

Codex PR#5 round-2 P1 fix: project IDs are normalised via
``re.sub(r'[^A-Z0-9]+', '-', raw_prefix).strip('-')``, which can produce
multi-hyphen prefixes like ``MY-PR``. The previous single-hyphen regex
matched ``MY-PR-001`` as ``PR-001``, corrupting ``by_task`` aggregation
and ``unique_task_ids`` in ``reflect history-import``. The alternation
keeps the original single-segment rejection of 1-char prefixes
(``X-001``) intact — only multi-segment prefixes can use a 1-char
first segment, because they're disambiguated by the additional
hyphenated segments."""


@dataclass
class TaskMention:
    """One entry in an agent log file that references a task_id."""

    task_id: str
    timestamp: str
    log_file: str
    line_no: int
    text_snippet: str

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "timestamp": self.timestamp,
            "log_file": self.log_file,
            "line_no": self.line_no,
            "text_snippet": self.text_snippet,
        }


def find_log_files(source_dir: Path) -> list[Path]:
    """Find all .jsonl files in source_dir (recursive)."""
    if not source_dir.is_dir():
        return []
    return sorted(source_dir.rglob("*.jsonl"))


def _extract_timestamp(raw: dict) -> str:
    """Best-effort timestamp extraction from heterogeneous log shapes.

    Different agent runtimes (Claude Code, OpenClaw, generic) use different keys.
    Try the common ones in order; return empty string if none found.
    """
    for key in ("timestamp", "ts", "occurred_at", "created_at", "time"):
        val = raw.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, (int, float)):
            return str(val)
    msg = raw.get("message", {})
    if isinstance(msg, dict):
        for key in ("timestamp", "ts", "created_at"):
            val = msg.get(key)
            if isinstance(val, (str, int, float)):
                return str(val)
    return ""


def extract_task_mentions(
    log_path: Path,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
) -> list[TaskMention]:
    """Parse a .jsonl log file; return entries referencing task IDs.

    For each line that contains a task-ID pattern, parse the JSON and emit one
    TaskMention per distinct task_id mentioned. Truncates the text snippet to
    `output_limit` chars to bound aggregate output size.

    Errors are tolerated silently (unreadable file → []; malformed JSON line →
    skipped). The CLI wrapper surfaces a summary of files processed.
    """
    mentions: list[TaskMention] = []
    try:
        with open(log_path, encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                # Cheap pre-filter: skip lines without any task_id pattern
                if not TASK_ID_RE.search(line):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                text = json.dumps(raw)
                ts = _extract_timestamp(raw)
                seen_in_line: set[str] = set()
                for match in TASK_ID_RE.finditer(text):
                    task_id = match.group(1)
                    if task_id in seen_in_line:
                        continue
                    seen_in_line.add(task_id)
                    mentions.append(TaskMention(
                        task_id=task_id,
                        timestamp=ts,
                        log_file=log_path.as_posix(),
                        line_no=line_no,
                        text_snippet=text[:output_limit],
                    ))
    except OSError:
        return []
    return mentions


def import_history(
    source_dir: Path,
    output_limit: int = DEFAULT_OUTPUT_LIMIT,
    max_files: int = 1000,
) -> dict:
    """Scan source_dir for task mentions; return aggregate report.

    Bounded by max_files to avoid runaway scans on large log archives. Returns:
        {
            "source_dir": str,
            "files_scanned": int,
            "files_truncated": bool,  # True if max_files cap hit
            "mentions_found": int,
            "unique_task_ids": int,
            "by_task": {task_id: count},
            "mentions": [TaskMention as dict, ...],  # limited per-task
        }
    """
    files = find_log_files(source_dir)
    truncated = False
    if len(files) > max_files:
        files = files[:max_files]
        truncated = True

    all_mentions: list[TaskMention] = []
    for f in files:
        all_mentions.extend(extract_task_mentions(f, output_limit))

    by_task: dict[str, int] = {}
    for m in all_mentions:
        by_task[m.task_id] = by_task.get(m.task_id, 0) + 1

    return {
        "source_dir": source_dir.as_posix(),
        "files_scanned": len(files),
        "files_truncated": truncated,
        "mentions_found": len(all_mentions),
        "unique_task_ids": len(by_task),
        "by_task": dict(sorted(by_task.items())),
        "mentions": [m.to_dict() for m in all_mentions],
    }
