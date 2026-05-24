"""Tests for Mission Control (CLAWP-022)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.mission import (
    Mission,
    MissionMiniGoal,
    add_mission,
    add_mission_mini_goal,
    get_mission,
    list_missions,
    mission_status,
    mission_tasks,
    set_mission_status,
)
from clawpm.models import TaskState
from clawpm.tasks import add_task, change_task_state, get_task


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_mission_test_")
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
    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "config": config,
    }
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Direct API: add_mission / list_missions / get_mission
# ---------------------------------------------------------------------------


class TestMissionCRUD:
    def test_add_mission_creates_file_with_frontmatter(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(
            config, "test",
            title="Ship X",
            binary_outcome="X deployed to prod",
            deadline_days=14,
            description="The X mission.",
        )
        assert m.id.startswith("TEST-MISSION-")
        assert m.title == "Ship X"
        assert m.binary_outcome == "X deployed to prod"
        assert m.deadline_days == 14
        assert m.status == "active"
        assert m.created == date.today().isoformat()
        assert m.mini_goals == []
        assert m.file_path is not None
        assert m.file_path.exists()

    def test_add_mission_rejects_invalid_deadline(self, temp_portfolio):
        config = temp_portfolio["config"]
        with pytest.raises(ValueError, match="deadline_days"):
            add_mission(config, "test", "T", "O", deadline_days=3)
        with pytest.raises(ValueError, match="deadline_days"):
            add_mission(config, "test", "T", "O", deadline_days=100)

    def test_get_mission_roundtrips(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "Ship Y", "Y is live", deadline_days=28)
        reloaded = get_mission(config, "test", m.id)
        assert reloaded is not None
        assert reloaded.title == m.title
        assert reloaded.binary_outcome == m.binary_outcome
        assert reloaded.deadline_days == m.deadline_days

    def test_list_missions_filters_by_status(self, temp_portfolio):
        config = temp_portfolio["config"]
        m1 = add_mission(config, "test", "M1", "O1")
        m2 = add_mission(config, "test", "M2", "O2")
        set_mission_status(config, "test", m2.id, "complete")

        all_m = list_missions(config, "test")
        assert len(all_m) == 2

        active = list_missions(config, "test", status_filter="active")
        assert len(active) == 1 and active[0].id == m1.id

        complete = list_missions(config, "test", status_filter="complete")
        assert len(complete) == 1 and complete[0].id == m2.id

    def test_deadline_date_computed(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O", deadline_days=10)
        assert m.deadline_date == date.today() + timedelta(days=10)


# ---------------------------------------------------------------------------
# Mini-goal linking
# ---------------------------------------------------------------------------


class TestMissionMiniGoals:
    def test_add_mini_goal_stamps_task_frontmatter(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        task = add_task(config, "test", title="Mini A")

        updated = add_mission_mini_goal(config, "test", m.id, task.id, actor="agent")
        assert len(updated.mini_goals) == 1
        assert updated.mini_goals[0].id == task.id
        assert updated.mini_goals[0].actor == "agent"

        # Task's frontmatter now carries parent_mission + actor
        reloaded = get_task(config, "test", task.id)
        assert reloaded.parent_mission == m.id
        assert reloaded.actor == "agent"

    def test_add_mini_goal_human_actor(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        task = add_task(config, "test", title="Record demo")
        add_mission_mini_goal(config, "test", m.id, task.id, actor="human")
        reloaded = get_task(config, "test", task.id)
        assert reloaded.actor == "human"

    def test_add_mini_goal_rejects_invalid_actor(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        task = add_task(config, "test", title="X")
        with pytest.raises(ValueError, match="actor"):
            add_mission_mini_goal(config, "test", m.id, task.id, actor="robot")

    def test_add_mini_goal_idempotent(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        task = add_task(config, "test", title="A")
        add_mission_mini_goal(config, "test", m.id, task.id)
        updated = add_mission_mini_goal(config, "test", m.id, task.id)
        assert len(updated.mini_goals) == 1

    def test_add_mini_goal_caps_at_10(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        for i in range(10):
            t = add_task(config, "test", title=f"G{i}")
            add_mission_mini_goal(config, "test", m.id, t.id)
        eleventh = add_task(config, "test", title="too many")
        with pytest.raises(ValueError, match="10 mini-goals"):
            add_mission_mini_goal(config, "test", m.id, eleventh.id)

    def test_add_mini_goal_rejects_missing_task(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        with pytest.raises(ValueError, match="Task not found"):
            add_mission_mini_goal(config, "test", m.id, "TEST-9999")

    def test_add_mini_goal_refuses_cross_mission_relink(self, temp_portfolio):
        """Codex round-1 P2: a task already mini-goal of mission A must
        NOT be silently re-linked to mission B. Operator must unlink
        from A first; otherwise A and B both count the task and metadata
        diverges from the task's actual parent_mission."""
        config = temp_portfolio["config"]
        m1 = add_mission(config, "test", "M1", "O1")
        m2 = add_mission(config, "test", "M2", "O2")
        task = add_task(config, "test", title="Shared")
        add_mission_mini_goal(config, "test", m1.id, task.id, actor="agent")

        with pytest.raises(ValueError, match="already a mini-goal"):
            add_mission_mini_goal(config, "test", m2.id, task.id, actor="agent")

    def test_add_mini_goal_same_mission_idempotent(self, temp_portfolio):
        """Re-linking to the SAME mission is still a no-op (back-compat)."""
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "O")
        task = add_task(config, "test", title="X")
        add_mission_mini_goal(config, "test", m.id, task.id)
        # Same mission, same task — should not raise
        updated = add_mission_mini_goal(config, "test", m.id, task.id)
        assert len(updated.mini_goals) == 1

    def test_idempotent_relink_works_even_at_cap(self, temp_portfolio):
        """Codex round-2 P2: re-running add-goal for a task already linked
        must be a no-op even when the mission is at the 10-goal cap.
        Otherwise automation retries break."""
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "O")
        tasks = []
        for i in range(10):
            t = add_task(config, "test", title=f"G{i}")
            add_mission_mini_goal(config, "test", m.id, t.id)
            tasks.append(t)
        # Mission is at the 10-goal cap. Re-linking the FIRST task must
        # still be a no-op, not a cap-violation error.
        updated = add_mission_mini_goal(config, "test", m.id, tasks[0].id)
        assert len(updated.mini_goals) == 10  # still 10, no duplicate

        # Adding a NEW task (11th) MUST still hit the cap
        eleventh = add_task(config, "test", title="too many")
        with pytest.raises(ValueError, match="10 mini-goals"):
            add_mission_mini_goal(config, "test", m.id, eleventh.id)


