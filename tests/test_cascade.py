"""Tests for dependency cascade auto-unblock (CLAWP-020)."""

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
    cascade_unblock_dependents,
    change_task_state,
    get_task,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_cascade_test_")
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
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n'
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
# Direct function tests
# ---------------------------------------------------------------------------


def _add_with_depends(config, project_id, title, depends):
    """add_task accepts depends; helper just keeps the test body terse."""
    return add_task(config, project_id, title=title, depends=depends)


class TestCascadeFunction:
    def test_single_dep_satisfied_promotes_blocked_to_open(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])

        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

        # Complete the parent, then cascade
        change_task_state(config, "test", parent.id, TaskState.DONE)
        transitions = cascade_unblock_dependents(config, "test", parent.id)

        assert len(transitions) == 1
        assert transitions[0]["task_id"] == child.id
        assert transitions[0]["from_state"] == "blocked"
        assert transitions[0]["to_state"] == "open"
        assert transitions[0]["trigger"] == parent.id
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_multi_dep_only_promotes_when_all_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="Dep A")
        b = add_task(config, "test", title="Dep B")
        child = _add_with_depends(config, "test", "Child", depends=[a.id, b.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)

        # Complete A only — child must remain blocked.
        change_task_state(config, "test", a.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", a.id)
        assert trans == []
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

        # Complete B — child cascades.
        change_task_state(config, "test", b.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", b.id)
        assert len(trans) == 1
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_no_cascade_for_unrelated_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        unrelated = add_task(config, "test", title="Unrelated")
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)

        change_task_state(config, "test", unrelated.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", unrelated.id)
        assert trans == []
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

    def test_open_task_with_dep_not_touched(self, temp_portfolio):
        """Tasks already in OPEN aren't moved; we only promote from BLOCKED."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])
        # child stays OPEN (operator chose not to mark blocked)

        change_task_state(config, "test", parent.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", parent.id)
        assert trans == []
        # Child was already OPEN, still OPEN
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_cycle_does_not_loop(self, temp_portfolio):
        """A malformed graph where A.deps=[B] and B.deps=[A] must not loop."""
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A")
        # b's depends references a; we then manually edit a to depend on b
        b = _add_with_depends(config, "test", "B", depends=[a.id])
        # Hand-edit a.md to add depends: [b.id] (simulating malformed graph)
        a_path = temp_portfolio["tasks_dir"] / f"{a.id}.md"
        text = a_path.read_text(encoding="utf-8")
        text = text.replace(
            f"id: {a.id}",
            f"depends:\n- {b.id}\nid: {a.id}",
        )
        a_path.write_text(text, encoding="utf-8")

        change_task_state(config, "test", b.id, TaskState.BLOCKED)
        change_task_state(config, "test", a.id, TaskState.BLOCKED)

        # Completing a (somehow — only possible via --force in practice) should
        # not lock the cascade up. The cascade itself uses a visited set; we
        # verify by ensuring the call returns within the test timeout.
        change_task_state(config, "test", a.id, TaskState.DONE, force=True)
        trans = cascade_unblock_dependents(config, "test", a.id)
        # b had only a as dep, a is now done → b cascades
        assert len(trans) == 1
        assert trans[0]["task_id"] == b.id


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCascadeCLI:
    def test_cli_done_emits_cascade_in_output(self, temp_portfolio):
        runner = CliRunner()
        # Create parent + blocked child
        r = runner.invoke(main, ["-p", "test", "tasks", "add", "-t", "Parent"])
        assert r.exit_code == 0
        parent_id = json.loads(r.output)["data"]["id"]

        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add", "-t", "Child", "-d", parent_id],
        )
        assert r.exit_code == 0
        child_id = json.loads(r.output)["data"]["id"]

        r = runner.invoke(
            main, ["-p", "test", "tasks", "state", child_id, "blocked"]
        )
        assert r.exit_code == 0

        # Done parent → cascade fires
        r = runner.invoke(
            main, ["-p", "test", "tasks", "state", parent_id, "done"]
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "cascade_unblocks" in payload["data"]
        assert len(payload["data"]["cascade_unblocks"]) == 1
        cu = payload["data"]["cascade_unblocks"][0]
        assert cu["task_id"] == child_id
        assert cu["from_state"] == "blocked"
        assert cu["to_state"] == "open"
        assert cu["trigger"] == parent_id

    def test_cli_emits_work_log_entry(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(main, ["-p", "test", "tasks", "add", "-t", "Parent"])
        parent_id = json.loads(r.output)["data"]["id"]
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add", "-t", "Child", "-d", parent_id],
        )
        child_id = json.loads(r.output)["data"]["id"]
        runner.invoke(
            main, ["-p", "test", "tasks", "state", child_id, "blocked"]
        )
        runner.invoke(
            main, ["-p", "test", "tasks", "state", parent_id, "done"]
        )

        # Inspect work_log.jsonl
        worklog = temp_portfolio["root"] / "work_log.jsonl"
        assert worklog.exists()
        lines = [
            json.loads(line)
            for line in worklog.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cascade_entries = [
            e for e in lines if e.get("action") == "cascade_unblock"
        ]
        assert len(cascade_entries) == 1
        assert cascade_entries[0]["task"] == child_id
        assert parent_id in cascade_entries[0]["summary"]


# ---------------------------------------------------------------------------
# Doctor stale-blocked check
# ---------------------------------------------------------------------------


class TestDoctorStaleBlocked:
    def test_doctor_flags_blocked_with_done_deps(self, temp_portfolio, monkeypatch):
        """A task in blocked/ whose deps are all done is stale-blocked."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = add_task(
            config, "test", title="Child", depends=[parent.id]
        )

        # Mark child blocked, parent done, but DON'T run cascade
        # (simulating historical state from before cascade landed).
        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        change_task_state(config, "test", parent.id, TaskState.DONE)

        # Backdate child.md mtime by 48h so it's past the 24h cutoff.
        child_blocked_path = (
            temp_portfolio["tasks_dir"] / "blocked" / f"{child.id}.md"
        )
        assert child_blocked_path.exists()
        import time
        old_ts = time.time() - (48 * 3600)
        os.utime(child_blocked_path, (old_ts, old_ts))

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        sb_ids = [sb["task_id"] for sb in payload.get("stale_blocked", [])]
        assert child.id in sb_ids
