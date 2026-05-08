"""Work log operations for ClawPM."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .models import PortfolioConfig, WorkLogEntry, WorkLogAction


def get_worklog_path(config: PortfolioConfig) -> Path:
    """Get the work log file path."""
    return config.portfolio_root / "work_log.jsonl"


def add_entry(
    config: PortfolioConfig,
    project: str,
    action: WorkLogAction,
    task: str | None = None,
    summary: str | None = None,
    next_steps: str | None = None,
    files_changed: list[str] | None = None,
    blockers: str | None = None,
    agent: str = "main",
    session_key: str | None = None,
    auto: bool = False,
    commit_hash: str | None = None,
    ts: datetime | None = None,
) -> WorkLogEntry:
    """Add an entry to the work log."""
    entry = WorkLogEntry(
        ts=ts or datetime.now(timezone.utc),
        project=project,
        action=action,
        task=task,
        summary=summary,
        next=next_steps,
        files_changed=files_changed,
        blockers=blockers,
        agent=agent,
        session_key=session_key,
        auto=auto,
        commit_hash=commit_hash,
    )

    worklog_path = get_worklog_path(config)

    # Ensure parent directory exists
    worklog_path.parent.mkdir(parents=True, exist_ok=True)

    # Append to file — utf-8 so summaries with non-Latin chars survive on Windows
    with open(worklog_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")

    return entry


def read_entries(
    config: PortfolioConfig,
    project: str | None = None,
    limit: int | None = None,
) -> list[WorkLogEntry]:
    """Read entries from the work log."""
    worklog_path = get_worklog_path(config)

    if not worklog_path.exists():
        return []

    entries: list[WorkLogEntry] = []

    with open(worklog_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                data = json.loads(line)
                entry = WorkLogEntry.from_dict(data)

                # Apply project filter
                if project is not None and entry.project != project:
                    continue

                entries.append(entry)
            except (json.JSONDecodeError, KeyError, ValueError):
                # Skip malformed entries
                continue

    # Sort by timestamp descending (most recent first)
    entries.sort(key=lambda e: e.ts, reverse=True)

    # Apply limit
    if limit is not None:
        entries = entries[:limit]

    return entries


def get_last_entry(
    config: PortfolioConfig,
    project: str | None = None,
) -> WorkLogEntry | None:
    """Get the most recent work log entry."""
    entries = read_entries(config, project=project, limit=1)
    return entries[0] if entries else None


def get_logged_commit_hashes(
    config: PortfolioConfig,
    project: str | None = None,
) -> set[str]:
    """Get set of commit hashes already logged (from commit_hash field)."""
    worklog_path = get_worklog_path(config)
    if not worklog_path.exists():
        return set()

    hashes: set[str] = set()
    with open(worklog_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                if data.get("action") == "commit" and data.get("commit_hash"):
                    if project is None or data.get("project") == project:
                        hashes.add(data["commit_hash"])
            except (json.JSONDecodeError, KeyError):
                continue

    return hashes


def tail_entries(
    config: PortfolioConfig,
    project: str | None = None,
    limit: int = 20,
) -> list[WorkLogEntry]:
    """Get the most recent entries (tail)."""
    entries = read_entries(config, project=project, limit=limit)
    # Reverse to show oldest first (like tail)
    return list(reversed(entries))
