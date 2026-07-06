"""Project context management for ClawPM."""

from __future__ import annotations

import os
import re
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
