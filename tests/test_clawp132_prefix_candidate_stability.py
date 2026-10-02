"""CLAWP-132 — CLAWP-130's residual gap, found while reproducing it: the
LITERAL team-2-a/team-2-b walkthrough in the task writeup (Codex, PR #68)
does not reproduce against current (pre-this-fix) code the way it describes
— ``check_explicit_id_prefix_collision`` already refuses team-2-b's explicit
``TEAM-2-001`` create outright, via the longest-match sibling check CLAWP-130
added (team-2-a's real "TEAM" is an ancestor-match), and
``_infer_prefix_from_tasks`` does not interpret a materialized
``TEAM-2-001.md`` as prefix "TEAM" either (its own subtask-shape filter
extracts "TEAM-2", not "TEAM", and then excludes it) — both verified
interactively before writing this file.

What DOES reproduce, and is fixed here, is the general bug class the task
named ("assign_all_prefixes' deterministic candidate for a still-taskless
project can diverge from what _infer_prefix_from_tasks later infers"), via
a different, more direct mechanism than predicted-vs-materialized drift:

``_naive_prefix_candidates`` could hand a still-taskless project a candidate
that itself ends in ``-<digits>`` (e.g. project id ``team-2-b`` sliced to
``TEAM-2``, CLAWP-096's own CODE-quorum-style slicing). ``_infer_prefix_from_tasks``
treats ANY prefix ending in ``-<digits>`` as "subtask-shaped" and refuses to
recognise it as real (CLAWP-048's own anchored exclusion of stray
``{prefix}-NNN-MMM`` files) — so a project minted into such a candidate is
NEVER subsequently recognised as having a real claim, stays "task-less"
forever from ``resolve_existing_prefix``'s point of view, and is therefore
RE-RESOLVED from scratch on every later ``assign_all_prefixes`` call. If the
taskless pool's composition changes between two of that project's own
mints (a new sibling joins, or an existing one gains a real claim), the
re-resolution can hand the project a DIFFERENT prefix than before — and the
OLD, abandoned prefix (which its EXISTING files still use) is then free for
a completely different project to be assigned, in the exact scenario below,
literally minting an identical ``TEAM-2-000.md`` filename under two
different projects.

Fix: ``_naive_prefix_candidates`` never yields a subtask-shaped candidate
(``_is_subtask_shaped``, shared with ``_infer_prefix_from_tasks``'s own
filter) — the allocator only ever hands out a prefix that function can
recognise as real forever after.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import _infer_prefix_from_tasks, add_task, assign_all_prefixes


def _make_portfolio(tmp_dir: Path, project_id: str) -> None:
    (tmp_dir / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_dir.as_posix()}"\n'
        f'project_roots = ["{(tmp_dir / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    _add_project(tmp_dir, project_id)


def _add_project(tmp_dir: Path, project_id: str) -> None:
    meta = tmp_dir / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )


def _load_isolated_config(tmp_path: Path, monkeypatch):
    """Mirrors test_clawp129/130's isolation helper."""
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return load_portfolio_config(tmp_path)


