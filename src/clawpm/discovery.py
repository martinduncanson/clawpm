"""Project and portfolio discovery for ClawPM."""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import PortfolioConfig, ProjectSettings, ProjectStatus

logger = logging.getLogger(__name__)


@dataclass
class UntrackedRepo:
    """A git repo without .project/ tracking."""

    path: Path
    name: str
    remote: str | None = None

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "name": self.name,
            "remote": self.remote,
            "tracked": False,
        }


def path_for_config(p: Path) -> str:
    """Convert a path to a config-friendly string, using ~/ when possible.

    Always uses forward slashes so the result is valid in TOML string literals
    on all platforms (backslashes are TOML escape sequences and break parsing).
    """
    try:
        relative = p.relative_to(Path.home())
        # Use posix separators so ~/ paths are cross-platform and TOML-safe
        return f"~/{relative.as_posix()}"
    except ValueError:
        return p.as_posix()


def get_portfolio_path() -> Path | None:
    """Get the portfolio path from default location or environment override.

    Returns the portfolio root directory, or None if not found.
    Note: Even if None is returned, load_portfolio_config() will use defaults.
    """
    # Environment override takes priority
    if env_path := os.environ.get("CLAWPM_PORTFOLIO"):
        path = Path(env_path).expanduser()
        if path.exists():
            return path

    # Default location: ~/clawpm
    default = Path.home() / "clawpm"
    if default.exists():
        return default

    return None


def load_portfolio_config(portfolio_path: Path | None = None) -> PortfolioConfig | None:
    """Load portfolio configuration.

    If portfolio.toml exists, loads it. Otherwise creates a default config
    with sensible defaults (~/clawpm/projects as project root).

    Environment variables:
      CLAWPM_PORTFOLIO: Override portfolio root directory
      CLAWPM_PROJECT_ROOTS: Colon-separated list of additional project roots
      CLAWPM_WORKSPACE: Override OpenClaw workspace path
    """
    if portfolio_path is None:
        portfolio_path = get_portfolio_path()

    # Try loading from portfolio.toml if it exists
    if portfolio_path:
        config_file = portfolio_path / "portfolio.toml"
        if config_file.exists():
            config = PortfolioConfig.load(config_file)
            # Merge in env var project roots
            config = _merge_env_project_roots(config)
            return config

    # No portfolio.toml - use defaults
    return _default_portfolio_config()


def _default_portfolio_config() -> PortfolioConfig:
    """Create a default portfolio config with sensible defaults."""
    from .models import PortfolioConfig, ProjectStatus

    portfolio_root = Path.home() / "clawpm"

    # Default project roots: ~/clawpm/projects
    project_roots = [portfolio_root / "projects"]

    # Add any from environment
    if env_roots := os.environ.get("CLAWPM_PROJECT_ROOTS"):
        for root in env_roots.split(":"):
            if root.strip():
                project_roots.append(Path(root.strip()).expanduser())

    # OpenClaw workspace from env or default
    openclaw_workspace = None
    if ws := os.environ.get("CLAWPM_WORKSPACE"):
        openclaw_workspace = Path(ws).expanduser()
    else:
        default_ws = Path.home() / ".openclaw" / "workspace"
        if default_ws.exists():
            openclaw_workspace = default_ws

    return PortfolioConfig(
        portfolio_root=portfolio_root,
        project_roots=project_roots,
        default_status=ProjectStatus.ACTIVE,
        openclaw_workspace=openclaw_workspace,
    )


def _merge_env_project_roots(config: PortfolioConfig) -> PortfolioConfig:
    """Merge environment variable project roots into config."""
    if env_roots := os.environ.get("CLAWPM_PROJECT_ROOTS"):
        for root in env_roots.split(":"):
            if root.strip():
                path = Path(root.strip()).expanduser()
                if path not in config.project_roots:
                    config.project_roots.append(path)
    return config


def discover_projects(
    config: PortfolioConfig,
    status_filter: ProjectStatus | None = None,
) -> list[ProjectSettings]:
    """Discover all projects in configured roots."""
    projects: list[ProjectSettings] = []

    for root in config.project_roots:
        if not root.exists():
            continue

        # Skip OpenClaw workspace if configured
        if config.openclaw_workspace and root == config.openclaw_workspace:
            continue

        # Look for .project/settings.toml in immediate subdirectories
        for item in root.iterdir():
            if not item.is_dir():
                continue

            settings_file = item / ".project" / "settings.toml"
            if settings_file.exists():
                try:
                    project = ProjectSettings.load(settings_file)

                    # Apply status filter
                    if status_filter is not None and project.status != status_filter:
                        continue

                    projects.append(project)
                except Exception:
                    # Skip malformed projects
                    continue

    # Sort by priority (lower is higher priority), then by name
    projects.sort(key=lambda p: (p.priority, p.name))

    return projects


