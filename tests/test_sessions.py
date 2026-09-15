"""Unit tests for the CLAWP-098 session ledger (sessions.py).

These exercise sessions.py in isolation (no git worktree needed — a plain
directory stands in for a worktree path). End-to-end CLI coverage through
`tasks dispatch --worktree` lives in test_dispatch.py's
`TestWorktreeSessionScopedMutation`.
"""

from __future__ import annotations

import pytest

from clawpm.sessions import (
    SESSION_REGISTRY_FILENAME,
    active_sessions,
    find_session_for_cwd,
    register_session,
    release_session,
    release_sessions_for_task,
)


# ---------------------------------------------------------------------------
# Register / release round trip
# ---------------------------------------------------------------------------


class TestDirectoryExistenceLiveness:
    """CLAWP-098 review (Codex, PR #55): a session's liveness tracks whether
    its worktree_path still exists on disk, NOT an explicit `released`
    event — see the module docstring's "SESSION LIFETIME" section for why
    release-on-dispatch-teardown was reverted (it broke bulk multi-task
    `tasks state A B done` runs from inside a worktree)."""

    def test_active_sessions_excludes_removed_worktree(
        self, isolated_portfolio, tmp_path
    ):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        assert len(active_sessions(isolated_portfolio.root)) == 1

        wt.rmdir()  # simulates `git worktree remove`
        assert active_sessions(isolated_portfolio.root) == []

    def test_find_session_for_cwd_also_excludes_removed_worktree(
        self, isolated_portfolio, tmp_path
    ):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        wt.rmdir()
        assert find_session_for_cwd(isolated_portfolio.root, wt, project_id="test") is None

    def test_never_explicitly_released_but_worktree_intact_stays_active(
        self, isolated_portfolio, tmp_path
    ):
        """A crashed/never-torn-down dispatch (no `released` event) whose
        worktree is still there stays active and resolvable -- the accepted
        'inert leftover, not corruption' tradeoff documented in the module
        docstring, matching dispatches.jsonl's own no-reaping precedent."""
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        sessions = active_sessions(isolated_portfolio.root)
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-1"


class TestRegisterReleaseRoundTrip:
    def test_register_makes_session_active(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)

        sessions = active_sessions(isolated_portfolio.root)
        assert len(sessions) == 1
        assert sessions[0].session_id == "sess-1"
        assert sessions[0].task_id == "TEST-001"
        assert sessions[0].project_id == "test"
        assert sessions[0].worktree_path == wt.resolve()
        assert sessions[0].active is True

    def test_release_retires_the_session(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        release_session(isolated_portfolio.root, "sess-1")
        assert active_sessions(isolated_portfolio.root) == []

    def test_release_is_idempotent(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        release_session(isolated_portfolio.root, "sess-1")
        release_session(isolated_portfolio.root, "sess-1")  # must not raise
        assert active_sessions(isolated_portfolio.root) == []

    def test_release_unknown_session_is_a_safe_noop(self, isolated_portfolio):
        release_session(isolated_portfolio.root, "never-registered")  # must not raise
        assert active_sessions(isolated_portfolio.root) == []

    def test_release_sessions_for_task_returns_count_and_is_scoped(
        self, isolated_portfolio, tmp_path
    ):
        wt1 = tmp_path / "wt1"
        wt1.mkdir()
        wt2 = tmp_path / "wt2"
        wt2.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt1)
        register_session(isolated_portfolio.root, "sess-2", "TEST-002", "test", wt2)

        released = release_sessions_for_task(isolated_portfolio.root, "TEST-001", "test")
        assert released == 1

        remaining = active_sessions(isolated_portfolio.root)
        assert len(remaining) == 1
        assert remaining[0].task_id == "TEST-002"

    def test_release_sessions_for_task_no_match_is_a_safe_noop(self, isolated_portfolio):
        assert release_sessions_for_task(isolated_portfolio.root, "NOPE-001", "test") == 0

    def test_release_sessions_for_task_does_not_touch_a_different_project(
        self, isolated_portfolio, tmp_path
    ):
        """Same task_id, different project_id — must not cross-release
        (mirrors the cross-project isolation the dispatch registry already
        enforces for dispatches.jsonl)."""
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "projA", wt)
        released = release_sessions_for_task(isolated_portfolio.root, "TEST-001", "projB")
        assert released == 0
        assert len(active_sessions(isolated_portfolio.root)) == 1


# ---------------------------------------------------------------------------
# find_session_for_cwd
# ---------------------------------------------------------------------------


