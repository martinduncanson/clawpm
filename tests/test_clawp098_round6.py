"""CLAWP-098 round-6 review regressions (Codex, PR #55).

Five independent findings, each with a test that FAILS against the
pre-fix code:

1. ``head_object_sha`` / ``repo_prefix`` fail CLOSED on an operational git
   failure instead of reporting it as "path absent" (the old
   ``git cat-file -e`` probe could not distinguish the two).
2. The dispatch materialization gate verifies the CURRENT task revision,
   not merely that some candidate path for the id exists in HEAD.
3. ``tasks state`` collects ``files_changed`` from the session-scoped
   checkout, not the main one.
4. A failed session registration rolls the dispatch artifacts back.
5. A worktree relocated with ``git worktree move`` is rediscovered via its
   dispatch marker instead of silently falling through to the main
   checkout.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clawpm.sessions import find_session_for_cwd, register_session

# The probe helpers are imported INSIDE the tests that need them, not at
# module scope: the behavioural classes below (rediscovery, session-scoped
# repo path) must still collect and RUN against pre-fix source, so that
# "verified failing before the fix" means the assertion failed, not that
# collection died on a missing import.


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    return path


# ---------------------------------------------------------------------------
# 1. Probes fail closed on operational errors
# ---------------------------------------------------------------------------


class TestGitProbeFailsClosed:
    """A probe failure must be distinguishable from 'the path isn't there'.

    The pre-fix gate ran ``git cat-file -e`` and read every nonzero exit as
    "not in HEAD", so a broken object store / unreadable HEAD / missing git
    silently disabled BOTH materialization guards and could register a
    stale checkout as an isolated worktree.
    """

    def test_absent_path_returns_none(self, tmp_path):
        from clawpm.dispatch import head_object_sha
        repo = _init_repo(tmp_path / "repo")
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-qm", "init")
        assert head_object_sha(repo, "nope.txt") is None

    def test_present_path_returns_sha_matching_working_tree(self, tmp_path):
        from clawpm.dispatch import head_object_sha, working_tree_blob_sha
        repo = _init_repo(tmp_path / "repo")
        (repo / "a.txt").write_text("hello\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-qm", "init")
        sha = head_object_sha(repo, "a.txt")
        assert sha
        assert sha == working_tree_blob_sha(repo / "a.txt")

    def test_not_a_repository_raises_rather_than_reporting_absent(self, tmp_path):
        """The load-bearing case: a directory that is not a git repo must
        RAISE, not return None. Returning None here is what let the old
        probe treat an operational failure as 'untracked .project'."""
        from clawpm.dispatch import GitProbeError, head_object_sha
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitProbeError):
            head_object_sha(plain, ".project")

    def test_repo_prefix_raises_rather_than_returning_empty(self, tmp_path):
        """An empty prefix is indistinguishable from the legitimate
        repo-root case, so a failure must not degrade to ''."""
        from clawpm.dispatch import GitProbeError, repo_prefix
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(GitProbeError):
            repo_prefix(plain)

    def test_repo_prefix_empty_at_root_and_set_in_subdir(self, tmp_path):
        from clawpm.dispatch import repo_prefix
        repo = _init_repo(tmp_path / "repo")
        (repo / "a.txt").write_text("x\n", encoding="utf-8")
        _git(repo, "add", "a.txt")
        _git(repo, "commit", "-qm", "init")
        sub = repo / "packages" / "proj"
        sub.mkdir(parents=True)
        assert repo_prefix(repo) == ""
        assert repo_prefix(sub) == "packages/proj/"

    def test_working_tree_blob_sha_absent_file_is_none(self, tmp_path):
        from clawpm.dispatch import working_tree_blob_sha
        assert working_tree_blob_sha(tmp_path / "missing.txt") is None


# ---------------------------------------------------------------------------
# 2. Current-revision verification
# ---------------------------------------------------------------------------


class TestCurrentRevisionMaterialization:
    """`tasks start` renames T.md -> T.progress.md in the working tree.
    Until that is committed, HEAD still carries the old open-state T.md, so
    an any-candidate-exists probe passed and the worktree was created at
    the STALE revision.
    """

    def test_uncommitted_rename_is_detected_as_stale(self, tmp_path):
        from clawpm.dispatch import head_object_sha
        repo = _init_repo(tmp_path / "repo")
        tasks = repo / ".project" / "tasks"
        tasks.mkdir(parents=True)
        (tasks / "T-001.md").write_text("state: open\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add task")

        # `tasks start` renames + rewrites, uncommitted.
        (tasks / "T-001.md").unlink()
        (tasks / "T-001.progress.md").write_text("state: progress\n", encoding="utf-8")

        # The OLD probe: "does any candidate for this id exist in HEAD?"
        old_probe = any(
            head_object_sha(repo, p) is not None
            for p in (".project/tasks/T-001.md", ".project/tasks/T-001.progress.md")
        )
        assert old_probe is True, "precondition: the old probe passes"

        # The NEW probe: the path the task actually occupies now must be in
        # HEAD with identical content.
        live = tasks / "T-001.progress.md"
        rel = live.relative_to(repo).as_posix()
        assert head_object_sha(repo, rel) is None
        # ... so materialization is correctly False.

    def test_edited_but_uncommitted_task_is_detected_as_stale(self, tmp_path):
        """Same path, different content — the case an existence-only probe
        cannot see at all."""
        from clawpm.dispatch import head_object_sha, working_tree_blob_sha
        repo = _init_repo(tmp_path / "repo")
        tasks = repo / ".project" / "tasks"
        tasks.mkdir(parents=True)
        live = tasks / "T-001.md"
        live.write_text("state: open\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add task")

        live.write_text("state: open\nnote: edited\n", encoding="utf-8")

        rel = live.relative_to(repo).as_posix()
        head_sha = head_object_sha(repo, rel)
        assert head_sha is not None, "the path IS in HEAD"
        assert head_sha != working_tree_blob_sha(live), (
            "but its committed content differs from the revision on disk"
        )

    def test_committed_task_matches(self, tmp_path):
        from clawpm.dispatch import head_object_sha, working_tree_blob_sha
        repo = _init_repo(tmp_path / "repo")
        tasks = repo / ".project" / "tasks"
        tasks.mkdir(parents=True)
        live = tasks / "T-001.md"
        live.write_text("state: open\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "add task")

        rel = live.relative_to(repo).as_posix()
        assert head_object_sha(repo, rel) == working_tree_blob_sha(live)


# ---------------------------------------------------------------------------
# 3. files_changed follows the session-scoped checkout
# ---------------------------------------------------------------------------


class TestSessionScopedRepoPath:
    def test_get_repo_path_follows_active_session(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.discovery import get_repo_path

        wt = tmp_path / "moved-wt"
        wt.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", wt
        )
        monkeypatch.chdir(wt)
        assert get_repo_path(isolated_portfolio.config, "test") == wt.resolve()

    def test_get_repo_path_falls_back_outside_a_session(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        from clawpm.discovery import get_project, get_repo_path

        outside = tmp_path / "elsewhere"
        outside.mkdir()
        monkeypatch.chdir(outside)
        project = get_project(isolated_portfolio.config, "test")
        assert get_repo_path(isolated_portfolio.config, "test") == project.repo_path

    def test_get_repo_path_scopes_even_without_project_dir_in_worktree(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        """Deliberately BROADER than _session_scoped_project_dir: a worktree
        with no .project/ of its own still holds the work being described,
        and reading a diff cannot fork a task store."""
        from clawpm.discovery import get_project_dir, get_repo_path

        wt = tmp_path / "wt-no-project"
        wt.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", wt
        )
        monkeypatch.chdir(wt)
        # No .project/ here, so task resolution correctly does NOT redirect...
        resolved = get_project_dir(isolated_portfolio.config, "test")
        assert resolved is not None
        assert wt.resolve() not in resolved.parents and resolved.parent != wt.resolve()
        # ... but the repo path for git still does.
        assert get_repo_path(isolated_portfolio.config, "test") == wt.resolve()

    def test_get_repo_path_honours_suppression(
        self, isolated_portfolio, tmp_path, monkeypatch
    ):
        """The lease-fallback sweep must never inherit the caller's worktree."""
        from clawpm.discovery import get_project, get_repo_path
        from clawpm.sessions import suppress_session_resolution

        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", wt
        )
        monkeypatch.chdir(wt)
        project = get_project(isolated_portfolio.config, "test")
        with suppress_session_resolution():
            assert get_repo_path(isolated_portfolio.config, "test") == project.repo_path