def get_project(config: PortfolioConfig, project_id: str) -> ProjectSettings | None:
    """Get a specific project by ID."""
    for root in config.project_roots:
        if not root.exists():
            continue

        # Check direct match first
        direct = root / project_id / ".project" / "settings.toml"
        if direct.exists():
            try:
                return ProjectSettings.load(direct)
            except Exception as exc:
                logger.warning(
                    "Failed to load settings.toml for '%s' at %s: %s. "
                    "The file may contain backslashes in repo_path - use forward slashes.",
                    project_id, direct, exc,
                )
                # Fall through to full scan in case of id mismatch; don't return yet

        # Search all projects
        for item in root.iterdir():
            if not item.is_dir():
                continue

            settings_file = item / ".project" / "settings.toml"
            if settings_file.exists():
                try:
                    project = ProjectSettings.load(settings_file)
                    if project.id == project_id:
                        return project
                except Exception as exc:
                    logger.warning(
                        "Failed to load settings.toml at %s: %s. "
                        "The file may contain backslashes in repo_path - use forward slashes.",
                        settings_file, exc,
                    )
                    continue

    return None


def get_project_dir(config: PortfolioConfig, project_id: str) -> Path | None:
    """Get the .project directory for a project.

    Returns the ``.project/`` directory path (not the repo root) when found,
    or ``None`` if the project cannot be located via the portfolio registry.

    Use :func:`find_project_dir_fallback` if you need a best-effort lookup
    that also checks the CWD walk when the registry lookup fails.
    """
    project = get_project(config, project_id)
    if project and project.project_dir:
        return project.project_dir / ".project"
    return None


def _read_project_id_from_settings(settings_file: Path) -> str | None:
    """Extract the ``id`` field from a settings.toml using TOML parse first,
    then a regex fallback for malformed files (e.g. Windows backslashes).

    Returns the id string or None if it cannot be determined.
    """
    try:
        project = ProjectSettings.load(settings_file)
        return project.id
    except Exception:
        pass

    # TOML parse failed - try regex extraction of the id field only.
    # This handles the common case where only repo_path has backslashes.
    try:
        text = settings_file.read_text(encoding="utf-8", errors="replace")
        match = re.search(r'^id\s*=\s*"([^"]+)"', text, re.MULTILINE)
        if match:
            return match.group(1)
    except Exception:
        pass

    return None


def find_project_dir_fallback(config: PortfolioConfig, project_id: str) -> Path | None:
    """Locate the ``.project/`` directory for *project_id* when the registry fails.

    The portfolio registry (``get_project``) can fail to load a project when its
    ``settings.toml`` is malformed (e.g. Windows backslashes in ``repo_path``).
    This function tries an additional approach: walk up from the current working
    directory looking for a ``.project/settings.toml`` whose ``id`` field matches.

    The id is extracted with a regex fallback so malformed TOML files (where only
    ``repo_path`` is broken) do not block the lookup.

    Returns the ``.project/`` directory if found, ``None`` otherwise.
    Logs an info-level note about the divergence so it shows up in debug output
    without spamming normal operation.
    """
    cwd = Path.cwd().resolve()
    current = cwd
    while current != current.parent:
        settings_file = current / ".project" / "settings.toml"
        if settings_file.exists():
            found_id = _read_project_id_from_settings(settings_file)
            if found_id == project_id:
                logger.info(
                    "Project '%s' found via CWD walk at %s but not via portfolio registry. "
                    "Check settings.toml for backslashes in repo_path (use forward slashes).",
                    project_id, settings_file,
                )
                return current / ".project"
        current = current.parent
    return None