class TestAddMissionOverwrite:
    """Codex round-2 P2: explicit --id reuse must not silently destroy
    an existing mission file."""

    def test_explicit_id_refuses_overwrite_without_force(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_mission(config, "test", "First", "outcome A", mission_id="TEST-MISSION-099")
        with pytest.raises(ValueError, match="already exists"):
            add_mission(
                config, "test", "Second", "outcome B",
                mission_id="TEST-MISSION-099",
            )

    def test_force_overwrites(self, temp_portfolio):
        config = temp_portfolio["config"]
        add_mission(config, "test", "First", "outcome A", mission_id="TEST-MISSION-099")
        updated = add_mission(
            config, "test", "Second", "outcome B",
            mission_id="TEST-MISSION-099", force=True,
        )
        assert updated.title == "Second"
        assert updated.binary_outcome == "outcome B"

    def test_auto_generated_id_doesnt_collide(self, temp_portfolio):
        """Auto-IDs always pick the next free number — no collision risk."""
        config = temp_portfolio["config"]
        m1 = add_mission(config, "test", "A", "oA")
        m2 = add_mission(config, "test", "B", "oB")
        assert m1.id != m2.id

    def test_cli_refuses_overwrite_without_force(self, temp_portfolio):
        from click.testing import CliRunner
        from clawpm.cli import main

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "mission", "add",
            "-t", "A", "-o", "outcome",
            "--id", "TEST-MISSION-050",
        ])
        assert r.exit_code == 0, r.output

        r2 = runner.invoke(main, [
            "-p", "test", "mission", "add",
            "-t", "B", "-o", "outcome",
            "--id", "TEST-MISSION-050",
        ])
        assert r2.exit_code == 1
        assert "already exists" in r2.output

    def test_cli_force_overwrites(self, temp_portfolio):
        from click.testing import CliRunner
        from clawpm.cli import main

        runner = CliRunner()
        runner.invoke(main, [
            "-p", "test", "mission", "add",
            "-t", "A", "-o", "oA", "--id", "TEST-MISSION-051",
        ])
        r = runner.invoke(main, [
            "-p", "test", "mission", "add",
            "-t", "B", "-o", "oB", "--id", "TEST-MISSION-051",
            "--force",
        ])
        assert r.exit_code == 0, r.output


# ---------------------------------------------------------------------------
# mission_status
# ---------------------------------------------------------------------------