class TestFindSessionForCwd:
    def test_matches_worktree_root(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        found = find_session_for_cwd(isolated_portfolio.root, wt, project_id="test")
        assert found is not None
        assert found.session_id == "sess-1"

    def test_matches_subdirectory_of_worktree(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        sub = wt / "src" / "pkg"
        sub.mkdir(parents=True)
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        found = find_session_for_cwd(isolated_portfolio.root, sub, project_id="test")
        assert found is not None
        assert found.session_id == "sess-1"

    def test_no_match_outside_any_worktree(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        assert find_session_for_cwd(isolated_portfolio.root, elsewhere, project_id="test") is None

    def test_no_registry_file_returns_none(self, isolated_portfolio, tmp_path):
        assert find_session_for_cwd(isolated_portfolio.root, tmp_path, project_id="test") is None

    def test_project_id_cross_match_guard(self, isolated_portfolio, tmp_path):
        """The security-relevant branch: a session registered for project A
        must never resolve project B's lookup just because cwd happens to be
        nested under A's worktree."""
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "projA", wt)

        assert find_session_for_cwd(isolated_portfolio.root, wt, project_id="projB") is None

        # No project_id filter given -> visible; it's the caller's job to filter.
        found = find_session_for_cwd(isolated_portfolio.root, wt, project_id=None)
        assert found is not None
        assert found.project_id == "projA"

    def test_most_specific_session_wins_for_nested_worktrees(
        self, isolated_portfolio, tmp_path
    ):
        outer = tmp_path / "outer"
        outer.mkdir()
        inner = outer / "inner"
        inner.mkdir()
        register_session(isolated_portfolio.root, "outer-sess", "TEST-001", "test", outer)
        register_session(isolated_portfolio.root, "inner-sess", "TEST-002", "test", inner)

        found = find_session_for_cwd(isolated_portfolio.root, inner, project_id="test")
        assert found.session_id == "inner-sess"

        # cwd only under the outer one -> the outer session, not the inner.
        found_outer_only = find_session_for_cwd(
            isolated_portfolio.root, outer / "other-file-area", project_id="test"
        )
        assert found_outer_only.session_id == "outer-sess"

    def test_released_session_is_not_matched(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)
        release_session(isolated_portfolio.root, "sess-1")
        assert find_session_for_cwd(isolated_portfolio.root, wt, project_id="test") is None


# ---------------------------------------------------------------------------
# Replay robustness (CLAWP-098 review finding: fail-open must not be silent)
# ---------------------------------------------------------------------------


class TestReplayRobustness:
    def test_non_dict_json_line_is_skipped_not_fatal(self, isolated_portfolio, tmp_path):
        """A syntactically-valid JSON line that isn't an object (bare list /
        string / number / null) must not crash replay for every OTHER
        session (antigravity review, PR #55: an unguarded ev.get("action")
        raises AttributeError on a non-dict `ev`)."""
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)

        registry = isolated_portfolio.root / SESSION_REGISTRY_FILENAME
        with open(registry, "a", encoding="utf-8") as f:
            f.write("[1, 2, 3]\n")
            f.write("null\n")
            f.write('"just a string"\n')

        register_session(isolated_portfolio.root, "sess-2", "TEST-002", "test", wt)

        sessions = active_sessions(isolated_portfolio.root)
        assert {s.session_id for s in sessions} == {"sess-1", "sess-2"}

    def test_corrupted_line_is_skipped_not_fatal(self, isolated_portfolio, tmp_path):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)

        registry = isolated_portfolio.root / SESSION_REGISTRY_FILENAME
        with open(registry, "a", encoding="utf-8") as f:
            f.write("{not valid json\n")

        register_session(isolated_portfolio.root, "sess-2", "TEST-002", "test", wt)

        sessions = active_sessions(isolated_portfolio.root)
        assert {s.session_id for s in sessions} == {"sess-1", "sess-2"}

    def test_whole_file_read_failure_falls_back_to_empty_and_logs_a_warning(
        self, isolated_portfolio, tmp_path, caplog
    ):
        wt = tmp_path / "wt"
        wt.mkdir()
        register_session(isolated_portfolio.root, "sess-1", "TEST-001", "test", wt)

        registry = isolated_portfolio.root / SESSION_REGISTRY_FILENAME
        # Force a read failure (OSError family) without relying on filesystem
        # permissions (unreliable to set up portably): swap the file for a
        # directory of the same name, so `.read_text()` raises.
        registry.unlink()
        registry.mkdir()

        with caplog.at_level("WARNING", logger="clawpm.sessions"):
            result = active_sessions(isolated_portfolio.root)

        assert result == []
        assert any(
            "session registry" in rec.message.lower() for rec in caplog.records
        ), "a whole-file replay failure must log, not fail silently (CLAWP-098 review)"
