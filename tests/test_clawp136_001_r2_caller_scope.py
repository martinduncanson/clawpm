"""CLAWP-136-001 round 2: the hand-scoped callers take an explicit ``scope=``.

``agent.dispatch_agent``, ``leases.apply_fallback`` / ``sweep``, ``lease grant``
and ``project doctor --apply`` used to get their store by running under
``suppress_session_resolution()`` (an ambient contextvar). They now accept the
``Scope`` the caller already has and pass it to every task-store call; with no
scope the behaviour is the old canonical resolution.

Fixture: one project id with two stores (canonical + one registered worktree)
and the ambient cwd pointing at the OTHER one than the scope names.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import clawpm.agent as agmod
from clawpm.discovery import load_portfolio_config
from clawpm.doctor_apply import apply_stale_blocked, run_apply_phase
from clawpm.leases import FallbackPolicy, apply_fallback, grant_lease, sweep
from clawpm.models import TaskState
from clawpm.sessions import Scope, register_session
from clawpm.tasks import add_task, change_task_state, get_task

_SETTINGS = 'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'


def _make_store(base: Path, extra: str = "") -> Path:
    meta = base / ".project"
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    (meta / "settings.toml").write_text(_SETTINGS + extra, encoding="utf-8")
    return meta


def _snap(meta: Path) -> dict[str, bytes]:
    return {
        p.relative_to(meta).as_posix(): p.read_bytes()
        for p in sorted(meta.rglob("*")) if p.is_file()
    }


@pytest.fixture
def fx(tmp_path, monkeypatch):
    root = tmp_path / "portfolio"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi", encoding="utf-8")
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    canon = _make_store(repo, f'repo_path = "{repo.as_posix()}"\n')
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo),
         "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo),
         "commit", "-q", "-m", "init"], check=True, capture_output=True)
    wt = tmp_path / "wt"
    wt_meta = _make_store(wt)
    register_session(root, "sess-wt", "SEED", "test", wt)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    cfg = load_portfolio_config(root)
    # Same ids in both stores, each PROGRESS (reapable) with a BLOCKED dependent.
    for where in (elsewhere, wt):
        monkeypatch.chdir(where)
        add_task(cfg, "test", "work", task_id="TST-001")
        change_task_state(cfg, "test", "TST-001", TaskState.PROGRESS)
        add_task(cfg, "test", "dep", task_id="TST-002")
        add_task(cfg, "test", "blocked", task_id="TST-003", depends=["TST-002"])
        change_task_state(cfg, "test", "TST-003", TaskState.BLOCKED)
        change_task_state(cfg, "test", "TST-002", TaskState.DONE)
    monkeypatch.chdir(elsewhere)
    return {
        "root": root, "repo": repo, "canon": canon, "wt_meta": wt_meta,
        "wt": wt, "elsewhere": elsewhere, "config": cfg,
    }


def _expired_lease(fx, policy=FallbackPolicy.REQUEUE):
    grant_lease(fx["root"], "TST-001", "test", ttl_seconds=1, fallback_policy=policy)
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    from clawpm.leases import get_lease
    return get_lease(fx["root"], "TST-001", "test"), future


class TestApplyFallbackScope:
    def test_bound_scope_beats_the_canonical_default(self, fx):
        lease, now = _expired_lease(fx)
        canon0 = _snap(fx["canon"])
        out = apply_fallback(
            fx["config"], fx["root"], lease, now, scope=Scope.bound(fx["wt"])
        )
        assert out["transitioned"] is True
        assert _snap(fx["canon"]) == canon0
        assert get_task(
            fx["config"], "test", "TST-001", scope=Scope.bound(fx["wt"])
        ).state == TaskState.OPEN

    def test_canonical_scope_beats_a_worktree_cwd(self, fx, monkeypatch):
        lease, now = _expired_lease(fx)
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        apply_fallback(
            fx["config"], fx["root"], lease, now, scope=Scope.canonical()
        )
        assert _snap(fx["wt_meta"]) == wt0
        assert get_task(
            fx["config"], "test", "TST-001", scope=Scope.canonical()
        ).state == TaskState.OPEN

    def test_no_scope_stays_canonical(self, fx, monkeypatch):
        lease, now = _expired_lease(fx)
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        apply_fallback(fx["config"], fx["root"], lease, now)
        assert _snap(fx["wt_meta"]) == wt0

    def test_sweep_threads_scope(self, fx):
        _expired_lease(fx)
        canon0 = _snap(fx["canon"])
        now = datetime.now(timezone.utc) + timedelta(hours=1)
        acts = sweep(fx["config"], fx["root"], now=now, scope=Scope.bound(fx["wt"]))
        assert [a["transitioned"] for a in acts] == [True]
        assert _snap(fx["canon"]) == canon0


class TestDoctorApplyScope:
    ENTRY = {"task_id": "TST-003", "project_id": "test", "deps": ["TST-002"]}

    def test_apply_stale_blocked_honours_scope(self, fx, monkeypatch):
        # ambient = worktree; scope = canonical -> worktree untouched
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        res = apply_stale_blocked(
            dict(self.ENTRY), fx["config"], scope=Scope.canonical()
        )
        assert res["result"].startswith("promoted blocked -> open")
        assert _snap(fx["wt_meta"]) == wt0

    def test_run_apply_phase_threads_scope(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        applied, _ = run_apply_phase(
            config=fx["config"], drift_tasks=[], stale_blocked=[dict(self.ENTRY)],
            stale_tasks=[], prefix_collisions=[], unreadable_files=[],
            commit_drift=[], missing_markers=[], codex_availability=[],
            scope=Scope.canonical(),
        )
        assert [a["class"] for a in applied] == ["stale_blocked"]
        assert _snap(fx["wt_meta"]) == wt0

    def test_apply_stale_blocked_unscoped_is_ambient(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        canon0 = _snap(fx["canon"])
        apply_stale_blocked(dict(self.ENTRY), fx["config"])
        assert _snap(fx["canon"]) == canon0  # ambient cwd wins, as before


class TestLeaseGrantCli:
    def test_canonical_existence_check_passes_an_explicit_scope(self, fx, monkeypatch):
        from click.testing import CliRunner
        import clawpm.tasks as tasks_mod
        from clawpm.cli.base import main

        seen = []
        real = tasks_mod.get_task

        def spy(config, project_id, task_id, *, scope=None):
            seen.append(scope)
            return real(config, project_id, task_id, scope=scope)

        monkeypatch.setattr(tasks_mod, "get_task", spy)
        res = CliRunner().invoke(
            main,
            ["--format", "json", "lease", "grant", "--project", "test",
             "--task", "TST-001", "--ttl", "60"],
        )
        assert res.exit_code == 0, res.output
        assert seen and all(s is not None and s.is_canonical for s in seen)


class TestDispatchAgentScope:
    def _kw(self):
        return dict(
            project_id="test", prompt="Do a thing", success_criteria=["c1"],
            judge_invoker=lambda p: '{"ok": true, "reason": "done"}',
            init_codegraph=False,
        )

    def test_canonical_scope_beats_a_worktree_cwd_without_the_wrapper(
        self, fx, monkeypatch
    ):
        # `_dispatch_agent` is the unwrapped body: today it resolves ambiently,
        # so from a worktree cwd it writes the WORKTREE's store.
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        res = agmod._dispatch_agent(
            config=fx["config"], scope=Scope.canonical(), **self._kw()
        )
        assert _snap(fx["wt_meta"]) == wt0
        assert get_task(
            fx["config"], "test", res["subtask_id"], scope=Scope.canonical()
        ) is not None

    def test_bound_scope_beats_an_ambient_canonical_cwd(self, fx):
        canon0 = _snap(fx["canon"])
        res = agmod.dispatch_agent(
            config=fx["config"], scope=Scope.bound(fx["wt"]), **self._kw()
        )
        assert _snap(fx["canon"]) == canon0
        assert get_task(
            fx["config"], "test", res["subtask_id"], scope=Scope.bound(fx["wt"])
        ) is not None

    def test_no_scope_is_canonical_as_before(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        res = agmod.dispatch_agent(config=fx["config"], **self._kw())
        assert _snap(fx["wt_meta"]) == wt0
        assert get_task(
            fx["config"], "test", res["subtask_id"], scope=Scope.canonical()
        ) is not None


class TestDispatchOpportunisticSweepStore:
    """Gate r1 P1 (PR #123): the sweep `tasks dispatch` runs before loading its
    task must act on the CANONICAL store when dispatch runs from a registered
    worktree whose own store holds a same-id task."""

    def test_sweep_from_worktree_cwd_never_touches_worktree_store(
        self, fx, monkeypatch
    ):
        import clawpm.leases as leases_mod
        from click.testing import CliRunner
        from clawpm.cli.base import main

        _expired_lease(fx)  # TST-001 leased on the canonical store (ttl 1s)
        real_sweep = leases_mod.sweep
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        monkeypatch.setattr(
            leases_mod, "sweep", lambda *a, **k: real_sweep(*a, now=future, **k)
        )
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        CliRunner().invoke(
            main,
            ["--format", "json", "tasks", "dispatch", "TST-002", "--project", "test"],
        )
        assert _snap(fx["wt_meta"]) == wt0, "sweep leaked into the worktree store"
        assert get_task(
            fx["config"], "test", "TST-001", scope=Scope.canonical()
        ).state == TaskState.OPEN, "the canonical store's expired lease not reaped"
