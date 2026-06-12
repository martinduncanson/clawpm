"""Tests for CLAWP-063 — 'drift-not-checked' marker when drift gate skips on ERROR.

Success criteria:
  1. An ERROR-class drift skip (git subprocess failure / unverifiable ref) surfaces
     a 'drift-not-checked' warning at dispatch, and dispatch STILL proceeds (fail-open).
  2. EXPECTED-class skips (no-scope / no-baseline / ts: marker / non-git) remain
     SILENT — tests assert no warning for each, distinguishing the two classes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from click.testing import CliRunner

from clawpm.baseline import detect_scope_drift
from clawpm.cli import main
from clawpm.tasks import add_task
from clawpm.discovery import load_portfolio_config


# ---------------------------------------------------------------------------
# Unit tests: detect_scope_drift skip_class field
# ---------------------------------------------------------------------------

class TestDetectScopeDriftSkipClass:
    """detect_scope_drift must distinguish expected vs error-class skips."""

    def test_no_scope_returns_expected_skip_class(self, tmp_path):
        """No-scope skip is EXPECTED — skip_class must be 'expected'."""
        plain = tmp_path / "repo"
        plain.mkdir()
        result = detect_scope_drift(repo_path=plain, scope=[], baseline_ref="abc1234")
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "expected"

    def test_no_baseline_ref_returns_expected_skip_class(self, git_repo):
        """No-baseline-ref skip is EXPECTED — skip_class must be 'expected'."""
        result = detect_scope_drift(repo_path=git_repo, scope=["*.py"], baseline_ref=None)
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "expected"

    def test_timestamp_baseline_returns_expected_skip_class(self, git_repo):
        """ts: baseline skip is EXPECTED — skip_class must be 'expected'."""
        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref="ts:2025-01-01T00:00:00+00:00",
        )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "expected"

    def test_non_git_repo_returns_expected_skip_class(self, tmp_path):
        """Non-git-repo skip is EXPECTED — skip_class must be 'expected'."""
        plain = tmp_path / "plain"
        plain.mkdir()
        # Has a .git-less directory — not a git repo
        result = detect_scope_drift(
            repo_path=plain,
            scope=["*.py"],
            baseline_ref="abc1234",
        )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "expected"

    def test_git_rev_parse_failure_returns_error_skip_class(self, git_repo):
        """git rev-parse OSError => ERROR-class skip."""
        with patch("clawpm.baseline.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("git not found")
            result = detect_scope_drift(
                repo_path=git_repo,
                scope=["*.py"],
                baseline_ref="abc1234",
            )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "error"

    def test_unknown_baseline_ref_returns_error_skip_class(self, git_repo):
        """Unresolvable (force-pushed/unknown) baseline_ref => ERROR-class skip."""
        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref="deadbeef",  # does not exist in this repo
        )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "error"

    def test_git_diff_subprocess_failure_returns_error_skip_class(self, git_repo):
        """git diff OSError on the diff itself => ERROR-class skip."""
        # First call (rev-parse) must succeed; second call (diff) must fail.
        import subprocess as _sp
        real_run = _sp.run
        call_count = [0]

        def selective_fail(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: rev-parse — let it succeed for real sha
                return real_run(*args, **kwargs)
            raise OSError("disk I/O error")

        # Use the real HEAD sha so rev-parse passes
        real_sha = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        with patch("clawpm.baseline.subprocess.run", side_effect=selective_fail):
            result = detect_scope_drift(
                repo_path=git_repo,
                scope=["*.py"],
                baseline_ref=real_sha,
            )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "error"

    def test_git_diff_nonzero_exit_returns_error_skip_class(self, git_repo):
        """git diff non-zero exit code => ERROR-class skip."""
        import subprocess as _sp
        real_run = _sp.run
        call_count = [0]

        def selective_bad_rc(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return real_run(*args, **kwargs)
            # Second call: return non-zero exit
            m = MagicMock()
            m.returncode = 128
            m.stdout = ""
            m.stderr = "error"
            return m

        real_sha = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()

        with patch("clawpm.baseline.subprocess.run", side_effect=selective_bad_rc):
            result = detect_scope_drift(
                repo_path=git_repo,
                scope=["*.py"],
                baseline_ref=real_sha,
            )
        assert result["status"] == "skipped"
        assert result.get("skip_class") == "error"

    def test_clean_result_has_no_skip_class(self, git_repo):
        """'clean' results must NOT have skip_class (only skipped results do)."""
        import subprocess as _sp
        real_sha = _sp.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=git_repo, capture_output=True, text=True,
        ).stdout.strip()
        result = detect_scope_drift(
            repo_path=git_repo,
            scope=["*.py"],
            baseline_ref=real_sha,
        )
        assert result["status"] == "clean"
        assert "skip_class" not in result


# ---------------------------------------------------------------------------
# Integration tests: dispatch gate warns on ERROR skip, silent on EXPECTED skip
# ---------------------------------------------------------------------------

class TestDispatchDriftNotCheckedWarning:
    """The dispatch gate emits a 'drift-not-checked' warning ONLY on ERROR-class skips."""

    @pytest.fixture
    def dispatching_portfolio(self, tmp_path):
        """Portfolio with a real git repo for dispatch tests."""
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
            '[defaults]\nstatus = "active"\n'
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

    def _invoke_dispatch(self, portfolio_root, task_id, extra_args=None):
        runner = CliRunner()
        args = [
            "--format", "json",
            "tasks", "dispatch",
            "--project", "disp-proj",
            "--target-dir", (portfolio_root / "dispatch_out").as_posix(),
            task_id,
        ]
        if extra_args:
            args.extend(extra_args)
        return runner.invoke(
            main,
            args,
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )

    # --- ERROR-class: should emit warning and still proceed ---

    def test_error_skip_emits_drift_not_checked_warning(self, dispatching_portfolio):
        """When git fails with OSError, dispatch emits 'drift-not-checked' but proceeds."""
        portfolio_root, repo = dispatching_portfolio
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "disp-proj", "Scoped task", scope=["*.py"])
        assert task is not None

        with patch("clawpm.baseline.subprocess.run") as mock_run:
            # Simulate git subprocess failure for all calls
            mock_run.side_effect = OSError("git not found")
            result = self._invoke_dispatch(portfolio_root, task.id)

        # Must NOT be blocked by drift error
        out = result.output
        assert "stale_baseline" not in out
        # Must emit the drift-not-checked warning
        assert "drift-not-checked" in out.lower() or "drift_not_checked" in out.lower()

    def test_error_skip_dispatch_still_proceeds(self, dispatching_portfolio):
        """An ERROR-class skip must never block dispatch (fail-open preserved)."""
        portfolio_root, repo = dispatching_portfolio
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "disp-proj", "Scoped task 2", scope=["*.py"])
        assert task is not None

        with patch("clawpm.baseline.subprocess.run") as mock_run:
            mock_run.side_effect = OSError("git not found")
            result = self._invoke_dispatch(portfolio_root, task.id)

        # Exit code must NOT be 1 due to drift (stale_baseline would cause exit 1)
        out = result.output
        if out.strip().startswith("{"):
            import json
            data = json.loads(out.split("\n")[0]) if "\n" in out else json.loads(out)
            assert data.get("error") != "stale_baseline"

    # --- EXPECTED-class: must be completely silent ---

    def test_no_scope_skip_is_silent(self, dispatching_portfolio):
        """No-scope task: drift-not-checked must NOT appear in output."""
        portfolio_root, repo = dispatching_portfolio
        config = load_portfolio_config(portfolio_root)
        # Task with NO scope
        task = add_task(config, "disp-proj", "No-scope task")
        assert task is not None

        result = self._invoke_dispatch(portfolio_root, task.id)
        assert "drift-not-checked" not in result.output.lower()
        assert "drift_not_checked" not in result.output.lower()

    def test_no_baseline_ref_skip_is_silent(self, dispatching_portfolio):
        """Legacy task with no baseline_ref: drift-not-checked must NOT appear."""
        portfolio_root, repo = dispatching_portfolio
        projects_dir = portfolio_root / "projects" / "disp-proj"
        tasks_dir = projects_dir / ".project" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "DISP-LEG063.md").write_text(
            "---\nid: DISP-LEG063\npriority: 5\nscope:\n- '*.py'\n---\n# Legacy\n",
            encoding="utf-8",
        )

        result = self._invoke_dispatch(portfolio_root, "DISP-LEG063")
        assert "drift-not-checked" not in result.output.lower()
        assert "drift_not_checked" not in result.output.lower()

    def test_ts_baseline_ref_skip_is_silent(self, dispatching_portfolio):
        """ts: baseline_ref (non-git marker): drift-not-checked must NOT appear."""
        portfolio_root, repo = dispatching_portfolio
        projects_dir = portfolio_root / "projects" / "disp-proj"
        tasks_dir = projects_dir / ".project" / "tasks"
        tasks_dir.mkdir(parents=True, exist_ok=True)
        (tasks_dir / "DISP-TS063.md").write_text(
            "---\nid: DISP-TS063\npriority: 5\nscope:\n- '*.py'\n"
            "baseline_ref: 'ts:2025-01-01T00:00:00+00:00'\n---\n# Timestamp baseline\n",
            encoding="utf-8",
        )

        result = self._invoke_dispatch(portfolio_root, "DISP-TS063")
        assert "drift-not-checked" not in result.output.lower()
        assert "drift_not_checked" not in result.output.lower()

    def test_no_repo_skip_is_silent(self, tmp_path):
        """Non-git project: drift-not-checked must NOT appear."""
        # Build a portfolio with no repo_path
        portfolio_root = tmp_path / "portfolio"
        portfolio_root.mkdir()
        (portfolio_root / "portfolio.toml").write_text(
            f'portfolio_root = "{portfolio_root.as_posix()}"\n'
            f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
            '[defaults]\nstatus = "active"\n'
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
        # Task with scope but no repo → ts: baseline_ref stamped
        config = load_portfolio_config(portfolio_root)
        task = add_task(config, "nogit-proj", "Scoped no-git", scope=["*.py"])
        assert task is not None
        # ts: marker will be stamped — so skip class will be "expected"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json",
                "tasks", "dispatch",
                "--project", "nogit-proj",
                "--target-dir", (portfolio_root / "dispatch_out").as_posix(),
                task.id,
            ],
            env={"CLAWPM_PORTFOLIO": str(portfolio_root)},
        )
        assert "drift-not-checked" not in result.output.lower()
        assert "drift_not_checked" not in result.output.lower()


# ---------------------------------------------------------------------------
# Shared fixture
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
