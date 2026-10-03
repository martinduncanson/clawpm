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

Fix (operator decision (b), 2026-10-03): ``_naive_prefix_candidates``
NORMALISES a subtask-shaped candidate instead of skipping it
(``_desubtask_prefix``: drop the hyphen before the trailing digit run until
the shape is gone, ``TEAM-2`` -> ``TEAM2``) — the allocator only ever hands
out a prefix ``_infer_prefix_from_tasks`` can recognise as real forever
after, and short digit-suffixed ids (``web-2``) still get a prefix instead
of hard-failing.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import (
    _desubtask_prefix,
    _infer_prefix_from_tasks,
    add_task,
    assign_all_prefixes,
)


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
        # Decision (b): the unstable slice is NORMALISED, not dropped.
        assert "TEAM2" in candidates, candidates
        assert "TEAM-2-B" in candidates, candidates
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
        # TEAM is taken by team-2-a; the next candidate is the normalised
        # "TEAM-2" -> "TEAM2" (never the unstable "TEAM-2").
        assert task_b1.id == "TEAM2-000", task_b1.id

        tasks_dir_b = tmp_path / "projects" / "team-2-b" / ".project" / "tasks"
        inferred = _infer_prefix_from_tasks(tasks_dir_b)
        assert inferred == "TEAM2", (
            "team-2-b's own prefix must be stably re-inferable from its "
            "own materialized file, not perpetually None"
        )

        # A second mint must reuse the SAME (now-real) prefix, not re-derive
        # a fresh one from assign_all_prefixes.
        task_b2 = add_task(config, "team-2-b", "b2")
        assert task_b2.id == "TEAM2-001", (task_b1.id, task_b2.id)
        assert task_a.id.rsplit("-", 1)[0] != task_b2.id.rsplit("-", 1)[0]

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
        chain now normalises it to "TEAM2"), so this is an ordinary
        self-owned explicit id, not a cross-project collision --
        unaffected by, and not the bug targeted by, this fix. (Whether it
        stays a stable inferred prefix for team-2-a is NOT claimed here:
        the explicit id is a parent-task-shaped file.)"""
        _make_portfolio(tmp_path, "team-2-a")
        _add_project(tmp_path, "team-2-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "team-2-a", "self-owned", task_id="TEAM-2-001")
        assert task.id == "TEAM-2-001"


class TestSubtaskShapedCandidateIsNormalisedNotSkipped:
    """Operator decision (b), 2026-10-03: a digit-suffixed project id must
    still get a usable, stable prefix (the skip variant hard-failed
    ``web-2`` / ``ab-2``)."""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("WEB-2", "WEB2"),
            ("AB-2", "AB2"),
            ("TEAM-2", "TEAM2"),
            ("X-1-2", "X12"),  # one collapse gives X-12, still subtask-shaped
            ("Q3-20", "Q320"),
            ("ARB-P", "ARB-P"),  # not subtask-shaped: untouched
            ("TEAM2", "TEAM2"),
        ],
    )
    def test_desubtask_prefix(self, raw, expected):
        assert _desubtask_prefix(raw) == expected

    def test_placeholder_equals_first_candidate(self):
        from clawpm.tasks import _naive_prefix_candidates, _naive_prefix_placeholder

        for pid in ("web-2", "ab-2", "x-1-2", "q3-2026", "team-2-b", "code-quorum"):
            assert _naive_prefix_placeholder(pid) == next(
                iter(_naive_prefix_candidates(pid))
            ), pid

    def test_no_candidate_is_ever_subtask_shaped_or_empty(self):
        from clawpm.tasks import _naive_prefix_candidates

        for pid in ("web-2", "ab-2", "x-1", "x-1-2", "q3-2026", "a-1-2-3-4-5-6-7"):
            cands = list(_naive_prefix_candidates(pid))
            assert cands, pid
            assert all(not re.search(r"-\d+$", c) for c in cands), (pid, cands)

    def test_round_trip_file_name_infers_back_to_prefix(self, tmp_path):
        d = tmp_path / "tasks"
        d.mkdir()
        (d / "WEB2-000.md").write_text("x", encoding="utf-8")
        assert _infer_prefix_from_tasks(d) == "WEB2"

    @pytest.mark.parametrize(
        "pid, expected",
        [
            ("web-2", "WEB2"),
            ("ab-2", "AB2"),
            ("x-1-2", "X12"),
            # "q3-2026".upper()[:5] == "Q3-20" -> normalised "Q320"
            ("q3-2026", "Q320"),
        ],
    )
    def test_single_taskless_digit_suffixed_project_is_assigned_and_stable(
        self, tmp_path, monkeypatch, pid, expected
    ):
        _make_portfolio(tmp_path, pid)
        config = _load_isolated_config(tmp_path, monkeypatch)

        assignments, errors = assign_all_prefixes(config)
        assert errors == {}, errors
        assert assignments == {pid: expected}

        first = add_task(config, pid, "one")
        assert first.id == f"{expected}-000"
        # Stability round-trip: the minted file is re-inferred as the same
        # prefix, so the next mint continues the sequence.
        tasks_dir = tmp_path / "projects" / pid / ".project" / "tasks"
        assert _infer_prefix_from_tasks(tasks_dir) == expected
        second = add_task(config, pid, "two")
        assert second.id == f"{expected}-001"
