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
        # PostToolUse matcher covers ALL code-writing tools. Codex round-3
        # caught the MultiEdit gap — batched edits would otherwise skip
        # the work_log entry.
        matcher = p["hooks"]["PostToolUse"][0]["matcher"]
        assert "Write" in matcher
        assert "Edit" in matcher
        assert "MultiEdit" in matcher
        assert "NotebookEdit" in matcher
        # Reads still excluded — don't log Read/Grep/Glob etc.
        assert "Read" not in matcher
        assert "Grep" not in matcher

    def test_confirm_close_appends_flag_to_stop_command(self):
        # CLAWP-041 G1: the load-bearing seam. If --confirm-close is dropped
        # here the whole tier silently no-ops in the real Stop-hook path.
        on = build_settings_payload("TEST-001", "test", confirm_close=True, refute_votes=2)
        on_cmd = on["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "clawpm hook eval-stop" in on_cmd
        # The on-form signature is unambiguous (--no-confirm-close also
        # contains the substring "--confirm-close", so match the votes suffix).
        assert "--confirm-close --refute-votes 2" in on_cmd

    def test_default_emits_explicit_no_confirm_close(self):
        # Codex P2: dispatch-time gating must be authoritative — the false case
        # emits an EXPLICIT --no-confirm-close so CLAWPM_CONFIRM_CLOSE env can't
        # silently re-enable it at hook runtime.
        off = build_settings_payload("TEST-001", "test")
        off_cmd = off["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "--no-confirm-close" in off_cmd
        assert "--confirm-close --refute-votes" not in off_cmd

    def test_confirm_close_scales_stop_hook_timeout(self):
        # Codex P2: a flat 90s could kill a valid slow close (base + refuters
        # run sequentially, 60s each). Timeout must scale with the vote budget.
        off = build_settings_payload("TEST-001", "test")
        assert off["hooks"]["Stop"][0]["hooks"][0]["timeout"] == 90
        on1 = build_settings_payload("TEST-001", "test", confirm_close=True, refute_votes=1)
        on3 = build_settings_payload("TEST-001", "test", confirm_close=True, refute_votes=3)
        t1 = on1["hooks"]["Stop"][0]["hooks"][0]["timeout"]
        t3 = on3["hooks"]["Stop"][0]["hooks"][0]["timeout"]
        # base + N refuters, each 60s, + margin → must clear base+1refuter=120s.
        assert t1 >= 120
        assert t3 > t1


class TestConfirmCloseAutoGate:
    """CLAWP-041 G2: `tasks dispatch` auto-enables --confirm-close when the
    task's predicted confidence >= 4, end to end through the CLI."""

    def _dispatch_and_read_stop_cmd(self, env_root, repo_dir, config, confidence, extra_args=()):
        task = add_task(
            config, "test", title="t", description="body",
            predictions=Predictions(confidence=confidence, filled_by="agent"),
        )
        target = repo_dir / f"disp-{task.id}"
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "dispatch", task.id, "--project", "test",
             "--target-dir", str(target), "--no-session-context", *extra_args],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads((target / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        return payload["hooks"]["Stop"][0]["hooks"][0]["command"]

    def test_high_confidence_auto_enables(self, temp_portfolio_with_repo):
        cmd = self._dispatch_and_read_stop_cmd(
            temp_portfolio_with_repo["root"], temp_portfolio_with_repo["repo_dir"],
            temp_portfolio_with_repo["config"], confidence=4,
        )
        assert "--confirm-close --refute-votes" in cmd

    def test_low_confidence_stays_off(self, temp_portfolio_with_repo):
        cmd = self._dispatch_and_read_stop_cmd(
            temp_portfolio_with_repo["root"], temp_portfolio_with_repo["repo_dir"],
            temp_portfolio_with_repo["config"], confidence=3,
        )
        assert "--no-confirm-close" in cmd
        assert "--confirm-close --refute-votes" not in cmd

    def test_string_confidence_degrades_to_off(self, temp_portfolio_with_repo):
        # Codex P2: a hand-edited task file with confidence as a quoted string
        # must not crash dispatch — it degrades to confirm-close off.
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="t", description="b",
            predictions=Predictions(confidence=5, filled_by="agent"),
        )
        # Rewrite the task file's confidence as a YAML string to simulate a
        # legacy / hand-edited file.
        tfile = temp_portfolio_with_repo["tasks_dir"] / f"{task.id}.md"
        raw = tfile.read_text(encoding="utf-8")
        tfile.write_text(raw.replace("confidence: 5", 'confidence: "5"'), encoding="utf-8")
        target = temp_portfolio_with_repo["repo_dir"] / f"disp-str-{task.id}"
        result = CliRunner().invoke(
            main,
            ["tasks", "dispatch", task.id, "--project", "test",
             "--target-dir", str(target), "--no-session-context"],
        )
        assert result.exit_code == 0, result.output
        cmd = json.loads(
            (target / ".claude" / "settings.local.json").read_text(encoding="utf-8")
        )["hooks"]["Stop"][0]["hooks"][0]["command"]
        assert "--no-confirm-close" in cmd

    def test_explicit_no_confirm_close_overrides_high_confidence(self, temp_portfolio_with_repo):
        cmd = self._dispatch_and_read_stop_cmd(
            temp_portfolio_with_repo["root"], temp_portfolio_with_repo["repo_dir"],
            temp_portfolio_with_repo["config"], confidence=5,
            extra_args=("--no-confirm-close",),
        )
        assert "--no-confirm-close" in cmd
        assert "--confirm-close --refute-votes" not in cmd

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

    def test_teardown_skips_when_project_id_mismatch(self, tmp_path):
        """Codex round-7 P2: teardown must refuse to remove a dispatch
        belonging to a different project (same task_id collision)."""
        write_dispatch_settings(tmp_path, "SHARED-001", "project_a")
        removed = teardown_dispatch_settings(
            tmp_path, task_id="SHARED-001", project_id="project_b"
        )
        assert removed is False
        assert settings_path(tmp_path).exists()
        marker = read_dispatch_marker(tmp_path)
        assert marker["project_id"] == "project_a"

    def test_teardown_removes_when_project_id_matches(self, tmp_path):
        write_dispatch_settings(tmp_path, "SHARED-001", "project_a")
        removed = teardown_dispatch_settings(
            tmp_path, task_id="SHARED-001", project_id="project_a"
        )
        assert removed is True
        assert not settings_path(tmp_path).exists()


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

    def test_refuses_to_overwrite_same_task_id_different_project(self, tmp_path):
        """Codex round-6 P1: same task_id in a different project must NOT
        be treated as an idempotent re-dispatch — would silently redirect
        future hooks to the wrong project/task."""
        write_dispatch_settings(tmp_path, "SHARED-001", "project_a")
        with pytest.raises(ValueError) as exc:
            write_dispatch_settings(tmp_path, "SHARED-001", "project_b")
        assert "project_a" in str(exc.value)
        assert "project_b" in str(exc.value)

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


class TestIdentifierSafety:
    """Codex P1 fix: task_id and project_id flow into shell commands.
    Validate the safe-charset rejection at the dispatch boundary."""

    def test_safe_id_passes(self):
        from clawpm.dispatch import build_settings_payload
        # Normal clawpm-generated IDs all pass
        p = build_settings_payload("CLAWP-016", "clawpm")
        assert "CLAWP-016" in p["hooks"]["Stop"][0]["hooks"][0]["command"]

    def test_task_id_with_semicolon_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe task_id"):
            build_settings_payload("FOO; rm -rf /", "test")

    def test_task_id_with_dollar_substitution_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe task_id"):
            build_settings_payload("FOO-$(cat /etc/passwd)", "test")

    def test_task_id_with_backticks_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe task_id"):
            build_settings_payload("FOO-`whoami`", "test")

    def test_task_id_path_traversal_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe task_id"):
            build_settings_payload("../etc-passwd", "test")

    def test_task_id_with_space_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe task_id"):
            build_settings_payload("FOO BAR", "test")

    def test_project_id_with_semicolon_rejected(self):
        from clawpm.dispatch import build_settings_payload
        with pytest.raises(ValueError, match="unsafe project_id"):
            build_settings_payload("CLAWP-016", "test; rm")

    def test_worktree_rejects_unsafe_id(self, temp_portfolio_with_repo):
        from clawpm.dispatch import create_worktree
        repo = temp_portfolio_with_repo["repo_dir"]
        with pytest.raises(ValueError, match="unsafe task_id"):
            create_worktree(repo, "FOO/../bar")


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


class TestDispatchRegistry:
    """Codex round-4 P2: portfolio-level registry tracks every dispatch
    target_dir so done-time teardown finds them all (not just the
    hardcoded repo_path + worktree pair)."""

    def test_registry_appends_dispatch_event(
        self, temp_portfolio_with_repo, tmp_path
    ):
        from clawpm.dispatch import active_dispatch_dirs
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="Reg",
            predictions=Predictions(success_criteria=["c1"]),
        )

        # Dispatch to a custom dir that has no relation to repo_path
        custom_dir = tmp_path / "custom-target"
        custom_dir.mkdir()
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(custom_dir)],
        )

        dirs = active_dispatch_dirs(config.portfolio_root, task.id, "test")
        assert any(
            Path(str(d)).resolve() == custom_dir.resolve() for d in dirs
        )

    def test_done_tears_down_custom_target_dir(
        self, temp_portfolio_with_repo, tmp_path
    ):
        """The original bug Codex flagged: a dispatch to a custom
        --target-dir was NOT torn down on done. Verify the registry-
        backed teardown now finds it."""
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="CustomDir",
            predictions=Predictions(success_criteria=["c1"]),
        )
        # Custom dir lives nowhere near repo_path or .clawpm-worktrees/
        custom_dir = tmp_path / "elsewhere"
        custom_dir.mkdir()

        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(custom_dir)],
        )
        assert settings_path(custom_dir).exists()

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "state", task.id, "done"]
        )
        assert r.exit_code == 0
        payload = json.loads(r.output)["data"]
        assert "dispatch_teardowns" in payload
        # The custom dir IS in the teardowns list
        teardown_dirs = {
            Path(t["target_dir"]).resolve()
            for t in payload["dispatch_teardowns"]
        }
        assert custom_dir.resolve() in teardown_dirs
        # And the file is actually gone
        assert not settings_path(custom_dir).exists()

    def test_registry_torn_down_event_removes_from_active(
        self, temp_portfolio_with_repo, tmp_path
    ):
        from clawpm.dispatch import active_dispatch_dirs
        config = temp_portfolio_with_repo["config"]
        task = add_task(
            config, "test", title="TT",
            predictions=Predictions(success_criteria=["c1"]),
        )
        d = tmp_path / "td"
        d.mkdir()
        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id, "--target-dir", str(d)],
        )
        assert active_dispatch_dirs(config.portfolio_root, task.id, "test")

        CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "teardown-dispatch", task.id, "--target-dir", str(d)],
        )
        assert not active_dispatch_dirs(config.portfolio_root, task.id, "test")

    def test_auto_teardown_fallback_respects_project_id(
        self, temp_portfolio_with_repo, tmp_path
    ):
        """Codex round-6 P1: legacy fallback teardown checked marker's
        task_id alone, bypassing the cross-project registry filter. A
        completing project A task could tear down project B's same-task-
        id dispatch in the same target dir."""
        from clawpm.dispatch import (
            read_dispatch_marker,
            settings_path,
            write_dispatch_settings,
        )
        config = temp_portfolio_with_repo["config"]
        repo = temp_portfolio_with_repo["repo_dir"]

        # Write a dispatch into repo root marked as PROJECT B's task
        # (different project, same task ID). Done in project A SHOULD NOT
        # touch this dispatch.
        write_dispatch_settings(
            repo, "SHARED-001", "project_b",
            portfolio_root=config.portfolio_root,
        )
        assert settings_path(repo).exists()

        # Create the same task_id under our actual test project
        task = add_task(
            config, "test", title="A's task",
            task_id="SHARED-001",
            predictions=Predictions(success_criteria=["c1"]),
        )

        # Mark done — auto-teardown should NOT touch the repo's settings
        # (which belongs to project_b, not test)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "state", task.id, "done"]
        )
        assert r.exit_code == 0, r.output
        # Project B's dispatch survives
        assert settings_path(repo).exists()
        marker = read_dispatch_marker(repo)
        assert marker["project_id"] == "project_b"


    def test_cross_project_isolation(
        self, temp_portfolio_with_repo, tmp_path
    ):
        """Codex round-5 P1: same task_id in two different projects must
        not collide. Completing task in project A does NOT tear down
        dispatch hooks in project B even if both share the task ID."""
        from clawpm.dispatch import (
            active_dispatch_dirs,
            register_dispatch,
            register_teardown,
        )
        config = temp_portfolio_with_repo["config"]

        # Directly seed the registry with two dispatches for the same
        # task_id under different project_ids.
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        register_dispatch(config.portfolio_root, "SHARED-001", "proj_a", dir_a)
        register_dispatch(config.portfolio_root, "SHARED-001", "proj_b", dir_b)

        active_a = active_dispatch_dirs(
            config.portfolio_root, "SHARED-001", "proj_a"
        )
        active_b = active_dispatch_dirs(
            config.portfolio_root, "SHARED-001", "proj_b"
        )

        assert any(Path(str(d)).resolve() == dir_a.resolve() for d in active_a)
        assert not any(Path(str(d)).resolve() == dir_b.resolve() for d in active_a)
        assert any(Path(str(d)).resolve() == dir_b.resolve() for d in active_b)
        assert not any(Path(str(d)).resolve() == dir_a.resolve() for d in active_b)

        # Tearing down proj_a's dispatch does NOT remove proj_b's.
        register_teardown(
            config.portfolio_root, "SHARED-001", dir_a, project_id="proj_a"
        )
        active_a = active_dispatch_dirs(
            config.portfolio_root, "SHARED-001", "proj_a"
        )
        active_b = active_dispatch_dirs(
            config.portfolio_root, "SHARED-001", "proj_b"
        )
        assert not active_a
        # proj_b's dispatch is still active
        assert any(Path(str(d)).resolve() == dir_b.resolve() for d in active_b)


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
