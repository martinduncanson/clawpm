"""Tests for dispatch via hooks (CLAWP-018)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.dispatch import (
    CLAWPM_MARKER_KEY,
    build_settings_payload,
    read_dispatch_marker,
    settings_path,
    teardown_dispatch_settings,
    write_dispatch_settings,
)
from clawpm.models import Predictions, SuccessCriterion, TaskState
from clawpm.tasks import add_task, change_task_state, get_task


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio_with_repo():
    """Portfolio + an init'd git repo as the project repo_path."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_dispatch_test_")
    portfolio_root = Path(temp_dir)
    repo_dir = portfolio_root / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    # A throwaway commit so worktree-add can resolve HEAD
    (repo_dir / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "add", "README.md"],
        check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "commit", "-q", "-m", "init"],
        check=True,
    )

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{portfolio_root.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    project_meta = repo_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        f'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo_dir.as_posix()}"\n'
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
        "repo_dir": repo_dir,
        "tasks_dir": tasks_dir,
        "config": config,
    }
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    # Worktrees registered with git need cleanup before rmtree on Windows
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------


class TestSettingsPayload:
    def test_payload_carries_clawpm_marker(self):
        p = build_settings_payload("TEST-001", "test")
        assert CLAWPM_MARKER_KEY in p
        assert p[CLAWPM_MARKER_KEY]["task_id"] == "TEST-001"
        assert p[CLAWPM_MARKER_KEY]["project_id"] == "test"
        assert "dispatched_at" in p[CLAWPM_MARKER_KEY]

    def test_payload_includes_stop_and_posttooluse(self):
        p = build_settings_payload("TEST-001", "test")
        assert "Stop" in p["hooks"]
        assert "PostToolUse" in p["hooks"]
        stop_cmd = p["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "clawpm hook eval-stop" in stop_cmd
        assert "--task TEST-001" in stop_cmd
        ptu_cmd = p["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "clawpm log add" in ptu_cmd
        # PostToolUse matcher filters to Write|Edit (don't log reads)
        assert p["hooks"]["PostToolUse"][0]["matcher"] == "Write|Edit"

    def test_payload_with_rubric_adds_session_start(self):
        p = build_settings_payload(
            "TEST-001", "test",
            rubric_markdown="# Rubric: Test\n\nC1",
        )
        assert "SessionStart" in p["hooks"]
        ss = p["hooks"]["SessionStart"][0]["hooks"][0]
        assert ss["type"] == "command"
        # Cross-platform fix: command no longer embeds rubric content;
        # instead invokes `clawpm hook session-start` which reads a
        # sidecar JSON file at runtime. This avoids cmd.exe quoting
        # issues with embedded markdown / JSON.
        assert ss["command"] == "clawpm hook session-start --project test --task TEST-001"

    def test_payload_without_rubric_omits_session_start(self):
        p = build_settings_payload("TEST-001", "test")
        assert "SessionStart" not in p["hooks"]


# ---------------------------------------------------------------------------
# write / read / teardown
# ---------------------------------------------------------------------------


class TestWriteReadTeardown:
    def test_write_creates_file(self, tmp_path):
        path = write_dispatch_settings(tmp_path, "TEST-001", "test")
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data[CLAWPM_MARKER_KEY]["task_id"] == "TEST-001"

    def test_read_marker_returns_block(self, tmp_path):
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        marker = read_dispatch_marker(tmp_path)
        assert marker is not None
        assert marker["task_id"] == "TEST-001"

    def test_read_marker_returns_none_when_no_file(self, tmp_path):
        assert read_dispatch_marker(tmp_path) is None

    def test_read_marker_returns_none_when_not_clawpm_managed(self, tmp_path):
        path = settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"hooks": {"Stop": []}}, indent=2),
            encoding="utf-8",
        )
        assert read_dispatch_marker(tmp_path) is None

    def test_teardown_removes_clawpm_file(self, tmp_path):
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        removed = teardown_dispatch_settings(tmp_path, task_id="TEST-001")
        assert removed is True
        assert not settings_path(tmp_path).exists()

    def test_teardown_skips_non_clawpm_file(self, tmp_path):
        """Don't clobber operator-edited settings without --force."""
        path = settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        removed = teardown_dispatch_settings(tmp_path)
        assert removed is False
        assert path.exists()

    def test_teardown_skips_when_task_id_mismatch(self, tmp_path):
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        removed = teardown_dispatch_settings(tmp_path, task_id="TEST-999")
        assert removed is False
        assert settings_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# Write safety: refuses to clobber
# ---------------------------------------------------------------------------


class TestWriteSafety:
    def test_refuses_to_clobber_operator_file(self, tmp_path):
        path = settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"hooks": {"Stop": []}}), encoding="utf-8")

        with pytest.raises(FileExistsError):
            write_dispatch_settings(tmp_path, "TEST-001", "test")

    def test_refuses_to_overwrite_different_task(self, tmp_path):
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        with pytest.raises(ValueError) as exc:
            write_dispatch_settings(tmp_path, "TEST-002", "test")
        assert "TEST-001" in str(exc.value)
        assert "TEST-002" in str(exc.value)

    def test_force_backs_up_operator_file(self, tmp_path):
        path = settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        original = json.dumps({"hooks": {"Stop": [{"foo": "bar"}]}})
        path.write_text(original, encoding="utf-8")

        write_dispatch_settings(tmp_path, "TEST-001", "test", force=True)
        bak = path.with_suffix(path.suffix + ".bak")
        assert bak.exists()
        assert bak.read_text(encoding="utf-8") == original

    def test_re_dispatch_same_task_is_idempotent(self, tmp_path):
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        # Same task — should succeed (no-op overwrite)
        write_dispatch_settings(tmp_path, "TEST-001", "test")
        marker = read_dispatch_marker(tmp_path)
        assert marker["task_id"] == "TEST-001"


