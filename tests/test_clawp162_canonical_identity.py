"""CLAWP-162: identity resolves must refuse on a missing or unavailable path.

Non-strict ``Path.resolve()`` swallows winerror 3 (not found) and 5 (access
denied) on Windows and returns the unresolved spelling, so the CLAWP-159/161
guards never fired. ``sessions.canonical_path`` now stats first and resolves
strictly. The failures are injected at the stat / strict-resolve seam: a real
unavailable volume cannot be made portably.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.sessions import (
    IdentityResolveError,
    canonical_identity,
    canonical_path,
)


def deny_stat(monkeypatch, target: Path, exc=None):
    """``os.stat`` raises *exc* (default PermissionError, winerror 5) for *target* only."""
    real = os.stat
    target_s = os.path.normcase(str(target))

    def flaky(p, *a, **kw):
        if os.path.normcase(os.fspath(p)) == target_s:
            raise exc or OSError(13, "Access is denied", None, 5)
        return real(p, *a, **kw)

    monkeypatch.setattr(os, "stat", flaky)


def fail_strict_resolve(monkeypatch, target: Path, exc=OSError):
    """``Path.resolve(strict=True)`` raises for *target* only (non-strict resolves are untouched)."""
    real = Path.resolve

    def flaky(self, *a, **kw):
        strict = kw.get("strict", a[0] if a else False)
        if strict and self == target:
            raise exc("simulated unavailable volume")
        return real(self, *a, **kw)

    monkeypatch.setattr(Path, "resolve", flaky)


def make_alias(tmp_path: Path, real: Path) -> Path:
    """A directory symlink (junction fallback on Windows) to *real*, or skip."""
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        if os.name != "nt":
            pytest.skip("cannot create a symlink here")
        out = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(real)],
            capture_output=True,
        )
        if out.returncode != 0:
            pytest.skip("cannot create a symlink or junction here")
    return alias


# ---------------------------------------------------------------------------
# canonical_path
# ---------------------------------------------------------------------------


class TestCanonicalPath:
    def test_existing_path_is_strictly_resolved(self, tmp_path):
        d = tmp_path / "there"
        d.mkdir()
        assert canonical_path(d) == d.resolve(strict=True)

    def test_existing_path_refuses_when_strict_resolve_fails(self, tmp_path, monkeypatch):
        d = tmp_path / "there"
        d.mkdir()
        fail_strict_resolve(monkeypatch, d)
        with pytest.raises(OSError):
            canonical_path(d)

    def test_symlink_loop_runtimeerror_becomes_oserror(self, tmp_path, monkeypatch):
        d = tmp_path / "there"
        d.mkdir()
        fail_strict_resolve(monkeypatch, d, exc=RuntimeError)
        with pytest.raises(OSError):
            canonical_path(d)

    def test_access_denied_on_stat_refuses(self, tmp_path, monkeypatch):
        d = tmp_path / "locked"
        d.mkdir()
        deny_stat(monkeypatch, d)
        with pytest.raises(OSError) as info:
            canonical_path(d)
        assert not isinstance(info.value, FileNotFoundError)

    def test_missing_target_keeps_its_identity_once_created(self, tmp_path):
        target = tmp_path / "not" / "yet" / "made"
        before = canonical_path(target)
        target.mkdir(parents=True)
        assert before == canonical_path(target) == target.resolve(strict=True)

    def test_missing_relative_target_is_anchored_at_cwd(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        before = canonical_path(Path("fresh") / "dir")
        (tmp_path / "fresh" / "dir").mkdir(parents=True)
        assert before == canonical_path(Path("fresh") / "dir")

    def test_missing_target_refuses_when_the_ancestor_is_denied(self, tmp_path, monkeypatch):
        base = tmp_path / "base"
        base.mkdir()
        deny_stat(monkeypatch, base)
        with pytest.raises(OSError):
            canonical_path(base / "child")

    def test_missing_target_refuses_when_the_ancestor_resolve_fails(self, tmp_path, monkeypatch):
        base = tmp_path / "base"
        base.mkdir()
        fail_strict_resolve(monkeypatch, base)
        with pytest.raises(OSError):
            canonical_path(base / "child")

    def test_alias_and_real_spellings_share_one_identity(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        alias = make_alias(tmp_path, real)
        assert canonical_path(alias) == canonical_path(real)
        # A not-yet-created child keys the same identity through either spelling.
        assert canonical_path(alias / "new") == canonical_path(real / "new")
        (real / "new").mkdir()
        assert canonical_path(alias / "new") == canonical_path(real / "new")

    def test_dangling_link_refuses(self, tmp_path):
        gone = tmp_path / "gone"
        gone.mkdir()
        alias = make_alias(tmp_path, gone)
        gone.rmdir()
        with pytest.raises(FileNotFoundError):
            canonical_path(alias)

    def test_dangling_link_ancestor_of_a_missing_child_refuses(self, tmp_path):
        """``alias/child`` with a dangling ``alias``: restoring the target would
        change the identity, so it must refuse rather than return the spelling."""
        gone = tmp_path / "gone"
        gone.mkdir()
        alias = make_alias(tmp_path, gone)
        gone.rmdir()
        with pytest.raises(FileNotFoundError):
            canonical_path(alias / "child")
        with pytest.raises(FileNotFoundError):
            canonical_path(alias / "child" / "grandchild")

    def test_dangling_link_ancestor_resolves_once_target_returns(self, tmp_path):
        gone = tmp_path / "gone"
        gone.mkdir()
        alias = make_alias(tmp_path, gone)
        gone.rmdir()
        with pytest.raises(FileNotFoundError):
            canonical_path(alias / "child")
        gone.mkdir()
        assert canonical_path(alias / "child") == gone.resolve(strict=True) / "child"

    @pytest.mark.skipif(os.name != "nt", reason="Win32 strips trailing dots and spaces")
    @pytest.mark.parametrize("name", ["new.", "new ", "new..", "new. "])
    def test_windows_trailing_dot_or_space_in_a_missing_component_refuses(
        self, tmp_path, name
    ):
        with pytest.raises(FileNotFoundError, match="trailing"):
            canonical_path(tmp_path / name)
        with pytest.raises(FileNotFoundError, match="trailing"):
            canonical_path(tmp_path / name / "child")

    @pytest.mark.skipif(os.name != "nt", reason="Win32 strips trailing dots and spaces")
    def test_windows_trailing_dot_on_an_existing_component_is_resolved(self, tmp_path):
        (tmp_path / "there").mkdir()
        assert canonical_path(tmp_path / "there.") == (tmp_path / "there").resolve(
            strict=True
        )

    def test_dotdot_under_a_missing_directory_never_guesses(self, tmp_path):
        """POSIX cannot step through a missing directory: refuse. Windows
        normalises ``..`` lexically, so the stat sees the real parent."""
        odd = tmp_path / "missing" / ".." / "x"
        if os.name == "nt":
            assert canonical_path(odd) == tmp_path.resolve(strict=True) / "x"
        else:
            with pytest.raises(FileNotFoundError):
                canonical_path(odd)

    def test_known_canonical_is_not_touched(self, tmp_path, monkeypatch):
        d = tmp_path / "vouched"
        d.mkdir()
        deny_stat(monkeypatch, d)
        assert canonical_path(d, known_canonical=True) == d

    def test_canonical_identity_wraps_as_a_valueerror(self, tmp_path, monkeypatch):
        d = tmp_path / "there"
        d.mkdir()
        deny_stat(monkeypatch, d)
        with pytest.raises(IdentityResolveError, match="a lock"):
            canonical_identity(d, "a lock")
        assert issubclass(IdentityResolveError, ValueError)


# ---------------------------------------------------------------------------
# identity sites refuse (loud, clean) when the resolve fails persistently
# ---------------------------------------------------------------------------


def _task(ctx, title="T"):
    from clawpm.tasks import add_task

    return add_task(ctx.config, ctx.project_id, title)


def test_state_change_refuses_when_the_task_file_cannot_be_resolved(
    isolated_portfolio, monkeypatch
):
    ctx = isolated_portfolio
    task = _task(ctx)
    fail_strict_resolve(monkeypatch, task.file_path)
    result = CliRunner().invoke(main, ["-p", "test", "tasks", "state", task.id, "progress"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Could not resolve" in result.output, result.output
    # Nothing moved.
    assert task.file_path.exists()


def test_state_change_refuses_when_a_parent_task_dir_cannot_be_resolved(
    isolated_portfolio, monkeypatch
):
    from clawpm.tasks import add_subtask, get_task

    ctx = isolated_portfolio
    parent = _task(ctx, "Parent")
    add_subtask(ctx.config, ctx.project_id, parent.id, "Child")
    parent = get_task(ctx.config, ctx.project_id, parent.id)
    assert parent.file_path.name == "_task.md"
    fail_strict_resolve(monkeypatch, parent.file_path.parent)
    result = CliRunner().invoke(main, ["-p", "test", "tasks", "state", parent.id, "progress"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Could not resolve" in result.output, result.output


def test_done_precheck_refuses_when_the_destination_cannot_be_resolved(
    isolated_portfolio, monkeypatch
):
    ctx = isolated_portfolio
    task = _task(ctx)
    fail_strict_resolve(monkeypatch, task.file_path)
    result = CliRunner().invoke(main, ["-p", "test", "tasks", "state", task.id, "done"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Could not resolve" in result.output, result.output
    assert task.file_path.exists()


def test_task_add_refuses_when_the_portfolio_root_cannot_be_resolved(
    isolated_portfolio, monkeypatch
):
    ctx = isolated_portfolio
    fail_strict_resolve(monkeypatch, ctx.config.portfolio_root)
    result = CliRunner().invoke(main, ["-p", "test", "tasks", "add", "-t", "Blocked"])
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Could not resolve" in result.output, result.output
    assert not list(ctx.tasks_dir.rglob("*.md"))


def test_research_add_refuses_when_the_research_dir_cannot_be_resolved(
    isolated_portfolio, monkeypatch
):
    from clawpm.research import get_research_dir

    ctx = isolated_portfolio
    research_dir = get_research_dir(ctx.config, ctx.project_id)
    research_dir.mkdir(parents=True, exist_ok=True)
    fail_strict_resolve(monkeypatch, research_dir)
    result = CliRunner().invoke(
        main,
        ["-p", "test", "research", "add", "-t", "spike", "--title", "X", "--summary", "s"],
    )
    assert result.exit_code != 0, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "research_dir_unresolvable" in result.output, result.output
    assert not list(research_dir.glob("*.md"))


def test_session_lookup_skips_a_candidate_whose_worktree_cannot_be_resolved(
    tmp_path, monkeypatch, caplog
):
    import logging

    from clawpm.sessions import find_session_for_cwd, register_session

    root = tmp_path / "portfolio"
    root.mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    register_session(root, "s1", "T", "foo", wt)
    assert find_session_for_cwd(root, wt, project_id="foo") is not None
    fail_strict_resolve(monkeypatch, wt.resolve())
    with caplog.at_level(logging.ERROR):
        assert find_session_for_cwd(root, wt, project_id="foo") is None
    assert any("Failed to resolve candidate session worktree" in m for m in caplog.messages)


def test_land_refuses_when_git_dirs_cannot_be_resolved(tmp_path, monkeypatch):
    from clawpm import land

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    git_dir = (repo / ".git").resolve()
    fail_strict_resolve(monkeypatch, git_dir)
    with pytest.raises(land.LandError) as info:
        land.land_task(None, "test", "T-1", 1, cwd=repo)
    assert info.value.code == "path_unresolvable"
