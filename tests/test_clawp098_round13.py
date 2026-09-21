"""CLAWP-098 round-13 review regressions (Codex + antigravity, PR #55).

One structural invariant covers findings 1-5 (see the PR reply): an
operation's artifacts — task store, settings, dispatch hooks, sessions — are
RESOLVED from, WRITTEN into, and TORN DOWN under a single scope; a
half-installed dispatch is never left behind.

- P1 dispatch_agent from a registered worktree wrote the subtask into the
  caller's worktree while its (unregistered) nested worktree's hooks resolve
  canonically -> the hook could never find the task.
- P1 only `tasks dispatch` held the per-target lock; every teardown path
  could unlink a fresh dispatch's settings.
- P2 the dispatch drift gate diffed the canonical checkout while the task was
  loaded from the session checkout.
- P2 emit-tree's root-ID prefix came from the canonical settings.toml.
- P1 a failure INSIDE write_dispatch_settings (after settings.local.json
  landed) escaped before rollback, leaving armed hooks and no session.

Plus the operator decision (2026-09-21): a foreign project id in a registered
worktree's settings.toml FAILS CLOSED, and the missing regression test for the
malformed-TOML fallback narrowed to (OSError, ValueError, KeyError).

Each test fails against the source it was written against.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.concurrency import LockTimeout
from clawpm.discovery import (
    ScopedSettingsMismatchError,
    get_scoped_project_settings,
    load_portfolio_config,
)
from clawpm.dispatch import (
    PartialDispatchWrite,
    dispatch_lock_path,
    settings_path,
    session_start_payload_path,
    teardown_dispatch_settings,
    write_dispatch_settings,
)
from clawpm.models import Predictions, ProjectSettings
from clawpm.sessions import register_session
from clawpm.tasks import add_task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def git_portfolio(tmp_path, monkeypatch):
    """A portfolio whose single project 'test' lives at the root of a real git repo."""
    root = tmp_path / "portfolio"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")

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
    tasks = meta / "tasks"
    for sub in ("", "done", "blocked"):
        (tasks / sub).mkdir(parents=True, exist_ok=True)

    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    yield {
        "root": root,
        "repo": repo,
        "tasks_dir": tasks,
        "config": load_portfolio_config(root),
    }
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        check=False, capture_output=True,
    )


def _emit_doc():
    from clawpm.emit_tree import parse_emit_document

    return parse_emit_document({
        "schema_version": 1,
        "root": {"title": "root"},
        "leaves": [{
            "ref": "L1",
            "parent_ref": None,
            "title": "Leaf one",
            "leaf_key": "round13-L1",
            "success_criteria": [{
                "criterion": "Tests pass",
                "gradeable_signal": "pytest exit 0",
                "comparator": "eq:0",
            }],
            "delegability": "agent",
        }],
    })


def _registered_worktree(fx, tmp_path, monkeypatch, settings_text, name="wt"):
    """A directory with its own .project/ registered as a session, cwd inside it."""
    wt = tmp_path / name
    tasks = wt / ".project" / "tasks"
    for sub in ("", "done", "blocked"):
        (tasks / sub).mkdir(parents=True, exist_ok=True)
    (wt / ".project" / "settings.toml").write_text(settings_text, encoding="utf-8")
    register_session(fx["root"], f"sess-{name}", "SEED", "test", wt)
    monkeypatch.chdir(wt)
    return wt


_OWN_SETTINGS = (
    'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
    'task_prefix = "WTPFX"\n'
)
_FOREIGN_SETTINGS = (
    'id = "other-project"\nname = "Other"\nstatus = "active"\npriority = 3\n'
    'task_prefix = "FOREIGN"\n'
)


# ---------------------------------------------------------------------------
# Operator decision: foreign project id FAILS CLOSED
# ---------------------------------------------------------------------------


class TestForeignProjectIdFailsClosed:
    def test_add_task_raises_and_writes_nothing(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS
        )
        with pytest.raises(ScopedSettingsMismatchError) as ei:
            add_task(git_portfolio["config"], "test", "from a confused worktree")
        assert isinstance(ei.value, ValueError)  # CLI mutation wrappers map it
        msg = str(ei.value)
        assert "'test'" in msg and "'other-project'" in msg
        assert not list((wt / ".project" / "tasks").glob("*.md"))
        # ... and nothing was silently minted in the canonical store either.
        assert not list(git_portfolio["tasks_dir"].glob("*.md"))

    def test_cli_add_reports_a_structured_error(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS
        )
        r = CliRunner().invoke(main, ["-p", "test", "tasks", "add", "-t", "x"])
        assert r.exit_code == 1, r.output
        assert "add_failed" in r.output
        assert "other-project" in r.output

    def test_emit_tree_root_prediction_raises_too(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.emit_tree import _predict_parent_id

        _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS
        )
        doc = _emit_doc()
        with pytest.raises(ScopedSettingsMismatchError):
            _predict_parent_id(doc, git_portfolio["config"], "test")

    # Round 14 (Codex P2): the guard is not confined to the auto-ID branch.

    def test_explicit_id_add_is_guarded_too(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS
        )
        with pytest.raises(ScopedSettingsMismatchError):
            add_task(git_portfolio["config"], "test", "explicit", task_id="TEST-900")
        assert not list((wt / ".project" / "tasks").glob("*.md"))

    def test_add_subtask_is_guarded_too(self, git_portfolio, tmp_path, monkeypatch):
        from clawpm.tasks import add_subtask

        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS)
        with pytest.raises(ScopedSettingsMismatchError):
            add_subtask(git_portfolio["config"], "test", "TEST-001", "child")

    def test_emit_tree_attach_to_is_guarded_too(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.emit_tree import emit_tree, parse_emit_document

        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _FOREIGN_SETTINGS)
        doc = parse_emit_document({
            "schema_version": 1,
            "root": {"attach_to": "TEST-001"},
            "leaves": [{
                "ref": "L1", "parent_ref": None, "title": "Leaf one",
                "leaf_key": "round14-L1",
                "success_criteria": [{
                    "criterion": "Tests pass", "gradeable_signal": "pytest exit 0",
                    "comparator": "eq:0",
                }],
                "delegability": "agent",
            }],
        })
        with pytest.raises(ScopedSettingsMismatchError):
            emit_tree(git_portfolio["config"], "test", doc)

    def test_ordinary_checkout_is_unaffected(self, git_portfolio, monkeypatch):
        """The guard is only about a REGISTERED worktree; outside one the
        resolver is exactly `get_project` (cwd-independent)."""
        monkeypatch.chdir(git_portfolio["repo"])
        got = get_scoped_project_settings(git_portfolio["config"], "test")
        assert got is not None and got.id == "test"


# ---------------------------------------------------------------------------
# Malformed-TOML fallback: the missing regression test (narrowed except)
# ---------------------------------------------------------------------------


class TestMalformedScopedSettingsFallback:
    def test_malformed_toml_logs_and_falls_back_to_the_registry(
        self, git_portfolio, tmp_path, monkeypatch, caplog
    ):
        wt = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, "this is = [not valid toml"
        )
        with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
            task = add_task(git_portfolio["config"], "test", "degraded prefix")
        assert task is not None
        # Fallback = canonical settings (no explicit prefix) -> derived "TEST".
        assert task.id.startswith("TEST-"), task.id
        # The task still lands in the worktree's own store.
        assert (wt / ".project" / "tasks" / f"{task.id}.md").exists()
        # Fail-open WITH a marker (CLAWP-039/041): the degrade is logged.
        assert any(
            "Failed to load session-scoped settings.toml" in rec.getMessage()
            for rec in caplog.records
        ), [r.getMessage() for r in caplog.records]

    @pytest.mark.parametrize("exc", [OSError("EACCES"), ValueError("bad"), KeyError("k")])
    def test_the_three_narrowed_exceptions_fall_back(
        self, git_portfolio, tmp_path, monkeypatch, caplog, exc
    ):
        wt = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS
        )
        wt_settings = wt / ".project" / "settings.toml"
        real = ProjectSettings.load

        def _load(cls, path):
            if Path(path) == wt_settings:
                raise exc
            return real(path)

        monkeypatch.setattr(ProjectSettings, "load", classmethod(_load))
        with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
            got = get_scoped_project_settings(git_portfolio["config"], "test")
        assert got is not None and got.task_prefix != "WTPFX"
        assert any("session-scoped settings.toml" in r.getMessage() for r in caplog.records)

    def test_a_stat_fault_is_logged_not_read_as_no_settings(
        self, git_portfolio, tmp_path, monkeypatch, caplog
    ):
        """Codex P2, round 14: `Path.exists()` turns a permission fault into
        False, so an unreadable settings.toml read as "no settings of its own"
        with no signal. The fault must be distinguished from a missing file."""
        import os as _os

        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        wt_settings = wt / ".project" / "settings.toml"
        real_stat = _os.stat

        def _stat(path, *a, **k):
            if Path(path) == wt_settings:
                raise PermissionError("simulated EACCES")
            return real_stat(path, *a, **k)

        monkeypatch.setattr("clawpm.discovery.os.stat", _stat)
        with caplog.at_level(logging.ERROR, logger="clawpm.discovery"):
            got = get_scoped_project_settings(git_portfolio["config"], "test")
        assert got is not None and got.task_prefix != "WTPFX"
        assert any(
            "Failed to stat session-scoped settings.toml" in r.getMessage()
            for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_a_missing_settings_file_falls_back_silently(
        self, git_portfolio, tmp_path, monkeypatch, caplog
    ):
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        (wt / ".project" / "settings.toml").unlink()
        with caplog.at_level(logging.WARNING, logger="clawpm.discovery"):
            got = get_scoped_project_settings(git_portfolio["config"], "test")
        assert got is not None and got.id == "test"
        assert not caplog.records

    def test_a_genuine_bug_is_not_swallowed(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        """The except was NARROWED on purpose: a programming error must surface,
        not masquerade as a bad settings file."""
        wt = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS
        )
        wt_settings = wt / ".project" / "settings.toml"
        real = ProjectSettings.load

        def _load(cls, path):
            if Path(path) == wt_settings:
                raise RuntimeError("a bug, not a bad file")
            return real(path)

        monkeypatch.setattr(ProjectSettings, "load", classmethod(_load))
        with pytest.raises(RuntimeError, match="a bug"):
            get_scoped_project_settings(git_portfolio["config"], "test")


# ---------------------------------------------------------------------------
# P2: emit-tree prefix is scoped with its task store
# ---------------------------------------------------------------------------


class TestEmitTreePrefixIsSessionScoped:
    def test_root_id_uses_the_worktrees_task_prefix(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.emit_tree import _predict_parent_id

        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        doc = _emit_doc()
        pid = _predict_parent_id(doc, git_portfolio["config"], "test")
        assert pid.startswith("WTPFX-"), pid


# ---------------------------------------------------------------------------
# P2: dispatch drift gate diffs the checkout the task was loaded from
# ---------------------------------------------------------------------------


class TestDriftGateUsesTheSessionCheckout:
    def test_in_place_dispatch_from_a_worktree_checks_that_worktree(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        task = add_task(
            git_portfolio["config"], "test", "in-place",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None

        seen: list = []

        def _record(repo_path, scope, baseline_ref):
            seen.append(Path(repo_path) if repo_path else None)
            return {"status": "skipped", "skip_class": "expected"}

        monkeypatch.setattr("clawpm.baseline.detect_scope_drift", _record)
        r = CliRunner().invoke(main, ["-p", "test", "tasks", "dispatch", task.id])
        assert r.exit_code == 0, r.output
        assert seen, "drift gate did not run"
        assert seen[0].resolve() == wt.resolve(), (
            f"drift gate diffed {seen[0]} but the task was loaded from {wt}"
        )


class TestCustomTargetDispatchResolvesWhereItsHooksWill:
    """Codex P1, round 14: `--target-dir` outside every registered worktree
    launches a process with no session, so its hooks resolve the CANONICAL
    store. The task must be read from there, not from the caller's worktree."""

    def test_outside_target_reads_the_canonical_store(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        # Exists ONLY in the caller's worktree store.
        task = add_task(
            git_portfolio["config"], "test", "worktree-only",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None
        outside = tmp_path / "elsewhere"
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(outside)],
        )
        assert r.exit_code == 1, r.output
        assert "task_not_found" in r.output
        assert not settings_path(outside).exists()

    def test_outside_target_dispatches_a_canonical_task(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.sessions import suppress_session_resolution

        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        with suppress_session_resolution():
            canonical = add_task(
                git_portfolio["config"], "test", "canonical",
                predictions=Predictions(success_criteria=["C1"]),
            )
        assert canonical is not None
        outside = tmp_path / "elsewhere"
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", canonical.id,
                   "--target-dir", str(outside)],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(outside).exists()

    def test_target_in_a_different_registered_worktree_uses_its_store(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        """Codex P1, round 15: binding resolution to 'no session' is not
        enough — a target inside ANOTHER registered worktree launches hooks
        that resolve THAT worktree's store."""
        wt2 = _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS, name="wt2"
        )
        task = add_task(  # lands in wt2's store (cwd is wt2)
            git_portfolio["config"], "test", "in wt2 only",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None
        assert (wt2 / ".project" / "tasks" / f"{task.id}.md").exists()
        # Caller is now a DIFFERENT registered worktree.
        _registered_worktree(
            git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS, name="wt1"
        )
        target = wt2 / "subdir"
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(target)],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(target).exists()

    def test_scope_override_is_reset_after_the_command(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.sessions import scope_cwd

        _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        task = add_task(
            git_portfolio["config"], "test", "t",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None
        CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(tmp_path / "elsewhere")],
        )
        assert scope_cwd() == Path.cwd()

    def test_relative_target_is_resolved_against_cwd(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        """`--target-dir .` from inside a registered worktree is inside it;
        the session lookup resolves the path, it does not compare it raw."""
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        task = add_task(
            git_portfolio["config"], "test", "worktree-only",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--target-dir", "."],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(wt).exists()

    def test_target_inside_the_worktree_keeps_the_session_scope(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        task = add_task(
            git_portfolio["config"], "test", "worktree-only",
            predictions=Predictions(success_criteria=["C1"]),
        )
        assert task is not None
        inside = wt / "subdir"
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(inside)],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(inside).exists()


# ---------------------------------------------------------------------------
# P2 (round 15): progress-hook / commit logging diff the session checkout
# ---------------------------------------------------------------------------


class TestWorkLogDiffsTheSessionCheckout:
    def _capture_git_cwd(self, monkeypatch):
        seen: list = []
        real_run = subprocess.run

        def _run(cmd, *a, **k):
            # `subprocess` is one shared module, so only intercept the git
            # calls this command makes; anything else runs for real.
            if k.get("cwd") is not None and cmd[:1] == ["git"]:
                seen.append(Path(k["cwd"]))
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *a, **k)

        monkeypatch.setattr("clawpm.cli.log.subprocess.run", _run)
        return seen

    def test_log_add_diffs_the_registered_worktree(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        seen = self._capture_git_cwd(monkeypatch)
        r = CliRunner().invoke(
            main, ["-p", "test", "log", "add", "--action", "progress",
                   "--summary", "subagent-tool-use"],
        )
        assert r.exit_code == 0, r.output
        assert seen and seen[0].resolve() == wt.resolve(), seen

    def test_log_commit_reads_the_registered_worktree(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        seen = self._capture_git_cwd(monkeypatch)
        CliRunner().invoke(main, ["-p", "test", "log", "commit", "--dry-run"])
        assert seen and seen[0].resolve() == wt.resolve(), seen


# ---------------------------------------------------------------------------
# P1 (round 15): a stat fault at the final materialization gate fails the
# dispatch and rolls back instead of skipping registration
# ---------------------------------------------------------------------------


class TestMaterializationStatFaultFailsClosed:
    def test_stat_exists_distinguishes_absent_from_a_fault(self, tmp_path, monkeypatch):
        from clawpm.cli.tasks import _stat_exists

        assert _stat_exists(tmp_path) is True
        assert _stat_exists(tmp_path / "nope") is False

        def _boom(path, *a, **k):
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr("clawpm.sessions.os.stat", _boom)
        with pytest.raises(PermissionError):
            _stat_exists(tmp_path)

    def test_final_check_fault_rolls_back_and_registers_nothing(
        self, git_portfolio, monkeypatch
    ):
        from clawpm.sessions import active_sessions

        task = add_task(
            git_portfolio["config"], "test", "t",
            predictions=Predictions(success_criteria=["C1"]),
        )
        _git(git_portfolio["repo"], "add", ".project")
        _git(git_portfolio["repo"], "commit", "-q", "-m", "seed")

        real = __import__("clawpm.cli.tasks", fromlist=["_stat_exists"])._stat_exists
        wt_root = git_portfolio["repo"] / ".clawpm-worktrees" / task.id

        def _fault_once_hooks_are_live(path):
            p = Path(path)
            armed = (wt_root / ".claude" / "settings.local.json").exists()
            if armed and (wt_root == p or wt_root in p.parents):
                raise PermissionError("simulated transient stat fault")
            return real(path)

        monkeypatch.setattr("clawpm.cli.tasks._stat_exists", _fault_once_hooks_are_live)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 1, r.output
        assert "dispatch_blocked" in r.output
        assert not (wt_root / ".claude" / "settings.local.json").exists(), (
            "hooks left live with no session mapping"
        )
        assert active_sessions(git_portfolio["root"]) == []


# ---------------------------------------------------------------------------
# P1: dispatch_agent from a registered worktree stays in the canonical store
# ---------------------------------------------------------------------------


class TestDispatchAgentFromAWorktreeIsCanonical:
    def test_subtask_is_created_where_the_unregistered_worktrees_hooks_look(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.agent import dispatch_agent
        from clawpm.sessions import active_sessions

        wt = _registered_worktree(git_portfolio, tmp_path, monkeypatch, _OWN_SETTINGS)
        result = dispatch_agent(
            config=git_portfolio["config"],
            project_id="test",
            prompt="Do a thing",
            success_criteria=["c1"],
            judge_invoker=lambda prompt: '{"ok": true, "reason": "done"}',
            init_codegraph=False,
        )
        sid = result["subtask_id"]
        canonical = list(git_portfolio["tasks_dir"].rglob(f"{sid}*.md"))
        in_caller = list((wt / ".project" / "tasks").rglob(f"{sid}*.md"))
        assert canonical, (
            "the nested worktree is unregistered, so its Stop hook resolves "
            "the canonical store; the subtask must live there"
        )
        assert not in_caller, f"subtask leaked into the caller's worktree: {in_caller}"
        # And the prefix came from the canonical settings, not the caller's.
        assert not sid.startswith("WTPFX-"), sid
        # The nested worktree stays unregistered (unchanged contract).
        assert all(s.session_id == "sess-wt" for s in active_sessions(git_portfolio["root"]))


# ---------------------------------------------------------------------------
# P1: every teardown takes the same per-target lock as dispatch
# ---------------------------------------------------------------------------


class TestTeardownTakesTheTargetLock:
    def _install(self, root, target):
        target.mkdir(parents=True, exist_ok=True)
        write_dispatch_settings(
            target, "T-1", "test", rubric_markdown="rubric", portfolio_root=root
        )

    def test_teardown_acquires_the_dispatch_sentinel(self, tmp_path, monkeypatch):
        import clawpm.dispatch as dmod

        root, target = tmp_path / "root", tmp_path / "target"
        root.mkdir()
        self._install(root, target)
        held: list[Path] = []
        real = dmod.file_lock

        def _record(lock_path, *a, **k):
            held.append(Path(lock_path))
            return real(lock_path, *a, **k)

        monkeypatch.setattr(dmod, "file_lock", _record)
        assert teardown_dispatch_settings(
            target, task_id="T-1", portfolio_root=root, project_id="test"
        )
        assert held == [dispatch_lock_path(root, target)]

    def test_teardown_waits_out_a_dispatch_in_progress(self, tmp_path, monkeypatch):
        """A teardown from ANOTHER actor cannot unlink settings while a
        dispatch holds the target — the lock `tasks dispatch` takes is the
        very sentinel teardown contends on."""
        import clawpm.dispatch as dmod
        from clawpm.cli.tasks import _dispatch_target_lock

        root, target = tmp_path / "root", tmp_path / "target"
        root.mkdir()
        self._install(root, target)

        real = dmod.file_lock
        monkeypatch.setattr(
            dmod, "file_lock", lambda p, *a, **k: real(p, timeout=0.3)
        )
        outcome: dict = {}

        def _other_actor():
            try:
                outcome["removed"] = teardown_dispatch_settings(
                    target, task_id="T-1", portfolio_root=root, project_id="test"
                )
            except BaseException as exc:  # noqa: BLE001 - recorded for the assert
                outcome["exc"] = exc

        with _dispatch_target_lock(root, target, "json"):
            t = threading.Thread(target=_other_actor)
            t.start()
            t.join(30)
        assert isinstance(outcome.get("exc"), LockTimeout), outcome
        assert settings_path(target).exists(), "teardown ran without the lock"

    def test_teardown_cli_reports_a_contended_lock_structurally(
        self, tmp_path, monkeypatch
    ):
        import clawpm.dispatch as dmod
        from clawpm.cli.tasks import _dispatch_target_lock

        root, target = tmp_path / "root", tmp_path / "target"
        root.mkdir()
        (root / "portfolio.toml").write_text(
            f'portfolio_root = "{root.as_posix()}"\nproject_roots = []\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
        self._install(root, target)
        real = dmod.file_lock
        monkeypatch.setattr(
            dmod, "file_lock", lambda p, *a, **k: real(p, timeout=0.3)
        )
        outcome: dict = {}

        def _run_cli():
            outcome["r"] = CliRunner().invoke(
                main, ["tasks", "teardown-dispatch", "--target-dir", str(target)]
            )

        with _dispatch_target_lock(root, target, "json"):
            t = threading.Thread(target=_run_cli)
            t.start()
            t.join(30)
        r = outcome["r"]
        assert r.exit_code == 1, r.output
        assert "dispatch_blocked" in r.output
        assert settings_path(target).exists()

    def test_teardown_nests_inside_dispatch_rollback(self, tmp_path):
        """Dispatch's own rollback calls teardown from inside the lock; the
        lock is reentrant per thread, so that must not self-deadlock."""
        from clawpm.cli.tasks import _dispatch_target_lock

        root, target = tmp_path / "root", tmp_path / "target"
        root.mkdir()
        self._install(root, target)
        with _dispatch_target_lock(root, target, "json"):
            assert teardown_dispatch_settings(
                target, task_id="T-1", portfolio_root=root, project_id="test"
            )
        assert not settings_path(target).exists()


# ---------------------------------------------------------------------------
# P1: a failure inside the writer rolls back instead of escaping armed
# ---------------------------------------------------------------------------


class TestWriterFailureIsRolledBack:
    def test_writer_reports_what_landed_when_it_fails_afterwards(
        self, tmp_path, monkeypatch
    ):
        root, target = tmp_path / "root", tmp_path / "target"
        root.mkdir()
        target.mkdir()

        def _boom(*a, **k):
            raise OSError("simulated disk full appending dispatches.jsonl")

        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        with pytest.raises(PartialDispatchWrite) as ei:
            write_dispatch_settings(
                target, "T-1", "test", rubric_markdown="rubric", portfolio_root=root
            )
        w = ei.value.written
        assert isinstance(ei.value.cause, OSError)
        assert w.settings_bytes == settings_path(target).read_bytes()
        assert w.sidecar_written is True
        assert w.sidecar_bytes == session_start_payload_path(target).read_bytes()

    def test_a_failed_sidecar_write_is_reported_as_not_written(
        self, tmp_path, monkeypatch
    ):
        target = tmp_path / "target"
        target.mkdir()

        def _boom(*a, **k):
            raise OSError("simulated sidecar failure")

        monkeypatch.setattr("clawpm.dispatch.write_session_start_sidecar_bytes", _boom)
        with pytest.raises(PartialDispatchWrite) as ei:
            write_dispatch_settings(target, "T-1", "test", rubric_markdown="rubric")
        assert ei.value.written.sidecar_written is False
        assert not session_start_payload_path(target).exists()

    def test_cli_leaves_nothing_armed_after_a_writer_failure(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        task = add_task(
            git_portfolio["config"], "test", "t",
            predictions=Predictions(success_criteria=["C1"]),
        )
        target = tmp_path / "target"

        def _boom(*a, **k):
            raise OSError("simulated disk full")

        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(target)],
        )
        assert r.exit_code == 1, r.output
        assert "dispatch_write_failed" in r.output
        assert not settings_path(target).exists(), "hooks left armed"
        assert not session_start_payload_path(target).exists()

    def test_registry_failure_during_rollback_is_not_reported_as_armed(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        """Disk-full breaks BOTH the dispatch append and the follow-up
        `torn_down` append. The hooks are gone by then, so the operator must
        not be told the settings still need manual attention."""
        task = add_task(
            git_portfolio["config"], "test", "t",
            predictions=Predictions(success_criteria=["C1"]),
        )
        target = tmp_path / "target"

        def _boom(*a, **k):
            raise OSError("simulated disk full")

        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        monkeypatch.setattr("clawpm.dispatch.register_teardown", _boom)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(target)],
        )
        assert r.exit_code == 1, r.output
        assert not settings_path(target).exists()
        assert "were removed, but recording" in r.output
        assert "inspect" not in r.output

    def test_a_stat_fault_during_rollback_is_reported_as_still_present(
        self, tmp_path, monkeypatch
    ):
        """Codex P2, round 16: `Path.exists()` swallowed the fault and the
        operator was told the hooks were gone when nobody could tell."""
        from clawpm.cli.tasks import _settings_still_present

        def _boom(path, *a, **k):
            raise PermissionError("simulated EACCES")

        monkeypatch.setattr("clawpm.sessions.os.stat", _boom)
        assert _settings_still_present(tmp_path / "settings.local.json") is True

    def test_dispatch_agent_writes_under_the_target_lock(
        self, git_portfolio, monkeypatch
    ):
        """Codex P2, round 14: teardown takes the per-target lock, so the
        second production writer must too."""
        import clawpm.agent as agmod
        from contextlib import contextmanager

        held: list[tuple] = []
        real = agmod.dispatch_target_lock

        @contextmanager
        def _record(portfolio_root, target_dir):
            held.append((Path(portfolio_root), Path(target_dir)))
            with real(portfolio_root, target_dir):
                yield

        monkeypatch.setattr(agmod, "dispatch_target_lock", _record)
        result = agmod.dispatch_agent(
            config=git_portfolio["config"],
            project_id="test",
            prompt="Do a thing",
            success_criteria=["c1"],
            judge_invoker=lambda prompt: '{"ok": true, "reason": "done"}',
            init_codegraph=False,
        )
        assert [t for _, t in held] == [Path(result["target_dir"])], held

    def test_dispatch_agent_cleans_up_inside_the_same_critical_section(
        self, git_portfolio, monkeypatch
    ):
        """Codex P1, round 15: releasing the lock before the cleanup lets a
        same-task dispatch install its settings in the gap, which a
        marker-only teardown would then delete."""
        import clawpm.agent as agmod
        from contextlib import contextmanager

        events: list[str] = []
        real_lock = agmod.dispatch_target_lock
        real_td = agmod.teardown_dispatch_settings

        @contextmanager
        def _lock(portfolio_root, target_dir):
            events.append("enter")
            try:
                with real_lock(portfolio_root, target_dir):
                    yield
            finally:
                events.append("exit")

        def _td(*a, **k):
            events.append("teardown")
            return real_td(*a, **k)

        def _boom(*a, **k):
            raise OSError("simulated disk full")

        monkeypatch.setattr(agmod, "dispatch_target_lock", _lock)
        monkeypatch.setattr(agmod, "teardown_dispatch_settings", _td)
        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        with pytest.raises(agmod.AgentDispatchError):
            agmod.dispatch_agent(
                config=git_portfolio["config"],
                project_id="test",
                prompt="Do a thing",
                success_criteria=["c1"],
                judge_invoker=lambda prompt: '{"ok": true, "reason": "done"}',
                init_codegraph=False,
            )
        assert events == ["enter", "teardown", "exit"], events

    def test_dispatch_agent_takes_a_partial_write_back_down(
        self, git_portfolio, monkeypatch
    ):
        from clawpm.agent import AgentDispatchError, dispatch_agent

        def _boom(*a, **k):
            raise OSError("simulated disk full")

        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        with pytest.raises(AgentDispatchError, match="write_dispatch_settings"):
            dispatch_agent(
                config=git_portfolio["config"],
                project_id="test",
                prompt="Do a thing",
                success_criteria=["c1"],
                judge_invoker=lambda prompt: '{"ok": true, "reason": "done"}',
                init_codegraph=False,
            )
        armed = list(
            (git_portfolio["repo"] / ".clawpm-worktrees").glob(
                "*/.claude/settings.local.json"
            )
        )
        assert not armed, f"nested worktree left armed: {armed}"

    def test_cli_restores_the_prior_dispatch_after_a_writer_failure(
        self, git_portfolio, tmp_path, monkeypatch
    ):
        task = add_task(
            git_portfolio["config"], "test", "t",
            predictions=Predictions(success_criteria=["C1"]),
        )
        target = tmp_path / "target"
        args = ["-p", "test", "tasks", "dispatch", task.id, "--target-dir", str(target)]
        r1 = CliRunner().invoke(main, args + ["--no-confirm-close"])
        assert r1.exit_code == 0, r1.output
        first = settings_path(target).read_bytes()
        first_sidecar = session_start_payload_path(target).read_bytes()

        def _boom(*a, **k):
            raise OSError("simulated disk full")

        monkeypatch.setattr("clawpm.dispatch.register_dispatch", _boom)
        r2 = CliRunner().invoke(main, args + ["--confirm-close"])
        assert r2.exit_code == 1, r2.output
        assert "dispatch_write_failed" in r2.output
        assert settings_path(target).read_bytes() == first
        assert session_start_payload_path(target).read_bytes() == first_sidecar

    def test_the_settings_write_is_atomic(self, tmp_path, monkeypatch):
        """`PartialDispatchWrite` treats the settings file as all-or-nothing;
        that only holds if a failed write leaves the previous bytes alone."""
        import clawpm.dispatch as dmod

        target = tmp_path / "target"
        target.mkdir()
        write_dispatch_settings(target, "T-1", "test")
        before = settings_path(target).read_bytes()

        def _boom(*a, **k):
            raise OSError("simulated failure at the rename")

        monkeypatch.setattr(dmod.os, "replace", _boom)
        with pytest.raises(OSError):
            write_dispatch_settings(target, "T-1", "test", confirm_close=True)
        assert settings_path(target).read_bytes() == before
        assert not list((target / ".claude").glob("*.tmp")), "temp file leaked"
