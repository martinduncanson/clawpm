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
    add_subtask,
    add_task,
    change_task_state,
    get_task,
    split_task,
)

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401


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


class TestNestedGrandchildResolution:
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
