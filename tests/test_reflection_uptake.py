"""Regression tests for reflection-uptake-v1 fixes.

Fix 2: parse_duration — human-friendly unit suffixes (m/h/d/w)
Fix 3: subtask duration uses own start, not parent's
Fix 4: filter_files_changed strips build artefacts and gitignored files
Fix 5: clawpm unblock command
Fix 6: re-start warning (informational, non-blocking)
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import (
    Predictions,
    TaskComplexity,
    TaskState,
    WorkLogAction,
    WorkLogEntry,
)
from clawpm.reflect import _compute_actuals, parse_duration
from clawpm.tasks import add_task, add_subtask, change_task_state, get_task, split_task
from clawpm.worklog import add_entry, filter_files_changed


# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_reflect_phase1.py)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_uptake_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )

    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    (portfolio_root / "work_log.jsonl").touch()

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
    shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# Fix 2 — parse_duration
# ---------------------------------------------------------------------------


class TestParseDuration:
    def test_bare_integer_string(self):
        assert parse_duration("45") == 45

    def test_minutes_suffix(self):
        assert parse_duration("90m") == 90

    def test_hours_suffix(self):
        assert parse_duration("2h") == 120

    def test_days_suffix_wall_clock(self):
        # 3d = 3 × 24 h × 60 min = 4320 min (wall-clock, NOT 8-hour workday)
        assert parse_duration("3d") == 4320

    def test_weeks_suffix(self):
        assert parse_duration("1w") == 60 * 24 * 7

    def test_integer_passthrough(self):
        assert parse_duration(45) == 45

    def test_none_passthrough(self):
        assert parse_duration(None) is None

    def test_invalid_format_raises(self):
        import click

        with pytest.raises(click.BadParameter):
            parse_duration("2x")

    def test_cli_tasks_add_accepts_unit_suffix(self, temp_portfolio):
        """CLI: --predict-duration 2h should store 120 minutes."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks",
                "add",
                "--title",
                "Duration unit test",
                "--predict-duration",
                "2h",
                "--project",
                "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["duration_min"] == 120

    def test_cli_tasks_add_days(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks",
                "add",
                "--title",
                "Days duration test",
                "--predict-duration",
                "1d",
                "--project",
                "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["duration_min"] == 1440

    def test_cli_tasks_edit_accepts_unit_suffix(self, temp_portfolio):
        """CLI: tasks edit --predict-duration 1h should store 60 minutes."""
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit duration unit")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks",
                "edit",
                task.id,
                "--predict-duration",
                "1h",
                "--project",
                "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["duration_min"] == 60

    def test_cli_invalid_duration_exits_nonzero(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks",
                "add",
                "--title",
                "Bad duration",
                "--predict-duration",
                "2x",
                "--project",
                "test",
            ],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Fix 3 — subtask duration uses its OWN start, not parent's
# ---------------------------------------------------------------------------


class TestSubtaskDurationIsolation:
    def _make_entry(
        self,
        project: str,
        task_id: str,
        action: WorkLogAction,
        ts: datetime | None = None,
    ) -> WorkLogEntry:
        return WorkLogEntry(
            ts=ts or datetime.now(timezone.utc),
            project=project,
            action=action,
            task=task_id,
        )

    def test_subtask_duration_none_when_only_parent_started(self, temp_portfolio):
        """Subtask with no own start event → duration_min must be None."""
        config = temp_portfolio["config"]

        parent = add_task(config, "test", "Parent task")
        assert parent is not None
        parent = split_task(config, "test", parent.id)
        assert parent is not None

        subtask = add_subtask(config, "test", parent.id, "Subtask A")
        assert subtask is not None

        # Only a start event for the PARENT, nothing for the subtask
        parent_start = datetime.now(timezone.utc) - timedelta(minutes=300)
        entries = [
            self._make_entry("test", parent.id, WorkLogAction.START, ts=parent_start),
        ]

        actuals = _compute_actuals(subtask.id, None, entries)
        assert actuals.duration_min is None, (
            f"Expected None but got {actuals.duration_min!r} — "
            "subtask should not inherit parent's start event"
        )

    def test_subtask_duration_uses_own_start(self, temp_portfolio):
        """Subtask with its own start event → duration computed from that event."""
        config = temp_portfolio["config"]

        parent = add_task(config, "test", "Parent task 2")
        assert parent is not None
        parent = split_task(config, "test", parent.id)
        assert parent is not None

        subtask = add_subtask(config, "test", parent.id, "Subtask B")
        assert subtask is not None

        # Start event for parent (300 min ago) AND own start for subtask (30 min ago)
        parent_start = datetime.now(timezone.utc) - timedelta(minutes=300)
        sub_start = datetime.now(timezone.utc) - timedelta(minutes=30)
        entries = [
            self._make_entry("test", parent.id, WorkLogAction.START, ts=parent_start),
            self._make_entry("test", subtask.id, WorkLogAction.START, ts=sub_start),
        ]

        actuals = _compute_actuals(subtask.id, None, entries)
        assert actuals.duration_min is not None
        # Should be ~30 min, not ~300 min
        assert 28 <= actuals.duration_min <= 35, (
            f"Expected ~30 min but got {actuals.duration_min} — "
            "should use subtask's own start, not parent's"
        )

    def test_parent_duration_unaffected_by_subtask_start(self, temp_portfolio):
        """Parent duration must be computed from parent's own start, ignoring subtask starts."""
        config = temp_portfolio["config"]

        parent = add_task(config, "test", "Parent task 3")
        assert parent is not None
        parent = split_task(config, "test", parent.id)
        assert parent is not None

        subtask = add_subtask(config, "test", parent.id, "Subtask C")
        assert subtask is not None

        parent_start = datetime.now(timezone.utc) - timedelta(minutes=60)
        sub_start = datetime.now(timezone.utc) - timedelta(minutes=5)
        entries = [
            self._make_entry("test", parent.id, WorkLogAction.START, ts=parent_start),
            self._make_entry("test", subtask.id, WorkLogAction.START, ts=sub_start),
        ]

        parent_actuals = _compute_actuals(parent.id, None, entries)
        assert parent_actuals.duration_min is not None
        # ~60 min, not ~5 min
        assert 58 <= parent_actuals.duration_min <= 65

    def test_cli_subtask_done_reflection_uses_own_start(self, temp_portfolio):
        """End-to-end: done CLI reflects subtask's own start in actuals."""
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]

        parent = add_task(config, "test", "Parent E2E")
        assert parent is not None
        parent = split_task(config, "test", parent.id)
        assert parent is not None

        subtask = add_subtask(config, "test", parent.id, "Subtask E2E")
        assert subtask is not None

        # Log start only for the subtask, 10 minutes ago
        sub_start_ts = datetime.now(timezone.utc) - timedelta(minutes=10)
        add_entry(
            config,
            project="test",
            action=WorkLogAction.START,
            task=subtask.id,
            ts=sub_start_ts,
        )
        change_task_state(config, "test", subtask.id, TaskState.PROGRESS)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "state", subtask.id, "done", "--project", "test"],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{subtask.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip().splitlines()[-1])
        duration = record["actuals"]["duration_min"]
        # Should be ~10 min, not the parent's 300 days
        assert duration is None or duration < 60, (
            f"Expected None or <60 but got {duration!r} — "
            "subtask should not inherit parent start"
        )


