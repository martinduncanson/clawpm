"""Tests for `clawpm agent dispatch` — parent-spawned subagent wrapper (CLAWP-024).

Coverage:

1. Happy path: judge stub returns ok=true → subtask DONE, reflection event written.
2. Judge returns ok=false → subtask BLOCKED, iteration event written.
3. Judge returns impossible=true → subtask BLOCKED, iteration_event records the
   impossibility flag.
4. Judge subprocess error (`--judge-cmd-override` points to a non-existent
   command) → wrapper completes gracefully, subtask BLOCKED with JUDGE_ERROR reason.
5. Parent task ID linking: --parent flag flows through to the reflection event.
6. Reflection event content: success-path event carries the predicted criteria
   and the agent_dispatch_done event marker.

These tests deliberately bypass a real `claude` CLI via the
``judge_invoker`` Python kwarg (for the in-process tests) and via
``--judge-cmd-override`` (for the CLI tests). No network, no model. The
testable-without-real-claude requirement was the load-bearing piece of
CLAWP-024 spec #4.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.agent import AgentDispatchError, dispatch_agent
from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import get_task


@pytest.fixture
def temp_portfolio_with_repo():
    """Portfolio + init'd git repo as the project repo_path.

    Mirrors test_dispatch.py's fixture so agent_dispatch tests share the
    same on-disk shape: a tasks/ dir, a settings.toml with forward-slash
    repo_path (Windows CLAWPM gotcha), and a commit so `git worktree add`
    can resolve HEAD.
    """
    temp_dir = tempfile.mkdtemp(prefix="clawpm_agent_test_")
    portfolio_root = Path(temp_dir)
    repo_dir = portfolio_root / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
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
    try:
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False,
            capture_output=True,
        )
    except Exception:
        pass
    shutil.rmtree(temp_dir, ignore_errors=True)


def _make_stub_invoker(verdict_json: str):
    """Return a judge invoker that ignores the prompt and returns a canned JSON.

    The Stop-condition evaluator parses the invoker output via
    ``JudgeVerdict.parse``; the canned string just needs to be the
    expected JSON shape. Tests pass this in via ``dispatch_agent(
    ..., judge_invoker=stub)`` to bypass any real subprocess call.
    """
    def _invoke(prompt: str) -> str:
        return verdict_json
    return _invoke


def _read_reflection_jsonl(portfolio_root: Path, task_id: str) -> list[dict]:
    """Read all JSONL events for a task. Helper for the reflection-event checks."""
    path = portfolio_root / "reflections" / f"{task_id}.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        events.append(json.loads(line))
    return events


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_ok_verdict_marks_done_and_writes_reflection_event(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker(
            '{"ok": true, "reason": "criterion satisfied — file written"}'
        )

        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Write README.md with project description",
            success_criteria=["README.md exists and contains 'project description'"],
            judge_invoker=stub,
        )

        assert result["verdict"]["ok"] is True
        assert result["verdict"]["impossible"] is False
        assert "criterion satisfied" in result["verdict"]["reason"]

        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask is not None
        assert subtask.state == TaskState.DONE

        # Reflection event written with the agent_dispatch_done marker
        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        assert len(events) == 1
        assert events[0]["event"] == "agent_dispatch_done"
        assert events[0]["task_id"] == result["subtask_id"]
        assert events[0]["project_id"] == "test"

        # Transcript file was written into the worktree's .claude/
        assert Path(result["transcript_path"]).exists()
        # Dispatch settings written into the same worktree
        assert Path(result["settings_path"]).exists()


# ---------------------------------------------------------------------------
# 2. ok=false → BLOCKED
# ---------------------------------------------------------------------------


class TestNotOkBlocked:
    def test_ok_false_marks_blocked_and_writes_iteration_event(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker(
            '{"ok": false, "reason": "no evidence in transcript"}'
        )

        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Implement feature X",
            success_criteria=["tests pass"],
            judge_invoker=stub,
        )

        assert result["verdict"]["ok"] is False
        assert result["verdict"]["impossible"] is False

        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask is not None
        assert subtask.state == TaskState.BLOCKED

        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        # Iteration event written, NOT a terminal reflection event.
        assert any(ev.get("event") == "iteration_event" for ev in events)
        iter_ev = [ev for ev in events if ev.get("event") == "iteration_event"][0]
        assert iter_ev["verdict"]["ok"] is False
        assert iter_ev["verdict"]["impossible"] is False
        assert "no evidence" in iter_ev["verdict"]["reason"]


# ---------------------------------------------------------------------------
# 3. impossible=true
# ---------------------------------------------------------------------------


class TestImpossibleVerdict:
    def test_impossible_marks_blocked_with_impossible_flag_in_event(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker(
            '{"ok": false, "impossible": true, '
            '"reason": "criterion contradicts itself"}'
        )

        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Square a circle",
            success_criteria=["geometrically impossible thing"],
            judge_invoker=stub,
        )

        assert result["verdict"]["ok"] is False
        assert result["verdict"]["impossible"] is True

        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask.state == TaskState.BLOCKED

        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        iter_ev = [ev for ev in events if ev.get("event") == "iteration_event"][0]
        assert iter_ev["verdict"]["impossible"] is True


# ---------------------------------------------------------------------------
# 4. Judge subprocess error → graceful failure
# ---------------------------------------------------------------------------


class TestJudgeError:
    def test_judge_subprocess_failure_blocks_subtask_with_error_reason(
        self, temp_portfolio_with_repo
    ):
        """Judge invoker raises RuntimeError → wrapper must NOT propagate.

        The subagent + judge share the same invoker; an invoker error
        on the SUBAGENT phase short-circuits to a SUBAGENT_ERROR
        verdict. The subtask is BLOCKED, the function returns normally,
        and the error reason is captured in the iteration_event so
        calibration aggregates can see the failure mode.
        """
        config = temp_portfolio_with_repo["config"]

        def _broken_invoker(prompt: str) -> str:
            raise RuntimeError("Judge command not found: 'nonexistent-cli'")

        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Doesn't matter",
            success_criteria=["anything"],
            judge_invoker=_broken_invoker,
        )

        assert result["verdict"]["ok"] is False
        # Surfaces as SUBAGENT_ERROR because the same invoker is used
        # for the subagent phase, which runs first and short-circuits.
        assert "SUBAGENT_ERROR" in result["verdict"]["reason"]

        subtask = get_task(config, "test", result["subtask_id"])
        assert subtask.state == TaskState.BLOCKED

        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        iter_ev = [ev for ev in events if ev.get("event") == "iteration_event"][0]
        assert "SUBAGENT_ERROR" in iter_ev["verdict"]["reason"]


# ---------------------------------------------------------------------------
# 5. Parent task ID linking
# ---------------------------------------------------------------------------


class TestParentLinking:
    def test_parent_id_recorded_in_iteration_event_reason(
        self, temp_portfolio_with_repo
    ):
        """`--parent TASK-001` flows through to the iteration_event reason
        so blocked agent-dispatch failures can be traced back to the parent.
        """
        config = temp_portfolio_with_repo["config"]
        # Create a parent task first
        from clawpm.tasks import add_task
        from clawpm.models import Predictions
        parent = add_task(
            config, "test",
            title="Parent",
            predictions=Predictions(success_criteria=["parent done"]),
        )
        assert parent is not None

        stub = _make_stub_invoker(
            '{"ok": false, "reason": "child not done"}'
        )
        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Child task body",
            success_criteria=["child criterion"],
            parent_id=parent.id,
            judge_invoker=stub,
        )
        assert result["parent_id"] == parent.id

        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        iter_ev = [ev for ev in events if ev.get("event") == "iteration_event"][0]
        assert f"parent={parent.id!r}" in iter_ev["verdict"]["reason"]


# ---------------------------------------------------------------------------
# 6. Reflection event content (success path)
# ---------------------------------------------------------------------------


class TestReflectionEventContent:
    def test_success_reflection_event_carries_predictions_and_criteria(
        self, temp_portfolio_with_repo
    ):
        """Success-path reflection_event must include the predicted
        success_criteria so the calibration aggregates can see what the
        agent was meant to achieve.
        """
        config = temp_portfolio_with_repo["config"]
        stub = _make_stub_invoker(
            '{"ok": true, "reason": "verified"}'
        )
        result = dispatch_agent(
            config=config,
            project_id="test",
            prompt="Do the thing",
            success_criteria=[
                "criterion-A",
                '{"criterion": "criterion-B", "gradeable_signal": "log line", "comparator": "exists"}',
            ],
            judge_invoker=stub,
        )

        events = _read_reflection_jsonl(
            temp_portfolio_with_repo["root"], result["subtask_id"]
        )
        ev = [e for e in events if e.get("event") == "agent_dispatch_done"][0]
        criteria = ev["predictions"]["success_criteria"]
        # First criterion is plain-string; SuccessCriterion serialises
        # bare strings as a bare string in to_dict() via to_yaml().
        criterion_texts = []
        for c in criteria:
            if isinstance(c, dict):
                criterion_texts.append(c.get("criterion"))
            else:
                criterion_texts.append(c)
        assert "criterion-A" in criterion_texts
        assert "criterion-B" in criterion_texts


# ---------------------------------------------------------------------------
# 7. CLI integration — judge-cmd-override smoke test
# ---------------------------------------------------------------------------


class TestCLIJudgeOverride:
    def test_cli_judge_cmd_override_with_stub_command(
        self, temp_portfolio_with_repo, tmp_path
    ):
        """`--judge-cmd-override` lets the operator inject a non-`claude` CLI.

        We write a tiny python stub script that prints an ok=true verdict
        and invoke it via ``sys.executable <script>``. ``shlex.quote`` on
        both tokens lets Windows paths with spaces (``C:\\Users\\Martin
        Workspace\\...``) survive ``shlex.split`` in
        ``agent._make_default_invoker``.
        """
        import shlex
        stub_script = tmp_path / "stub_judge.py"
        stub_script.write_text(
            "import sys\n"
            "sys.stdin.read()\n"
            'print(\'{"ok": true, "reason": "stub-ok"}\')\n',
            encoding="utf-8",
        )
        stub_cmd = (
            f"{shlex.quote(sys.executable)} "
            f"{shlex.quote(str(stub_script))}"
        )
        runner = CliRunner()
        r = runner.invoke(
            main,
            [
                "-p", "test", "agent", "dispatch",
                "--prompt", "Do a thing",
                "--rubric-criteria", "x exists",
                "--judge-cmd-override", stub_cmd,
            ],
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)["data"]
        assert payload["verdict"]["ok"] is True
        assert "stub-ok" in payload["verdict"]["reason"]


# ---------------------------------------------------------------------------
# 8. Edge: no repo_path → AgentDispatchError surfaced as CLI error
# ---------------------------------------------------------------------------


class TestNoRepoPathError:
    def test_dispatch_without_repo_path_raises_agent_dispatch_error(
        self, temp_portfolio_with_repo
    ):
        """If the project has no usable repo_path, dispatch_agent must
        raise AgentDispatchError BEFORE creating a subtask — we don't
        want to leave an orphan task in OPEN state when the dispatch
        can't actually proceed.
        """
        config = temp_portfolio_with_repo["config"]
        # Make the repo_path point at a non-existent directory by
        # rewriting the project settings.toml.
        bogus_repo = temp_portfolio_with_repo["root"] / "no-such-repo"
        settings = (
            temp_portfolio_with_repo["repo_dir"] / ".project" / "settings.toml"
        )
        settings.write_text(
            f'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
            f'repo_path = "{bogus_repo.as_posix()}"\n'
        )
        # Reload config so the change takes effect
        config = load_portfolio_config(temp_portfolio_with_repo["root"])
        stub = _make_stub_invoker('{"ok": true, "reason": "x"}')

        with pytest.raises(AgentDispatchError):
            dispatch_agent(
                config=config,
                project_id="test",
                prompt="x",
                success_criteria=["y"],
                judge_invoker=stub,
            )
