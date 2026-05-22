"""Tests for parallel_group YAML + clawpm next --batch (CLAWP-021)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import (
    add_task,
    change_task_state,
    get_task,
    select_next_batch,
)


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_batch_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {"root": portfolio_root, "tasks_dir": tasks_dir, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Frontmatter round-trip
# ---------------------------------------------------------------------------


class TestFrontmatterRoundTrip:
    def test_add_with_parallel_group_persists(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="A", parallel_group=1)
        reloaded = get_task(config, "test", task.id)
        assert reloaded.parallel_group == 1

    def test_to_dict_includes_parallel_group(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="A", parallel_group=2)
        reloaded = get_task(config, "test", task.id)
        assert reloaded.to_dict()["parallel_group"] == 2

    def test_add_without_parallel_group_is_none(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="A")
        reloaded = get_task(config, "test", task.id)
        assert reloaded.parallel_group is None


# ---------------------------------------------------------------------------
# select_next_batch — pure logic
# ---------------------------------------------------------------------------


class TestSelectNextBatch:
    def test_no_groups_returns_none(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(config, "test", title="Plain")
        group, candidates, conflicts = select_next_batch(config, "test")
        assert group is None
        assert candidates == []

    def test_single_group_returned(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(config, "test", title="A1", parallel_group=1)
        add_task(config, "test", title="A2", parallel_group=1)
        add_task(config, "test", title="Plain")
        group, candidates, conflicts = select_next_batch(config, "test")
        assert group == 1
        assert {t.title for t in candidates} == {"A1", "A2"}
        assert conflicts == []

    def test_group_2_waits_for_group_1_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A", parallel_group=1)
        add_task(config, "test", title="B", parallel_group=1)
        add_task(config, "test", title="C", parallel_group=2)

        # Group 1 not done — selector returns group 1
        group, candidates, _ = select_next_batch(config, "test")
        assert group == 1

        # Complete only one of group 1 — still group 1 (B remains)
        change_task_state(config, "test", a.id, TaskState.DONE)
        group, _, _ = select_next_batch(config, "test")
        assert group == 1

        # Complete second of group 1 → group 2 becomes eligible
        b_id = [t for t in candidates if t.title == "B"][0].id
        change_task_state(config, "test", b_id, TaskState.DONE)
        group, candidates, _ = select_next_batch(config, "test")
        assert group == 2
        assert {t.title for t in candidates} == {"C"}

    def test_blocked_task_in_group_does_not_block_eligibility(self, temp_portfolio):
        """Blocked tasks satisfy the 'must be DONE' rule only when actually DONE."""
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A", parallel_group=1)
        b = add_task(config, "test", title="B", parallel_group=1)
        # Block A — group 1 still has work (B is open)
        change_task_state(config, "test", a.id, TaskState.BLOCKED)
        group, candidates, _ = select_next_batch(config, "test")
        # B remains as a candidate; A is blocked (not in candidates)
        assert group == 1
        assert {t.title for t in candidates} == {"B"}

    def test_scope_conflicts_surfaced(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(
            config, "test", title="A", parallel_group=1,
            scope=["src/auth/**"],
        )
        add_task(
            config, "test", title="B", parallel_group=1,
            scope=["src/auth/login.py"],
        )
        add_task(
            config, "test", title="C", parallel_group=1,
            scope=["src/billing/**"],
        )
        group, candidates, conflicts = select_next_batch(config, "test")
        assert group == 1
        assert len(candidates) == 3
        # A and B overlap, C is clear
        conflict_pairs = {
            tuple(sorted([c["task_a"], c["task_b"]]))
            for c in conflicts
        }
        a_id = [t for t in candidates if t.title == "A"][0].id
        b_id = [t for t in candidates if t.title == "B"][0].id
        assert tuple(sorted([a_id, b_id])) in conflict_pairs

    def test_no_open_tasks_in_eligible_group_skips_to_next(self, temp_portfolio):
        """If group 1 has only done/blocked tasks, selector skips to group 2."""
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A", parallel_group=1)
        b = add_task(config, "test", title="B", parallel_group=1)
        add_task(config, "test", title="C", parallel_group=2)
        change_task_state(config, "test", a.id, TaskState.DONE)
        change_task_state(config, "test", b.id, TaskState.DONE)

        group, candidates, _ = select_next_batch(config, "test")
        assert group == 2


# ---------------------------------------------------------------------------
# CLI: clawpm next --batch
# ---------------------------------------------------------------------------


class TestCLINextBatch:
    def test_cli_next_batch_returns_candidates(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(config, "test", title="A", parallel_group=1)
        add_task(config, "test", title="B", parallel_group=1)

        r = CliRunner().invoke(main, ["-p", "test", "next", "--batch"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["group"] == 1
        assert len(payload["candidates"]) == 2
        assert payload["dispatch_safe"] is True

    def test_cli_next_batch_none_when_no_groups(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(config, "test", title="Plain")
        r = CliRunner().invoke(main, ["-p", "test", "next", "--batch"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert payload["group"] is None
        assert payload["candidates"] == []

    def test_cli_next_batch_surfaces_conflicts(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_task(config, "test", title="A", parallel_group=1, scope=["src/auth/**"])
        add_task(config, "test", title="B", parallel_group=1, scope=["src/auth/login.py"])

        r = CliRunner().invoke(main, ["-p", "test", "next", "--batch"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert payload["group"] == 1
        assert payload["dispatch_safe"] is False
        assert len(payload["conflicts"]) == 1


# ---------------------------------------------------------------------------
# CLI: add/edit --parallel-group
# ---------------------------------------------------------------------------


class TestCLIParallelGroupFlag:
    def test_tasks_add_parallel_group(self, temp_portfolio):
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "add", "-t", "A", "--parallel-group", "3"],
        )
        assert r.exit_code == 0, r.output
        tid = json.loads(r.output)["data"]["id"]
        r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "show", tid])
        assert json.loads(r2.output)["parallel_group"] == 3

    def test_tasks_edit_set_parallel_group(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="A")
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "edit", task.id, "--parallel-group", "2"],
        )
        assert r.exit_code == 0, r.output
        r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "show", task.id])
        assert json.loads(r2.output)["parallel_group"] == 2

    def test_tasks_edit_clear_parallel_group(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="A", parallel_group=5)
        # Pass 0 to clear
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "edit", task.id, "--parallel-group", "0"],
        )
        assert r.exit_code == 0, r.output
        r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "show", task.id])
        assert json.loads(r2.output)["parallel_group"] is None
