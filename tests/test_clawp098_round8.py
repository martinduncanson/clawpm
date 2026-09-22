"""CLAWP-098 round-8 and round-9 review regressions (Codex, PR #55).

Round 8, after the decision to split moved-worktree rediscovery (and the
session-scoped `_source_repo`) out of this PR:

1. P1 — dispatch's rollback restored its snapshot over a CONCURRENT
   dispatch's freshly written settings.
2. P2 — the post-create materialization check dropped the monorepo project
   prefix, so every committed `--worktree` dispatch for a project living in
   a subdirectory was rejected as unmaterialized. The second, pre-register
   check had the same defect; Codex flagged only the first.
3. P2 — `emit-tree` stamped every emitted task with a baseline resolved from
   the cwd-independent `get_project`, bypassing the session-scoped resolver
   that `add_task` had just been given.
4. P2 — the recovery advice for a stale reused worktree named only
   `git worktree remove`, which leaves the `clawpm/<task>` branch behind at
   its old revision, so every retry rebuilt the same stale checkout.

Round 9 then reversed two of those:

5. P1 — finding 1's compare-before-restore narrowed the race without
   closing it (the "what we wrote" read-back is itself unsynchronised), so
   the sequence now runs under a target-scoped lock. The comparison stays,
   for the writer a lock cannot see: an operator editing the file.
6. P1 — finding 2's fix made monorepo `--worktree` dispatch SUCCEED with a
   session mapping that cannot resolve, which is worse than the abort it
   replaced. It now fails closed, and the layout is tracked separately.
7. P2 — the lease sweep ran after the task was loaded and its worktree
   created, so a fallback applied to this task's own expired lease left two
   revisions in play. The sweep now runs first.

Round 10 then finished finding 5:

8. P2 — the ownership comparison the lock left in place still LEARNED
   ownership by reading the file back after writing it, which the lock
   cannot protect (its whole point is that an operator or editor is not a
   dispatch). `write_dispatch_settings` now returns the bytes it wrote.

Each test fails against the source it was written against.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.dispatch import settings_path
from clawpm.models import Predictions
from clawpm.tasks import add_task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


# ---------------------------------------------------------------------------
# 1. Rollback must not clobber a concurrent dispatch
# ---------------------------------------------------------------------------


class TestReadBytesOrNone:
    """The rollback's ownership comparison depends on this distinguishing
    "absent" from "unreadable" — collapsing them would let a failed read
    compare equal to "we wrote no sidecar" and re-arm the clobber."""

    def test_absent_is_none(self, tmp_path):
        from clawpm.cli.tasks import _read_bytes_or_none

        assert _read_bytes_or_none(tmp_path / "nope") is None

    def test_present_returns_bytes(self, tmp_path):
        from clawpm.cli.tasks import _read_bytes_or_none

        p = tmp_path / "f"
        p.write_bytes(b"payload")
        assert _read_bytes_or_none(p) == b"payload"

    def test_unreadable_is_neither_none_nor_any_content(self, tmp_path, monkeypatch):
        from clawpm.cli.tasks import _read_bytes_or_none

        p = tmp_path / "f"
        p.write_bytes(b"payload")

        def _boom(self, *a, **k):
            raise OSError("simulated EACCES")

        monkeypatch.setattr(Path, "read_bytes", _boom)
        sentinel = _read_bytes_or_none(p)
        assert sentinel is not None
        assert sentinel != b"payload"
        assert sentinel != b""


class TestRollbackLeavesAConcurrentDispatchAlone:
    """Codex P1, PR #55 round 8.

    Two dispatches targeting one directory can both snapshot the same prior
    settings before either writes. If one succeeds while the other's session
    registration fails, the failing one restored its now-stale snapshot over
    the winner's freshly installed hooks — leaving that command reporting
    success with obsolete settings.

    Simulated by having `register_session` replace the settings file (the
    concurrent dispatch winning the race) and then raise (our registration
    failing), which is exactly the interleaving described.
    """

    def test_foreign_settings_are_not_overwritten_by_our_rollback(
        self, monorepo_free_portfolio, monkeypatch
    ):
        config = monorepo_free_portfolio["config"]
        repo_dir = monorepo_free_portfolio["repo_dir"]
        task = add_task(config, "test", title="Concurrent",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".project")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output
        wt_path = Path(json.loads(r.output)["data"]["target_dir"])
        first = settings_path(wt_path).read_bytes()

        foreign = b'{"_concurrent_dispatch": true}'

        def _win_the_race_then_fail(*args, **kwargs):
            # The other dispatch installs ITS settings after ours landed...
            settings_path(wt_path).write_bytes(foreign)
            # ... and our own registration is what fails.
            raise OSError("simulated sessions.jsonl append failure")

        monkeypatch.setattr(
            "clawpm.sessions.register_session", _win_the_race_then_fail
        )
        r2 = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree",
                   "--force"]
        )
        assert r2.exit_code == 1, r2.output
        assert "session_registration_failed" in r2.output

        on_disk = settings_path(wt_path).read_bytes()
        assert on_disk == foreign, (
            "the concurrent dispatch's settings must survive our rollback; "
            "restoring our snapshot over them leaves that command reporting "
            "success with obsolete hooks"
        )
        assert on_disk != first
        assert "no longer the ones this command wrote" in r2.output


# ---------------------------------------------------------------------------
# 2. Monorepo project prefix in both materialization checks
# ---------------------------------------------------------------------------


@pytest.fixture
def monorepo_free_portfolio():
    """Portfolio whose project sits AT the repo root (prefix is empty)."""
    yield from _portfolio(project_subdir=None)


@pytest.fixture
def monorepo_portfolio():
    """Portfolio whose project sits at `packages/proj` inside the repo."""
    yield from _portfolio(project_subdir="packages/proj")


def _portfolio(project_subdir: str | None):
    temp_dir = tempfile.mkdtemp(prefix="clawpm_round8_")
    portfolio_root = Path(temp_dir)
    repo_dir = portfolio_root / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    (repo_dir / "README.md").write_text("hi", encoding="utf-8")
    _git(repo_dir, "add", "README.md")
    _git(repo_dir, "commit", "-q", "-m", "init")

    project_root = (
        repo_dir if project_subdir is None else repo_dir / project_subdir
    )
    project_root.mkdir(parents=True, exist_ok=True)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{project_root.parent.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    project_meta = project_root / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        f'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{project_root.as_posix()}"\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    try:
        yield {
            "root": portfolio_root,
            "repo_dir": repo_dir,
            "project_root": project_root,
            "tasks_dir": tasks_dir,
            "config": load_portfolio_config(portfolio_root),
        }
    finally:
        if old_env:
            os.environ["CLAWPM_PORTFOLIO"] = old_env
        else:
            os.environ.pop("CLAWPM_PORTFOLIO", None)
        subprocess.run(
            ["git", "-C", str(repo_dir), "worktree", "prune"],
            check=False, capture_output=True,
        )


class TestMonorepoWorktreeFailsClosed:
    """Codex P2 round 8, then P1 round 9 — the second reversed the first.

    Round 8 taught the materialization checks to look under the project
    prefix, so a monorepo `--worktree` dispatch stopped aborting and started
    succeeding. Round 9 showed that was the worse outcome: `git worktree
    add` checks out the repository ROOT, so the session gets registered
    against a directory whose `.project/` is one level down,
    `_session_scoped_project_dir` looks only at `worktree_path/.project`,
    and every ID-based mutator in that checkout falls through to the MAIN
    one — CLAWP-098's own corruption, arriving silently.

    Until the session record can carry the project prefix, refusing is the
    honest answer: it is the same outcome the pre-round-8 code produced,
    with a message that says why.
    """

    def test_dispatch_refuses_a_project_in_a_repository_subdirectory(
        self, monorepo_portfolio
    ):
        config = monorepo_portfolio["config"]
        repo_dir = monorepo_portfolio["repo_dir"]
        task = add_task(config, "test", title="Monorepo",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 1, r.output
        assert "monorepo_worktree_unsupported" in r.output
        # And it must refuse BEFORE creating anything, so a retry after the
        # layout changes is not fighting a leftover checkout.
        assert not (repo_dir / ".clawpm-worktrees").exists(), (
            "the guard must run before create_worktree"
        )

    def test_dispatch_without_worktree_still_works_in_a_monorepo(
        self, monorepo_portfolio, tmp_path
    ):
        """The refusal is scoped to --worktree, not to monorepo projects."""
        config = monorepo_portfolio["config"]
        repo_dir = monorepo_portfolio["repo_dir"]
        task = add_task(config, "test", title="MonorepoInPlace",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        target = tmp_path / "in-place"
        target.mkdir()
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id,
                   "--target-dir", str(target)]
        )
        assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# 3. emit-tree's shared baseline is session-scoped
# ---------------------------------------------------------------------------


class TestEmitTreeBaselineIsSessionScoped:
    """Codex P2, PR #55 round 8: the round-7 baseline fix covered `add_task`
    only. `emit-tree` resolves ONE baseline for the whole tree and stamped it
    on every leaf, so emitting from inside a registered worktree on a
    different commit gave every emitted task the main checkout's baseline."""

    def test_baseline_is_resolved_from_the_registered_worktree(
        self, monorepo_free_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.emit_tree import emit_tree, parse_emit_document
        from clawpm.sessions import register_session

        config = monorepo_free_portfolio["config"]
        wt = tmp_path / "registered-wt"
        wt.mkdir()
        register_session(
            monorepo_free_portfolio["root"], "sess-1", "TEST-001", "test", wt
        )
        monkeypatch.chdir(wt)

        seen: list = []

        def _record(repo_path):
            seen.append(repo_path)
            return "recorded-baseline"

        monkeypatch.setattr("clawpm.baseline.resolve_baseline_ref", _record)

        doc = parse_emit_document({
            "schema_version": 1,
            "root": {"title": "Round8 baseline root"},
            "leaves": [
                {
                    "ref": "L1",
                    "parent_ref": None,
                    "title": "Leaf one",
                    "leaf_key": "round8-baseline-L1",
                    "success_criteria": [
                        {
                            "criterion": "Tests pass",
                            "gradeable_signal": "pytest exit 0",
                            "comparator": "eq:0",
                        }
                    ],
                    "delegability": "agent",
                }
            ],
        })
        emit_tree(config, "test", doc)

        assert seen, "emit_tree must resolve a baseline"
        assert seen[0] == wt.resolve(), (
            "emit-tree's shared baseline must come from the session-scoped "
            "checkout its task store was redirected to, not from the "
            "cwd-independent project.repo_path"
        )


# ---------------------------------------------------------------------------
# 4. Stale-worktree recovery must name the branch
# ---------------------------------------------------------------------------


class TestStaleWorktreeRecoveryAdvice:
    """Codex P2, PR #55 round 8: `git worktree remove` leaves the
    `clawpm/<task>` branch behind at its old revision, and create_worktree
    checks that branch out again — so following the advice as given rebuilds
    the same stale checkout and lands back on the same error."""

    def test_recovery_advice_names_the_branch_deletion(
        self, monorepo_free_portfolio
    ):
        config = monorepo_free_portfolio["config"]
        repo_dir = monorepo_free_portfolio["repo_dir"]
        task = add_task(config, "test", title="StaleAdvice",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".project")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output
        wt_path = Path(json.loads(r.output)["data"]["target_dir"])
        wt_task = wt_path / ".project" / "tasks" / f"{task.id}.md"
        wt_task.write_text(
            wt_task.read_text(encoding="utf-8") + "\nstale drift\n",
            encoding="utf-8",
        )

        r2 = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree",
                   "--force"]
        )
        assert r2.exit_code == 1, r2.output
        assert "task_not_materialized" in r2.output
        assert f"git branch -D clawpm/{task.id}" in r2.output, (
            "removing the worktree alone leaves the stale branch, so every "
            "retry recreates the same checkout — the advice has to say so"
        )


