"""CLAWP-161: the remaining unguarded ``Path.resolve()`` sites (CLAWP-159 follow-up).

Policy, per site (see the CLAWP-159 lessons in ``test_explicit_scope``):

* The value is an IDENTITY (persisted in a ledger, compared for equality or
  containment, authorising a write): a failed canonicalisation REFUSES with a
  clean structured outcome. It never falls back to an unresolved spelling,
  because a junction alias would then key a different identity.
* The value only feeds a best-effort listing: warn and skip.
* A value is resolved ONCE per operation; the result is passed down.

Each test fails resolve at the guarded call and, where the site has several
resolves, persistently from the Nth call on.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import (
    ScopeStoreMismatchError,
    assert_scope_store_in_portfolio,
    discover_untracked_repos,
    is_task_store_canonical,
    load_portfolio_config,
)
from clawpm.models import PortfolioConfig, ProjectStatus
from clawpm.pr_record import PrRecordError, record_pr_verification
from clawpm.sessions import (
    SESSION_REGISTRY_FILENAME,
    Scope,
    SessionRebindError,
    persist_relocated_worktree,
    register_session,
)

_SETTINGS = 'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
_RECORD = {
    "pr": 7,
    "head": "a" * 40,
    "merge_commit": "c" * 40,
    "verified_at": "2026-10-15T10:00:00+00:00",
    "remote": "fork",
    "repo": "acme/widgets",
}


def fail_resolve(monkeypatch, match, *, from_call=1, exc=OSError):
    """Make ``Path.resolve`` raise for paths *match* selects, from call N on.

    Persistent, not one-shot: a volume that went away stays away. Returns the
    list of matching calls so a test can also assert how many there were.
    """
    real = Path.resolve
    seen: list[Path] = []

    def flaky(self, *a, **kw):
        if match(self):
            seen.append(self)
            if len(seen) >= from_call:
                raise exc("simulated unavailable volume")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", flaky)
    return seen


def fail_realpath(monkeypatch, match, *, from_call=1, exc=OSError):
    """The same for ``os.path.realpath`` (``discovery._same_dir`` calls it directly)."""
    real = os.path.realpath
    seen: list[str] = []

    def flaky(p, *a, **kw):
        if match(Path(p)):
            seen.append(p)
            if len(seen) >= from_call:
                raise exc("simulated unavailable volume")
        return real(p, *a, **kw)

    monkeypatch.setattr(os.path, "realpath", flaky)
    return seen


def _named(name):
    return lambda p: name in p.parts


def _make_store(base: Path, extra: str = "") -> Path:
    meta = base / ".project"
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    (meta / "settings.toml").write_text(_SETTINGS + extra, encoding="utf-8")
    return meta


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Portfolio with project 'test' (repo dir ``canonrepo``) and a scratch dir."""
    root = tmp_path / "portfolio"
    repo = root / "canonrepo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    canon = _make_store(repo, f'repo_path = "{repo.as_posix()}"\n')
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    monkeypatch.chdir(elsewhere)
    return {
        "root": root, "repo": repo, "canon": canon, "elsewhere": elsewhere,
        "config": load_portfolio_config(root),
    }


# ---------------------------------------------------------------------------
# sessions.register_session / persist_relocated_worktree  (identity: ledger)
# ---------------------------------------------------------------------------