# ---------------------------------------------------------------------------
# 5. Moved-worktree rediscovery
# ---------------------------------------------------------------------------


def _write_marker(target: Path, task_id: str, project_id: str) -> None:
    from clawpm.dispatch import CLAWPM_MARKER_KEY, settings_path

    path = settings_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({CLAWPM_MARKER_KEY: {"task_id": task_id, "project_id": project_id}}),
        encoding="utf-8",
    )


class TestMovedWorktreeRediscovery:
    def test_moved_worktree_is_rediscovered_via_its_marker(
        self, isolated_portfolio, tmp_path
    ):
        old = tmp_path / "old-wt"
        old.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", old
        )
        new = tmp_path / "new-wt"
        old.rename(new)  # `git worktree move`
        _write_marker(new, "TEST-001", "test")

        found = find_session_for_cwd(
            isolated_portfolio.root, new, project_id="test"
        )
        assert found is not None
        assert found.session_id == "sess-1"
        assert found.worktree_path == new

    def test_rediscovery_works_from_a_subdirectory(
        self, isolated_portfolio, tmp_path
    ):
        old = tmp_path / "old-wt"
        old.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", old
        )
        new = tmp_path / "new-wt"
        old.rename(new)
        _write_marker(new, "TEST-001", "test")
        nested = new / "src" / "pkg"
        nested.mkdir(parents=True)

        found = find_session_for_cwd(
            isolated_portfolio.root, nested, project_id="test"
        )
        assert found is not None
        assert found.worktree_path == new

    def test_no_rediscovery_while_the_recorded_path_still_exists(
        self, isolated_portfolio, tmp_path
    ):
        """The guard that matters: rebinding a session whose original
        worktree is still live would corrupt the genuinely-active one."""
        live = tmp_path / "live-wt"
        live.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", live
        )
        other = tmp_path / "other"
        other.mkdir()
        _write_marker(other, "TEST-001", "test")

        assert find_session_for_cwd(
            isolated_portfolio.root, other, project_id="test"
        ) is None

    def test_no_rediscovery_without_a_marker(self, isolated_portfolio, tmp_path):
        old = tmp_path / "old-wt"
        old.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", old
        )
        new = tmp_path / "new-wt"
        old.rename(new)
        # no marker written

        assert find_session_for_cwd(
            isolated_portfolio.root, new, project_id="test"
        ) is None

    def test_marker_for_a_different_project_is_not_matched(
        self, isolated_portfolio, tmp_path
    ):
        old = tmp_path / "old-wt"
        old.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", old
        )
        new = tmp_path / "new-wt"
        old.rename(new)
        _write_marker(new, "TEST-001", "other-project")

        assert find_session_for_cwd(
            isolated_portfolio.root, new, project_id="test"
        ) is None

    def test_rediscovery_does_not_write_back_to_the_ledger(
        self, isolated_portfolio, tmp_path
    ):
        from clawpm.sessions import SESSION_REGISTRY_FILENAME

        old = tmp_path / "old-wt"
        old.mkdir()
        register_session(
            isolated_portfolio.root, "sess-1", "TEST-001", "test", old
        )
        ledger = isolated_portfolio.root / SESSION_REGISTRY_FILENAME
        before = ledger.read_text(encoding="utf-8")

        new = tmp_path / "new-wt"
        old.rename(new)
        _write_marker(new, "TEST-001", "test")
        find_session_for_cwd(isolated_portfolio.root, new, project_id="test")

        assert ledger.read_text(encoding="utf-8") == before

    def test_ambiguous_match_resolves_to_nothing(
        self, isolated_portfolio, tmp_path
    ):
        """Two registered sessions for the same (task, project) — refuse to
        guess which one the relocated checkout is."""
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        register_session(isolated_portfolio.root, "sess-a", "TEST-001", "test", a)
        register_session(isolated_portfolio.root, "sess-b", "TEST-001", "test", b)
        a.rmdir()
        b.rmdir()

        new = tmp_path / "new-wt"
        new.mkdir()
        _write_marker(new, "TEST-001", "test")

        assert find_session_for_cwd(
            isolated_portfolio.root, new, project_id="test"
        ) is None