# ---------------------------------------------------------------------------
# 5. Round 9 — the rollback race needs a lock, not a comparison
# ---------------------------------------------------------------------------


class TestDispatchTargetLock:
    """Codex P1, PR #55 round 9.

    Round 8's compare-before-restore reads back "what we wrote" AFTER
    `write_dispatch_settings` returns, so a competing dispatch replacing the
    file in that window makes this command record the OTHER command's bytes
    as its own — the comparison then succeeds on a false premise. Only
    serialising the whole sequence closes it.
    """

    def test_dispatch_takes_a_target_scoped_lock(
        self, monorepo_free_portfolio, monkeypatch
    ):
        from clawpm.cli import tasks as tasks_cli

        config = monorepo_free_portfolio["config"]
        repo_dir = monorepo_free_portfolio["repo_dir"]
        task = add_task(config, "test", title="Locked",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".project")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        held: list = []
        real_lock = tasks_cli.file_lock

        def _record(lock_path, *a, **k):
            held.append(Path(lock_path))
            return real_lock(lock_path, *a, **k)

        monkeypatch.setattr(tasks_cli, "file_lock", _record)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output

        target = Path(json.loads(r.output)["data"]["target_dir"])
        dispatch_locks = [p for p in held if p.name.startswith("dispatch-")]
        assert dispatch_locks, f"no target-scoped lock acquired: {held}"
        # Under the portfolio root, never inside the target directory: a
        # sentinel in the target shows up as untracked in the operator's repo.
        for lock in dispatch_locks:
            assert lock.parent == monorepo_free_portfolio["root"] / "locks", lock
            assert target.resolve() not in lock.parents, lock

    def test_lock_is_keyed_on_the_target_so_different_targets_dont_contend(
        self, monorepo_free_portfolio, tmp_path
    ):
        from clawpm.cli.tasks import _dispatch_target_lock

        root = monorepo_free_portfolio["root"]
        a, b = tmp_path / "a", tmp_path / "b"
        a.mkdir()
        b.mkdir()

        # Both acquire concurrently; a shared lock would deadlock or time out.
        with _dispatch_target_lock(root, a, "json"):
            with _dispatch_target_lock(root, b, "json"):
                pass

        # And the same target maps to the same sentinel across calls.
        seen = []
        for _ in range(2):
            with _dispatch_target_lock(root, a, "json"):
                seen.append(sorted(p.name for p in (root / "locks").glob("*.lock")))
        assert seen[0] == seen[1], seen


