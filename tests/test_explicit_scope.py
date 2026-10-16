"""CLAWP-122: opt-in explicit scope at the command entry point.

``scope=None`` is today's ambient (cwd / contextvar) resolution, unchanged. A
non-None :class:`clawpm.sessions.Scope` bypasses ambient resolution entirely:
CANONICAL means "never redirect", BOUND means "resolve sessions as if this
directory were cwd", independent of the real cwd.
"""

from __future__ import annotations

import json
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

# Path.resolve() calls on the target of a successful `tasks dispatch
# --lease-ttl --target-dir`. The first four are informational session-scope
# lookups (warn, fall open); the last four are identity uses: canonicalisation,
# lock key, hook holder token, registry row.
_INFORMATIONAL_RESOLVES = 4
_SUCCESS_PATH_RESOLVES = 8

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

    def test_relative_target_is_absolutised_at_bind_time(self, fx, monkeypatch):
        # Codex r1 P2: Scope.bound(Path(".")) kept "." and re-resolved it
        # against whatever cwd was current at each lookup, so moving cwd
        # changed the same scope's answer.
        monkeypatch.chdir(fx["wt"])
        scope = Scope.bound(Path("."))
        monkeypatch.chdir(fx["repo"])
        assert scope.target.is_absolute()
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["wt_meta"])

    def test_direct_construction_is_absolutised_too(self, fx, monkeypatch):
        monkeypatch.chdir(fx["wt"])
        scope = Scope(Path("."))
        monkeypatch.chdir(fx["repo"])
        assert scope.target.is_absolute()
        assert _same(get_project_dir(fx["config"], "test", scope=scope), fx["wt_meta"])


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

    def test_unresolvable_target_dir_falls_back_to_canonical_loudly(
        self, fx, monkeypatch, caplog
    ):
        # Codex r1 P2: an unavailable explicit target raised out of
        # resolve_scope; ambient resolution logs and falls open to canonical.
        bad = fx["elsewhere"] / "unavailable-volume"
        real_resolve = Path.resolve

        def flaky(self, *a, **kw):
            if self.name == "unavailable-volume":
                raise OSError("simulated unavailable volume")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", flaky)
        with caplog.at_level("WARNING"):
            scope = resolve_scope(fx["config"], "test", target_dir=bad)
        assert scope.is_canonical
        assert any(
            r.levelno >= 30 and "simulated unavailable volume" in r.getMessage()
            for r in caplog.records
        )


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


class TestDispatchTargetDirResolveGuard:
    """CLAWP-136: ``tasks dispatch --target-dir`` must not crash when the
    target cannot be resolved (same class as PR #75 Codex r1 P2 #2)."""

    def test_unresolvable_target_dir_logs_and_does_not_crash(
        self, fx, monkeypatch, caplog
    ):
        task = add_task(fx["config"], "test", "dispatch me")
        bad = fx["elsewhere"] / "unavailable-volume"
        real_resolve = Path.resolve
        failed = []

        def flaky(self, *a, **kw):
            # Fail only the scope resolve (the first); later sites are not
            # the guard under test.
            if self.name == "unavailable-volume" and not failed:
                failed.append(1)
                raise OSError("simulated unavailable volume")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", flaky)
        with caplog.at_level("ERROR"):
            result = CliRunner().invoke(
                main,
                ["--project", "test", "tasks", "dispatch", task.id,
                 "--target-dir", str(bad)],
            )
        assert not isinstance(result.exception, OSError), result.output
        assert result.exit_code == 0, result.output
        assert any(
            "simulated unavailable volume" in r.getMessage() for r in caplog.records
        )


