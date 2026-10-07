"""CLAWP-114: local-worktree fallback when the session ledger is unreadable.

When ``sessions.jsonl`` cannot be read or stat-ed, ``sessions._replay`` used to
return an empty session set, so ``get_project_dir`` fell through to the
cwd-independent portfolio registry and an ID-based mutator run inside a
dispatched worktree mutated the MAIN checkout (the CLAWP-098 corruption).

Option (b), chosen: with the ledger unavailable, resolve from the dispatch
marker (``.claude/settings.local.json``) that travels with the checkout.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import get_project_dir, load_portfolio_config
from clawpm.dispatch import CLAWPM_MARKER_KEY
from clawpm.models import Predictions
from clawpm.sessions import SESSION_REGISTRY_FILENAME, register_session
from clawpm.tasks import add_task


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Portfolio with project 'test' (main checkout) holding one open task."""
    root = tmp_path / "portfolio"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
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
    config = load_portfolio_config(root)
    task = add_task(
        config, "test", title="T", predictions=Predictions(success_criteria=["C1"])
    )
    return {"root": root, "config": config, "tasks": tasks, "task": task}


def _worktree(fx, tmp_path, *, marker_project="test", with_marker=True, name="wt"):
    """A dispatched-worktree lookalike: own .project/ holding a copy of the
    task, plus (optionally) the dispatch marker. NOT registered in the ledger."""
    wt = tmp_path / name
    wt_tasks = wt / ".project" / "tasks"
    for sub in ("", "done", "blocked"):
        (wt_tasks / sub).mkdir(parents=True, exist_ok=True)
    (wt / ".project" / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tid = fx["task"].id
    (wt_tasks / f"{tid}.md").write_text(
        (fx["tasks"] / f"{tid}.md").read_text(encoding="utf-8"), encoding="utf-8"
    )
    if with_marker:
        (wt / ".claude").mkdir()
        (wt / ".claude" / "settings.local.json").write_text(
            json.dumps({CLAWPM_MARKER_KEY: {
                "task_id": tid, "project_id": marker_project, "version": 1,
            }}),
            encoding="utf-8",
        )
    return wt


@pytest.fixture
def broken_ledger(monkeypatch):
    """Make reading sessions.jsonl raise OSError (no reliance on Windows ACLs)."""
    real = Path.read_text

    def fake(self, *a, **k):
        if self.name == SESSION_REGISTRY_FILENAME:
            raise PermissionError("simulated unreadable ledger")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)


def _arm_ledger(fx, wt):
    # A real ledger entry, so the file exists (and the read fault is what bites).
    register_session(fx["root"], "sess-1", fx["task"].id, "test", wt)


def _block(fx):
    return CliRunner().invoke(
        main, ["-p", "test", "tasks", "state", fx["task"].id, "blocked"]
    )


def test_unreadable_ledger_mutator_hits_the_worktree_not_main(
    fx, tmp_path, monkeypatch, broken_ledger, caplog
):
    wt = _worktree(fx, tmp_path)
    _arm_ledger(fx, wt)
    monkeypatch.chdir(wt)
    tid = fx["task"].id
    with caplog.at_level(logging.WARNING):
        r = _block(fx)
    assert r.exit_code == 0, r.output
    assert (wt / ".project" / "tasks" / "blocked" / f"{tid}.md").exists()
    assert not (wt / ".project" / "tasks" / f"{tid}.md").exists()
    # The main checkout copy is untouched.
    assert (fx["tasks"] / f"{tid}.md").exists()
    assert not (fx["tasks"] / "blocked" / f"{tid}.md").exists()
    text = caplog.text
    assert "Failed to read session registry" in text  # original ERROR kept
    assert "dispatch marker" in text  # fallback leaves its own trace


def test_unreadable_ledger_stat_fault_also_falls_back(
    fx, tmp_path, monkeypatch, caplog
):
    wt = _worktree(fx, tmp_path)
    _arm_ledger(fx, wt)
    real = Path.stat

    def fake(self, *a, **k):
        if self.name == SESSION_REGISTRY_FILENAME:
            raise PermissionError("simulated stat fault")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "stat", fake)
    monkeypatch.chdir(wt / ".project")  # a subdirectory is fine too
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == (wt / ".project").resolve()
    assert "dispatch marker" in caplog.text


def test_no_marker_keeps_todays_registry_behaviour_with_a_trace(
    fx, tmp_path, monkeypatch, broken_ledger, caplog
):
    """Main checkout (or any cwd with no marker): read-only commands still work."""
    (tmp_path / "plain").mkdir()
    _arm_ledger(fx, tmp_path / "plain")
    monkeypatch.chdir(tmp_path / "plain")
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent
    assert "Failed to read session registry" in caplog.text
    assert "dispatch marker" not in caplog.text
    r = CliRunner().invoke(main, ["-p", "test", "tasks", "list"])
    assert r.exit_code == 0, r.output


def test_marker_for_another_project_is_never_matched(
    fx, tmp_path, monkeypatch, broken_ledger
):
    wt = _worktree(fx, tmp_path, marker_project="other-project")
    _arm_ledger(fx, wt)
    monkeypatch.chdir(wt)
    got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent  # registry answer, not the other project's tree


def test_healthy_ledger_does_not_consult_the_marker(fx, tmp_path, monkeypatch, caplog):
    """The marker fallback is for a degraded ledger only; a readable ledger with
    no session for cwd stays a plain registry lookup."""
    wt = _worktree(fx, tmp_path)  # marker, but never registered
    monkeypatch.chdir(wt)
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent
    assert "dispatch marker" not in caplog.text


def test_ledger_with_only_garbage_counts_as_unavailable(
    fx, tmp_path, monkeypatch, caplog
):
    wt = _worktree(fx, tmp_path)
    (fx["root"] / SESSION_REGISTRY_FILENAME).write_text(
        "not json\n[1, 2]\n", encoding="utf-8"
    )
    monkeypatch.chdir(wt)
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == (wt / ".project").resolve()
    assert "dispatch marker" in caplog.text


def test_unreadable_marker_logs_and_falls_through(
    fx, tmp_path, monkeypatch, broken_ledger, caplog
):
    wt = _worktree(fx, tmp_path)
    _arm_ledger(fx, wt)
    (wt / ".claude" / "settings.local.json").write_text("{not json", encoding="utf-8")
    monkeypatch.chdir(wt)
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent
    assert "Failed to read session registry" in caplog.text