class TestLeaseSweepRunsBeforeTheTaskIsLoaded:
    """Codex P2, PR #55 round 9.

    The sweep applies a fallback policy to expired leases, which rewrites
    the canonical task — even an OPEN-to-OPEN fallback restamps `updated`.
    Sweeping after the task was read and its SHA validated meant registering
    a pre-fallback revision while the canonical file held another.
    """

    def test_sweep_precedes_task_load(self, monorepo_free_portfolio, monkeypatch):
        from clawpm.cli import tasks as tasks_cli

        config = monorepo_free_portfolio["config"]
        repo_dir = monorepo_free_portfolio["repo_dir"]
        task = add_task(config, "test", title="SweepOrder",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".project")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        order: list[str] = []
        real_get_task = tasks_cli.get_task

        def _sweep(*a, **k):
            order.append("sweep")
            return []

        def _get_task(*a, **k):
            order.append("get_task")
            return real_get_task(*a, **k)

        monkeypatch.setattr("clawpm.leases.sweep", _sweep)
        monkeypatch.setattr(tasks_cli, "get_task", _get_task)

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output
        assert order, "neither sweep nor get_task ran"
        assert order[0] == "sweep", (
            f"the lease sweep must run before the task is loaded, got {order}"
        )


# ---------------------------------------------------------------------------
# 6. Round 10 — ownership must be derived, not read back
# ---------------------------------------------------------------------------


