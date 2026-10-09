"""CLAWP-117: worktree identity (relocation) + dispatch source-repo scoping.

Split out of PR #55 (CLAWP-098). Design: RECORDED IDENTITY, no path rewrite.

* A dispatched worktree that was ``git worktree move``d keeps its dispatch
  marker (``.claude/settings.local.json``), which travels with the checkout.
  The ledger still records the OLD path, so ``_live_sessions`` drops the
  session and resolution used to fall through to the main checkout. Now the
  marker alone identifies the worktree when its (task, project) has an active
  ledger session whose recorded path is no longer a live directory. Nothing
  is written on this read path.
* Dispatch computes the source repo once and hands the same value to the HEAD
  probe and ``create_worktree``; a ``clawpm/<task>`` branch already checked out
  elsewhere fails closed with an actionable message naming that path.
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
from clawpm.sessions import (
    SESSION_REGISTRY_FILENAME,
    find_session_for_cwd,
    register_session,
)
from clawpm.tasks import add_task


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )


@pytest.fixture
def fx(tmp_path, monkeypatch):
    """Portfolio with project 'test' in a real git repo; one COMMITTED task."""
    root = tmp_path / "portfolio"
    root.mkdir()
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("hi", encoding="utf-8")
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
    _git(repo, "add", "README.md", ".project")
    _git(repo, "commit", "-q", "-m", "init")
    yield {
        "root": root, "repo": repo, "config": config, "tasks": tasks, "task": task,
    }
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "prune"],
        check=False, capture_output=True,
    )


def _dispatch(fx, *extra):
    return CliRunner().invoke(
        main,
        ["-p", "test", "tasks", "dispatch", fx["task"].id, "--worktree", *extra],
    )


def _fake_worktree(fx, tmp_path, *, name, project="test", marker=True):
    wt = tmp_path / name
    (wt / ".project" / "tasks").mkdir(parents=True)
    (wt / ".project" / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    if marker:
        (wt / ".claude").mkdir()
        (wt / ".claude" / "settings.local.json").write_text(
            json.dumps({CLAWPM_MARKER_KEY: {
                "task_id": fx["task"].id, "project_id": project, "version": 1,
            }}),
            encoding="utf-8",
        )
    return wt


# ---------------------------------------------------------------------------
# (a) relocation: marker identifies the moved worktree
# ---------------------------------------------------------------------------


def test_moved_worktree_resolves_to_itself_not_main(fx, tmp_path, monkeypatch):
    tid = fx["task"].id
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / "relocated"
    _git(fx["repo"], "worktree", "move", str(old), str(moved))
    assert not old.exists() and moved.exists()

    monkeypatch.chdir(moved)
    ledger_before = (fx["root"] / SESSION_REGISTRY_FILENAME).read_bytes()
    got = get_project_dir(fx["config"], "test")
    assert got == (moved / ".project").resolve()

    r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "state", tid, "blocked"])
    assert r2.exit_code == 0, r2.output
    assert (moved / ".project" / "tasks" / "blocked" / f"{tid}.md").exists()
    # The main checkout copy is untouched.
    assert (fx["tasks"] / f"{tid}.md").exists()
    assert not (fx["tasks"] / "blocked" / f"{tid}.md").exists()
    # Read path writes nothing (the source of PR #55's findings).
    assert (fx["root"] / SESSION_REGISTRY_FILENAME).read_bytes() == ledger_before


def test_moved_worktree_subdirectory_also_resolves(fx, tmp_path, monkeypatch):
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / "relocated"
    _git(fx["repo"], "worktree", "move", str(old), str(moved))
    monkeypatch.chdir(moved / ".project")
    assert get_project_dir(fx["config"], "test") == (moved / ".project").resolve()


# ---------------------------------------------------------------------------
# (b) a record whose dir is still live is never rebound
# ---------------------------------------------------------------------------


def test_record_with_live_dir_is_not_rebound(fx, tmp_path, monkeypatch):
    live = _fake_worktree(fx, tmp_path, name="live", marker=False)
    other = _fake_worktree(fx, tmp_path, name="other")  # same task marker
    register_session(fx["root"], "s-live", fx["task"].id, "test", live)
    monkeypatch.chdir(other)
    assert find_session_for_cwd(fx["root"], other, "test") is None
    assert get_project_dir(fx["config"], "test") == fx["tasks"].parent


def test_live_record_blocks_rebind_even_beside_a_stale_one(fx, tmp_path, monkeypatch):
    live = _fake_worktree(fx, tmp_path, name="live", marker=False)
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s-gone", fx["task"].id, "test", tmp_path / "gone")
    register_session(fx["root"], "s-live", fx["task"].id, "test", live)
    assert find_session_for_cwd(fx["root"], other, "test") is None


def test_stale_record_rebinds_to_marker_worktree(fx, tmp_path):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s-gone", fx["task"].id, "test", tmp_path / "gone")
    got = find_session_for_cwd(fx["root"], other / ".project", "test")
    assert got is not None
    assert got.task_id == fx["task"].id and got.project_id == "test"
    assert Path(got.worktree_path).resolve() == other.resolve()


def test_ambiguous_stale_records_resolve_by_marker(fx, tmp_path):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone1")
    register_session(fx["root"], "s2", fx["task"].id, "test", tmp_path / "gone2")
    got = find_session_for_cwd(fx["root"], other, "test")
    assert got is not None
    assert Path(got.worktree_path).resolve() == other.resolve()


def test_released_stale_record_does_not_rebind(fx, tmp_path):
    from clawpm.sessions import release_session

    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    release_session(fx["root"], "s1")
    assert find_session_for_cwd(fx["root"], other, "test") is None


def test_marker_for_a_task_with_no_session_does_not_rebind(fx, tmp_path):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", "SOME-OTHER-TASK", "test", tmp_path / "gone")
    assert find_session_for_cwd(fx["root"], other, "test") is None


# ---------------------------------------------------------------------------
# (c) cross-project isolation
# ---------------------------------------------------------------------------


def test_marker_for_a_different_project_is_ignored(fx, tmp_path, monkeypatch):
    other = _fake_worktree(fx, tmp_path, name="other", project="other-project")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    monkeypatch.chdir(other)
    assert find_session_for_cwd(fx["root"], other, "test") is None
    assert get_project_dir(fx["config"], "test") == fx["tasks"].parent


def test_stale_record_of_a_different_project_does_not_rebind(fx, tmp_path):
    other = _fake_worktree(fx, tmp_path, name="other")  # marker says 'test'
    register_session(
        fx["root"], "s1", fx["task"].id, "other-project", tmp_path / "gone"
    )
    assert find_session_for_cwd(fx["root"], other, "test") is None


# ---------------------------------------------------------------------------
# (f) never raises on a damaged marker
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    ["{not json", "[1, 2]", '{"%s": 42}' % CLAWPM_MARKER_KEY,
     '{"%s": {"task_id": 7, "project_id": "test"}}' % CLAWPM_MARKER_KEY],
)
def test_damaged_marker_never_raises(fx, tmp_path, monkeypatch, caplog, payload):
    other = _fake_worktree(fx, tmp_path, name="other")
    (other / ".claude" / "settings.local.json").write_text(payload, encoding="utf-8")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    monkeypatch.chdir(other)
    with caplog.at_level(logging.WARNING):
        got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent


def test_marker_read_oserror_never_raises(fx, tmp_path, monkeypatch, caplog):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    import clawpm.dispatch as dispatch_mod

    def boom(_p):
        raise PermissionError("simulated")

    monkeypatch.setattr(dispatch_mod, "inspect_dispatch_marker", boom)
    monkeypatch.chdir(other)
    with caplog.at_level(logging.ERROR):
        got = get_project_dir(fx["config"], "test")
    assert got == fx["tasks"].parent
    assert "Failed to read dispatch marker" in caplog.text


# ---------------------------------------------------------------------------
# (d) probe and create_worktree resolve from the SAME checkout
# ---------------------------------------------------------------------------


def test_probe_and_create_worktree_share_one_repo(fx, monkeypatch):
    import clawpm.dispatch as dispatch_mod

    seen: dict[str, list] = {"probe": [], "prefix": [], "create": []}
    real_head, real_prefix, real_create = (
        dispatch_mod.head_object_sha, dispatch_mod.repo_prefix,
        dispatch_mod.create_worktree,
    )

    def head(repo, rel):
        seen["probe"].append(Path(repo))
        return real_head(repo, rel)

    def prefix(repo):
        seen["prefix"].append(Path(repo))
        return real_prefix(repo)

    def create(repo, tid):
        seen["create"].append(Path(repo))
        return real_create(repo, tid)

    monkeypatch.setattr(dispatch_mod, "head_object_sha", head)
    monkeypatch.setattr(dispatch_mod, "repo_prefix", prefix)
    monkeypatch.setattr(dispatch_mod, "create_worktree", create)
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    assert seen["probe"] and seen["create"] and seen["prefix"]
    repos = {str(p) for p in seen["probe"] + seen["prefix"] + seen["create"]}
    assert len(repos) == 1, repos
    assert repos == {str(fx["repo"])}


# ---------------------------------------------------------------------------
# (e) re-dispatch with the branch checked out elsewhere
# ---------------------------------------------------------------------------


def test_redispatch_with_branch_checked_out_elsewhere_fails_actionably(
    fx, tmp_path
):
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / "relocated"
    _git(fx["repo"], "worktree", "move", str(old), str(moved))

    r2 = _dispatch(fx, "--force")
    assert r2.exit_code == 1, r2.output
    assert "branch_checked_out_elsewhere" in r2.output
    assert str(moved.name) in r2.output  # names where the branch lives
    # Nothing was silently created at the canonical location.
    assert not old.exists()


def test_redispatch_into_the_same_worktree_still_works(fx):
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    r2 = _dispatch(fx, "--force")
    assert r2.exit_code == 0, r2.output


# ---------------------------------------------------------------------------
# (g) round 2: teardown persists the moved path before removing the marker
# ---------------------------------------------------------------------------


def _moved_dispatch(fx, tmp_path, name="relocated"):
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / name
    _git(fx["repo"], "worktree", "move", str(old), str(moved))
    return old, moved


def _teardown(fx, target):
    from clawpm.dispatch import teardown_dispatch_settings

    return teardown_dispatch_settings(
        target, task_id=fx["task"].id, portfolio_root=fx["root"], project_id="test"
    )


def test_moved_worktree_still_resolves_after_teardown(fx, tmp_path, monkeypatch):
    tid = fx["task"].id
    _old, moved = _moved_dispatch(fx, tmp_path)
    monkeypatch.chdir(moved)
    assert _teardown(fx, moved) is True
    assert not (moved / ".claude" / "settings.local.json").exists()
    got = find_session_for_cwd(fx["root"], moved, "test")
    assert got is not None and Path(got.worktree_path).resolve() == moved.resolve()
    assert get_project_dir(fx["config"], "test") == (moved / ".project").resolve()
    r = CliRunner().invoke(main, ["-p", "test", "tasks", "state", tid, "blocked"])
    assert r.exit_code == 0, r.output
    assert (moved / ".project" / "tasks" / "blocked" / f"{tid}.md").exists()
    assert not (fx["tasks"] / "blocked" / f"{tid}.md").exists()


def test_teardown_moves_every_stale_record(fx, tmp_path):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone1")
    register_session(fx["root"], "s2", fx["task"].id, "test", tmp_path / "gone1")
    register_session(fx["root"], "s3", fx["task"].id, "test", tmp_path / "gone2")
    assert _teardown(fx, other) is True
    from clawpm.sessions import _replay

    recs = {s.session_id: s for s in _replay(fx["root"]).values()}
    assert {Path(r.worktree_path).resolve() for r in recs.values()} == {other.resolve()}
    assert all(r.active for r in recs.values())


def test_teardown_does_not_rebind_while_a_recorded_path_is_live(fx, tmp_path):
    live = _fake_worktree(fx, tmp_path, name="live", marker=False)
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s-gone", fx["task"].id, "test", tmp_path / "gone")
    register_session(fx["root"], "s-live", fx["task"].id, "test", live)
    before = (fx["root"] / SESSION_REGISTRY_FILENAME).read_bytes()
    assert _teardown(fx, other) is True
    assert (fx["root"] / SESSION_REGISTRY_FILENAME).read_bytes() == before


def test_teardown_failed_ledger_update_keeps_marker_and_errors(
    fx, tmp_path, caplog
):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    import clawpm.sessions as sessions_mod

    def boom(*_a, **_k):
        raise PermissionError("simulated ledger write failure")

    orig = sessions_mod.append_jsonl_line
    sessions_mod.append_jsonl_line = boom
    try:
        with caplog.at_level(logging.ERROR):
            with pytest.raises(OSError):
                _teardown(fx, other)
    finally:
        sessions_mod.append_jsonl_line = orig
    assert (other / ".claude" / "settings.local.json").exists()
    assert "ERROR" in {r.levelname for r in caplog.records}


def test_teardown_cli_reports_failed_ledger_update(fx, tmp_path, monkeypatch):
    other = _fake_worktree(fx, tmp_path, name="other")
    register_session(fx["root"], "s1", fx["task"].id, "test", tmp_path / "gone")
    import clawpm.sessions as sessions_mod

    def boom(*_a, **_k):
        raise PermissionError("simulated")

    monkeypatch.setattr(sessions_mod, "append_jsonl_line", boom)
    r = CliRunner().invoke(
        main,
        ["-p", "test", "tasks", "teardown-dispatch", fx["task"].id,
         "--target-dir", str(other)],
    )
    assert r.exit_code == 1, r.output
    assert (other / ".claude" / "settings.local.json").exists()


# ---------------------------------------------------------------------------
# (h) round 2: worktree list parsed with -z (no quoted/escaped paths)
# ---------------------------------------------------------------------------


def test_worktree_path_for_branch_handles_spaces_and_quoted_chars(fx, tmp_path):
    from clawpm.dispatch import worktree_path_for_branch

    old, _ = None, None
    r = _dispatch(fx)
    assert r.exit_code == 0, r.output
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / "re loc\u00e9 \u00fcmlaut"
    _git(fx["repo"], "worktree", "move", str(old), str(moved))
    got = worktree_path_for_branch(fx["repo"], f"clawpm/{fx['task'].id}")
    assert got is not None
    assert Path(got).resolve() == moved.resolve()


def test_redispatch_error_names_literal_path_with_special_chars(fx, tmp_path):
    r = _dispatch(fx)
    old = Path(json.loads(r.output)["data"]["target_dir"])
    moved = tmp_path / "sp ace \u00e9"
    _git(fx["repo"], "worktree", "move", str(old), str(moved))
    r2 = _dispatch(fx, "--force")
    assert r2.exit_code == 1, r2.output
    assert "branch_checked_out_elsewhere" in r2.output
    assert "sp ace \u00e9" in json.loads(r2.output)["message"]


def test_worktree_list_is_read_nul_delimited(tmp_path, monkeypatch):
    """-z output: literal paths (newline, quote, space) with no git quoting,
    bare / detached / prunable records, records split by an empty field."""
    import clawpm.dispatch as dispatch_mod

    weird = 'C:/w t/we"ird\nname'
    out = (
        "worktree C:/main\0HEAD aaa\0branch refs/heads/main\0\0"
        "worktree C:/bare\0bare\0\0"
        "worktree C:/det\0HEAD bbb\0detached\0\0"
        f"worktree {weird}\0HEAD ccc\0branch refs/heads/clawpm/X-1\0"
        "prunable gitdir file points to non-existent location\0\0"
    )
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(dispatch_mod.subprocess, "run", fake_run)
    got = dispatch_mod.worktree_path_for_branch(tmp_path, "clawpm/X-1")
    assert "-z" in seen["cmd"]
    assert str(got) == str(Path(weird))
    assert dispatch_mod.worktree_path_for_branch(tmp_path, "main") == Path("C:/main")
    assert dispatch_mod.worktree_path_for_branch(tmp_path, "nope") is None
