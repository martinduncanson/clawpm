"""Tests for the parent rollup gate + `tasks decompose` (CLAWP-037).

The rollup gate already half-existed (change_task_state grew a `force` param
and a children check). This suite covers the completion:
  - parent_rollup_status helper, including the dangling-child-ref = unsatisfied
    rule (mirrors cascade_unblock_dependents' missing-dep handling).
  - the gate blocking parent DONE until children are DONE, and --force override.
  - `tasks decompose` creating children that each carry their own rubric.
  - --force logging the incomplete children to the work_log.
  - the parent-ready advisory emitted when the last child completes.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import Predictions, Task, TaskState
from clawpm.tasks import (
    add_subtask,
    add_task,
    change_task_state,
    get_task,
    parent_ready_signal,
    parent_rollup_status,
)

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401


class TestParentRollupStatus:
    def test_no_children_is_ready(self):
        t = Task(id="P", title="p", state=TaskState.OPEN)
        st = parent_rollup_status(None, "test", t)
        assert st["ready"] is True
        assert st["incomplete"] == [] and st["missing"] == []

    def test_missing_child_ref_is_unsatisfied(self, temp_portfolio_with_repo):
        """A child id with no task file counts as UNSATISFIED — a ref we
        cannot verify is not silently treated as done."""
        config = temp_portfolio_with_repo["config"]
        t = Task(id="P", title="p", state=TaskState.OPEN, children=["GHOST-001"])
        st = parent_rollup_status(config, "test", t)
        assert st["ready"] is False
        assert "GHOST-001" in st["missing"]

    def test_incomplete_child_listed(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        c1 = add_subtask(config, "test", parent.id, "c1")
        p = get_task(config, "test", parent.id)  # now a directory w/ children
        st = parent_rollup_status(config, "test", p)
        assert st["ready"] is False
        assert any(x["id"] == c1.id for x in st["incomplete"])


class TestRollupGate:
    def test_parent_done_blocked_while_child_open(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        add_subtask(config, "test", parent.id, "c1")
        res = change_task_state(config, "test", parent.id, TaskState.DONE)
        assert res is None  # gate refused
        assert get_task(config, "test", parent.id).state != TaskState.DONE

    def test_parent_done_allowed_when_all_children_done(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        child = add_subtask(config, "test", parent.id, "c1")
        change_task_state(config, "test", child.id, TaskState.DONE)
        res = change_task_state(config, "test", parent.id, TaskState.DONE)
        assert res is not None and res.state == TaskState.DONE

    def test_force_overrides_gate(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        add_subtask(config, "test", parent.id, "c1")
        res = change_task_state(
            config, "test", parent.id, TaskState.DONE, force=True
        )
        assert res is not None and res.state == TaskState.DONE


class TestParentReadySignal:
    def test_signal_fires_only_when_all_children_done(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="Parent")
        c1 = add_subtask(config, "test", parent.id, "c1")
        c2 = add_subtask(config, "test", parent.id, "c2")

        change_task_state(config, "test", c1.id, TaskState.DONE)
        assert parent_ready_signal(config, "test", c1.id) is None  # c2 still open

        change_task_state(config, "test", c2.id, TaskState.DONE)
        sig = parent_ready_signal(config, "test", c2.id)
        assert sig is not None
        assert sig["parent_id"] == parent.id and sig["ready"] is True


class TestDecomposeCLI:
    def test_decompose_creates_children_with_rubrics(
        self, temp_portfolio_with_repo
    ):
        config = temp_portfolio_with_repo["config"]
        add_task(config, "test", title="Parent", task_id="TEST-500")
        runner = CliRunner()
        r = runner.invoke(main, [
            "tasks", "decompose", "TEST-500", "-p", "test",
            "--child", json.dumps({"title": "A", "success_criteria": ["a done"]}),
            "--child", json.dumps({"title": "B", "success_criteria": ["b done"]}),
        ])
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        children = out["data"]["children"]
        assert len(children) == 2
        for ch in children:
            assert ch["predictions"]["success_criteria"], "child missing rubric"
        p = get_task(config, "test", "TEST-500")
        assert len(p.children) == 2

    def test_done_blocked_then_force_logs_incomplete(
        self, temp_portfolio_with_repo
    ):
        from clawpm.worklog import read_entries

        config = temp_portfolio_with_repo["config"]
        add_task(config, "test", title="Parent", task_id="TEST-501")
        runner = CliRunner()
        runner.invoke(main, [
            "tasks", "decompose", "TEST-501", "-p", "test",
            "--child", "A", "--child", "B",
        ])
        # done without --force → blocked, non-zero exit
        r = runner.invoke(main, ["tasks", "state", "TEST-501", "done", "-p", "test"])
        assert r.exit_code != 0
        assert "incomplete" in r.output.lower()

        # --force → completes + logs which children were outstanding
        r2 = runner.invoke(
            main, ["tasks", "state", "TEST-501", "done", "-p", "test", "--force"]
        )
        assert r2.exit_code == 0, r2.output
        assert get_task(config, "test", "TEST-501").state == TaskState.DONE
        entries = read_entries(config, project="test")
        force_notes = [
            e for e in entries
            if e.task == "TEST-501"
            and e.summary
            and "Force-completed over incomplete subtasks" in e.summary
        ]
        assert force_notes, "expected a work_log note naming incomplete children"
        assert "TEST-501-001" in force_notes[0].summary

    def test_child_done_emits_parent_ready(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        add_task(config, "test", title="Parent", task_id="TEST-502")
        runner = CliRunner()
        runner.invoke(main, [
            "tasks", "decompose", "TEST-502", "-p", "test", "--child", "Only",
        ])
        r = runner.invoke(
            main, ["tasks", "state", "TEST-502-001", "done", "-p", "test"]
        )
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert out["data"].get("parent_ready", {}).get("parent_id") == "TEST-502"


class TestChildrenPersistence:
    """Codex round-1 P1 regressions: children must survive migration out
    of the parent directory and outright deletion."""

    def test_blocked_child_still_blocks_parent(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P")
        c1 = add_subtask(config, "test", parent.id, "c1")
        c2 = add_subtask(config, "test", parent.id, "c2")
        change_task_state(config, "test", c1.id, TaskState.BLOCKED)
        change_task_state(config, "test", c2.id, TaskState.DONE)
        # Persisted in parent frontmatter — c1 still visible after migrating
        # out of the parent directory into blocked/.
        p = get_task(config, "test", parent.id)
        assert c1.id in p.children and c2.id in p.children
        # Gate must still refuse because c1 is BLOCKED, not DONE.
        res = change_task_state(config, "test", parent.id, TaskState.DONE)
        assert res is None
        assert get_task(config, "test", parent.id).state != TaskState.DONE

    def test_deleted_child_counted_as_missing(self, temp_portfolio_with_repo):
        config = temp_portfolio_with_repo["config"]
        parent = add_task(config, "test", title="P")
        c1 = add_subtask(config, "test", parent.id, "c1")
        # Simulate file loss (typo'd id, accidental delete, etc.).
        c1.file_path.unlink()
        p = get_task(config, "test", parent.id)
        st = parent_rollup_status(config, "test", p)
        assert c1.id in st["missing"]
        assert st["ready"] is False

    def test_subtask_id_does_not_collide_with_migrated_child(
        self, temp_portfolio_with_repo,
    ):
        """Codex round-2 P2 regression: after a child migrates to done/ or
        blocked/, the next add_subtask must skip past its id rather than
        re-issuing it (which would shadow the migrated record)."""
        config = temp_portfolio_with_repo["config"]
        add_task(config, "test", title="P", task_id="TEST-700")
        c1 = add_subtask(config, "test", "TEST-700", "first")
        change_task_state(config, "test", c1.id, TaskState.DONE)
        # Now the parent directory has no open subtasks; the naive
        # parent_dir.glob would suggest the next id is -001 again.
        c2 = add_subtask(config, "test", "TEST-700", "second")
        assert c2.id != c1.id
        assert c2.id.endswith("-002")
        p = get_task(config, "test", "TEST-700")
        assert c1.id in p.children and c2.id in p.children