class TestOwnershipIsDerivedNotReadBack:
    """Codex P2, PR #55 round 10.

    Round 9's lock serialises other clawpm dispatches. It explicitly cannot
    serialise an operator or editor writing `settings.local.json`, and the
    round-8 ownership capture read the file back AFTER the write — so such a
    writer landing in that window had their bytes recorded as ours, the
    comparison passed, and the rollback overwrote their edit with the
    pre-dispatch snapshot. The window is removed by taking the bytes from
    the writer instead.
    """

    def test_write_dispatch_settings_returns_exactly_what_is_on_disk(
        self, tmp_path
    ):
        from clawpm.dispatch import write_dispatch_settings

        written = write_dispatch_settings(
            tmp_path, "TEST-001", "test", rubric_markdown="## Criteria\n- one\n"
        )
        assert written.settings_bytes == written.path.read_bytes()
        assert written.sidecar_bytes is not None
        from clawpm.dispatch import session_start_payload_path

        assert written.sidecar_bytes == session_start_payload_path(
            tmp_path
        ).read_bytes()

    def test_written_bytes_have_no_newline_translation(self, tmp_path):
        """Text-mode writes turn \n into \r\n on Windows, which would make
        the returned bytes disagree with the file and break the comparison
        outright. Bytes are written directly for that reason."""
        from clawpm.dispatch import write_dispatch_settings

        written = write_dispatch_settings(tmp_path, "TEST-001", "test")
        assert b"\r\n" not in written.settings_bytes

    def test_an_operator_edit_after_the_write_survives_the_rollback(
        self, monorepo_free_portfolio, monkeypatch
    ):
        config = monorepo_free_portfolio["config"]
        repo_dir = monorepo_free_portfolio["repo_dir"]
        task = add_task(config, "test", title="OperatorEdit",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".project")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output
        wt_path = Path(json.loads(r.output)["data"]["target_dir"])

        edited = b'{"operator": "hand-edited this mid-dispatch"}'

        def _edit_then_fail(*args, **kwargs):
            # Not another dispatch — a writer the lock cannot serialise.
            settings_path(wt_path).write_bytes(edited)
            raise OSError("simulated sessions.jsonl append failure")

        monkeypatch.setattr("clawpm.sessions.register_session", _edit_then_fail)
        r2 = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree",
                   "--force"]
        )
        assert r2.exit_code == 1, r2.output
        assert settings_path(wt_path).read_bytes() == edited, (
            "an edit made after our write is not ours to roll back over"
        )
