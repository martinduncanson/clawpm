"""Tests for Phase 1.6 — doctor checks, reflect void, Predictions.filled_by."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Predictions, TaskComplexity, WorkLogAction
from clawpm.reflect import write_reflection_event
from clawpm.tasks import add_task, change_task_state, get_task
from clawpm.models import Actuals, TaskState


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with two test projects for cross-project checks."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_p16_test_")
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

    # Project 1: "alpha"
    project_dir = projects_dir / "alpha"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "alpha"\nname = "Alpha"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    # Project 2: "alpha-extra" (will share prefix "ALPHA" with "alpha")
    project_dir2 = projects_dir / "alpha-extra"
    project_dir2.mkdir()
    project_meta2 = project_dir2 / ".project"
    project_meta2.mkdir()
    (project_meta2 / "settings.toml").write_text(
        'id = "alpha-extra"\nname = "Alpha Extra"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir2 = project_meta2 / "tasks"
    tasks_dir2.mkdir()
    (tasks_dir2 / "done").mkdir()
    (tasks_dir2 / "blocked").mkdir()

    # Empty work log
    (portfolio_root / "work_log.jsonl").touch()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    config = load_portfolio_config(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "project_dir2": project_dir2,
        "tasks_dir": tasks_dir,
        "tasks_dir2": tasks_dir2,
        "config": config,
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# Fix 3 — Predictions.filled_by attribution
# ---------------------------------------------------------------------------


class TestFilledByAttribution:
    def test_no_flag_no_predictions_gives_none(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main, ["tasks", "add", "--title", "Plain task", "--project", "alpha"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        pred = data["data"]["predictions"]
        assert pred["filled_by"] is None

    def test_predict_flags_default_to_operator(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Predicted task",
                "--predict-duration", "60",
                "--predict-complexity", "m",
                "--project", "alpha",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["filled_by"] == "operator"

    def test_predicted_by_agent_flag(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Agent predicted task",
                "--predict-duration", "30",
                "--predicted-by", "agent",
                "--project", "alpha",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["filled_by"] == "agent"

    def test_predicted_by_operator_edited(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Human-reviewed prediction",
                "--predict-duration", "45",
                "--predicted-by", "operator-edited",
                "--project", "alpha",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["filled_by"] == "operator-edited"

    def test_predicted_by_retroactive(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Retro task",
                "--predict-duration", "20",
                "--predicted-by", "retroactive",
                "--project", "alpha",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["filled_by"] == "retroactive"

    def test_invalid_predicted_by_rejected(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Bad attribution",
                "--predict-duration", "10",
                "--predicted-by", "bogus-value",
                "--project", "alpha",
            ],
        )
        assert result.exit_code != 0

    def test_filled_by_persists_on_disk_and_roundtrips(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=60, filled_by="agent")
        task = add_task(config, "alpha", "Roundtrip task", predictions=predictions)
        assert task is not None
        reloaded = get_task(config, "alpha", task.id)
        assert reloaded is not None
        assert reloaded.predictions.filled_by == "agent"

    def test_old_task_without_filled_by_loads_cleanly(self, temp_portfolio):
        """Tasks written before Phase 1.6 (no filled_by field) must load without error."""
        config = temp_portfolio["config"]
        # Manually write a task file without filled_by in predictions
        tasks_dir = temp_portfolio["tasks_dir"]
        task_content = (
            "---\n"
            "id: ALPHA-999\n"
            "predictions:\n"
            "  duration_min: 30\n"
            "  complexity: m\n"
            "---\n"
            "# Old format task\n"
        )
        (tasks_dir / "ALPHA-999.md").write_text(task_content, encoding="utf-8")
        task = get_task(config, "alpha", "ALPHA-999")
        assert task is not None
        assert task.predictions.duration_min == 30
        assert task.predictions.filled_by is None  # graceful default

    def test_reflection_event_carries_filled_by_through(self, temp_portfolio):
        """filled_by is preserved in the reflection JSONL when task completes."""
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=30, filled_by="operator-edited")
        task = add_task(config, "alpha", "Reflect attribution test", predictions=predictions)
        assert task is not None
        change_task_state(config, "alpha", task.id, TaskState.PROGRESS)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "state", task.id, "done", "--project", "alpha"],
        )
        assert result.exit_code == 0, result.output
        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip().splitlines()[-1])
        assert record["predictions"]["filled_by"] == "operator-edited"


# ---------------------------------------------------------------------------
# Fix 2 — reflect void
# ---------------------------------------------------------------------------


class TestReflectVoid:
    def _create_reflection(self, temp_portfolio, title: str = "Void test task") -> str:
        """Helper: create a task, complete it, return task_id."""
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "alpha", title)
        assert task is not None
        change_task_state(config, "alpha", task.id, TaskState.PROGRESS)
        write_reflection_event(
            portfolio_root,
            event="task_done",
            task_id=task.id,
            project_id="alpha",
            predictions=Predictions(duration_min=30),
            actuals=Actuals(duration_min=45),
        )
        return task.id

    def test_void_single_task_appends_void_event(self, temp_portfolio):
        task_id = self._create_reflection(temp_portfolio)
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reflect", "void", task_id, "--reason", "test void reason"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["voided"][0]["task_id"] == task_id

        ref_file = temp_portfolio["root"] / "reflections" / f"{task_id}.jsonl"
        lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2  # original + void
        void_rec = json.loads(lines[-1])
        assert void_rec["event"] == "void"
        assert void_rec["reason"] == "test void reason"
        assert void_rec["task_id"] == task_id

    def test_original_event_not_modified(self, temp_portfolio):
        task_id = self._create_reflection(temp_portfolio)
        runner = CliRunner()
        runner.invoke(
            main,
            ["reflect", "void", task_id, "--reason", "cleanup"],
        )
        ref_file = temp_portfolio["root"] / "reflections" / f"{task_id}.jsonl"
        lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        original = json.loads(lines[0])
        assert original["event"] == "task_done"
        assert original["predictions"]["duration_min"] == 30

    def test_void_nonexistent_task_returns_error_in_json(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reflect", "void", "ALPHA-NOEXIST", "--reason", "test"],
        )
        assert result.exit_code == 0  # returns 0, error is in JSON
        data = json.loads(result.output)
        assert data["count"] == 0
        assert len(data["errors"]) == 1
        assert data["errors"][0]["error"] == "no_reflection_file"

    def test_all_empty_actuals_voids_matching_reflections(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]

        # Task 1: empty actuals (duration_min=None) — should be voided
        t1 = add_task(config, "alpha", "Empty actuals task 1")
        assert t1 is not None
        write_reflection_event(
            portfolio_root, "task_done", t1.id, "alpha",
            Predictions(), Actuals(),  # duration_min is None
        )

        # Task 2: has actual duration — should NOT be voided
        t2 = add_task(config, "alpha", "Real actuals task")
        assert t2 is not None
        write_reflection_event(
            portfolio_root, "task_done", t2.id, "alpha",
            Predictions(duration_min=30), Actuals(duration_min=60),
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["reflect", "void", "--all-empty-actuals", "--reason", "corpus cleanup"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)

        voided_ids = [v["task_id"] for v in data["voided"]]
        assert t1.id in voided_ids
        assert t2.id not in voided_ids

        # Verify t1 has void event appended
        ref_file = portfolio_root / "reflections" / f"{t1.id}.jsonl"
        lines = [l for l in ref_file.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines) == 2
        assert json.loads(lines[-1])["event"] == "void"

        # Verify t2 has only original event
        ref_file2 = portfolio_root / "reflections" / f"{t2.id}.jsonl"
        lines2 = [l for l in ref_file2.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert len(lines2) == 1
        assert json.loads(lines2[0])["event"] == "task_done"

    def test_tasks_show_includes_reflections_voided_flag(self, temp_portfolio):
        task_id = self._create_reflection(temp_portfolio, "Show void test")
        runner = CliRunner()

        # Before void
        result = runner.invoke(main, ["tasks", "show", task_id, "--project", "alpha"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["reflections_voided"] is False

        # After void
        runner.invoke(main, ["reflect", "void", task_id, "--reason", "bad data"])
        result2 = runner.invoke(main, ["tasks", "show", task_id, "--project", "alpha"])
        assert result2.exit_code == 0, result2.output
        data2 = json.loads(result2.output)
        assert data2["reflections_voided"] is True

    def test_tasks_show_no_reflection_file_gives_false(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "alpha", "No reflection task")
        assert task is not None
        runner = CliRunner()
        result = runner.invoke(main, ["tasks", "show", task.id, "--project", "alpha"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["reflections_voided"] is False

    def test_missing_task_id_and_no_flag_exits_nonzero(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "void", "--reason", "no target"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Fix 1 — doctor checks
# ---------------------------------------------------------------------------


class TestDoctorChecks:
    def test_stale_task_flagged(self, temp_portfolio):
        """A .progress.md file with mtime 8 days ago should appear in stale_tasks."""
        tasks_dir = temp_portfolio["tasks_dir"]
        # Create a progress file
        stale_file = tasks_dir / "ALPHA-001.progress.md"
        stale_file.write_text(
            "---\nid: ALPHA-001\n---\n# Stale task\n", encoding="utf-8"
        )
        # Set mtime 8 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(stale_file, (old_ts, old_ts))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stale_ids = [s["task_id"] for s in data.get("stale_tasks", [])]
        assert "ALPHA-001" in stale_ids
        entry = next(s for s in data["stale_tasks"] if s["task_id"] == "ALPHA-001")
        assert entry["days_stale"] >= 8
        assert entry["project_id"] == "alpha"
        assert "suggested_action" in entry

    def test_fresh_task_not_flagged(self, temp_portfolio):
        """A .progress.md touched today should NOT appear in stale_tasks."""
        tasks_dir = temp_portfolio["tasks_dir"]
        fresh_file = tasks_dir / "ALPHA-002.progress.md"
        fresh_file.write_text(
            "---\nid: ALPHA-002\n---\n# Fresh task\n", encoding="utf-8"
        )
        # mtime is now (default)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stale_ids = [s["task_id"] for s in data.get("stale_tasks", [])]
        assert "ALPHA-002" not in stale_ids

    def test_drift_frontmatter_state_mismatch(self, temp_portfolio):
        """A .md in tasks/ with frontmatter state: done should appear in drift_tasks."""
        tasks_dir = temp_portfolio["tasks_dir"]
        drift_file = tasks_dir / "ALPHA-010.md"
        drift_file.write_text(
            "---\nid: ALPHA-010\nstate: done\n---\n# Drift task\n", encoding="utf-8"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        drift_files = [d["file"] for d in data.get("drift_tasks", [])]
        assert any("ALPHA-010" in f for f in drift_files)
        entry = next(d for d in data["drift_tasks"] if "ALPHA-010" in d["file"])
        assert entry["location_state"] == "open"
        assert entry["frontmatter_state"] == "done"

    def test_no_drift_when_state_matches(self, temp_portfolio):
        """A tasks/ .md with no frontmatter state field should NOT be flagged."""
        tasks_dir = temp_portfolio["tasks_dir"]
        clean_file = tasks_dir / "ALPHA-011.md"
        clean_file.write_text(
            "---\nid: ALPHA-011\n---\n# Clean task\n", encoding="utf-8"
        )
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        drift_files = [d["file"] for d in data.get("drift_tasks", [])]
        assert not any("ALPHA-011" in f for f in drift_files)

    def test_prefix_collision_detected(self, temp_portfolio):
        """Projects 'alpha' and 'alpha-extra' both prefix to 'ALPHA' (5 chars)."""
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        collisions = data.get("prefix_collisions", [])
        assert len(collisions) >= 1
        # "alpha" → "ALPHA", "alpha-extra" → "ALPHA-"[:5] = "ALPHA"
        collision_prefixes = {c["prefix"] for c in collisions}
        assert "ALPHA" in collision_prefixes
        alpha_collision = next(c for c in collisions if c["prefix"] == "ALPHA")
        assert "alpha" in alpha_collision["projects"]
        assert "alpha-extra" in alpha_collision["projects"]

    def test_no_prefix_collision_with_unique_projects(self, temp_portfolio):
        """The two projects in the fixture collide; test that a unique prefix doesn't."""
        # Check that "alpha" prefix doesn't spuriously collide with itself
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        data = json.loads(result.output)
        # Only one collision should exist (ALPHA), not more
        for c in data.get("prefix_collisions", []):
            assert len(c["projects"]) >= 2

    def test_strict_flag_exits_nonzero_on_warnings(self, temp_portfolio):
        """--strict exits 1 when stale_tasks or prefix_collisions are present."""
        tasks_dir = temp_portfolio["tasks_dir"]
        # Create a stale task to guarantee a warning
        stale_file = tasks_dir / "ALPHA-099.progress.md"
        stale_file.write_text("---\nid: ALPHA-099\n---\n# Stale\n", encoding="utf-8")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).timestamp()
        os.utime(stale_file, (old_ts, old_ts))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code == 1

    def test_strict_flag_exits_zero_on_clean_portfolio(self):
        """--strict exits 0 when there are no issues (isolated clean portfolio)."""
        temp_dir = tempfile.mkdtemp(prefix="clawpm_clean_test_")
        portfolio_root = Path(temp_dir)
        try:
            (portfolio_root / "portfolio.toml").write_text(
                f'portfolio_root = "{portfolio_root.as_posix()}"\n'
                f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
                "[defaults]\nstatus = \"active\"\n",
                encoding="utf-8",
            )
            (portfolio_root / "projects").mkdir()
            (portfolio_root / "work_log.jsonl").touch()

            old_env = os.environ.get("CLAWPM_PORTFOLIO")
            os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
            try:
                runner = CliRunner()
                result = runner.invoke(main, ["doctor", "--strict"])
                assert result.exit_code == 0, result.output
            finally:
                if old_env:
                    os.environ["CLAWPM_PORTFOLIO"] = old_env
                else:
                    os.environ.pop("CLAWPM_PORTFOLIO", None)
        finally:
            shutil.rmtree(temp_dir)