class TestSessionLedgerResolves:
    @pytest.mark.parametrize("exc", [OSError, RuntimeError])
    def test_register_session_refuses_with_oserror_and_writes_nothing(
        self, tmp_path, monkeypatch, exc
    ):
        root = tmp_path / "portfolio"
        root.mkdir()
        fail_resolve(monkeypatch, _named("unavailable-volume"), exc=exc)
        # RuntimeError is Python 3.11's symlink-loop signal; callers of
        # register_session catch OSError, so it must arrive as one.
        with pytest.raises(OSError):
            register_session(
                root, "s1", "T", "foo", tmp_path / "unavailable-volume"
            )
        assert not (root / SESSION_REGISTRY_FILENAME).exists()

    @pytest.mark.parametrize("exc", [OSError, RuntimeError])
    def test_persist_relocated_worktree_refuses_and_keeps_ledger(
        self, tmp_path, monkeypatch, caplog, exc
    ):
        root = tmp_path / "portfolio"
        root.mkdir()
        # The recorded path is gone, so the ledger needs the relocated path.
        register_session(root, "s1", "T", "foo", tmp_path / "gone-root")
        ledger = root / SESSION_REGISTRY_FILENAME
        before = ledger.read_bytes()
        marker_dir = tmp_path / "unavailable-volume"
        marker_dir.mkdir()
        fail_resolve(monkeypatch, _named("unavailable-volume"), exc=exc)
        with caplog.at_level(logging.ERROR):
            with pytest.raises(SessionRebindError):
                persist_relocated_worktree(root, marker_dir, "T", "foo")
        assert ledger.read_bytes() == before
        assert any("unavailable-volume" in m for m in caplog.messages)

    def test_persist_resolves_the_marker_dir_once(self, tmp_path, monkeypatch):
        """Two sessions to move, one canonicalisation (never one per record)."""
        root = tmp_path / "portfolio"
        root.mkdir()
        register_session(root, "s1", "T", "foo", tmp_path / "gone-root")
        register_session(root, "s2", "T", "foo", tmp_path / "gone-root")
        moved = tmp_path / "moved-root"
        moved.mkdir()
        seen = fail_resolve(monkeypatch, _named("moved-root"), from_call=99)
        assert persist_relocated_worktree(root, moved, "T", "foo") == 2
        assert len(seen) == 1


# ---------------------------------------------------------------------------
# agent._pin_worktree_scope / _refuse_links_under_store  (identity: containment)
# ---------------------------------------------------------------------------


def _worktree_with_task(base: Path, name: str = "wt") -> Path:
    wt = base / name
    (wt / ".project" / "tasks" / "done").mkdir(parents=True)
    (wt / ".project" / "tasks" / "TEST-001.md").write_text(
        "---\nid: TEST-001\ntitle: t\nstate: open\n---\n# t\n", encoding="utf-8"
    )
    return wt


