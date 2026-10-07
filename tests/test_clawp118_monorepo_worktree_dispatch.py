"""CLAWP-118: ``tasks dispatch --worktree`` for a project in a repo SUBDIRECTORY.

``git worktree add`` checks out the whole repository, so the project root
inside the worktree is ``<worktree>/<prefix>``. The session record carries that
prefix (``project_prefix``; absent/empty for a project at the repo root), the
agent runs in the project root, and session-scoped resolution maps any cwd under
the worktree to the project's ``.project/`` at ``<worktree>/<prefix>/.project``.
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
from clawpm.models import Predictions
from clawpm.sessions import (
    SESSION_REGISTRY_FILENAME,
    SessionRebindError,
    find_session_for_cwd,
    persist_relocated_worktree,
    register_session,
)
from clawpm.tasks import add_task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


def _make_project(root_dir: Path, pid: str) -> Path:
    meta = root_dir / ".project"
    for sub in ("", "done", "blocked"):
        (meta / "tasks" / sub).mkdir(parents=True, exist_ok=True)
    (meta / "settings.toml").write_text(
        f'id = "{pid}"\nname = "{pid}"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{root_dir.as_posix()}"\n',
        encoding="utf-8",
    )
    return meta / "tasks"


def _build(tmp_path, monkeypatch, subdir):
    """Repo with project foo at ``subdir`` (None = repo root) and, for a
    prefixed layout, a sibling project bar next to it. One committed task."""
    root = tmp_path / "portfolio"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi", encoding="utf-8")
    foo_root = repo if subdir is None else repo / subdir
    foo_root.mkdir(parents=True, exist_ok=True)
    foo_tasks = _make_project(foo_root, "foo")
    bar_root = None
    if subdir is not None:
        bar_root = foo_root.parent / "bar"
        bar_root.mkdir()
        _make_project(bar_root, "bar")
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{foo_root.parent.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    config = load_portfolio_config(root)
    task = add_task(
        config, "foo", title="T", predictions=Predictions(success_criteria=["C1"])
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    return {
        "root": root, "repo": repo, "config": config, "task": task,
        "foo_root": foo_root, "bar_root": bar_root, "foo_tasks": foo_tasks,
        "prefix": subdir,
    }


@pytest.fixture
def mono(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, "packages/foo")
    yield fx
    subprocess.run(
        ["git", "-C", str(fx["repo"]), "worktree", "prune"],
        check=False, capture_output=True,
    )


@pytest.fixture
def flat(tmp_path, monkeypatch):
    fx = _build(tmp_path, monkeypatch, None)
    yield fx
    subprocess.run(
        ["git", "-C", str(fx["repo"]), "worktree", "prune"],
        check=False, capture_output=True,
    )


def _dispatch(fx):
    r = CliRunner().invoke(
        main, ["-p", "foo", "tasks", "dispatch", fx["task"].id, "--worktree"]
    )
    assert r.exit_code == 0, r.output
    return Path(json.loads(r.output)["data"]["target_dir"])


def _ledger_events(fx):
    path = fx["root"] / SESSION_REGISTRY_FILENAME
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# (a) dispatch registers a session that resolves from the project root and below


def test_dispatch_resolves_from_project_root_and_subdir(mono, monkeypatch):
    target = _dispatch(mono)
    wt = mono["foo_root"] / ".clawpm-worktrees" / mono["task"].id
    assert target.resolve() == (wt / "packages" / "foo").resolve()
    assert (target / ".claude" / "settings.local.json").exists()
    events = _ledger_events(mono)
    assert events[-1]["project_prefix"] == "packages/foo"

    sub = target / "sub"
    sub.mkdir()
    want = (target / ".project").resolve()
    for cwd in (target, sub, wt):
        monkeypatch.chdir(cwd)
        assert get_project_dir(mono["config"], "foo") == want
    rec = find_session_for_cwd(mono["root"], sub, "foo")
    assert rec is not None and rec.project_prefix == "packages/foo"
    assert rec.project_root.resolve() == target.resolve()


# (b) an ID-based mutator mutates the worktree copy, never the main one


def test_id_mutator_in_worktree_never_touches_main(mono, monkeypatch):
    tid = mono["task"].id
    target = _dispatch(mono)
    monkeypatch.chdir(target)
    r = CliRunner().invoke(main, ["-p", "foo", "tasks", "state", tid, "blocked"])
    assert r.exit_code == 0, r.output
    assert (target / ".project" / "tasks" / "blocked" / f"{tid}.md").exists()
    assert (mono["foo_tasks"] / f"{tid}.md").exists()
    assert not (mono["foo_tasks"] / "blocked" / f"{tid}.md").exists()


# (c) a record without the prefix field resolves exactly as before


def test_record_without_prefix_field_resolves_as_before(tmp_path, monkeypatch, flat):
    wt = tmp_path / "oldwt"
    (wt / ".project" / "tasks").mkdir(parents=True)
    (wt / ".project" / "settings.toml").write_text(
        'id = "foo"\nname = "foo"\nstatus = "active"\npriority = 3\n', encoding="utf-8"
    )
    line = {
        "action": "registered", "session_id": "old-1", "task_id": flat["task"].id,
        "project_id": "foo", "worktree_path": str(wt.resolve()), "ts": "2026-01-01T00:00:00Z",
    }
    ledger = flat["root"] / SESSION_REGISTRY_FILENAME
    ledger.write_text(json.dumps(line) + "\n", encoding="utf-8")
    rec = find_session_for_cwd(flat["root"], wt, "foo")
    assert rec is not None and rec.project_prefix == ""
    assert rec.project_root.resolve() == wt.resolve()
    monkeypatch.chdir(wt)
    assert get_project_dir(flat["config"], "foo") == (wt / ".project").resolve()


def test_malformed_prefix_skips_event_and_logs_error(flat, tmp_path, caplog):
    wt = tmp_path / "w"
    wt.mkdir()
    ledger = flat["root"] / SESSION_REGISTRY_FILENAME
    base = {
        "action": "registered", "task_id": "T", "project_id": "foo",
        "worktree_path": str(wt.resolve()), "ts": "2026-01-01T00:00:00Z",
    }
    lines = [
        {**base, "session_id": "s-int", "project_prefix": 42},
        {**base, "session_id": "s-abs", "project_prefix": "/etc"},
        {**base, "session_id": "s-dots", "project_prefix": "a/../../b"},
        {**base, "session_id": "s-ok", "project_prefix": "packages/foo/"},
    ]
    ledger.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    with caplog.at_level(logging.ERROR):
        rec = find_session_for_cwd(flat["root"], wt, "foo")
    assert rec is not None and rec.session_id == "s-ok"
    assert rec.project_prefix == "packages/foo"
    assert sum("project_prefix" in m for m in caplog.messages) >= 3


# (d) a prefixed project and a sibling project do not cross-match


def test_sibling_project_does_not_cross_match(mono, monkeypatch):
    target = _dispatch(mono)
    wt = mono["foo_root"] / ".clawpm-worktrees" / mono["task"].id
    foo_wt_proj = (target / ".project").resolve()
    monkeypatch.chdir(wt / "packages" / "bar")
    got = get_project_dir(mono["config"], "bar")
    assert got is None or got.resolve() != foo_wt_proj
    assert find_session_for_cwd(mono["root"], wt / "packages" / "bar", "bar") is None


# (e) relocated prefixed worktree: marker resolves, teardown persists


def test_relocated_prefixed_worktree_resolves_and_teardown_persists(
    mono, tmp_path, monkeypatch
):
    from clawpm.dispatch import teardown_dispatch_settings

    tid = mono["task"].id
    target = _dispatch(mono)
    old_root = mono["foo_root"] / ".clawpm-worktrees" / tid
    moved = tmp_path / "relocated"
    _git(mono["repo"], "worktree", "move", str(old_root), str(moved))
    proj = moved / "packages" / "foo"
    monkeypatch.chdir(proj)
    ledger_before = (mono["root"] / SESSION_REGISTRY_FILENAME).read_bytes()
    assert get_project_dir(mono["config"], "foo") == (proj / ".project").resolve()
    assert (mono["root"] / SESSION_REGISTRY_FILENAME).read_bytes() == ledger_before

    assert teardown_dispatch_settings(
        proj, task_id=tid, portfolio_root=mono["root"], project_id="foo"
    ) is True
    rec = find_session_for_cwd(mono["root"], proj, "foo")
    assert rec is not None and rec.worktree_path.resolve() == moved.resolve()
    assert rec.project_prefix == "packages/foo"
    assert get_project_dir(mono["config"], "foo") == (proj / ".project").resolve()
    r = CliRunner().invoke(main, ["-p", "foo", "tasks", "state", tid, "blocked"])
    assert r.exit_code == 0, r.output
    assert (proj / ".project" / "tasks" / "blocked" / f"{tid}.md").exists()
    assert not (mono["foo_tasks"] / "blocked" / f"{tid}.md").exists()
    assert target.resolve() != proj.resolve()


def test_persist_refuses_when_marker_dir_does_not_end_in_prefix(
    flat, tmp_path, caplog
):
    gone = tmp_path / "gone-root"
    register_session(
        flat["root"], "s1", "T", "foo", gone, project_prefix="packages/foo"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SessionRebindError):
            persist_relocated_worktree(flat["root"], elsewhere, "T", "foo")
    assert any("packages/foo" in m for m in caplog.messages)


# (f) the old guard is gone


def test_guard_error_no_longer_exists():
    import clawpm.cli.tasks as t

    assert "monorepo_worktree_unsupported" not in Path(t.__file__).read_text(
        encoding="utf-8"
    )


# (g) root-level project behaviour is unchanged


def test_root_level_project_unchanged(flat, monkeypatch):
    tid = flat["task"].id
    target = _dispatch(flat)
    wt = flat["repo"] / ".clawpm-worktrees" / tid
    assert target.resolve() == wt.resolve()
    ev = _ledger_events(flat)[-1]
    assert "project_prefix" not in ev
    monkeypatch.chdir(wt)
    assert get_project_dir(flat["config"], "foo") == (wt / ".project").resolve()