def discover_untracked_repos(config: PortfolioConfig) -> list[UntrackedRepo]:
    """Discover git repos in project_roots that don't have .project/ tracking."""
    untracked: list[UntrackedRepo] = []

    # Get IDs of tracked projects to exclude
    tracked_paths = set()
    for root in config.project_roots:
        if not root.exists():
            continue
        for item in root.iterdir():
            if not item.is_dir():
                continue
            if (item / ".project" / "settings.toml").exists():
                tracked_paths.add(item.resolve())

    # Find git repos without .project/
    for root in config.project_roots:
        if not root.exists():
            continue

        # Skip OpenClaw workspace
        if config.openclaw_workspace and root == config.openclaw_workspace:
            continue

        for item in root.iterdir():
            if not item.is_dir():
                continue

            # Skip if already tracked
            if item.resolve() in tracked_paths:
                continue

            # Check if it's a git repo
            if not (item / ".git").exists():
                continue

            # Get remote URL if available
            remote = None
            try:
                result = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    cwd=item,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0:
                    remote = result.stdout.strip()
            except Exception:
                pass

            untracked.append(UntrackedRepo(
                path=item,
                name=item.name,
                remote=remote,
            ))

    # Sort by name
    untracked.sort(key=lambda r: r.name)

    return untracked


def is_git_repo(path: Path) -> bool:
    """Check if a path is a git repository."""
    return (path / ".git").exists()


def init_project_from_repo(repo_path: Path, project_id: str | None = None) -> ProjectSettings | None:
    """Initialize a .project/ structure in a git repo.

    Auto-detects project name from directory and remote.
    Returns the created ProjectSettings.
    """
    if not repo_path.is_dir():
        return None

    # Generate project ID from directory name if not provided
    if not project_id:
        project_id = repo_path.name.lower().replace(" ", "-").replace("_", "-")

    # Generate project name from directory or remote
    project_name = repo_path.name

    # Try to get a better name from git remote
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            remote = result.stdout.strip()
            # Extract repo name from remote URL
            # e.g., git@github.com:user/repo.git -> repo
            # or https://github.com/user/repo.git -> repo
            if "/" in remote:
                name = remote.split("/")[-1]
                if name.endswith(".git"):
                    name = name[:-4]
                project_name = name
    except Exception:
        pass

    # Create .project directory structure
    project_dir = repo_path / ".project"
    project_dir.mkdir(exist_ok=True)

    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    (tasks_dir / "done").mkdir(exist_ok=True)
    (tasks_dir / "blocked").mkdir(exist_ok=True)

    (project_dir / "research").mkdir(exist_ok=True)
    (project_dir / "notes").mkdir(exist_ok=True)

    # Write settings.toml - path_for_config produces forward slashes (TOML-safe)
    repo_path_str = path_for_config(repo_path)
    settings_content = f'''id = "{project_id}"
name = "{project_name}"
status = "active"
priority = 5
repo_path = "{repo_path_str}"
'''
    (project_dir / "settings.toml").write_text(settings_content, encoding="utf-8")

    # Create minimal SPEC.md
    spec_content = f'''# {project_name}

## Purpose

(Describe the purpose of this project)

## Goals

- (Add goals)

## Notes

Auto-initialized by clawpm from git repo.
'''
    (project_dir / "SPEC.md").write_text(spec_content, encoding="utf-8")

    # Create learnings.md
    (project_dir / "learnings.md").write_text(f"# Learnings - {project_name}\n\n", encoding="utf-8")

    # Load and return the project
    return ProjectSettings.load(project_dir / "settings.toml")


def validate_portfolio(config: PortfolioConfig) -> list[str]:
    """Validate portfolio configuration and return issues."""
    issues: list[str] = []

    # Check portfolio root exists
    if not config.portfolio_root.exists():
        issues.append(f"Portfolio root does not exist: {config.portfolio_root}")

    # Check project roots exist
    for root in config.project_roots:
        if not root.exists():
            issues.append(f"Project root does not exist: {root}")

    # Check for OpenClaw workspace collision
    if config.openclaw_workspace:
        for root in config.project_roots:
            try:
                if root.resolve() == config.openclaw_workspace.resolve():
                    issues.append(
                        f"Project root overlaps with OpenClaw workspace: {root}"
                    )
                elif config.openclaw_workspace.resolve() in root.resolve().parents:
                    issues.append(
                        f"Project root is inside OpenClaw workspace: {root}"
                    )
            except Exception:
                pass

    # Check work log exists
    work_log = config.portfolio_root / "work_log.jsonl"
    if not work_log.exists():
        issues.append(f"Work log does not exist: {work_log}")

    return issues