class TestAgentWorktreePin:
    def test_canonical_store_resolved_once(self, fx, tmp_path, monkeypatch):
        import clawpm.agent as agmod

        wt = _worktree_with_task(tmp_path)
        canon = fx["canon"]
        seen = fail_resolve(monkeypatch, lambda p: p == canon, from_call=99)
        scope = agmod._pin_worktree_scope(fx["config"], "test", "TEST-001", wt)
        assert scope.pinned_project_dir is not None
        assert len(seen) == 1, seen

    @pytest.mark.parametrize("exc", [OSError, RuntimeError])
    @pytest.mark.parametrize("from_call", [1, 2])
    def test_sync_with_unresolvable_worktree_logs_and_changes_nothing(
        self, fx, tmp_path, monkeypatch, caplog, exc, from_call
    ):
        import clawpm.agent as agmod
        from clawpm.models import TaskState

        wt = _worktree_with_task(tmp_path, "unavailable-volume")
        register_session(fx["root"], "sess", "TEST-001", "test", wt)
        task_file = wt / ".project" / "tasks" / "TEST-001.md"
        before = task_file.read_bytes()
        fail_resolve(
            monkeypatch, _named("unavailable-volume"), from_call=from_call, exc=exc
        )
        with caplog.at_level(logging.ERROR):
            agmod._sync_worktree_copy(
                fx["config"], "test", "TEST-001", wt, "sess", TaskState.DONE, "n"
            )
        assert task_file.read_bytes() == before
        assert any("could not sync" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# pr_record._verified_tasks_dir  (identity: write-through-a-link check)
# ---------------------------------------------------------------------------


class TestPrRecordVerifiedTasksDir:
    @pytest.fixture
    def store(self, tmp_path):
        project_dir = tmp_path / ".project"
        tasks_dir = project_dir / "tasks"
        tasks_dir.mkdir(parents=True)
        task = tasks_dir / "CLAWP-900.md"
        task.write_bytes(b"---\nid: CLAWP-900\ntitle: t\n---\n\nBody.\n")
        config = PortfolioConfig(
            portfolio_root=tmp_path,
            project_roots=[tmp_path],
            default_status=ProjectStatus.ACTIVE,
        )
        return config, Scope(pinned_project_dir=project_dir), task

    @pytest.mark.parametrize("exc", [OSError, RuntimeError])
    @pytest.mark.parametrize("from_call", [1, 2])
    def test_unresolvable_store_refuses_cleanly(
        self, store, monkeypatch, exc, from_call
    ):
        config, scope, task = store
        before = task.read_bytes()
        fail_resolve(
            monkeypatch, lambda p: p.name in (".project", "tasks"),
            from_call=from_call, exc=exc,
        )
        with pytest.raises(PrRecordError) as info:
            record_pr_verification(config, "clawp", "CLAWP-900", _RECORD, scope=scope)
        assert info.value.code == "unsafe_store"
        assert task.read_bytes() == before


# ---------------------------------------------------------------------------
# discovery._same_dir consumers  (identity: authorises a store write / lease)
# ---------------------------------------------------------------------------


class TestSameDirConsumers:
    @pytest.mark.parametrize("exc", [OSError, RuntimeError])
    def test_pinned_store_that_cannot_be_resolved_is_refused(
        self, fx, monkeypatch, exc
    ):
        pinned = fx["elsewhere"] / "unavailable-volume" / ".project"
        fail_realpath(monkeypatch, _named("unavailable-volume"), exc=exc)
        with pytest.raises(ScopeStoreMismatchError):
            assert_scope_store_in_portfolio(
                fx["config"], "test", Scope.pinned(pinned)
            )

    @pytest.mark.parametrize("from_call", [1, 2])
    def test_equal_spelling_is_not_accepted_when_resolve_fails(
        self, fx, monkeypatch, from_call
    ):
        """The pinned store IS the canonical one by spelling; a failed
        canonicalisation must not accept it on string equality."""
        fail_realpath(monkeypatch, _named("canonrepo"), from_call=from_call)
        with pytest.raises(ScopeStoreMismatchError):
            assert_scope_store_in_portfolio(
                fx["config"], "test", Scope.pinned(fx["canon"])
            )

    def test_is_task_store_canonical_refuses_instead_of_raising(
        self, fx, monkeypatch, caplog
    ):
        fail_realpath(monkeypatch, _named("canonrepo"))
        with caplog.at_level(logging.ERROR):
            got = is_task_store_canonical(
                fx["config"], "test", scope=Scope.pinned(fx["canon"])
            )
        assert got is False
        assert any("simulated unavailable volume" in m for m in caplog.messages)


# ---------------------------------------------------------------------------
# cli project init --in-repo  (identity: becomes the registered repo_path)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [OSError, RuntimeError])
def test_project_init_unresolvable_repo_is_a_clean_error(fx, monkeypatch, exc):
    repo = fx["elsewhere"] / "unavailable-volume"
    repo.mkdir()
    fail_resolve(monkeypatch, _named("unavailable-volume"), exc=exc)
    result = CliRunner().invoke(main, ["project", "init", "--in-repo", str(repo)])
    assert not isinstance(result.exception, (OSError, RuntimeError)), result.output
    assert result.exit_code == 1, result.output
    assert "repo_path_unresolvable" in result.output, result.output
    assert not (repo / ".project").exists()


# ---------------------------------------------------------------------------
# discovery.discover_untracked_repos  (best-effort listing: warn and skip)
# ---------------------------------------------------------------------------


def test_untracked_listing_skips_an_unresolvable_dir(fx, monkeypatch, caplog):
    root = fx["root"]
    for name in ("good-repo", "unavailable-volume"):
        (root / name).mkdir()
        (root / name / ".git").mkdir()
    fail_resolve(monkeypatch, _named("unavailable-volume"))
    with caplog.at_level(logging.ERROR):
        found = discover_untracked_repos(fx["config"])
    names = {r.path.name for r in found}
    assert "good-repo" in names
    assert "unavailable-volume" not in names
    assert any("unavailable-volume" in m for m in caplog.messages)