class TestTargetDirResolveGuardEverySite:
    """CLAWP-159: every ``--target-dir`` ``resolve()`` site, not just the first.

    Policy: the scope binding is informational (it only picks which task store
    to read), so it warns and falls back to canonical. Every other site uses
    the path as an IDENTITY (lock key, hook holder token, registry row, lease
    key): a failed canonicalisation there exits with a clean error instead of
    falling back, because a fallback spelling would key a different lock and a
    different holder than a dispatch that resolved successfully.
    """

    @staticmethod
    def _patch(monkeypatch, fail_on):
        """Fail ``Path.resolve()`` on the unavailable volume.

        ``fail_on=None`` fails every call, ``fail_on=N`` fails only the Nth
        (the junction-style "fails once, then succeeds"), ``fail_on=0`` never
        fails but still counts the calls.
        """
        real_resolve = Path.resolve
        seen = []

        def flaky(self, *a, **kw):
            if self.name == "unavailable-volume":
                seen.append(1)
                if fail_on is None or len(seen) == fail_on:
                    raise OSError("simulated unavailable volume")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", flaky)
        return seen

    @staticmethod
    def _dispatch(fx, task, target, *extra):
        return CliRunner().invoke(
            main,
            ["--project", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(target), "--lease-ttl", "600", *extra],
        )

    @staticmethod
    def _assert_clean_refusal(result, target):
        assert not isinstance(result.exception, (OSError, RuntimeError)), (
            result.output
        )
        assert result.exit_code == 1, result.output
        assert "target_dir_unresolvable" in result.output, result.output
        assert not (target / ".claude").exists()

    @staticmethod
    def _assert_one_identity(fx, task, target):
        """The hook's holder token and the lease's holder are the same token."""
        from clawpm.leases import get_lease, holder_token

        canonical = Path(target).resolve()
        token = holder_token(canonical.as_posix())
        settings = canonical / ".claude" / "settings.local.json"
        assert token in settings.read_text(encoding="utf-8")
        lease = get_lease(fx["root"], task.id, "test")
        assert lease.holder_id == token
        assert lease.target_dir == canonical.as_posix()

    def test_lease_grant_target_dir_unresolvable_is_a_clean_error(
        self, fx, monkeypatch
    ):
        from clawpm.leases import get_lease

        task = add_task(fx["config"], "test", "lease me")
        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, None)
        result = CliRunner().invoke(
            main,
            ["--project", "test", "lease", "grant", "--task", task.id,
             "--ttl", "600", "--target-dir", str(bad)],
        )
        self._assert_clean_refusal(result, bad)
        assert get_lease(fx["root"], task.id, "test") is None

    def test_dispatch_with_lease_all_resolves_unavailable(self, fx, monkeypatch):
        from clawpm.leases import get_lease

        task = add_task(fx["config"], "test", "probe")
        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, None)
        result = self._dispatch(fx, task, bad)
        self._assert_clean_refusal(result, bad)
        assert get_lease(fx["root"], task.id, "test") is None

    def test_dispatch_resolve_and_mkdir_both_unavailable(self, fx, monkeypatch):
        """An unreachable mapped drive fails resolve AND every filesystem touch."""
        task = add_task(fx["config"], "test", "probe")
        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, None)
        real_mkdir = Path.mkdir

        def dead_mkdir(self, *a, **kw):
            if self.name == "unavailable-volume":
                raise OSError("simulated unavailable volume (mkdir)")
            return real_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", dead_mkdir)
        result = self._dispatch(fx, task, bad)
        assert not isinstance(result.exception, (OSError, RuntimeError)), (
            result.output
        )
        assert result.exit_code == 1, result.output
        assert "Nothing was dispatched" in result.output, result.output
        assert not (bad / ".claude").exists()

    def test_dispatch_mkdir_unavailable_is_a_clean_error(self, fx, monkeypatch):
        """Resolve works but the directory cannot be created (read-only volume)."""
        task = add_task(fx["config"], "test", "probe")
        bad = fx["elsewhere"] / "unavailable-volume"
        real_mkdir = Path.mkdir

        def dead_mkdir(self, *a, **kw):
            if self.name == "unavailable-volume":
                raise PermissionError("simulated read-only volume")
            return real_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", dead_mkdir)
        result = self._dispatch(fx, task, bad)
        assert not isinstance(result.exception, OSError), result.output
        assert result.exit_code == 1, result.output
        assert "dispatch_blocked" in result.output, result.output

    def test_resolve_call_count_on_the_success_path(self, fx, monkeypatch):
        """Pins the parametrised test's range: it bites only if every call runs."""
        task = add_task(fx["config"], "test", "probe")
        bad = fx["elsewhere"] / "unavailable-volume"
        seen = self._patch(monkeypatch, 0)
        result = self._dispatch(fx, task, bad)
        assert result.exit_code == 0, result.output
        assert len(seen) == _SUCCESS_PATH_RESOLVES, len(seen)

    @pytest.mark.parametrize("nth", range(1, _SUCCESS_PATH_RESOLVES + 1))
    def test_dispatch_with_lease_nth_resolve_unavailable(
        self, fx, monkeypatch, nth
    ):
        """Resolve fails ONCE (the junction case), at each call site in turn.

        The informational scope lookups warn and fall open; the command goes
        on and every identity consumer still agrees. Every identity use must
        refuse cleanly rather than substitute a different spelling: no
        traceback, no lease, nothing left installed.
        """
        from clawpm.leases import get_lease

        task = add_task(fx["config"], "test", "dispatch me")
        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, nth)
        result = self._dispatch(fx, task, bad)
        assert not isinstance(result.exception, (OSError, RuntimeError)), (
            result.output
        )
        if nth <= _INFORMATIONAL_RESOLVES:
            assert result.exit_code == 0, result.output
            self._assert_one_identity(fx, task, bad)
        else:
            assert result.exit_code == 1, result.output
            assert get_lease(fx["root"], task.id, "test") is None
            assert not (bad / ".claude" / "settings.local.json").exists()

    def test_alias_spelling_yields_one_lock_key_and_one_holder(
        self, fx, monkeypatch
    ):
        """Two spellings of one target converge on one identity everywhere."""
        from clawpm.dispatch import dispatch_lock_path

        real = fx["elsewhere"] / "real-target"
        real.mkdir()
        alias = fx["elsewhere"] / "alias-spelling"
        real_resolve = Path.resolve

        def junction(self, *a, **kw):
            if self.name == "alias-spelling":
                return real_resolve(real)
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", junction)
        assert dispatch_lock_path(fx["root"], alias) == dispatch_lock_path(
            fx["root"], real
        )

        task = add_task(fx["config"], "test", "alias dispatch")
        result = self._dispatch(fx, task, alias)
        assert result.exit_code == 0, result.output
        canonical = real_resolve(real)
        self._assert_one_identity(fx, task, real)
        events = [
            json.loads(line) for line in
            (fx["root"] / "dispatches.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        assert [e["target_dir"] for e in events] == [str(canonical)]

    def test_identity_helper_refuses_instead_of_falling_back(self, fx, monkeypatch):
        from clawpm.dispatch import dispatch_lock_path

        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, None)
        with pytest.raises(OSError):
            dispatch_lock_path(fx["root"], bad)

    def test_symlink_loop_runtime_error_becomes_oserror(self, fx, monkeypatch):
        """Python 3.11 raises RuntimeError for a symlink loop; callers catch OSError."""
        from clawpm.sessions import canonical_path

        real_resolve = Path.resolve

        def loop(self, *a, **kw):
            if self.name == "symlink-loop":
                raise RuntimeError("Symlink loop from 'symlink-loop'")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", loop)
        with pytest.raises(OSError, match="Symlink loop"):
            canonical_path(fx["elsewhere"] / "symlink-loop")

    def test_teardown_target_unresolvable_is_a_clean_error(self, fx, monkeypatch):
        bad = fx["elsewhere"] / "unavailable-volume"
        self._patch(monkeypatch, None)
        result = CliRunner().invoke(
            main,
            ["--project", "test", "tasks", "teardown-dispatch",
             "--target-dir", str(bad)],
        )
        assert not isinstance(result.exception, (OSError, RuntimeError)), (
            result.output
        )
        assert result.exit_code == 1, result.output
        assert "teardown_failed" in result.output, result.output


class TestIsTaskStoreCanonicalScope:
    def test_scope_overrides_ambient(self, fx, monkeypatch):
        from clawpm.discovery import is_task_store_canonical

        cfg = fx["config"]
        monkeypatch.chdir(fx["wt"])
        assert not is_task_store_canonical(cfg, "test")
        assert is_task_store_canonical(cfg, "test", scope=Scope.canonical())
        monkeypatch.chdir(fx["elsewhere"])
        assert is_task_store_canonical(cfg, "test")
        assert not is_task_store_canonical(cfg, "test", scope=Scope.bound(fx["wt"]))


class TestRound2Findings:
    """CLAWP-136 Codex r1: RuntimeError from resolve() and pinned stores."""

    def test_resolve_runtimeerror_logs_and_falls_back(self, fx, monkeypatch, caplog):
        from clawpm.sessions import resolve_path_or_none

        real_resolve = Path.resolve

        def loop(self, *a, **kw):
            # Python 3.11 raises RuntimeError("Symlink loop ...") here.
            if self.name == "symlink-loop":
                raise RuntimeError("Symlink loop from 'symlink-loop'")
            return real_resolve(self, *a, **kw)

        monkeypatch.setattr(Path, "resolve", loop)
        with caplog.at_level("ERROR"):
            assert resolve_path_or_none(fx["elsewhere"] / "symlink-loop") is None
            scope = resolve_scope(
                fx["config"], "test", fx["elsewhere"] / "symlink-loop"
            )
        assert scope == Scope.canonical()
        assert any("Symlink loop" in r.getMessage() for r in caplog.records)

    def test_pinned_store_canonicality(self, fx, monkeypatch):
        from clawpm.discovery import is_task_store_canonical

        cfg = fx["config"]
        assert not is_task_store_canonical(
            cfg, "test", scope=Scope.pinned(fx["wt_meta"])
        )
        assert is_task_store_canonical(
            cfg, "test", scope=Scope.pinned(fx["canon"])
        )
        assert is_task_store_canonical(cfg, "test")
        monkeypatch.chdir(fx["wt"])
        assert not is_task_store_canonical(cfg, "test")
