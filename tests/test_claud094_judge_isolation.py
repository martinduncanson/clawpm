"""CLAUD-094: judge launches use the slim profile; tests cannot spawn `claude`."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from clawpm import agent as agent_mod
from clawpm import resume as resume_mod
from clawpm.judges import stop_condition as sc_mod
from clawpm.judges.stop_condition import SLIM_JUDGE_FLAGS, make_judge_invoker


def _has_slim_profile(cmd: list[str]) -> bool:
    """True when every slim flag appears, with its value where it takes one."""
    for i, flag in enumerate(SLIM_JUDGE_FLAGS):
        if flag.startswith("--") and flag not in cmd:
            return False
    for flag in ("--tools", "--setting-sources"):
        idx = cmd.index(flag) if flag in cmd else -1
        if idx < 0 or idx + 1 >= len(cmd) or cmd[idx + 1] != "":
            return False
    return True


@pytest.fixture
def recorded_runs(monkeypatch):
    """Replace subprocess.run with a recorder; the neutral dir is inspected
    while it still exists."""
    monkeypatch.delenv("CLAWPM_JUDGE_CMD", raising=False)
    monkeypatch.delenv("CLAWPM_RESUME_CMD", raising=False)
    runs: list[SimpleNamespace] = []

    def fake_run(cmd, **kwargs):
        cwd = kwargs.get("cwd")
        runs.append(
            SimpleNamespace(
                cmd=list(cmd),
                cwd=cwd,
                cwd_existed=bool(cwd) and Path(cwd).is_dir(),
                cwd_entries=list(Path(cwd).iterdir()) if cwd and Path(cwd).is_dir() else None,
            )
        )
        return subprocess.CompletedProcess(cmd, 0, stdout='{"ok": false, "reason": "x"}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return runs


class TestGuard:
    def test_guard_fires_on_claude_spawn(self, claude_spawn_attempts):
        with pytest.raises(pytest.fail.Exception, match="real `claude` process"):
            subprocess.run(["claude", "--version"])
        assert len(claude_spawn_attempts) == 1
        claude_spawn_attempts.clear()  # provoked on purpose; keep teardown quiet

    def test_guard_matches_exe_and_full_path(self, claude_spawn_attempts):
        for args in (["claude.exe", "-p"], [r"C:\tools\claude.cmd"], "claude --print"):
            with pytest.raises(pytest.fail.Exception):
                subprocess.Popen(args)
        assert len(claude_spawn_attempts) == 3
        claude_spawn_attempts.clear()

    def test_guard_ignores_other_commands(self):
        r = subprocess.run(["git", "--version"], capture_output=True, text=True)
        assert r.returncode == 0


class TestSlimJudgeProfile:
    def test_default_judge_uses_slim_profile_in_neutral_dir(self, recorded_runs, tmp_path):
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        out = make_judge_invoker(cwd=worktree)("prompt")
        assert out
        (run,) = recorded_runs
        assert run.cmd[:2] == ["claude", "--print"]
        assert _has_slim_profile(run.cmd), run.cmd
        assert run.cwd_existed and run.cwd_entries == []
        assert Path(run.cwd) != worktree
        assert not Path(run.cwd).exists(), "neutral dir must be cleaned up"

    def test_resume_launch_uses_slim_profile(self, recorded_runs):
        resume_mod._default_resume_invoker("prompt")
        (run,) = recorded_runs
        assert _has_slim_profile(run.cmd), run.cmd
        assert run.cwd_existed and run.cwd_entries == []

    def test_explicit_judge_cmd_is_not_rewritten(self, recorded_runs, monkeypatch):
        monkeypatch.setenv("CLAWPM_JUDGE_CMD", "stubjudge --flag")
        make_judge_invoker()("prompt")
        assert recorded_runs[0].cmd == ["stubjudge", "--flag"]

    def test_agent_dispatch_subagent_keeps_its_tools(self, recorded_runs, tmp_path):
        """The dispatch subagent DOES the work: no slim profile there."""
        agent_mod._make_default_invoker(None, cwd=tmp_path)("prompt")
        (run,) = recorded_runs
        assert run.cmd == sc_mod.DEFAULT_JUDGE_CMD
        assert Path(run.cwd) == tmp_path

    def test_slim_flags_are_the_agreed_profile(self):
        assert SLIM_JUDGE_FLAGS == [
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
        assert "--bare" not in SLIM_JUDGE_FLAGS
        assert "--max-turns" not in SLIM_JUDGE_FLAGS
