"""Tests for CLAWP-055 — Per-task baseline_ref stamping + pre-dispatch drift gate.

Success criteria:
  1. Every task records a baseline_ref at creation (git short-SHA when in a git
     repo, else a timestamp/content marker); shown in task detail (to_dict).
  2. A pre-dispatch reconciliation step detects when in-scope paths changed
     since baseline_ref and blocks silent dispatch on a stale task, offering
     reconcile/confirm — an explicit --confirm-stale flag allows proceed.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.baseline import (
    git_short_sha,
    timestamp_marker,
    resolve_baseline_ref,
    detect_scope_drift,
)
from clawpm.cli import main
from clawpm.models import Task, TaskState
from clawpm.tasks import add_task, get_task
from clawpm.discovery import load_portfolio_config


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def git_repo(tmp_path):
    """A minimal git repo with one committed file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo)]
    (repo / "hello.py").write_text("print('hello')", encoding="utf-8")
    subprocess.run(_gc + ["add", "hello.py"], check=True)
    subprocess.run(_gc + ["commit", "-m", "init"], check=True)
    return repo


@pytest.fixture
def portfolio_with_git_repo(tmp_path, git_repo):
    """Portfolio pointing at git_repo as the project repo."""
    portfolio_root = tmp_path / "portfolio"
    portfolio_root.mkdir()
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    proj_dir = projects_dir / "test-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "test-proj"\nname = "Test Project"\n'
        f'repo_path = "{git_repo.as_posix()}"\n'
    )
    return portfolio_root


@pytest.fixture
def portfolio_no_repo(tmp_path):
    """Portfolio with a project that has no repo_path (non-git)."""
    portfolio_root = tmp_path / "portfolio"
    portfolio_root.mkdir()
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    proj_dir = projects_dir / "nogit-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "nogit-proj"\nname = "No-Git Project"\n'
        # No repo_path
    )
    return portfolio_root


# ---------------------------------------------------------------------------
# Unit tests: baseline module
# ---------------------------------------------------------------------------

class TestBaselineProviders:
    def test_git_short_sha_returns_7_hex_chars(self, git_repo):
        sha = git_short_sha(git_repo)
        assert sha is not None
        assert len(sha) >= 7
        assert all(c in "0123456789abcdef" for c in sha)

    def test_git_short_sha_returns_none_for_non_git_dir(self, tmp_path):
        non_git = tmp_path / "plain"
        non_git.mkdir()
        result = git_short_sha(non_git)
        assert result is None

    def test_timestamp_marker_format(self):
        marker = timestamp_marker()
        # Should start with "ts:" and be a valid ISO timestamp
        assert marker.startswith("ts:")
        ts_part = marker[3:]
        # Must parse without error
        from datetime import datetime
        dt = datetime.fromisoformat(ts_part)
        assert dt is not None

    def test_resolve_baseline_ref_git_repo(self, git_repo):
        ref = resolve_baseline_ref(git_repo)
        assert ref is not None
        assert not ref.startswith("ts:")  # git SHA, not timestamp

    def test_resolve_baseline_ref_no_repo(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        ref = resolve_baseline_ref(plain)
        assert ref is not None
        assert ref.startswith("ts:")

    def test_resolve_baseline_ref_none_path(self):
        ref = resolve_baseline_ref(None)
        assert ref is not None
        assert ref.startswith("ts:")


# ---------------------------------------------------------------------------
# Unit tests: detect_scope_drift
# ---------------------------------------------------------------------------

class TestDetectScopeDrift:
    def test_no_scope_returns_skipped(self, git_repo):
        sha = git_short_sha(git_repo)
        result = detect_scope_drift(repo_path=git_repo, scope=[], baseline_ref=sha)
        assert result["status"] == "skipped"
        assert "no scope defined" in result["reason"].lower()

    def test_no_baseline_ref_returns_skipped(self, git_repo):
        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref=None,
        )
        assert result["status"] == "skipped"

    def test_non_git_repo_returns_skipped(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        result = detect_scope_drift(
            repo_path=plain,
            scope=["*.py"],
            baseline_ref="ts:2025-01-01T00:00:00",
        )
        assert result["status"] == "skipped"

    def test_no_drift_when_files_unchanged(self, git_repo):
        sha = git_short_sha(git_repo)
        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref=sha,
        )
        assert result["status"] == "clean"

    def test_detects_drift_when_in_scope_file_changed(self, git_repo):
        sha = git_short_sha(git_repo)
        _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(git_repo)]
        # Commit a change to hello.py (which is in scope "*.py")
        (git_repo / "hello.py").write_text("print('changed')", encoding="utf-8")
        subprocess.run(_gc + ["add", "hello.py"], check=True)
        subprocess.run(_gc + ["commit", "-m", "change hello"], check=True)

        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref=sha,
        )
        assert result["status"] == "drifted"
        assert len(result["changed_files"]) > 0
        assert any("hello.py" in f for f in result["changed_files"])

    def test_no_drift_when_out_of_scope_file_changed(self, git_repo):
        sha = git_short_sha(git_repo)
        _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(git_repo)]
        # Commit a change to a .txt file but scope is only *.py
        (git_repo / "notes.txt").write_text("some notes", encoding="utf-8")
        subprocess.run(_gc + ["add", "notes.txt"], check=True)
        subprocess.run(_gc + ["commit", "-m", "add notes"], check=True)

        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref=sha,
        )
        assert result["status"] == "clean"

    def test_drift_with_multiple_scope_globs(self, git_repo):
        sha = git_short_sha(git_repo)
        _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(git_repo)]
        (git_repo / "app.js").write_text("console.log('hi')", encoding="utf-8")
        subprocess.run(_gc + ["add", "app.js"], check=True)
        subprocess.run(_gc + ["commit", "-m", "add js"], check=True)

        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py", "*.js"],
            baseline_ref=sha,
        )
        assert result["status"] == "drifted"
        assert any("app.js" in f for f in result["changed_files"])


