"""CLAWP-115: `agent dispatch` materializes its generated subtask into the
worktree it creates, then registers a session for that worktree.

Before: add_task wrote the subtask into the canonical checkout (uncommitted) and
create_worktree checked out committed HEAD, so the worktree never held the
task; agent dispatch therefore registered no session and got none of
CLAWP-098's worktree isolation.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

import clawpm.agent as agmod
from clawpm.discovery import load_portfolio_config
from clawpm.models import Task, TaskState
from clawpm.sessions import active_sessions
from clawpm.tasks import get_next_task, get_task, get_tasks_dir


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _make_portfolio(tmp_path, monkeypatch, *, track_project: bool):
    root = tmp_path / "portfolio"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi", encoding="utf-8")
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    meta = repo / ".project"
    meta.mkdir()
    (meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo.as_posix()}"\n',
        encoding="utf-8",
    )
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    _git(repo, "add", "README.md")
    if track_project:
        _git(repo, "add", ".project/settings.toml")
    _git(repo, "commit", "-q", "-m", "init")

    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    return {"root": root, "repo": repo, "config": load_portfolio_config(root)}


@pytest.fixture
def tracked(tmp_path, monkeypatch):
    fx = _make_portfolio(tmp_path, monkeypatch, track_project=True)
    yield fx
    subprocess.run(
        ["git", "-C", str(fx["repo"]), "worktree", "prune"],
        check=False, capture_output=True,
    )


@pytest.fixture
def untracked(tmp_path, monkeypatch):
    fx = _make_portfolio(tmp_path, monkeypatch, track_project=False)
    yield fx
    subprocess.run(
        ["git", "-C", str(fx["repo"]), "worktree", "prune"],
        check=False, capture_output=True,
    )


def _dispatch(fx):
    return agmod.dispatch_agent(
        config=fx["config"],
        project_id="test",
        prompt="Do a thing",
        success_criteria=["c1"],
        judge_invoker=lambda prompt: '{"ok": true, "reason": "done"}',
        init_codegraph=False,
    )


def _worktree_sessions(fx, target_dir):
    want = Path(target_dir).resolve()
    return [
        s for s in active_sessions(fx["root"])
        if Path(s.worktree_path).resolve() == want
    ]


class TestMaterializeAndRegister:
    def test_session_registered_and_subtask_resolves_in_worktree(
        self, tracked, monkeypatch
    ):
        result = _dispatch(tracked)
        sid, target = result["subtask_id"], Path(result["target_dir"])

        # The verdict is DONE, and the worktree copy follows it into done/.
        wt_file = target / ".project" / "tasks" / "done" / f"{sid}.md"
        assert wt_file.exists(), "subtask was not copied into the worktree"
        assert Task.from_file(wt_file).id == sid

        sessions = _worktree_sessions(tracked, target)
        assert [s.task_id for s in sessions] == [sid]
        assert result["session_id"] == sessions[0].session_id

    def test_eval_stop_lookup_from_the_worktree_finds_the_task_there(
        self, tracked, monkeypatch
    ):
        """`hook eval-stop` calls get_task(config, project, task) with cwd =
        the worktree. Without the copy+session it falls through to the main
        checkout (or, with a session but no copy, to None -> block forever)."""
        result = _dispatch(tracked)
        sid, target = result["subtask_id"], Path(result["target_dir"])

        monkeypatch.chdir(target)
        tasks_dir = get_tasks_dir(tracked["config"], "test")
        assert tasks_dir is not None
        assert tasks_dir.resolve() == (target / ".project" / "tasks").resolve()
        task = get_task(tracked["config"], "test", sid)
        assert task is not None and task.id == sid


def _dispatch_with(fx, verdict_json):
    return agmod.dispatch_agent(
        config=fx["config"],
        project_id="test",
        prompt="Do a thing",
        success_criteria=["c1"],
        judge_invoker=lambda prompt: verdict_json,
        init_codegraph=False,
    )


class TestWorktreeCopyFollowsTheVerdict:
    """Codex r1 P2 (PR #79): the verdict transitions only the canonical store;
    the worktree copy must agree, else `get_next_task` from the worktree picks
    the finished/blocked subtask up again as OPEN."""

    @pytest.mark.parametrize(
        "verdict_json, want",
        [
            ('{"ok": true, "reason": "done"}', TaskState.DONE),
            ('{"ok": false, "reason": "nope", "impossible": false}', TaskState.BLOCKED),
        ],
    )
    def test_worktree_view_matches_canonical(
        self, tracked, monkeypatch, verdict_json, want
    ):
        result = _dispatch_with(tracked, verdict_json)
        sid, target = result["subtask_id"], Path(result["target_dir"])
        assert result["session_id"] is not None

        monkeypatch.chdir(target)
        wt_task = get_task(tracked["config"], "test", sid)
        assert wt_task is not None and wt_task.state == want
        assert target.resolve() in wt_task.file_path.resolve().parents
        nxt = get_next_task(tracked["config"], "test")
        assert nxt is None or nxt.id != sid

    def test_sync_failure_is_logged_and_never_aborts(
        self, tracked, monkeypatch, caplog
    ):
        real = agmod.change_task_state
        calls = []

        def _flaky(config, project_id, task_id, new_state, **kw):
            calls.append(new_state)
            if len(calls) == 2:  # the worktree-scoped sync, not the canonical one
                raise OSError("simulated sync failure")
            return real(config, project_id, task_id, new_state, **kw)

        monkeypatch.setattr(agmod, "change_task_state", _flaky)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            result = _dispatch(tracked)
        assert result["verdict"]["ok"] is True
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "CLAWP-115" in msgs and "simulated sync failure" in msgs

    def test_no_session_means_no_worktree_sync(self, untracked):
        # Nothing was materialized, so the sync must not touch anything.
        result = _dispatch(untracked)
        assert result["session_id"] is None
        assert result["verdict"]["ok"] is True


class TestProjectGateFilesystemError:
    def test_gate_oserror_is_graceful(self, tracked, monkeypatch, caplog):
        real = agmod.stat_is_dir

        def _deny(path):
            if Path(path).name == ".project":
                raise PermissionError("simulated EACCES")
            return real(path)

        monkeypatch.setattr(agmod, "stat_is_dir", _deny)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            result = _dispatch(tracked)
        target = Path(result["target_dir"])
        assert _worktree_sessions(tracked, target) == []
        assert result["session_id"] is None
        assert "simulated EACCES" in result["materialize_error"]
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "CLAWP-115" in msgs and "simulated EACCES" in msgs
        assert result["verdict"]["ok"] is True


class TestNoProjectDirKeepsOldBehaviour:
    def test_no_copy_and_no_session(self, untracked):
        result = _dispatch(untracked)
        target = Path(result["target_dir"])
        assert not (target / ".project").exists()
        assert _worktree_sessions(untracked, target) == []
        assert result["session_id"] is None
        assert result["materialize_error"] is None


class TestCopyFailureIsLoudAndUnregistered:
    def test_copy_failure(self, tracked, monkeypatch, caplog):
        def _boom(src, dst):
            raise OSError("simulated disk full")

        monkeypatch.setattr(agmod, "_copy_subtask_file", _boom)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            result = _dispatch(tracked)
        target = Path(result["target_dir"])

        assert _worktree_sessions(tracked, target) == []
        assert result["session_id"] is None
        assert "simulated disk full" in result["materialize_error"]
        err = " ".join(r.getMessage() for r in caplog.records)
        assert "CLAWP-115" in err and "simulated disk full" in err
        # The dispatch itself still ran to a verdict.
        assert result["verdict"]["ok"] is True

    def test_verification_failure_when_copy_is_not_the_task(
        self, tracked, monkeypatch, caplog
    ):
        def _wrong(src, dst):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text("---\nid: OTHER-999\n---\n# nope\n", encoding="utf-8")

        monkeypatch.setattr(agmod, "_copy_subtask_file", _wrong)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            result = _dispatch(tracked)
        assert _worktree_sessions(tracked, Path(result["target_dir"])) == []
        assert result["session_id"] is None
        assert "OTHER-999" in result["materialize_error"]
        assert any("CLAWP-115" in r.getMessage() for r in caplog.records)
