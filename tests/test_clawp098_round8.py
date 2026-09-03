"""CLAWP-098 round-8 review regressions (Codex, PR #55).

Four findings survived the round-8 decision to split moved-worktree
rediscovery (and the session-scoped `_source_repo`) out of this PR:

1. P1 — dispatch's rollback restored its snapshot over a CONCURRENT
   dispatch's freshly written settings. Guarded by comparing the file on
   disk against what this invocation itself wrote.
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

Each test fails against the pre-fix source.
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


class TestMonorepoWorktreeMaterialization:
    """Codex P2, PR #55 round 8.

    `git worktree add` checks out the repository ROOT, so a project at
    `packages/proj` keeps its `.project/` under that prefix inside the new
    checkout. Both materialization checks looked for it at the worktree
    root, so every committed `--worktree` dispatch for such a project was
    rejected as unmaterialized.
    """

    def test_dispatch_succeeds_for_a_project_in_a_subdirectory(
        self, monorepo_portfolio
    ):
        config = monorepo_portfolio["config"]
        repo_dir = monorepo_portfolio["repo_dir"]
        project_root = monorepo_portfolio["project_root"]
        task = add_task(config, "test", title="Monorepo",
                        predictions=Predictions(success_criteria=["C1"]))
        _git(repo_dir, "add", ".")
        _git(repo_dir, "commit", "-q", "-m", "seed")

        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "dispatch", task.id, "--worktree"]
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["session_id"], (
            "a committed task in a monorepo project must register a session; "
            "dropping the project prefix reports it as unmaterialized and "
            "silently skips CLAWP-098's isolation"
        )
        # The task really is under the prefix, not at the worktree root.
        wt = Path(data["target_dir"])
        prefix = project_root.relative_to(repo_dir).as_posix()
        assert (wt / prefix / ".project" / "tasks" / f"{task.id}.md").exists()
        assert not (wt / ".project" / "tasks" / f"{task.id}.md").exists()


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