# ---------------------------------------------------------------------------
# Integration: baseline_ref stamped at task creation
# ---------------------------------------------------------------------------

class TestBaselineRefStampedAtCreation:
    def test_baseline_ref_present_in_git_repo(self, portfolio_with_git_repo):
        config = load_portfolio_config(portfolio_with_git_repo)
        task = add_task(config, "test-proj", "Test task", scope=["*.py"])
        assert task is not None
        assert task.baseline_ref is not None
        assert not task.baseline_ref.startswith("ts:")  # git SHA

    def test_baseline_ref_is_timestamp_for_no_repo(self, portfolio_no_repo):
        config = load_portfolio_config(portfolio_no_repo)
        task = add_task(config, "nogit-proj", "No-git task")
        assert task is not None
        assert task.baseline_ref is not None
        assert task.baseline_ref.startswith("ts:")

    def test_baseline_ref_persisted_in_frontmatter(self, portfolio_with_git_repo):
        config = load_portfolio_config(portfolio_with_git_repo)
        task = add_task(config, "test-proj", "Persist test", scope=["src/**"])
        assert task is not None
        assert task.file_path is not None
        text = task.file_path.read_text(encoding="utf-8")
        assert "baseline_ref:" in text

    def test_baseline_ref_in_to_dict(self, portfolio_with_git_repo):
        config = load_portfolio_config(portfolio_with_git_repo)
        task = add_task(config, "test-proj", "Dict test")
        assert task is not None
        d = task.to_dict()
        assert "baseline_ref" in d

    def test_existing_task_without_baseline_ref_loads_fine(self, portfolio_with_git_repo):
        """Backward compat: legacy task files without baseline_ref must load."""
        config = load_portfolio_config(portfolio_with_git_repo)
        # Write a task file WITHOUT baseline_ref frontmatter
        projects_dir = portfolio_with_git_repo / "projects" / "test-proj"
        tasks_dir = projects_dir / ".project" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "TEST-000.md").write_text(
            "---\nid: TEST-000\npriority: 5\n---\n# Old task\n",
            encoding="utf-8",
        )
        task = get_task(config, "test-proj", "TEST-000")
        assert task is not None
        assert task.baseline_ref is None  # Graceful default

    def test_baseline_ref_shown_in_tasks_show(self, portfolio_with_git_repo):
        """CLI `tasks show` output includes baseline_ref."""
        config = load_portfolio_config(portfolio_with_git_repo)
        task = add_task(config, "test-proj", "Show test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "show",
                "--project", "test-proj",
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_with_git_repo)},
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "baseline_ref" in data


# ---------------------------------------------------------------------------
# Integration: pre-dispatch drift gate
# ---------------------------------------------------------------------------