class TestSubtaskShapedPrefixIsNeverAssigned:
    def test_naive_candidates_never_include_a_subtask_shaped_prefix(self):
        """Direct unit check on the allocator's own candidate chain: an id
        whose natural slice lands on ``-<digits>`` (``team-2-b`` ->
        ``TEAM-2``) must never offer that candidate at all."""
        from clawpm.tasks import _naive_prefix_candidates

        candidates = list(_naive_prefix_candidates("team-2-b"))
        assert "TEAM-2" not in candidates, candidates
        # The chain must still be non-empty -- it extends past the unstable
        # slice to a stable, longer one instead of just disappearing.
        assert candidates, "team-2-b must still have a reachable candidate"
        assert all(not re.search(r"-\d+$", c) for c in candidates), candidates

    def test_two_near_twin_projects_each_mint_a_stable_distinct_prefix(
        self, tmp_path, monkeypatch
    ):
        """The reproduction scenario: two taskless siblings whose ids only
        differ in a trailing letter after a shared ``-<digit>`` segment.
        Before the fix, the second ("team-2-b") was minted into the
        unstable "TEAM-2" candidate, which its own files could never
        re-establish as real on a later mint."""
        _make_portfolio(tmp_path, "team-2-a")
        _add_project(tmp_path, "team-2-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task_a = add_task(config, "team-2-a", "a1")
        task_b1 = add_task(config, "team-2-b", "b1")
        assert task_a.id == "TEAM-000"
        # Stable, non-subtask-shaped prefix from the very first mint.
        assert task_b1.id.startswith("TEAM-2-"), task_b1.id
        assert task_b1.id != "TEAM-2-000", (
            "must not be minted into the unstable TEAM-2 candidate"
        )

        tasks_dir_b = tmp_path / "projects" / "team-2-b" / ".project" / "tasks"
        inferred = _infer_prefix_from_tasks(tasks_dir_b)
        assert inferred is not None, (
            "team-2-b's own prefix must be stably re-inferable from its "
            "own materialized file, not perpetually None"
        )

        # A second mint must reuse the SAME (now-real) prefix, not re-derive
        # a fresh one from assign_all_prefixes.
        task_b2 = add_task(config, "team-2-b", "b2")
        assert task_b2.id.rsplit("-", 1)[0] == task_b1.id.rsplit("-", 1)[0], (
            task_b1.id, task_b2.id,
        )

    def test_abandoned_candidate_cannot_be_squatted_by_a_later_sibling(
        self, tmp_path, monkeypatch
    ):
        """The literal cross-project collision this bug produced pre-fix:
        a THIRD, still-taskless project joining the portfolio later must
        never be assigned a prefix that an existing project's on-disk task
        already uses — reproduced pre-fix as two literal ``TEAM-2-000.md``
        files under two different projects' task stores.
        """
        _make_portfolio(tmp_path, "team-2-a")
        _add_project(tmp_path, "team-2-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        add_task(config, "team-2-a", "a1")
        task_b1 = add_task(config, "team-2-b", "b1")

        # A new sibling joins whose own naive chain also reaches into the
        # same family of candidates.
        _add_project(tmp_path, "team-2-aa")
        task_aa1 = add_task(config, "team-2-aa", "aa1")

        assert task_aa1.id != task_b1.id, (
            "two different projects minted the identical literal task id"
        )
        tasks_dir_b = tmp_path / "projects" / "team-2-b" / ".project" / "tasks"
        tasks_dir_aa = tmp_path / "projects" / "team-2-aa" / ".project" / "tasks"
        assert not (tasks_dir_b / f"{task_aa1.id}.md").exists()
        assert not (tasks_dir_aa / f"{task_b1.id}.md").exists()

        assignments, errors = assign_all_prefixes(config)
        assert len(set(assignments.values())) == len(assignments), assignments
        assert errors == {}

    def test_explicit_id_future_mint_squat_still_refused_from_team_2_b(
        self, tmp_path, monkeypatch
    ):
        """Sanity check that CLAWP-130's own guard is unaffected by this fix:
        the literal id from the task's illustrative example is still refused
        when team-2-b (the project whose OWN naive candidate is "TEAM-2",
        before this fix extends it to "TEAM-2-B") is the one creating it —
        team-2-a's real "TEAM" is an ancestor-match either way. This is the
        reason the literal walkthrough doesn't reproduce a successful create
        in the first place."""
        _make_portfolio(tmp_path, "team-2-a")
        _add_project(tmp_path, "team-2-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="TEAM"):
            add_task(config, "team-2-b", "squat attempt", task_id="TEAM-2-001")

    def test_team_2_a_may_still_explicitly_claim_its_own_ambiguous_id(
        self, tmp_path, monkeypatch
    ):
        """Unlike the team-2-b direction, team-2-a creating 'TEAM-2-001'
        explicitly for ITSELF is fine post-fix: nobody else's real or
        predicted prefix is "TEAM-2" any more (team-2-b's own candidate
        chain now skips straight past it to "TEAM-2-B"), so this is an
        ordinary self-owned explicit id, not a cross-project collision —
        unaffected by, and not the bug targeted by, this fix."""
        _make_portfolio(tmp_path, "team-2-a")
        _add_project(tmp_path, "team-2-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "team-2-a", "self-owned", task_id="TEAM-2-001")
        assert task.id == "TEAM-2-001"
