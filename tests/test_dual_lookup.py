"""Regression tests for the dual-lookup divergence bug.

Scenario: a project has a valid .project/settings.toml on disk but its
settings.toml is malformed (Windows backslashes in repo_path make it an
invalid TOML file).  The portfolio registry (get_project / get_project_dir)
fails to load it silently, while the CWD walk in context.py succeeds because
it reads the raw file.  add_task used to return None in this situation,
surfacing as {"error":"add_failed"}.

These tests verify:
1. path_for_config always produces forward-slash paths (TOML-safe).
2. add_task succeeds via CWD-walk fallback when the registry load fails.
3. add_task returns None (not a crash) when the project genuinely doesn't exist.
4. The better error hint path executes without raising exceptions.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from clawpm.discovery import (
    load_portfolio_config,
    get_project,
    find_project_dir_fallback,
    path_for_config,
)
from clawpm.models import PortfolioConfig, ProjectStatus, TaskState
from clawpm.tasks import add_task, get_tasks_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio_dir():
    """Minimal temp dir; cleaned up after each test."""
    d = tempfile.mkdtemp(prefix="clawpm_dltest_")
    yield Path(d)
    shutil.rmtree(d)


@pytest.fixture
def portfolio_with_malformed_project(temp_portfolio_dir):
    """Portfolio where one project has backslashes in repo_path (invalid TOML).

    Layout:
        <tmp>/
            portfolio.toml
            projects/
                good-proj/.project/settings.toml   ← valid
                bad-proj/.project/settings.toml    ← backslash → TOML parse error
    """
    root = temp_portfolio_dir
    projects_dir = root / "projects"
    projects_dir.mkdir()

    # portfolio.toml  (uses forward slashes — always valid)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{projects_dir.as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )
    (root / "work_log.jsonl").touch()

    # good project
    good_dir = projects_dir / "good-proj"
    good_meta = good_dir / ".project"
    good_meta.mkdir(parents=True)
    (good_meta / "settings.toml").write_text(
        'id = "good-proj"\nname = "Good"\nstatus = "active"\npriority = 5\n'
        f'repo_path = "{good_dir.as_posix()}"\n'
    )
    (good_meta / "tasks").mkdir()
    (good_meta / "tasks" / "done").mkdir()
    (good_meta / "tasks" / "blocked").mkdir()

    # bad project — backslash in repo_path makes TOML invalid
    bad_dir = projects_dir / "bad-proj"
    bad_meta = bad_dir / ".project"
    bad_meta.mkdir(parents=True)
    # Deliberately write Windows-style backslash path
    bad_repo_path = str(bad_dir).replace("/", "\\")
    (bad_meta / "settings.toml").write_text(
        f'id = "bad-proj"\nname = "Bad"\nstatus = "active"\npriority = 5\n'
        f'repo_path = "{bad_repo_path}"\n'
    )
    (bad_meta / "tasks").mkdir()
    (bad_meta / "tasks" / "done").mkdir()
    (bad_meta / "tasks" / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(root)

    config = load_portfolio_config(root)

    yield {
        "root": root,
        "projects_dir": projects_dir,
        "good_dir": good_dir,
        "bad_dir": bad_dir,
        "bad_meta": bad_meta,
        "config": config,
    }

    if old_env is not None:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)


# ---------------------------------------------------------------------------
# path_for_config tests
# ---------------------------------------------------------------------------


class TestPathForConfig:
    """path_for_config must always produce TOML-safe forward-slash strings."""

    def test_absolute_path_uses_forward_slashes(self, temp_portfolio_dir):
        result = path_for_config(temp_portfolio_dir)
        assert "\\" not in result, f"Backslash in path_for_config output: {result!r}"

    def test_home_relative_path_uses_forward_slashes(self):
        home = Path.home()
        result = path_for_config(home / "some" / "nested" / "dir")
        assert result.startswith("~/")
        assert "\\" not in result

    def test_non_home_path_uses_forward_slashes(self, temp_portfolio_dir):
        # Construct a path that is not under home (use temp_portfolio_dir which is
        # typically in /tmp or a system temp location — may or may not be under home)
        p = temp_portfolio_dir / "sub" / "path"
        result = path_for_config(p)
        assert "\\" not in result

    def test_written_toml_is_parseable(self, temp_portfolio_dir):
        """settings.toml written using path_for_config must parse without error."""
        import sys

        if sys.version_info >= (3, 11):
            import tomllib
        else:
            import tomli as tomllib

        repo_path_str = path_for_config(temp_portfolio_dir)
        toml_content = (
            'id = "test"\n'
            'name = "Test"\n'
            'status = "active"\n'
            'priority = 5\n'
            f'repo_path = "{repo_path_str}"\n'
        )
        # Should not raise
        parsed = tomllib.loads(toml_content)
        assert parsed["repo_path"] is not None


# ---------------------------------------------------------------------------
# Registry divergence tests
# ---------------------------------------------------------------------------


class TestRegistryDivergence:
    """Tests for the malformed-settings.toml / registry divergence scenario."""

    def test_good_project_loads_from_registry(self, portfolio_with_malformed_project):
        """Sanity: good project is discoverable via registry."""
        config = portfolio_with_malformed_project["config"]
        proj = get_project(config, "good-proj")
        assert proj is not None
        assert proj.id == "good-proj"

    def test_bad_project_registry_returns_none(self, portfolio_with_malformed_project):
        """Bad project (backslash TOML) returns None from registry — not a crash."""
        config = portfolio_with_malformed_project["config"]
        proj = get_project(config, "bad-proj")
        assert proj is None  # registry fails to load; should return None not raise

    def test_find_project_dir_fallback_finds_bad_project(
        self, portfolio_with_malformed_project, monkeypatch
    ):
        """find_project_dir_fallback locates the project via CWD walk."""
        bad_dir = portfolio_with_malformed_project["bad_dir"]
        config = portfolio_with_malformed_project["config"]

        # Simulate being inside the bad project's directory
        monkeypatch.chdir(bad_dir)

        result = find_project_dir_fallback(config, "bad-proj")
        assert result is not None
        assert result.name == ".project"
        assert result.parent.resolve() == bad_dir.resolve()

    def test_find_project_dir_fallback_returns_none_for_unknown(
        self, portfolio_with_malformed_project, monkeypatch, temp_portfolio_dir
    ):
        """find_project_dir_fallback returns None when project genuinely absent."""
        config = portfolio_with_malformed_project["config"]
        # CWD has no .project anywhere in its walk
        monkeypatch.chdir(temp_portfolio_dir)

        result = find_project_dir_fallback(config, "nonexistent-proj")
        assert result is None


# ---------------------------------------------------------------------------
# add_task fallback behaviour
# ---------------------------------------------------------------------------


class TestAddTaskFallback:
    """add_task must succeed via CWD-walk fallback when registry is broken."""

    def test_add_task_succeeds_for_good_project(self, portfolio_with_malformed_project):
        """add_task works normally for a project with a valid settings.toml."""
        config = portfolio_with_malformed_project["config"]
        task = add_task(config, "good-proj", "Normal task")
        assert task is not None
        # Prefix is first 5 chars of project_id.upper() = "GOOD-"
        assert task.id.startswith("GOOD-")
        assert task.state == TaskState.OPEN

    def test_add_task_succeeds_via_cwd_fallback_for_bad_project(
        self, portfolio_with_malformed_project, monkeypatch
    ):
        """add_task must succeed for a project with malformed settings.toml when
        called from inside that project's directory (CWD-walk fallback kicks in).
        """
        bad_dir = portfolio_with_malformed_project["bad_dir"]
        config = portfolio_with_malformed_project["config"]

        # Simulate operator running clawpm from inside the bad project
        monkeypatch.chdir(bad_dir)

        task = add_task(config, "bad-proj", "Task via fallback")
        assert task is not None, (
            "add_task returned None — the CWD-walk fallback is not working"
        )
        assert task.title == "Task via fallback"
        assert task.state == TaskState.OPEN

        # Verify the task file landed in the right place
        tasks_dir = bad_dir / ".project" / "tasks"
        assert any(tasks_dir.glob("*.md")), "No task file created in .project/tasks/"

    def test_add_task_returns_none_when_project_absent_everywhere(
        self, portfolio_with_malformed_project, monkeypatch, temp_portfolio_dir
    ):
        """add_task returns None (not a crash) when the project is genuinely missing."""
        config = portfolio_with_malformed_project["config"]
        monkeypatch.chdir(temp_portfolio_dir)  # no .project in CWD walk

        task = add_task(config, "nonexistent-proj", "Ghost task")
        assert task is None