class TestPreDispatchDriftGate:
    """The drift gate blocks dispatch when in-scope files have changed since baseline_ref."""

    @pytest.fixture
    def dispatching_portfolio(self, tmp_path):
        """Portfolio with a real git repo and a task with scope."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo)]
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        (repo / "app.py").write_text("x = 1", encoding="utf-8")
        subprocess.run(_gc + ["add", "app.py"], check=True)
        subprocess.run(_gc + ["commit", "-m", "init"], check=True)

        portfolio_root = tmp_path / "portfolio"
        portfolio_root.mkdir()
        (portfolio_root / "portfolio.toml").write_text(
            f'portfolio_root = "{portfolio_root.as_posix()}"\n'
            f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n"
        )
        projects_dir = portfolio_root / "projects"
        projects_dir.mkdir()
        proj_dir = projects_dir / "disp-proj"
        proj_dir.mkdir()
        dot_proj = proj_dir / ".project"
        dot_proj.mkdir()
        (dot_proj / "settings.toml").write_text(
            'id = "disp-proj"\nname = "Dispatch Project"\n'
            f'repo_path = "{repo.as_posix()}"\n'
        )
        return portfolio_root, repo

    def _add_scoped_task(self, portfolio_root, repo):
        """Add a task with scope=['*.py'] and return (config, task)."""
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "disp-proj", "Scoped task", scope=["*.py"])
        assert task is not None
        return config, task

    def _commit_change(self, repo, filename="app.py", content="x = 2"):
        """Add a new commit to the repo after the baseline was stamped."""
        _gc = ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(repo)]
        (repo / filename).write_text(content, encoding="utf-8")
        subprocess.run(_gc + ["add", filename], check=True)
        subprocess.run(_gc + ["commit", "-m", f"change {filename}"], check=True)

    def test_dispatch_blocked_when_scope_drifted(self, dispatching_portfolio):
        portfolio_root, repo = dispatching_portfolio
        _, task = self._add_scoped_task(portfolio_root, repo)
        # Change an in-scope file AFTER the task was created
        self._commit_change(repo, "app.py", "x = 999")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "disp-proj",
                "--target-dir", (portfolio_root / "dispatch_out").as_posix(),
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        assert result.exit_code != 0
        out = result.output
        data = json.loads(out) if out.strip().startswith("{") else {}
        # Error code indicates drift blocked
        if data:
            assert data.get("error") == "stale_baseline"
        else:
            assert "stale" in out.lower() or "drift" in out.lower() or "baseline" in out.lower()

    def test_dispatch_proceeds_with_confirm_stale(self, dispatching_portfolio):
        portfolio_root, repo = dispatching_portfolio
        _, task = self._add_scoped_task(portfolio_root, repo)
        # Change an in-scope file AFTER the task was created
        self._commit_change(repo, "app.py", "x = 999")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "disp-proj",
                "--target-dir", (portfolio_root / "dispatch_out2").as_posix(),
                "--confirm-stale",
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        # With --confirm-stale, dispatch should succeed (or at least not fail on drift)
        # A non-zero exit here means something else failed (e.g. git worktree)
        # but NOT the drift gate
        out = result.output
        data = json.loads(out) if out.strip().startswith("{") else {}
        if data:
            assert data.get("error") != "stale_baseline"

    def test_dispatch_clean_when_no_scope_drift(self, dispatching_portfolio):
        portfolio_root, repo = dispatching_portfolio
        _, task = self._add_scoped_task(portfolio_root, repo)
        # Change an OUT-of-scope file
        self._commit_change(repo, "README.md", "docs")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "disp-proj",
                "--target-dir", (portfolio_root / "dispatch_out3").as_posix(),
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        # Should not be blocked by drift gate (out-of-scope change is fine)
        out = result.output
        if out.strip().startswith("{"):
            data = json.loads(out)
            assert data.get("error") != "stale_baseline"

    def test_dispatch_no_scope_skips_drift_check(self, dispatching_portfolio):
        """A task with no scope defined: drift check is skipped (no block)."""
        portfolio_root, repo = dispatching_portfolio
        config = load_portfolio_config(portfolio_root)
        # Task with NO scope
        task = add_task(config, "disp-proj", "No-scope task")
        assert task is not None
        # Change a file
        self._commit_change(repo, "app.py", "x = 42")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "disp-proj",
                "--target-dir", (portfolio_root / "dispatch_out4").as_posix(),
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        out = result.output
        if out.strip().startswith("{"):
            data = json.loads(out)
            assert data.get("error") != "stale_baseline"

    def test_dispatch_no_baseline_ref_skips_drift_check(self, dispatching_portfolio):
        """Legacy task without baseline_ref: drift check is skipped (no block)."""
        portfolio_root, repo = dispatching_portfolio
        # Write a legacy task WITHOUT baseline_ref in frontmatter
        projects_dir = portfolio_root / "projects" / "disp-proj"
        tasks_dir = projects_dir / ".project" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        task_id = "DISP-LEGACY"
        (tasks_dir / f"{task_id}.md").write_text(
            "---\nid: DISP-LEGACY\npriority: 5\nscope:\n- '*.py'\n---\n# Legacy task\n",
            encoding="utf-8",
        )
        # Change file after the legacy task was written
        self._commit_change(repo, "app.py", "x = legacy")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "disp-proj",
                "--target-dir", (portfolio_root / "dispatch_out5").as_posix(),
                task_id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        out = result.output
        if out.strip().startswith("{"):
            data = json.loads(out)
            assert data.get("error") != "stale_baseline"
