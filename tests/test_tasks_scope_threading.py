"""CLAWP-136-001: ``scope=`` threaded through the ``tasks.py`` mutators.

Two project stores (canonical + one registered worktree) with the ambient cwd
pointing at the OTHER one: an explicit scope must touch only its own store,
the other must stay byte-identical, and an unscoped call keeps the old
ambient behaviour. Where the function already takes ``scope`` as the task's
file globs (add_task, add_task_with_status, edit_task, add_subtask) the store
scope is the ``store_scope=`` keyword.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.sessions import Scope, register_session
from clawpm.tasks import (
    add_subtask,
    add_task,
    add_task_with_status,
    archive_done_tasks,
    cascade_unblock_dependents,
    change_task_state,
    distinct_tags,
    edit_fog,
    edit_task,
    find_task_by_source_request,
    frontier,
    get_next_task,
    list_tasks,
    parent_ready_signal,
    select_next_batch,
    split_task,
)

_SETTINGS = 'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'


def _make_store(base: Path, extra: str = "") -> Path:
    meta = base / ".project"
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    (meta / "settings.toml").write_text(_SETTINGS + extra, encoding="utf-8")
    return meta


@pytest.fixture
def fx(tmp_path, monkeypatch):
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
    cfg = load_portfolio_config(root)
    # Seed both stores ambiently: cwd=elsewhere hits canon, cwd=wt hits the worktree.
    for where, label in ((elsewhere, "canon"), (wt, "wt")):
        monkeypatch.chdir(where)
        add_task(cfg, "test", f"{label}-one", task_id="TST-001", tags=[label],
                 source_request=f"key-{label}")
        add_task(cfg, "test", f"{label}-dep", task_id="TST-002")
        add_task(cfg, "test", f"{label}-blocked", task_id="TST-003",
                 depends=["TST-002"])
        change_task_state(cfg, "test", "TST-003", TaskState.BLOCKED)
        change_task_state(cfg, "test", "TST-002", TaskState.DONE)
    monkeypatch.chdir(elsewhere)
    return {
        "root": root, "canon": canon, "wt_meta": wt_meta, "wt": wt,
        "elsewhere": elsewhere, "config": cfg,
    }


def _snap(meta: Path) -> dict[str, bytes]:
    return {
        p.relative_to(meta).as_posix(): p.read_bytes()
        for p in sorted(meta.rglob("*")) if p.is_file()
    }


_MUTATORS = {
    "add_task": lambda c, kw: add_task(
        c, "test", "new", task_id="TST-099", **kw["store"]),
    "add_task_with_status": lambda c, kw: add_task_with_status(
        c, "test", "new", task_id="TST-098", **kw["store"]),
    "edit_task": lambda c, kw: edit_task(
        c, "test", "TST-001", title="edited", **kw["store"]),
    "edit_fog": lambda c, kw: edit_fog(
        c, "test", "TST-001", add="later", **kw["plain"]),
    "split_task": lambda c, kw: split_task(c, "test", "TST-001", **kw["plain"]),
    "add_subtask": lambda c, kw: add_subtask(
        c, "test", "TST-001", "child", **kw["store"]),
    "archive_done_tasks": lambda c, kw: archive_done_tasks(
        c, "test", older_than_days=0, **kw["plain"]),
    "cascade_unblock_dependents": lambda c, kw: cascade_unblock_dependents(
        c, "test", "TST-002", **kw["plain"]),
}


def _kw(scope: Scope | None) -> dict:
    if scope is None:
        return {"store": {}, "plain": {}}
    return {"store": {"store_scope": scope}, "plain": {"scope": scope}}


@pytest.mark.parametrize("name", sorted(_MUTATORS))
class TestMutatorsHonourScope:
    def test_canonical_scope_beats_a_worktree_cwd(self, fx, monkeypatch, name):
        monkeypatch.chdir(fx["wt"])
        canon0, wt0 = _snap(fx["canon"]), _snap(fx["wt_meta"])
        _MUTATORS[name](fx["config"], _kw(Scope.canonical()))
        assert _snap(fx["wt_meta"]) == wt0
        assert _snap(fx["canon"]) != canon0

    def test_bound_scope_beats_an_ambient_canonical_cwd(self, fx, monkeypatch, name):
        monkeypatch.chdir(fx["elsewhere"])
        canon0, wt0 = _snap(fx["canon"]), _snap(fx["wt_meta"])
        _MUTATORS[name](fx["config"], _kw(Scope.bound(fx["wt"])))
        assert _snap(fx["canon"]) == canon0
        assert _snap(fx["wt_meta"]) != wt0

    def test_unscoped_call_is_still_ambient(self, fx, monkeypatch, name):
        monkeypatch.chdir(fx["wt"])
        canon0, wt0 = _snap(fx["canon"]), _snap(fx["wt_meta"])
        _MUTATORS[name](fx["config"], _kw(None))
        assert _snap(fx["canon"]) == canon0
        assert _snap(fx["wt_meta"]) != wt0


class TestReadersHonourScope:
    def test_list_tasks_and_tags(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        cfg, canon = fx["config"], Scope.canonical()
        titles = {t.title for t in list_tasks(cfg, "test", scope=canon)}
        assert "canon-one" in titles and "wt-one" not in titles
        assert {t.title for t in list_tasks(cfg, "test")} >= {"wt-one"}
        assert ("canon", 1) in distinct_tags(cfg, "test", scope=canon)
        assert ("wt", 1) not in distinct_tags(cfg, "test", scope=canon)
        assert ("wt", 1) in distinct_tags(cfg, "test")

    def test_get_next_task_and_frontier(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        cfg, canon = fx["config"], Scope.canonical()
        nxt = get_next_task(cfg, "test", scope=canon)
        assert nxt is not None and nxt.title.startswith("canon-")
        assert get_next_task(cfg, "test").title.startswith("wt-")
        assert frontier(cfg, "test", "TST-001", scope=canon) is not None

    def test_select_next_batch_and_source_request(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        cfg, canon = fx["config"], Scope.canonical()
        assert select_next_batch(cfg, "test", scope=canon)[0] is None
        hit = find_task_by_source_request(cfg, "test", "key-canon", scope=canon)
        assert hit is not None and hit.title == "canon-one"
        assert find_task_by_source_request(cfg, "test", "key-canon") is None

    def test_parent_ready_signal_threads_scope(self, fx, monkeypatch):
        monkeypatch.chdir(fx["elsewhere"])
        cfg, wt = fx["config"], Scope.bound(fx["wt"])
        child = add_subtask(cfg, "test", "TST-001", "kid", store_scope=wt)
        change_task_state(cfg, "test", child.id, TaskState.DONE, scope=wt)
        sig = parent_ready_signal(cfg, "test", child.id, scope=wt)
        assert sig is not None and sig["parent_id"] == "TST-001"
        # Ambient (canonical) resolution cannot see the worktree-only child.
        assert parent_ready_signal(cfg, "test", child.id) is None


class TestScopeNeverFallsBackToAmbientDiscovery:
    def test_add_task_with_unknown_project_returns_none_not_cwd_store(
        self, fx, monkeypatch
    ):
        # cwd sits inside the worktree's store; an explicit canonical scope for
        # a project the portfolio does not know must not walk up to it.
        monkeypatch.chdir(fx["wt"])
        wt0 = _snap(fx["wt_meta"])
        got = add_task_with_status(
            fx["config"], "no-such-project", "x", task_id="TST-500",
            store_scope=Scope.canonical(),
        )
        assert got == (None, False)
        assert _snap(fx["wt_meta"]) == wt0


def _ledger_rows(root: Path) -> list[dict]:
    path = root / "id_reservations.jsonl"
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*")) if p.is_file() and ".git" not in p.parts
    }


@pytest.fixture
def other_portfolio(fx, tmp_path, monkeypatch):
    """A second portfolio (same project id, own ledger with history) that the
    AMBIENT environment points at: CLAWPM_PORTFOLIO and cwd are both inside it,
    while every call below passes the first portfolio's config explicitly."""
    root = tmp_path / "other-portfolio"
    repo = root / "repo"
    repo.mkdir(parents=True)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    _make_store(repo, f'repo_path = "{repo.as_posix()}"\n')
    ledger = root / "id_reservations.jsonl"
    ledger.write_bytes(
        b'{"key": "TST", "ordinal": 41, "task_id": "TST-041", "project_id": "test", '
        b'"ts": "2026-01-01T00:00:00+00:00"}\n'
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    monkeypatch.chdir(repo)
    return root


class TestReservationLedgerFollowsTheExplicitPortfolio:
    """Codex r1 P2 (PR #121): reservation writes under an explicit scope.

    The ledger is ``<config.portfolio_root>/id_reservations.jsonl`` -- the SAME
    root the scope itself resolves against (a bound scope looks sessions up in
    that portfolio's registry), keyed by the scoped project's own id. Neither
    comes from the ambient environment, so an explicit scope cannot reach
    another portfolio's ledger.
    """

    @pytest.mark.parametrize("kind", ["auto_id", "explicit_id", "subtask"])
    @pytest.mark.parametrize("which", ["canonical", "bound", "pinned"])
    def test_reservation_lands_in_the_callers_portfolio_only(
        self, fx, other_portfolio, which, kind
    ):
        cfg, root = fx["config"], fx["root"]
        scope, store = {
            "canonical": (Scope.canonical(), fx["canon"]),
            "bound": (Scope.bound(fx["wt"]), fx["wt_meta"]),
            "pinned": (Scope.pinned(fx["wt_meta"]), fx["wt_meta"]),
        }[which]
        other0 = _tree(other_portfolio)
        mine0 = _ledger_rows(root)

        if kind == "subtask":
            task = add_subtask(cfg, "test", "TST-001", "kid", store_scope=scope)
        else:
            task, _ = add_task_with_status(
                cfg, "test", "new",
                task_id="TST-077" if kind == "explicit_id" else None,
                store_scope=scope,
            )
        assert task is not None
        assert task.file_path is not None and store in task.file_path.parents

        new_rows = _ledger_rows(root)[len(mine0):]
        assert [r["task_id"] for r in new_rows] == [task.id]
        assert {r["project_id"] for r in new_rows} == {"test"}
        # Nothing at all changed in the portfolio the environment points at.
        assert _tree(other_portfolio) == other0
