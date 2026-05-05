"""Tests for task scope field and clawpm conflicts command."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main, _globs_overlap
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import add_task, change_task_state, get_task


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_scope_test_")
    portfolio_root = Path(temp_dir)

    # Use as_posix() so Windows backslashes don't break TOML parsing
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n'
    )

    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    config = load_portfolio_config(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "config": config,
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# 1. Round-trip: write scope to file, read back
# ---------------------------------------------------------------------------


class TestScopeRoundTrip:
    def test_add_task_with_scope_persists(self, temp_portfolio):
        config = temp_portfolio["config"]
        scope = ["src/auth/**", "tests/auth/**"]
        task = add_task(config, "test", "Auth refactor", scope=scope)

        assert task is not None
        assert task.scope == scope

        # Reload from disk
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.scope == scope

    def test_task_without_scope_has_empty_list(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "No scope task")

        assert task is not None
        assert task.scope == []

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.scope == []

    def test_scope_appears_in_to_dict(self, temp_portfolio):
        config = temp_portfolio["config"]
        scope = ["src/billing/**"]
        task = add_task(config, "test", "Billing task", scope=scope)
        assert task is not None
        d = task.to_dict()
        assert "scope" in d
        assert d["scope"] == scope


# ---------------------------------------------------------------------------
# 2. CLI: tasks add --scope; tasks show includes scope
# ---------------------------------------------------------------------------


class TestScopeCLI:
    def test_tasks_add_scope_flag(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Auth feature",
                "--scope", "src/auth/**",
                "--scope", "tests/auth/**",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["scope"] == ["src/auth/**", "tests/auth/**"]

    def test_tasks_show_includes_scope(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Show test", scope=["src/show/**"])
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(main, ["tasks", "show", task.id, "--project", "test"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["scope"] == ["src/show/**"]

    def test_tasks_edit_scope_flag(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit scope test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--scope", "src/payments/**",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["scope"] == ["src/payments/**"]

        # Confirm persisted to disk
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.scope == ["src/payments/**"]


# ---------------------------------------------------------------------------
# 3. Glob-overlap heuristic unit tests
# ---------------------------------------------------------------------------


class TestGlobOverlap:
    def test_exact_match(self):
        assert _globs_overlap("src/auth/**", "src/auth/**")

    def test_subtree_containment(self):
        assert _globs_overlap("src/auth/**", "src/auth/handlers/**")

    def test_auth_and_billing_no_overlap(self):
        assert not _globs_overlap("src/auth/**", "src/billing/**")

    def test_literal_under_glob(self):
        assert _globs_overlap("src/auth/**", "src/auth/login.py")

    def test_disjoint_literals(self):
        assert not _globs_overlap("src/auth/login.py", "src/billing/invoice.py")

    def test_root_glob_overlaps_everything(self):
        # A pattern with no literal prefix should overlap anything
        assert _globs_overlap("**", "src/anything/**")

    def test_different_top_level_dirs(self):
        assert not _globs_overlap("frontend/**", "backend/**")


# ---------------------------------------------------------------------------
# 4. conflicts command — no conflict between auth and billing
# ---------------------------------------------------------------------------


class TestConflictsNoConflict:
    def test_separate_scopes_no_conflict(self, temp_portfolio):
        config = temp_portfolio["config"]

        auth_task = add_task(config, "test", "Auth work", scope=["src/auth/**"])
        assert auth_task is not None
        change_task_state(config, "test", auth_task.id, TaskState.PROGRESS)

        billing_task = add_task(config, "test", "Billing work", scope=["src/billing/**"])
        assert billing_task is not None
        change_task_state(config, "test", billing_task.id, TaskState.PROGRESS)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--scope", "src/billing/**"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # auth task must not appear; billing task might — it IS in-flight with that scope
        # But we're querying from outside (not FROM billing_task), so billing_task IS a conflict
        # with itself only if clawpm conflicts is meant to find tasks conflicting with a proposed new scope.
        # The billing_task IS progress with scope src/billing/**, so it will appear.
        # The auth_task should NOT appear.
        conflict_ids = [c["task_id"] for c in data["conflicts"]]
        assert auth_task.id not in conflict_ids


# ---------------------------------------------------------------------------
# 5. conflicts command — overlap detected
# ---------------------------------------------------------------------------


class TestConflictsOverlap:
    def test_overlapping_scopes_detected(self, temp_portfolio):
        config = temp_portfolio["config"]

        auth_task = add_task(config, "test", "Auth work", scope=["src/auth/**"])
        assert auth_task is not None
        change_task_state(config, "test", auth_task.id, TaskState.PROGRESS)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--scope", "src/auth/login.py"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        conflict_ids = [c["task_id"] for c in data["conflicts"]]
        assert auth_task.id in conflict_ids

    def test_done_task_not_counted_as_conflict(self, temp_portfolio):
        config = temp_portfolio["config"]

        done_task = add_task(config, "test", "Done auth work", scope=["src/auth/**"])
        assert done_task is not None
        change_task_state(config, "test", done_task.id, TaskState.DONE)

        progress_task = add_task(config, "test", "Active work", scope=["src/billing/**"])
        assert progress_task is not None
        change_task_state(config, "test", progress_task.id, TaskState.PROGRESS)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--scope", "src/auth/login.py"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        conflict_ids = [c["task_id"] for c in data["conflicts"]]
        # done task must NOT appear — only progress tasks count
        assert done_task.id not in conflict_ids

    def test_open_task_not_counted_as_conflict(self, temp_portfolio):
        config = temp_portfolio["config"]

        open_task = add_task(config, "test", "Open auth work", scope=["src/auth/**"])
        assert open_task is not None
        # Left as OPEN (not in progress)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--scope", "src/auth/**"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        conflict_ids = [c["task_id"] for c in data["conflicts"]]
        assert open_task.id not in conflict_ids


# ---------------------------------------------------------------------------
# 6. conflicts --task <id> resolves scope from named task
# ---------------------------------------------------------------------------


class TestConflictsByTask:
    def test_conflicts_by_task_id(self, temp_portfolio):
        config = temp_portfolio["config"]

        # In-flight task claiming auth scope
        auth_task = add_task(config, "test", "Auth in flight", scope=["src/auth/**"])
        assert auth_task is not None
        change_task_state(config, "test", auth_task.id, TaskState.PROGRESS)

        # Query task (e.g. open, about to be dispatched) with overlapping scope
        query_task = add_task(config, "test", "New auth feature", scope=["src/auth/login.py"])
        assert query_task is not None
        # query_task is still OPEN — not dispatched yet

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--task", query_task.id, "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)

        conflict_ids = [c["task_id"] for c in data["conflicts"]]
        assert auth_task.id in conflict_ids

    def test_conflicts_by_task_no_scope_declared(self, temp_portfolio):
        config = temp_portfolio["config"]

        query_task = add_task(config, "test", "No scope task")
        assert query_task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["conflicts", "--task", query_task.id, "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["conflicts"] == []
        assert "note" in data