# ---------------------------------------------------------------------------
# Fix 4 — filter_files_changed strips build artefacts
# ---------------------------------------------------------------------------


class TestFilterFilesChanged:
    def test_pyc_files_excluded(self):
        files = ["src/foo.py", "src/__pycache__/foo.cpython-311.pyc", "tests/bar.py"]
        result = filter_files_changed(files)
        assert "src/__pycache__/foo.cpython-311.pyc" not in result
        assert "src/foo.py" in result
        assert "tests/bar.py" in result

    def test_pycache_dir_excluded(self):
        files = ["__pycache__/something.pyc", "src/real.py"]
        result = filter_files_changed(files)
        assert not any("__pycache__" in f for f in result)
        assert "src/real.py" in result

    def test_ds_store_excluded(self):
        files = [".DS_Store", "src/main.py", "Thumbs.db", "desktop.ini"]
        result = filter_files_changed(files)
        assert ".DS_Store" not in result
        assert "Thumbs.db" not in result
        assert "desktop.ini" not in result
        assert "src/main.py" in result

    def test_editor_temp_files_excluded(self):
        files = ["src/edit.py~", "src/edit.py.swp", "src/real.py", "notes.bak"]
        result = filter_files_changed(files)
        assert "src/edit.py~" not in result
        assert "src/edit.py.swp" not in result
        assert "notes.bak" not in result
        assert "src/real.py" in result

    def test_tmp_files_excluded(self):
        files = ["src/foo.tmp", "src/real.py"]
        result = filter_files_changed(files)
        assert "src/foo.tmp" not in result
        assert "src/real.py" in result

    def test_pytest_cache_excluded(self):
        files = [".pytest_cache/v/cache/lastfailed", "src/real.py"]
        result = filter_files_changed(files)
        assert not any(".pytest_cache" in f for f in result)
        assert "src/real.py" in result

    def test_node_modules_excluded(self):
        files = ["node_modules/some-dep/index.js", "src/app.js"]
        result = filter_files_changed(files)
        assert not any("node_modules" in f for f in result)
        assert "src/app.js" in result

    def test_none_passthrough(self):
        assert filter_files_changed(None) is None

    def test_empty_list_passthrough(self):
        assert filter_files_changed([]) == []

    def test_all_real_files_preserved(self):
        files = ["src/cli.py", "tests/test_foo.py", "README.md", "pyproject.toml"]
        result = filter_files_changed(files)
        assert result == files

    def test_pyo_excluded(self):
        files = ["compiled.pyo", "src/real.py"]
        result = filter_files_changed(files)
        assert "compiled.pyo" not in result
        assert "src/real.py" in result


