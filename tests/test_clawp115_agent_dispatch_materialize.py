"""CLAWP-115: `agent dispatch` materializes its generated subtask into the
worktree it creates, then registers a session for that worktree.

Before: add_task wrote the subtask into the canonical checkout (uncommitted) and
create_worktree checked out committed HEAD, so the worktree never held the
task; agent dispatch therefore registered no session and got none of
CLAWP-098's worktree isolation.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

import clawpm.agent as agmod
from clawpm.discovery import get_project_dir, load_portfolio_config
from clawpm.models import Task, TaskState
from clawpm.sessions import Scope, active_sessions, release_session
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
        # Patch the raw stat the gate performs (os.stat), not an agent-module
        # name: the pre-fix gate was `Path.is_dir()`, which on Python 3.12
        # propagates a PermissionError out of `os.stat`, so this test fails on
        # that code for the real reason (an uncaught fault), not at setup.
        real = os.stat
        canonical_repo = os.path.normcase(str(tracked["repo"]))

        def _deny(path, *args, **kwargs):
            p = Path(path)
            if p.name == ".project" and os.path.normcase(str(p.parent)) != canonical_repo:
                raise PermissionError("simulated EACCES")
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, "stat", _deny)
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


def _tree(root: Path) -> dict:
    """Relative path -> bytes (None for a directory) for everything under root."""
    return {
        p.relative_to(root).as_posix(): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


class TestPinnedScope:
    """The explicit-store scope the verdict sync uses (CLAWP-122 style)."""

    def test_pinned_scope_resolves_exactly_the_pinned_store(self, tracked):
        pinned = tracked["root"] / "elsewhere" / ".project"
        (pinned / "tasks").mkdir(parents=True)
        scope = Scope.pinned(pinned)
        assert not scope.is_canonical
        assert get_project_dir(tracked["config"], "test", scope=scope) == pinned
        assert get_tasks_dir(tracked["config"], "test", scope=scope) == pinned / "tasks"

    def test_pinned_scope_is_absolute_and_not_a_bound_target(self, tracked, monkeypatch):
        monkeypatch.chdir(tracked["root"])
        scope = Scope.pinned(Path("elsewhere") / ".project")
        assert scope.pinned_project_dir == (tracked["root"] / "elsewhere" / ".project")
        assert scope.target is None

    def test_cannot_be_both_bound_and_pinned(self, tmp_path):
        with pytest.raises(ValueError):
            Scope(target=tmp_path, pinned_project_dir=tmp_path / ".project")


class TestVerdictSyncIsPinnedToTheWorktree:
    """Codex r2 P2 (PR #79): the sync must resolve ONLY the worktree store, so
    nothing it does can reach the canonical store or another worktree."""

    @staticmethod
    def _race(fx, after_canonical_verdict):
        """Wrap change_task_state; run *after_canonical_verdict(sid, target)*
        right after the canonical verdict transition, before the sync."""
        real = agmod.change_task_state
        calls: list = []
        seen: dict = {}

        def _wrapped(config, project_id, task_id, new_state, **kw):
            calls.append(kw)
            result = real(config, project_id, task_id, new_state, **kw)
            if len(calls) == 1:
                sessions = [s for s in active_sessions(fx["root"]) if s.task_id == task_id]
                seen["sid"] = task_id
                seen["target"] = Path(sessions[0].worktree_path)
                after_canonical_verdict(fx, task_id, sessions, seen)
            return result

        return _wrapped, calls, seen

    @pytest.mark.parametrize(
        "verdict_json",
        [
            '{"ok": true, "reason": "done"}',
            '{"ok": false, "reason": "nope", "impossible": false}',
        ],
    )
    def test_released_session_cannot_redirect_the_sync_into_canonical(
        self, tracked, monkeypatch, caplog, verdict_json
    ):
        real = agmod.change_task_state
        canonical_meta = tracked["repo"] / ".project"

        def _reopen_and_release(fx, sid, sessions, seen):
            # A concurrent canonical reopen, plus the worktree session going away.
            real(fx["config"], "test", sid, TaskState.OPEN)
            for s in sessions:
                release_session(fx["root"], s.session_id)
            seen["canonical"] = _tree(canonical_meta)

        wrapped, calls, seen = self._race(tracked, _reopen_and_release)
        monkeypatch.setattr(agmod, "change_task_state", wrapped)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            _dispatch_with(tracked, verdict_json)

        assert "canonical" in seen, "the race hook never ran"
        assert _tree(canonical_meta) == seen["canonical"], (
            "the verdict sync wrote into the canonical store"
        )
        assert (canonical_meta / "tasks" / f"{seen['sid']}.md").exists()
        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "CLAWP-115" in msgs

    def test_missing_worktree_copy_skips_the_sync_without_touching_a_store(
        self, tracked, monkeypatch, caplog
    ):
        canonical_meta = tracked["repo"] / ".project"

        def _lose_the_copy(fx, sid, sessions, seen):
            (seen["target"] / ".project" / "tasks" / f"{sid}.md").unlink()
            seen["canonical"] = _tree(canonical_meta)

        wrapped, calls, seen = self._race(tracked, _lose_the_copy)
        monkeypatch.setattr(agmod, "change_task_state", wrapped)
        with caplog.at_level(logging.ERROR, logger="clawpm.agent"):
            result = _dispatch(tracked)

        assert result["verdict"]["ok"] is True
        assert _tree(canonical_meta) == seen["canonical"]
        # The pin failed validation, so the mutator was never called for it.
        assert len(calls) == 1
        assert any("CLAWP-115" in r.getMessage() for r in caplog.records)

    @pytest.mark.parametrize(
        "verdict_json, want",
        [
            ('{"ok": true, "reason": "done"}', TaskState.DONE),
            ('{"ok": false, "reason": "nope", "impossible": false}', TaskState.BLOCKED),
        ],
    )
    def test_normal_sync_moves_the_worktree_copy_via_the_pin(
        self, tracked, monkeypatch, verdict_json, want
    ):
        real = agmod.change_task_state
        calls: list = []

        def _spy(config, project_id, task_id, new_state, **kw):
            calls.append(kw)
            return real(config, project_id, task_id, new_state, **kw)

        monkeypatch.setattr(agmod, "change_task_state", _spy)
        result = _dispatch_with(tracked, verdict_json)
        target = Path(result["target_dir"])

        assert len(calls) == 2
        scope = calls[1]["scope"]
        assert scope.pinned_project_dir == (target / ".project").resolve()
        state_dir = "done" if want == TaskState.DONE else "blocked"
        wt_file = target / ".project" / "tasks" / state_dir / f"{result['subtask_id']}.md"
        assert wt_file.exists()
        assert Task.from_file(wt_file).state == want

    def test_pin_refuses_the_canonical_store(self, tracked):
        with pytest.raises(RuntimeError, match="canonical"):
            agmod._pin_worktree_scope(
                tracked["config"], "test", "TEST-001", tracked["repo"]
            )
