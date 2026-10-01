"""Regression tests for CLAWP-131.

``_candidate_task_paths``'s subtask branch computed only ONE level via
``_parent_id_of(task_id)``, assuming the immediate parent's own directory
always sits at the TOP level of ``tasks_dir``. When a child is itself
decomposed into a directory (``split_task``) and THAT directory is nested
under its own parent's directory, a grandchild's OPEN-state file lives at
``tasks_dir/<parent>/<child>/<grandchild>.md`` -- two levels down -- which
the one-level probe never reaches. ``get_task(grandchild_id)`` returned
``None`` despite the file existing on disk, and ``change_task_state`` (which
calls ``get_task`` internally) silently failed the same way.

``test_nested_directory_subtask_visible_to_list_tasks`` (test_rollup.py)
already builds this exact shape but only asserts ``list_tasks`` (a directory
walk) finds the grandchild -- it never calls ``get_task(grandchild_id)``
directly, so this gap went unnoticed until now.
"""

from __future__ import annotations

from clawpm.models import TaskState
from clawpm.tasks import (
    _ancestor_chain,
    _nested_dir_candidates,
    add_subtask,
    add_task,
    change_task_state,
    get_task,
    split_task,
)

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401


def _write_task(path, task_id: str, parent: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frontmatter = f"id: {task_id}\n"
    if parent:
        frontmatter += f"parent: {parent}\n"
    path.write_text(f"---\n{frontmatter}---\n# {task_id}\n", encoding="utf-8")


class TestAncestorChain:
    def test_bare_id_with_no_hyphen_has_empty_chain(self):
        assert _ancestor_chain("PARENT") == []

    def test_one_level_id_has_one_ancestor(self):
        # Matches `_parent_id_of`'s own pre-existing heuristic: ANY
        # "<stem>-<digits>" shaped id, including an ordinary top-level
        # task id, looks like a one-level subtask of its own stem. This
        # ambiguity predates CLAWP-131 and is harmless -- it only ever
        # produces an extra existence-checked candidate path.
        assert _ancestor_chain("PARENT-001") == ["PARENT"]

    def test_grandchild_has_full_shallow_first_chain(self):
        assert _ancestor_chain("PARENT-001-001") == ["PARENT", "PARENT-001"]

    def test_great_grandchild_has_full_chain(self):
        assert _ancestor_chain("PARENT-001-001-001") == [
            "PARENT",
            "PARENT-001",
            "PARENT-001-001",
        ]


class TestNestedDirCandidates:
    """Pins the suffix-slicing invariant directly -- this is the exact
    logic that regressed live during CLAWP-131 (the full-chain-only
    version silently broke on realistic PREFIX-NNN ids); a future off-by-one
    in the slicing should fail here, not only transitively via get_task."""

    def test_one_level_id_has_no_candidates(self):
        # chain length 1 -- already covered by the existing one-level probe.
        assert _nested_dir_candidates("PARENT-001") == []

    def test_two_level_chain_has_one_candidate(self):
        assert _nested_dir_candidates("PARENT-001-001") == [["PARENT", "PARENT-001"]]

    def test_three_level_chain_has_every_suffix_of_length_two_plus(self):
        assert _nested_dir_candidates("PARENT-001-001-001") == [
            ["PARENT", "PARENT-001", "PARENT-001-001"],
            ["PARENT-001", "PARENT-001-001"],
        ]

    def test_realistic_ambiguous_root_id_includes_the_real_boundary(self):
        # The exact live regression: CLAWP-900 is a top-level id, not nested
        # under a real "CLAWP" directory, but `_ancestor_chain` can't tell.
        # The real boundary ("CLAWP-900", "CLAWP-900-001") must be among
        # the candidates even though the naive full chain overshoots it.
        candidates = _nested_dir_candidates("CLAWP-900-001-001")
        assert ["CLAWP-900", "CLAWP-900-001"] in candidates


class TestNestedGrandchildResolution:
    def test_get_task_resolves_grandchild_with_auto_generated_top_level_id(
        self, temp_portfolio_with_repo,
    ):
        """The realistic repro: a top-level id auto-minted in the normal
        ``PREFIX-NNN`` shape (here ``TEST-000``), not a hand-picked
        letter-suffixed id. ``_parent_id_of`` can't tell this top-level id
        apart from a genuine subtask of a project called "TEST" -- every
        REAL clawpm task id (e.g. ``CLAWP-131`` itself) has this same
        ambiguity, so ``_ancestor_chain("TEST-000-001-001")`` returns
        ``["TEST", "TEST-000", "TEST-000-001"]``, one spurious level too
        many. An earlier version of this fix joined that FULL chain
        directly and silently regressed on exactly this realistic shape
        (confirmed live: get_task returned None) while passing every other
        test here, because every other test in this file uses a
        letter-suffixed top-level id (e.g. ``TEST-131-A``) that happens to
        sidestep the ambiguity. This test exists specifically so that
        shortcut can't recur unnoticed."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P")  # auto id: TEST-000
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")

        found = get_task(config, "test", grandchild.id)
        assert found is not None
        assert found.id == grandchild.id

    def test_get_task_resolves_grandchild_nested_two_levels_deep(
        self, temp_portfolio_with_repo,
    ):
        """The exact CLAWP-131 repro: parent -> child (split into a
        directory) -> grandchild (nests under child, itself nested under
        parent). get_task must find the grandchild by its bare id."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P", task_id="TEST-131-A")
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")

        found = get_task(config, "test", grandchild.id)
        assert found is not None
        assert found.id == grandchild.id

    def test_change_task_state_resolves_nested_grandchild(
        self, temp_portfolio_with_repo,
    ):
        """change_task_state calls get_task internally -- before the fix it
        silently returned None for a nested grandchild instead of
        transitioning it."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P", task_id="TEST-131-B")
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")

        updated = change_task_state(config, "test", grandchild.id, TaskState.DONE)
        assert updated is not None
        assert updated.state == TaskState.DONE

    def test_get_task_resolves_great_grandchild_nested_three_levels_deep(
        self, temp_portfolio_with_repo,
    ):
        """The fix walks the FULL chain, not just two levels -- verify a
        third level resolves too."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P", task_id="TEST-131-C")
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")
        split_task(config, "test", grandchild.id)
        great_grandchild = add_subtask(config, "test", grandchild.id, "ggchild")

        found = get_task(config, "test", great_grandchild.id)
        assert found is not None
        assert found.id == great_grandchild.id

    def test_get_task_resolves_grandchild_after_ancestor_blocked(
        self, temp_portfolio_with_repo,
    ):
        """PRE-REVIEW catch (confidence 92, verified live): the nested probe
        must cover all FOUR state roots, not just the open tasks_dir. A
        single ancestor transitioning (no `force` needed to block a parent)
        relocates the grandchild's whole nested subtree under blocked/ while
        the grandchild itself is still open -- the exact CLAWP-131 failure
        mode, reopened via a different trigger than the original repro."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P", task_id="TEST-131-D")
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")

        blocked = change_task_state(config, "test", parent.id, TaskState.BLOCKED)
        assert blocked is not None

        found = get_task(config, "test", grandchild.id)
        assert found is not None
        assert found.id == grandchild.id

    def test_get_task_resolves_grandchild_after_ancestor_done_forced(
        self, temp_portfolio_with_repo,
    ):
        """Same shape as the BLOCKED case above but via `done` + force --
        verifies the fix isn't accidentally specific to one state root."""
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P", task_id="TEST-131-E")
        child = add_subtask(config, "test", parent.id, "first")
        split_task(config, "test", child.id)
        grandchild = add_subtask(config, "test", child.id, "gchild")

        done = change_task_state(config, "test", parent.id, TaskState.DONE, force=True)
        assert done is not None

        found = get_task(config, "test", grandchild.id)
        assert found is not None
        assert found.id == grandchild.id


class TestNestedGrandchildArchiveResolution:
    def test_get_task_resolves_grandchild_nested_under_archived_grandparent(
        self, temp_portfolio_with_repo,
    ):
        """_archive_candidate_paths needs the identical full-chain
        treatment as _candidate_task_paths. Construct the nested archived
        shape directly (matching test_clawp085_archive.py's
        test_nested_archived_directory_subtask_resolves pattern) rather
        than driving the real age-based archive_done_tasks flow -- this is
        a chain of length 2 (grandparent/parent/grandchild), one level
        deeper than that existing CLAWP-085 test covers."""
        tasks_dir = temp_portfolio_with_repo["tasks_dir"]
        config = temp_portfolio_with_repo["config"]
        nested = tasks_dir / "done" / "archive" / "TEST-600" / "TEST-600-001"
        nested.mkdir(parents=True)
        _write_task(nested / "TEST-600-001-001.md", "TEST-600-001-001", parent="TEST-600-001")

        found = get_task(config, "test", "TEST-600-001-001")
        assert found is not None
        assert found.id == "TEST-600-001-001"
        assert found.state == TaskState.DONE
