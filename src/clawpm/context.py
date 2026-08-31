"""Project context management for ClawPM."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

from .discovery import load_portfolio_config, get_project, is_git_repo, init_project_from_repo
from .models import ProjectSettings


CONTEXT_FILE = Path.home() / ".clawpm-context"


def detect_project_from_cwd() -> ProjectSettings | None:
    """Detect project from current working directory.
    
    Walks up from cwd looking for .project/settings.toml.
    Returns the project if found, None otherwise.
    """
    config = load_portfolio_config()
    if not config:
        return None
    
    cwd = Path.cwd().resolve()
    
    # Walk up looking for .project/settings.toml
    current = cwd
    while current != current.parent:
        settings_file = current / ".project" / "settings.toml"
        if settings_file.exists():
            try:
                return ProjectSettings.load(settings_file)
            except Exception:
                pass
        current = current.parent
    
    return None


def detect_untracked_repo_from_cwd() -> Path | None:
    """Detect if cwd is inside an untracked git repo.
    
    Returns the repo root path if found, None otherwise.
    """
    config = load_portfolio_config()
    if not config:
        return None
    
    cwd = Path.cwd().resolve()
    
    # Walk up looking for .git (but not .project)
    current = cwd
    while current != current.parent:
        if (current / ".git").exists() and not (current / ".project" / "settings.toml").exists():
            # Check if this is under a project_root
            for root in config.project_roots:
                try:
                    if root.resolve() in current.parents or root.resolve() == current.parent:
                        return current
                except Exception:
                    pass
        current = current.parent
    
    return None


def auto_init_if_untracked() -> ProjectSettings | None:
    """Auto-initialize a project if cwd is in an untracked git repo.
    
    Returns the newly created ProjectSettings, or None if not applicable.
    """
    repo_path = detect_untracked_repo_from_cwd()
    if repo_path:
        return init_project_from_repo(repo_path)
    return None


def get_context_project() -> str | None:
    """Get the project ID from context file."""
    if not CONTEXT_FILE.exists():
        return None
    
    try:
        content = CONTEXT_FILE.read_text(encoding="utf-8").strip()
        if content:
            return content
    except Exception:
        pass
    
    return None


def set_context_project(project_id: str | None) -> None:
    """Set the context project ID."""
    if project_id is None:
        if CONTEXT_FILE.exists():
            CONTEXT_FILE.unlink()
    else:
        CONTEXT_FILE.write_text(project_id, encoding="utf-8")


def resolve_project(explicit: str | None = None) -> tuple[str | None, str]:
    """Resolve project ID from explicit arg, cwd, or context.
    
    Returns: (project_id, source) where source is one of:
        - "explicit": from command line argument
        - "cwd": detected from current directory
        - "context": from `clawpm use` context
        - "none": no project found
    """
    # 1. Explicit takes precedence
    if explicit:
        return (explicit, "explicit")
    
    # 2. Check cwd
    project = detect_project_from_cwd()
    if project:
        return (project.id, "cwd")
    
    # 3. Check context file
    context_id = get_context_project()
    if context_id:
        return (context_id, "context")
    
    return (None, "none")


def get_project_prefix(project_id: str) -> str:
    """Get the task ID prefix for a project.
    
    Converts project ID to uppercase prefix, e.g.:
        - clawpm -> CLAWP
        - my-project -> MYPRO (first 5 chars, uppercase, no hyphens)
    """
    # Remove hyphens/underscores and uppercase
    clean = re.sub(r'[-_]', '', project_id).upper()
    # Take first 5 chars
    return clean[:5]


def expand_task_id(task_ref: str, project_id: str, prefix: str | None = None) -> str:
    """Expand a short task reference to full ID.

    Examples:
        - "22" -> "CLAWP-022" (for clawpm project)
        - "CLAWP-022" -> "CLAWP-022" (already full)
        - "022" -> "CLAWP-022"
        - "4-001" -> "CLAWP-004-001" (subtask)
        - "CLAWP-004-001" -> "CLAWP-004-001" (already full subtask)

    ``prefix`` overrides the project's task-ID prefix (CLAWP-084). Pass the
    project's RESOLVED prefix (explicit ``task_prefix`` -> inferred-from-tasks,
    via ``tasks.resolve_existing_prefix``) when a project mints task ids under a
    prefix that differs from the naive ``project_id[:5]`` — otherwise a short
    ref like ``1`` expands to the wrong id (e.g. ``ALPHA-001`` instead of the
    real ``SAME-001``) and short-ref ``--parent`` / ``--linked`` filters silently
    match nothing. ``None`` falls back to the naive id-derived prefix.
    """
    resolved_prefix = prefix if prefix else get_project_prefix(project_id)

    # Already has a prefix (contains hyphen and letters before it)
    # Match both PREFIX-NNN and PREFIX-NNN-NNN (subtask)
    if '-' in task_ref and re.match(r'^[A-Z]+-\d+(-\d+)?$', task_ref.upper()):
        return task_ref.upper()

    # Subtask short ID: "4-001" or "004-001" -> "PREFIX-004-001"
    subtask_match = re.match(r'^(\d+)-(\d+)$', task_ref)
    if subtask_match:
        parent_num = int(subtask_match.group(1))
        sub_num = int(subtask_match.group(2))
        return f"{resolved_prefix}-{parent_num:03d}-{sub_num:03d}"

    # Pure numeric - expand with project prefix
    if task_ref.isdigit():
        num = int(task_ref)
        return f"{resolved_prefix}-{num:03d}"

    # Return as-is if unrecognized format
    return task_ref


def build_agent_context(config, project_id: str, source: str = "explicit", log_limit: int = 5) -> dict | None:
    """Assemble the full agent-resume context for a project (CLAWP-068).

    Returns the same dict the ``clawpm context`` command renders — project
    metadata, truncated spec, in-progress / next / blocked tasks (with
    wiki-link backlinks), open counts, recent work-log, git status, and open
    issues — or ``None`` when the project can't be resolved. Extracted from the
    CLI command so the ``context`` MCP tool and the CLI share ONE
    implementation and can never drift (the tool wraps this core function
    directly rather than shelling out).

    ``get_project`` and the git enrichment mirror the CLI exactly; imports are
    function-local because ``tasks``/``links``/``worklog`` import back into this
    module (``expand_task_id``), which would be a circular import at module
    load.
    """
    from .tasks import get_next_task, list_tasks
    from .worklog import tail_entries
    from .links import build_link_index
    from .models import Task, TaskState

    proj = get_project(config, project_id)
    if not proj:
        return None

    context: dict = {
        "project": {
            "id": proj.id,
            "name": proj.name,
            "status": proj.status.value,
            "priority": proj.priority,
            "labels": proj.labels,
            "repo_path": str(proj.repo_path) if proj.repo_path else None,
        },
        "source": source,
    }

    # Read spec if exists (truncated for LLM consumption)
    if proj.project_dir:
        spec_file = proj.project_dir / ".project" / "SPEC.md"
        if spec_file.exists():
            spec_content = spec_file.read_text(encoding="utf-8")
            if len(spec_content) > 2000:
                context["spec"] = spec_content[:2000] + "\n\n[...truncated...]"
            else:
                context["spec"] = spec_content

    # CLAWP-082 — derived link index once; attach backlinks to every task dict.
    _link_index = build_link_index(config, project_id)

    def _with_backlinks(t: Task) -> dict:
        d = t.to_dict()
        d["linked_from"] = _link_index.linked_from(t.id)
        return d

    in_progress = list_tasks(config, project_id, state_filter=TaskState.PROGRESS)
    context["in_progress"] = [_with_backlinks(t) for t in in_progress]

    if not in_progress:
        next_task = get_next_task(config, project_id)
        if next_task:
            context["next_task"] = _with_backlinks(next_task)

    blocked = list_tasks(config, project_id, state_filter=TaskState.BLOCKED)
    context["blockers"] = [_with_backlinks(t) for t in blocked]

    open_tasks = list_tasks(config, project_id, state_filter=TaskState.OPEN)
    context["open_count"] = len(open_tasks)

    recent_entries = tail_entries(config, project=project_id, limit=log_limit)
    context["recent_work"] = [e.to_dict() for e in recent_entries]

    # Git status if repo_path exists (same enrichment the CLI does; direct git,
    # not a clawpm shell-out).
    if proj.repo_path and proj.repo_path.exists():
        git_status: dict = {}
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=proj.repo_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0:
                git_status["branch"] = result.stdout.strip()

            result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=proj.repo_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0:
                changes = [line for line in result.stdout.strip().split("\n") if line]
                git_status["uncommitted_count"] = len(changes)
                if changes:
                    git_status["uncommitted"] = changes[:10]
                    if len(changes) > 10:
                        git_status["uncommitted"].append(f"... and {len(changes) - 10} more")

            result = subprocess.run(
                ["git", "log", "--oneline", "-3"],
                cwd=proj.repo_path, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode == 0:
                git_status["recent_commits"] = [line for line in result.stdout.strip().split("\n") if line]
        except Exception as exc:
            # Best-effort enrichment (git may be absent, repo_path may be stale,
            # etc.) — swallow rather than fail the whole context call, but flag
            # it so a caller (an MCP host in particular, which never sees this
            # process's stderr) can tell "degraded" apart from "no git repo"
            # (CLAWP-068 review F9).
            git_status["error"] = str(exc)

        if git_status:
            context["git"] = git_status

    # Open issues
    if proj.project_dir:
        issues_file = proj.project_dir / ".agent" / "issues.jsonl"
        if issues_file.exists():
            try:
                open_issues = []
                with open(issues_file, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            issue = json.loads(line)
                            if not issue.get("fixed"):
                                open_issues.append({
                                    "type": issue.get("type"),
                                    "severity": issue.get("severity"),
                                    "summary": (issue.get("actual") or issue.get("context", ""))[:100],
                                })
                if open_issues:
                    context["open_issues"] = open_issues[:5]
            except Exception as exc:
                # Same rationale as the git-status catch above: flag a parse
                # failure instead of silently reading as "no open issues"
                # (CLAWP-068 review F9).
                context["open_issues_error"] = str(exc)

    return context