# ---------------------------------------------------------------------------
# Fix 5 — clawpm unblock command
# ---------------------------------------------------------------------------


class TestUnblockCommand:
    def test_unblock_moves_blocked_to_open(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Unblock me")
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.BLOCKED)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["unblock", task.id, "--note", "hardware arrived", "--project", "test"],
        )
        assert result.exit_code == 0, result.output

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.state == TaskState.OPEN

    def test_unblock_with_start_moves_to_progress(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Unblock to progress")
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.BLOCKED)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "unblock",
                task.id,
                "--start",
                "--note",
                "ready to go",
                "--project",
                "test",
            ],
        )
        assert result.exit_code == 0, result.output

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.state == TaskState.PROGRESS

    def test_unblock_logs_unblock_action(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Log unblock")
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.BLOCKED)

        runner = CliRunner()
        runner.invoke(
            main,
            ["unblock", task.id, "--note", "blocker resolved", "--project", "test"],
        )

        # Check work log for unblock entry
        wl_path = portfolio_root / "work_log.jsonl"
        entries = [
            json.loads(line)
            for line in wl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        unblock_entries = [
            e for e in entries if e.get("action") == "unblock" and e.get("task") == task.id
        ]
        assert len(unblock_entries) >= 1
        assert unblock_entries[-1]["summary"] == "blocker resolved"

    def test_unblock_fails_on_non_blocked_task(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Open task")
        assert task is not None
        # Task is open, not blocked

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["unblock", task.id, "--project", "test"],
        )
        assert result.exit_code != 0

    def test_unblock_fails_on_nonexistent_task(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["unblock", "TEST-999", "--project", "test"],
        )
        assert result.exit_code != 0

    def test_worklog_action_unblock_in_enum(self):
        """WorkLogAction.UNBLOCK must exist in the enum."""
        assert WorkLogAction.UNBLOCK == WorkLogAction("unblock")


# ---------------------------------------------------------------------------
# Fix 6 — re-start warning (informational, not blocking)
# ---------------------------------------------------------------------------


class TestReStartWarning:
    def test_start_in_progress_emits_warning(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Already in progress")
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.PROGRESS)

        # In this Click version stderr is mixed into result.output
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["start", task.id, "--project", "test"],
        )
        # Must not fail — just warn
        assert result.exit_code == 0, result.output
        # Warning must appear somewhere in the combined output
        combined = (result.output or "") + (result.exception and str(result.exception) or "")
        assert "Warning" in combined or "already in progress" in combined.lower(), (
            f"Expected warning in output but got: {combined!r}"
        )

    def test_start_open_task_no_warning(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Open task for start")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["start", task.id, "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        # No warning for a clean open → progress transition
        assert "Warning" not in (result.output or "")