# ---------------------------------------------------------------------------
# CLI: tasks dispatch
# ---------------------------------------------------------------------------


class TestCLIDispatch:
    def test_cli_dispatch_default_target_dir(self, temp_portfolio_with_repo, tmp_path, monkeypatch):
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="Dispatchable",
            predictions=Predictions(success_criteria=["C1"]),
        )
        # cd to tmp_path so default target-dir is tmp_path
        monkeypatch.chdir(tmp_path)

        runner = CliRunner()
        r = runner.invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id]
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)["data"]
        assert payload["task_id"] == task.id
        assert payload["worktree"] is False
        assert payload["rubric_injected"] is True

        # File written
        assert settings_path(tmp_path).exists()
        marker = read_dispatch_marker(tmp_path)
        assert marker["task_id"] == task.id

    def test_cli_dispatch_explicit_target(self, temp_portfolio_with_repo, tmp_path):
        config = temp_portfolio_with_repo["config"]
        task = add_task(config, "test", title="X",
                        predictions=Predictions(success_criteria=["C1"]))
        target = tmp_path / "sub"
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(target)],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(target).exists()

    def test_cli_dispatch_worktree(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        task = add_task(config, "test", title="WT",
                        predictions=Predictions(success_criteria=["C1"]))
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id, "--worktree"],
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)["data"]
        assert payload["worktree"] is True
        wt_path = Path(payload["target_dir"])
        assert wt_path.exists()
        assert settings_path(wt_path).exists()

    def test_cli_dispatch_no_session_context(self, temp_portfolio_with_repo, tmp_path):
        config = temp_portfolio_with_repo["config"]
        task = add_task(config, "test", title="NoCtx",
                        predictions=Predictions(success_criteria=["C1"]))
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(tmp_path), "--no-session-context"],
        )
        assert r.exit_code == 0, r.output
        data = json.loads(settings_path(tmp_path).read_text(encoding="utf-8"))
        assert "SessionStart" not in data["hooks"]


# ---------------------------------------------------------------------------
# CLI: tasks teardown-dispatch
# ---------------------------------------------------------------------------


class TestCLITeardown:
    def test_cli_teardown_removes_clawpm_file(self, temp_portfolio_with_repo, tmp_path):
        config = temp_portfolio_with_repo["config"]
        task = add_task(config, "test", title="T",
                        predictions=Predictions(success_criteria=["C1"]))
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(tmp_path)],
        )
        assert settings_path(tmp_path).exists()

        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "teardown-dispatch", task.id,
             "--target-dir", str(tmp_path)],
        )
        assert r.exit_code == 0
        payload = json.loads(r.output)["data"]
        assert payload["removed"] is True
        assert not settings_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# Auto-teardown on done
# ---------------------------------------------------------------------------


class TestSessionStartSidecar:
    def test_dispatch_writes_sidecar(self, temp_portfolio_with_repo, tmp_path):
        from clawpm.dispatch import session_start_payload_path
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="Sidecar",
            predictions=Predictions(success_criteria=["c1"]),
        )
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(tmp_path)],
        )
        sidecar = session_start_payload_path(tmp_path)
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "Rubric" in payload["hookSpecificOutput"]["additionalContext"]

    def test_teardown_removes_sidecar_too(self, temp_portfolio_with_repo, tmp_path):
        from clawpm.dispatch import session_start_payload_path
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="Sidecar",
            predictions=Predictions(success_criteria=["c1"]),
        )
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(tmp_path)],
        )
        sidecar = session_start_payload_path(tmp_path)
        assert sidecar.exists()

        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "teardown-dispatch", task.id,
             "--target-dir", str(tmp_path)],
        )
        assert not sidecar.exists()


class TestPortableHookCommands:
    """Codex-review hardening: hook commands must be portable across
    cmd.exe (Windows default for Claude Code) and POSIX shells.

    Verifies no single quotes, no embedded shell-meta characters."""

    def test_post_tool_use_command_no_quoting_required(self):
        from clawpm.dispatch import build_settings_payload
        p = build_settings_payload("TEST-001", "test")
        cmd = p["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert "'" not in cmd  # no single quotes
        assert '"' not in cmd  # no double quotes either — whitespace-free
        assert "$" not in cmd  # no shell variable expansion
        assert "`" not in cmd  # no backticks
        assert "subagent-tool-use" in cmd  # hyphenated, no whitespace

    def test_session_start_command_no_embedded_json(self):
        from clawpm.dispatch import build_settings_payload
        p = build_settings_payload(
            "TEST-001", "test",
            rubric_markdown="# Rubric\n\nC1 with 'quotes' and \"doubles\"",
        )
        cmd = p["hooks"]["SessionStart"][0]["hooks"][0]["command"]
        # No embedded JSON — should be a clean clawpm subcommand
        assert "{" not in cmd
        assert "}" not in cmd
        assert "printf" not in cmd
        assert cmd.startswith("clawpm hook session-start")


class TestAutoTeardownOnDone:
    def test_done_auto_tears_down_repo_dispatch(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        repo_dir = temp_portfolio_with_repo["repo_dir"]
        task = add_task(config, "test", title="AutoTear",
                        predictions=Predictions(success_criteria=["C1"]))

        # Dispatch into repo root
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(repo_dir)],
        )
        assert settings_path(repo_dir).exists()

        # Mark done — auto-teardown fires
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "state", task.id, "done"]
        )
        assert r.exit_code == 0
        payload = json.loads(r.output)["data"]
        assert "dispatch_teardowns" in payload
        assert any(
            t["task_id"] == task.id for t in payload["dispatch_teardowns"]
        )
        assert not settings_path(repo_dir).exists()
