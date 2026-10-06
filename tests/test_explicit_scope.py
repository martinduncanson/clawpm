"""CLAWP-122: opt-in explicit scope at the command entry point.

``scope=None`` is today's ambient (cwd / contextvar) resolution, unchanged. A
non-None :class:`clawpm.sessions.Scope` bypasses ambient resolution entirely:
CANONICAL means "never redirect", BOUND means "resolve sessions as if this
directory were cwd", independent of the real cwd.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import (
    get_project_dir,
    get_repo_path,
    get_scoped_project_settings,
    load_portfolio_config,
    resolve_scope,
)
from clawpm.sessions import Scope, register_session, suppress_session_resolution
from clawpm.tasks import add_task, get_task, get_tasks_dir, touch_task_updated

_SETTINGS = 'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'


def _make_store(base: Path, extra: str = "") -> Path:
    meta = base / ".project"
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    (meta / "settings.toml").write_text(_SETTINGS + extra, encoding="utf-8")
    return meta


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Canonical project plus one registered worktree (both with a .project/)."""
    root = tmp_path / "portfolio"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    canon = _make_store(repo, f'repo_path = "{repo.as_posix()}"\n')
    wt = tmp_path / "wt"
    wt_meta = _make_store(wt)
    register_session(root, "sess-wt", "SEED", "test", wt)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    monkeypatch.chdir(elsewhere)
    return {
        "root": root, "repo": repo, "canon": canon, "wt": wt,
        "wt_meta": wt_meta, "elsewhere": elsewhere,
        "config": load_portfolio_config(root),
    }


def _same(a, b) -> bool:
    return a is not None and b is not None and Path(a).resolve() == Path(b).resolve()


class TestNoneScopeIsAmbient:
    def test_outside_a_worktree(self, fx):
        cfg = fx["config"]
        assert _same(get_project_dir(cfg, "test"), fx["canon"])
        assert _same(get_project_dir(cfg, "test", scope=None), fx["canon"])
        assert _same(get_repo_path(cfg, "test", scope=None), fx["repo"])
        assert _same(get_tasks_dir(cfg, "test", scope=None), fx["canon"] / "tasks")

    def test_inside_a_worktree(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        cfg = fx["config"]
        assert _same(get_project_dir(cfg, "test", scope=None), fx["wt_meta"])
        assert _same(get_repo_path(cfg, "test", scope=None), fx["wt"])
        assert _same(get_tasks_dir(cfg, "test", scope=None), fx["wt_meta"] / "tasks")

    def test_suppression_still_governs_a_none_scope(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        with suppress_session_resolution():
            assert _same(get_project_dir(fx["config"], "test", scope=None), fx["canon"])


class TestCanonicalScope:
    def test_ignores_a_worktree_cwd(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        cfg, scope = fx["config"], Scope.canonical()
        assert _same(get_project_dir(cfg, "test", scope=scope), fx["canon"])
        assert _same(get_repo_path(cfg, "test", scope=scope), fx["repo"])
        assert _same(get_tasks_dir(cfg, "test", scope=scope), fx["canon"] / "tasks")
        settings = get_scoped_project_settings(cfg, "test", scope=scope)
        assert settings is not None and settings.task_prefix != "WTPFX"


class TestBoundScope:
    def test_resolves_the_bound_worktree_from_elsewhere(self, fx):
        cfg, scope = fx["config"], Scope.bound(fx["wt"])
        assert _same(get_project_dir(cfg, "test", scope=scope), fx["wt_meta"])
        assert _same(get_repo_path(cfg, "test", scope=scope), fx["wt"])
        assert _same(get_tasks_dir(cfg, "test", scope=scope), fx["wt_meta"] / "tasks")

    def test_bound_to_a_non_worktree_is_canonical(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        scope = Scope.bound(fx["elsewhere"])
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["canon"])

    def test_beats_suppression(self, fx):
        # An explicit scope is the decision; the ambient contextvar is not consulted.
        with suppress_session_resolution():
            got = get_project_dir(fx["config"], "test", scope=Scope.bound(fx["wt"]))
        assert _same(got, fx["wt_meta"])

    def test_scope_is_frozen(self, fx):
        with pytest.raises(Exception):
            Scope.canonical().target = fx["wt"]  # type: ignore[misc]


class TestResolveScope:
    def test_freezes_the_ambient_answer(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        scope = resolve_scope(fx["config"], "test")
        monkeypatch.chdir(fx["elsewhere"])
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["wt_meta"])

    def test_outside_a_worktree_freezes_canonical_behaviour(self, fx, monkeypatch):
        scope = resolve_scope(fx["config"], "test")
        monkeypatch.chdir(fx["wt"])
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["canon"])

    def test_suppression_freezes_as_canonical(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        with suppress_session_resolution():
            scope = resolve_scope(fx["config"], "test")
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["canon"])

    def test_explicit_target_dir_wins_over_cwd(self, fx):
        scope = resolve_scope(fx["config"], "test", target_dir=fx["wt"])
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["wt_meta"])


class TestScopeThreadsThroughTasks:
    def test_get_task_and_touch_honour_scope(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        task = add_task(fx["config"], "test", "worktree-only task")
        monkeypatch.chdir(fx["elsewhere"])
        scope = Scope.bound(fx["wt"])
        assert get_task(fx["config"], "test", task.id) is None
        assert get_task(fx["config"], "test", task.id, scope=scope) is not None
        assert touch_task_updated(fx["config"], "test", task.id) is False
        assert touch_task_updated(fx["config"], "test", task.id, scope=scope) is True


class TestLogAddExemplar:
    """``log add`` resolves its scope once at the entry point and threads it."""

    def test_resolves_scope_exactly_once(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        import clawpm.discovery as discovery

        calls: list = []
        real = discovery.resolve_scope

        def spy(*a, **k):
            calls.append((a, k))
            return real(*a, **k)

        monkeypatch.setattr(discovery, "resolve_scope", spy)
        r = CliRunner().invoke(
            main, ["-p", "test", "log", "add", "--action", "progress", "--summary", "x"],
        )
        assert r.exit_code == 0, r.output
        assert len(calls) == 1, calls

    def test_stays_bound_when_cwd_changes_mid_command(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        task = add_task(fx["config"], "test", "worktree-only task")
        import clawpm.cli.log as cli_log

        real_add = cli_log.add_entry
        seen_git_cwd: list = []
        real_run = subprocess.run

        def run(cmd, *a, **k):
            if k.get("cwd") is not None and cmd[:1] == ["git"]:
                seen_git_cwd.append(Path(k["cwd"]))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *a, **k)

        def add_entry_then_move(*a, **k):
            monkeypatch.chdir(fx["elsewhere"])  # ambient cwd now points nowhere useful
            return real_add(*a, **k)

        monkeypatch.setattr(cli_log, "add_entry", add_entry_then_move)
        monkeypatch.setattr(cli_log.subprocess, "run", run)
        stamps: list = []
        import clawpm.tasks as tasks_mod

        real_touch = tasks_mod.touch_task_updated

        def touch(*a, **k):
            stamps.append(real_touch(*a, **k))
            return stamps[-1]

        monkeypatch.setattr(cli_log, "touch_task_updated", touch)
        r = CliRunner().invoke(
            main, ["-p", "test", "log", "add", "--action", "progress",
                   "--summary", "x", "--task", task.id],
        )
        assert r.exit_code == 0, r.output
        assert seen_git_cwd and _same(seen_git_cwd[0], fx["wt"])
        assert stamps == [True]