class TestMissionStatus:
    def test_empty_mission_status(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        s = mission_status(config, "test", m.id)
        assert s["outcome_status"] == "empty"
        assert s["complete_count"] == 0
        assert s["total_count"] == 0

    def test_in_progress_mission(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        a = add_task(config, "test", title="A")
        b = add_task(config, "test", title="B")
        add_mission_mini_goal(config, "test", m.id, a.id, actor="agent")
        add_mission_mini_goal(config, "test", m.id, b.id, actor="human")
        change_task_state(config, "test", a.id, TaskState.DONE)
        s = mission_status(config, "test", m.id)
        assert s["outcome_status"] == "in_progress"
        assert s["complete_count"] == 1
        assert s["total_count"] == 2
        assert s["pct_complete"] == 50.0
        assert s["agent_counts"]["done"] == 1
        assert s["human_counts"]["open"] == 1

    def test_ready_to_close(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        a = add_task(config, "test", title="A")
        add_mission_mini_goal(config, "test", m.id, a.id)
        change_task_state(config, "test", a.id, TaskState.DONE)
        s = mission_status(config, "test", m.id)
        assert s["outcome_status"] == "ready_to_close"

    def test_overdue(self, temp_portfolio):
        """Backdate the mission's created field to simulate overdue."""
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O", deadline_days=7)
        # Backdate created to 30 days ago via re.sub (yaml may quote dates)
        import re
        text = m.file_path.read_text(encoding="utf-8")
        old_date = (date.today() - timedelta(days=30)).isoformat()
        text = re.sub(
            r"^(created:\s*['\"]?)[\d-]+(['\"]?)\s*$",
            rf"\g<1>{old_date}\g<2>",
            text,
            count=1,
            flags=re.MULTILINE,
        )
        m.file_path.write_text(text, encoding="utf-8")

        a = add_task(config, "test", title="A")
        add_mission_mini_goal(config, "test", m.id, a.id)
        s = mission_status(config, "test", m.id)
        assert s["overdue"] is True
        assert s["outcome_status"] == "overdue"
        assert s["days_remaining"] < 0

    def test_terminal_status_overrides(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        a = add_task(config, "test", title="A")
        add_mission_mini_goal(config, "test", m.id, a.id)
        set_mission_status(config, "test", m.id, "cancelled")
        s = mission_status(config, "test", m.id)
        assert s["outcome_status"] == "cancelled"


# ---------------------------------------------------------------------------
# mission_tasks filter
# ---------------------------------------------------------------------------


class TestMissionTasks:
    def test_filter_by_actor(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        a1 = add_task(config, "test", title="agent1")
        a2 = add_task(config, "test", title="agent2")
        h1 = add_task(config, "test", title="human1")
        add_mission_mini_goal(config, "test", m.id, a1.id, actor="agent")
        add_mission_mini_goal(config, "test", m.id, a2.id, actor="agent")
        add_mission_mini_goal(config, "test", m.id, h1.id, actor="human")

        all_t = mission_tasks(config, "test", m.id)
        agents = mission_tasks(config, "test", m.id, actor_filter="agent")
        humans = mission_tasks(config, "test", m.id, actor_filter="human")
        assert len(all_t) == 3
        assert {t.id for t in agents} == {a1.id, a2.id}
        assert {t.id for t in humans} == {h1.id}


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


class TestMissionCLI:
    def test_mission_add_via_cli(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "mission", "add",
            "-t", "Ship feature",
            "-o", "feature live in prod",
            "-d", "21",
        ])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["data"]["title"] == "Ship feature"
        assert payload["data"]["deadline_days"] == 21

    def test_mission_add_goal_then_status_cli(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        t = add_task(config, "test", title="X")

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "mission", "add-goal", m.id,
            "--task", t.id, "--actor", "agent",
        ])
        assert r.exit_code == 0, r.output

        r2 = runner.invoke(main, [
            "-p", "test", "mission", "status", m.id,
        ])
        assert r2.exit_code == 0
        s = json.loads(r2.output)
        assert s["total_count"] == 1
        assert s["agent_counts"]["open"] == 1

    def test_mission_tasks_filter_via_cli(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")
        t1 = add_task(config, "test", title="agent task")
        t2 = add_task(config, "test", title="human task")
        add_mission_mini_goal(config, "test", m.id, t1.id, actor="agent")
        add_mission_mini_goal(config, "test", m.id, t2.id, actor="human")

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "mission", "tasks", m.id, "--actor", "human",
        ])
        assert r.exit_code == 0, r.output
        tasks = json.loads(r.output)
        assert len(tasks) == 1
        assert tasks[0]["id"] == t2.id

    def test_mission_state_cli(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "T", "O")

        runner = CliRunner()
        r = runner.invoke(main, [
            "-p", "test", "mission", "state", m.id, "complete",
        ])
        assert r.exit_code == 0, r.output
        reloaded = get_mission(config, "test", m.id)
        assert reloaded.status == "complete"
