"""Tests for Reflection Layer — Phase 1 (predictions, actuals, deltas, JSONL events)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Predictions, TaskComplexity, TaskState, WorkLogAction, WorkLogEntry
from clawpm.reflect import _compute_actuals, _compute_deltas, write_reflection_event
from clawpm.tasks import add_task, change_task_state, edit_task, get_task
from clawpm.worklog import add_entry, read_entries


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_reflect_test_")
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

    # Empty work log
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
# 1. Predictions round-trip through YAML
# ---------------------------------------------------------------------------


class TestPredictionsRoundTrip:
    def test_predictions_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(
            duration_min=90,
            complexity=TaskComplexity.M,
            files_changed=5,
            files_scope=["src/auth/**", "tests/auth/**"],
            frameworks=["fastapi", "pyjwt"],
            pitfalls="session token storage may need DB migration",
            hypothesis="moving to JWT will reduce session table contention by 80%",
        )
        task = add_task(config, "test", "Auth refactor", predictions=predictions)
        assert task is not None

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        pred = reloaded.predictions
        assert pred.duration_min == 90
        assert pred.complexity == TaskComplexity.M
        assert pred.files_changed == 5
        assert pred.files_scope == ["src/auth/**", "tests/auth/**"]
        assert pred.frameworks == ["fastapi", "pyjwt"]
        assert "session token" in (pred.pitfalls or "")
        assert "JWT" in (pred.hypothesis or "")

    def test_task_without_predictions_has_empty_defaults(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "No predictions task")
        assert task is not None

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        pred = reloaded.predictions
        assert pred.is_empty()
        assert pred.duration_min is None
        assert pred.complexity is None
        assert pred.files_scope == []
        assert pred.frameworks == []

    def test_predictions_nested_not_polluting_toplevel(self, temp_portfolio):
        """Predictions must live under a 'predictions:' key, not at YAML root."""
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=30, pitfalls="test pitfall")
        task = add_task(config, "test", "Nested check", predictions=predictions)
        assert task is not None and task.file_path

        raw_text = task.file_path.read_text()
        # The top-level YAML should have a 'predictions' block, not duration_min
        # directly at the top level
        assert "predictions:" in raw_text
        assert "duration_min: 30" in raw_text
        # Ensure duration_min is NOT a root-level key (it should be indented)
        import re
        root_keys = re.findall(r"^(\w+):", raw_text, re.MULTILINE)
        assert "duration_min" not in root_keys

    def test_predictions_to_dict_shape(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=60, complexity=TaskComplexity.L)
        task = add_task(config, "test", "Dict shape test", predictions=predictions)
        assert task is not None
        d = task.to_dict()
        assert "predictions" in d
        pred_d = d["predictions"]
        assert pred_d["duration_min"] == 60
        assert pred_d["complexity"] == "l"
        assert pred_d["files_scope"] == []


# ---------------------------------------------------------------------------
# 2. CLI: tasks add --predict-* writes predictions
# ---------------------------------------------------------------------------


class TestPredictCLIAdd:
    def test_tasks_add_predict_flags(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Refactor auth",
                "--predict-duration", "90",
                "--predict-complexity", "m",
                "--predict-files-changed", "5",
                "--predict-scope", "src/auth/**",
                "--predict-scope", "tests/auth/**",
                "--predict-frameworks", "fastapi",
                "--predict-frameworks", "pyjwt",
                "--predict-pitfalls", "session token storage may need DB migration",
                "--hypothesis", "moving to JWT will reduce session table contention by 80%",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        pred = data["data"]["predictions"]
        assert pred["duration_min"] == 90
        assert pred["complexity"] == "m"
        assert pred["files_changed"] == 5
        assert "src/auth/**" in pred["files_scope"]
        assert "fastapi" in pred["frameworks"]
        assert "session token" in pred["pitfalls"]
        assert "JWT" in pred["hypothesis"]

    def test_tasks_add_no_predict_flags_ok(self, temp_portfolio):
        """No --predict-* flags should work fine (all None / empty)."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["tasks", "add", "--title", "Simple task", "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        pred = data["data"]["predictions"]
        assert pred["duration_min"] is None
        assert pred["complexity"] is None
        assert pred["files_scope"] == []

    def test_tasks_show_includes_predictions_json(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=45, hypothesis="test hypothesis")
        task = add_task(config, "test", "Show test", predictions=predictions)
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(main, ["tasks", "show", task.id, "--project", "test"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "predictions" in data
        assert data["predictions"]["duration_min"] == 45
        assert data["predictions"]["hypothesis"] == "test hypothesis"


# ---------------------------------------------------------------------------
# 3. CLI: tasks edit --predict-* updates predictions
# ---------------------------------------------------------------------------


class TestPredictCLIEdit:
    def test_tasks_edit_predict_flags(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit predict test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--predict-duration", "120",
                "--predict-complexity", "l",
                "--predict-scope", "src/new/**",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        pred = data["data"]["predictions"]
        assert pred["duration_min"] == 120
        assert pred["complexity"] == "l"
        assert "src/new/**" in pred["files_scope"]

    def test_tasks_edit_updates_predictions_on_disk(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Persist predict test")
        assert task is not None

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--predict-duration", "60",
                "--project", "test",
            ],
        )

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.predictions.duration_min == 60


# ---------------------------------------------------------------------------
# 4. Actuals computed from work_log on task completion
# ---------------------------------------------------------------------------


class TestActualsComputation:
    def _make_log_entry(self, project: str, task_id: str, action: WorkLogAction, files=None, ts=None):
        return WorkLogEntry(
            ts=ts or datetime.now(timezone.utc),
            project=project,
            action=action,
            task=task_id,
            files_changed=files,
        )

    def test_duration_from_start_to_now(self, temp_portfolio):
        from datetime import timedelta
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Duration test")
        assert task is not None

        # Inject a start entry 30 minutes ago
        start_ts = datetime.now(timezone.utc) - timedelta(minutes=30)
        entries = [
            self._make_log_entry("test", task.id, WorkLogAction.START, ts=start_ts),
        ]
        actuals = _compute_actuals(task.id, None, entries)
        # Should be approximately 30 minutes (allow ±2 min slop)
        assert actuals.duration_min is not None
        assert 28 <= actuals.duration_min <= 32

    def test_no_start_entry_gives_none_duration(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "No start entry")
        assert task is not None

        entries = [
            self._make_log_entry("test", task.id, WorkLogAction.PROGRESS),
        ]
        actuals = _compute_actuals(task.id, None, entries)
        assert actuals.duration_min is None

    def test_files_touched_deduped(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Files dedup test")
        assert task is not None

        entries = [
            self._make_log_entry("test", task.id, WorkLogAction.PROGRESS,
                                 files=["src/a.py", "src/b.py"]),
            self._make_log_entry("test", task.id, WorkLogAction.DONE,
                                 files=["src/b.py", "src/c.py"]),
        ]
        actuals = _compute_actuals(task.id, None, entries)
        # src/b.py appears twice but should be deduped
        assert actuals.files_touched == sorted(["src/a.py", "src/b.py", "src/c.py"])
        assert actuals.files_changed == 3

    def test_complexity_taken_from_task_field(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Complexity test", complexity=TaskComplexity.L)
        assert task is not None

        actuals = _compute_actuals(task.id, TaskComplexity.L, [])
        assert actuals.complexity == TaskComplexity.L


# ---------------------------------------------------------------------------
# 5. Delta computation
# ---------------------------------------------------------------------------


class TestDeltaComputation:
    def test_duration_ratio(self):
        predictions = Predictions(duration_min=100)
        from clawpm.models import Actuals
        actuals = Actuals(duration_min=185)
        deltas = _compute_deltas(predictions, actuals)
        assert deltas["duration_ratio"] == pytest.approx(1.85, rel=0.01)

    def test_files_changed_ratio(self):
        predictions = Predictions(files_changed=10)
        from clawpm.models import Actuals
        actuals = Actuals(files_changed=6)
        deltas = _compute_deltas(predictions, actuals)
        assert deltas["files_changed_ratio"] == pytest.approx(0.6, rel=0.01)

    def test_scope_overrun_and_unused(self):
        predictions = Predictions(
            files_scope=["src/auth/**", "tests/auth/**"],
        )
        from clawpm.models import Actuals
        actuals = Actuals(
            files_touched=["src/auth/login.py", "src/billing/invoice.py"],
        )
        deltas = _compute_deltas(predictions, actuals)
        assert "src/billing/invoice.py" in deltas["files_scope_overrun"]
        assert "tests/auth/**" in deltas["files_scope_unused"]
        assert "src/auth/login.py" not in deltas["files_scope_overrun"]

    def test_complexity_match(self):
        predictions = Predictions(complexity=TaskComplexity.M)
        from clawpm.models import Actuals
        actuals = Actuals(complexity=TaskComplexity.L)
        deltas = _compute_deltas(predictions, actuals)
        assert deltas["complexity_match"] is False
        assert deltas["complexity_predicted"] == "m"
        assert deltas["complexity_actual"] == "l"

    def test_complexity_match_true(self):
        predictions = Predictions(complexity=TaskComplexity.S)
        from clawpm.models import Actuals
        actuals = Actuals(complexity=TaskComplexity.S)
        deltas = _compute_deltas(predictions, actuals)
        assert deltas["complexity_match"] is True

    def test_duration_ratio_none_when_no_prediction(self):
        predictions = Predictions()  # no duration set
        from clawpm.models import Actuals
        actuals = Actuals(duration_min=60)
        deltas = _compute_deltas(predictions, actuals)
        assert deltas["duration_ratio"] is None


# ---------------------------------------------------------------------------
# 6. Reflection event written to ~/clawpm/reflections/<task-id>.jsonl
# ---------------------------------------------------------------------------


class TestReflectionEvent:
    def test_write_reflection_event_creates_file(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Reflection file test")
        assert task is not None

        from clawpm.models import Actuals
        actuals = Actuals(duration_min=45)
        ref_file = write_reflection_event(
            portfolio_root,
            event="task_done",
            task_id=task.id,
            project_id="test",
            predictions=Predictions(duration_min=30),
            actuals=actuals,
            note="it took longer than expected",
            meta_reflection="should have accounted for test setup time",
        )

        assert ref_file.exists()
        lines = [l for l in ref_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])

        assert record["event"] == "task_done"
        assert record["task_id"] == task.id
        assert record["project_id"] == "test"
        assert record["predictions"]["duration_min"] == 30
        assert record["actuals"]["duration_min"] == 45
        assert record["deltas"]["duration_ratio"] == pytest.approx(1.5, rel=0.01)
        assert record["note"] == "it took longer than expected"
        assert "accounted for test setup time" in record["meta_reflection"]

    def test_reflection_file_appends_on_multiple_events(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Multi-event test")
        assert task is not None

        from clawpm.models import Actuals
        for _ in range(3):
            write_reflection_event(
                portfolio_root,
                event="task_done",
                task_id=task.id,
                project_id="test",
                predictions=Predictions(),
                actuals=Actuals(),
            )

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        lines = [l for l in ref_file.read_text().splitlines() if l.strip()]
        assert len(lines) == 3

    def test_reflection_schema_has_all_required_keys(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Schema test")
        assert task is not None

        from clawpm.models import Actuals
        ref_file = write_reflection_event(
            portfolio_root,
            event="task_blocked",
            task_id=task.id,
            project_id="test",
            predictions=Predictions(),
            actuals=Actuals(),
        )
        record = json.loads(ref_file.read_text().strip())
        required_keys = {
            "event", "task_id", "project_id", "occurred_at",
            "predictions", "actuals", "deltas", "note", "meta_reflection",
        }
        assert required_keys.issubset(record.keys())

    def test_reflections_dir_created_automatically(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        ref_dir = portfolio_root / "reflections"
        assert not ref_dir.exists()

        config = temp_portfolio["config"]
        task = add_task(config, "test", "Auto-dir test")
        assert task is not None

        from clawpm.models import Actuals
        write_reflection_event(
            portfolio_root, "task_done", task.id, "test",
            Predictions(), Actuals(),
        )
        assert ref_dir.exists()


# ---------------------------------------------------------------------------
# 7. tasks state done writes reflection event via CLI
# ---------------------------------------------------------------------------


class TestTasksStateDoneWritesReflection:
    def test_done_writes_reflection_jsonl(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]

        predictions = Predictions(duration_min=60, complexity=TaskComplexity.M)
        task = add_task(config, "test", "CLI done reflection", predictions=predictions)
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.PROGRESS)

        # Log a start entry so actuals duration can be computed
        add_entry(config, project="test", action=WorkLogAction.START, task=task.id,
                  summary="starting")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "state", task.id, "done",
                "--reflect-note", "took longer than expected",
                "--meta-reflect", "should have pre-checked schema",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text().strip().splitlines()[-1])
        assert record["event"] == "task_done"
        assert record["note"] == "took longer than expected"
        assert "pre-checked schema" in record["meta_reflection"]

    def test_blocked_writes_task_blocked_event(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]

        task = add_task(config, "test", "CLI block reflection")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "state", task.id, "blocked",
                "--reflect-note", "hit API rate limit",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text().strip().splitlines()[-1])
        assert record["event"] == "task_blocked"
        assert record["note"] == "hit API rate limit"

    def test_reflect_note_and_meta_reflect_stored_correctly(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Note storage test")
        assert task is not None

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "tasks", "state", task.id, "done",
                "--reflect-note", "unexpected complexity",
                "--meta-reflect", "could have anticipated the DB schema gap",
                "--project", "test",
            ],
        )

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        record = json.loads(ref_file.read_text().strip().splitlines()[-1])
        assert record["note"] == "unexpected complexity"
        assert "DB schema gap" in record["meta_reflection"]


# ---------------------------------------------------------------------------
# 8. clawpm done / clawpm block shortcuts pass flags through
# ---------------------------------------------------------------------------


class TestShortcutCommandsPassFlags:
    def test_done_shortcut_reflect_note(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Done shortcut test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "done", task.id,
                "--reflect-note", "shortcut note",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text().strip())
        assert record["note"] == "shortcut note"

    def test_block_shortcut_reflect_note(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Block shortcut test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "block", task.id,
                "--reflect-note", "block note",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text().strip())
        assert record["event"] == "task_blocked"
        assert record["note"] == "block note"


# ---------------------------------------------------------------------------
# 9. Phase 2 stubs return phase2_pending and exit 0
# ---------------------------------------------------------------------------


class TestPhase2Stubs:
    def test_reflect_summarize_stub(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "summarize", "--project", "test"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "phase2_pending"

    def test_reflect_suggest_stub(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "suggest", "TEST-001", "--project", "test"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "phase2_pending"

    def test_reflect_history_import_no_source(self, temp_portfolio):
        # Updated: history-import is now implemented (Phase 2 complete) — see
        # tests/test_history.py for full coverage. This test locks the no-source
        # error path.
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "history-import"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "no_source"

    def test_reflect_history_import_missing_source_dir(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["reflect", "history-import", "--source", "/some/dir/that/is/not/real"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "source_not_found"
