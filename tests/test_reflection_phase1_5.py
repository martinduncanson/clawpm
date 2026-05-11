"""Tests for Reflection Layer — Phase 1.5 (applied-science predictions + recursive meta-reflection)."""

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
from clawpm.models import (
    Actuals,
    Predictions,
    SURPRISE_TAXONOMY,
    TaskComplexity,
    TaskState,
    WorkLogAction,
    WorkLogEntry,
)
from clawpm.reflect import write_reflection_event
from clawpm.tasks import add_task, change_task_state, get_task
from clawpm.worklog import add_entry


# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_reflect_phase1.py structure)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_p15_test_")
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
# 1. New Predictions fields round-trip through YAML write/read
# ---------------------------------------------------------------------------


class TestPhase15PredictionsRoundTrip:
    def test_success_criteria_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(
            success_criteria=["P95 latency <200ms", "test coverage >=80%"],
        )
        task = add_task(config, "test", "SC round-trip", predictions=predictions)
        assert task is not None

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.predictions.success_criteria == ["P95 latency <200ms", "test coverage >=80%"]

    def test_approach_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(approach="Drop-in JWT middleware, keep session table for refresh tokens")
        task = add_task(config, "test", "Approach round-trip", predictions=predictions)
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert "JWT middleware" in (reloaded.predictions.approach or "")

    def test_unknowns_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(unknowns="Whether refresh-token rotation gives audit traceability")
        task = add_task(config, "test", "Unknowns round-trip", predictions=predictions)
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert "audit traceability" in (reloaded.predictions.unknowns or "")

    def test_confidence_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(confidence=3)
        task = add_task(config, "test", "Confidence round-trip", predictions=predictions)
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.predictions.confidence == 3

    def test_reference_tasks_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(reference_tasks=["CLAWP-042", "ARB-P-013"])
        task = add_task(config, "test", "RefTasks round-trip", predictions=predictions)
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.predictions.reference_tasks == ["CLAWP-042", "ARB-P-013"]

    def test_pre_mortem_stored_and_reloaded(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(pre_mortem="cookie domain edge case in mobile webview")
        task = add_task(config, "test", "PreMortem round-trip", predictions=predictions)
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert "mobile webview" in (reloaded.predictions.pre_mortem or "")

    def test_all_new_fields_in_to_dict(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(
            success_criteria=["latency <200ms"],
            approach="JWT middleware",
            unknowns="rotation audit",
            confidence=4,
            reference_tasks=["T-001"],
            pre_mortem="mobile cookie bug",
        )
        task = add_task(config, "test", "AllFields dict", predictions=predictions)
        assert task is not None
        d = task.to_dict()["predictions"]
        assert d["success_criteria"] == ["latency <200ms"]
        assert d["approach"] == "JWT middleware"
        assert d["unknowns"] == "rotation audit"
        assert d["confidence"] == 4
        assert d["reference_tasks"] == ["T-001"]
        assert d["pre_mortem"] == "mobile cookie bug"

    def test_old_task_without_phase15_fields_loads_with_defaults(self, temp_portfolio):
        """Backward compat: tasks written without Phase 1.5 fields load without error."""
        config = temp_portfolio["config"]
        # Create a task using only Phase 1 predictions
        predictions = Predictions(duration_min=60, hypothesis="test hypothesis")
        task = add_task(config, "test", "Old-style task", predictions=predictions)
        assert task is not None

        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        # Phase 1.5 fields should default to None / empty list
        assert reloaded.predictions.success_criteria == []
        assert reloaded.predictions.approach is None
        assert reloaded.predictions.unknowns is None
        assert reloaded.predictions.confidence is None
        assert reloaded.predictions.reference_tasks == []
        assert reloaded.predictions.pre_mortem is None
        # Phase 1 fields should still work
        assert reloaded.predictions.duration_min == 60

    def test_is_empty_still_works_without_phase15_fields(self, temp_portfolio):
        pred = Predictions()
        assert pred.is_empty()

    def test_is_empty_false_when_success_criteria_set(self):
        pred = Predictions(success_criteria=["latency <200ms"])
        assert not pred.is_empty()

    def test_is_empty_false_when_confidence_set(self):
        pred = Predictions(confidence=2)
        assert not pred.is_empty()


# ---------------------------------------------------------------------------
# 2. CLI flags on tasks add — Phase 1.5 flags parsed and stored
# ---------------------------------------------------------------------------


class TestPhase15CLIAdd:
    def test_tasks_add_success_criteria(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI SC test",
                "--success-criteria", "P95 latency <200ms",
                "--success-criteria", "test coverage >=80%",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        pred = data["data"]["predictions"]
        assert "P95 latency <200ms" in pred["success_criteria"]
        assert "test coverage >=80%" in pred["success_criteria"]

    def test_tasks_add_predict_approach(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI approach test",
                "--predict-approach", "Drop-in JWT middleware",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["data"]["predictions"]["approach"] == "Drop-in JWT middleware"

    def test_tasks_add_unknowns(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI unknowns test",
                "--unknowns", "Whether rotation gives audit traceability",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert "audit traceability" in pred["unknowns"]

    def test_tasks_add_confidence_valid(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI confidence test",
                "--confidence", "3",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert pred["confidence"] == 3

    def test_tasks_add_confidence_zero_rejected(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Bad confidence",
                "--confidence", "0",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0 or "bad_confidence" in result.output

    def test_tasks_add_confidence_six_rejected(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Bad confidence high",
                "--confidence", "6",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0 or "bad_confidence" in result.output

    def test_tasks_add_reference_task(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI reference-task test",
                "--reference-task", "CLAWP-042",
                "--reference-task", "ARB-P-013",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert "CLAWP-042" in pred["reference_tasks"]
        assert "ARB-P-013" in pred["reference_tasks"]

    def test_tasks_add_pre_mortem(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "CLI pre-mortem test",
                "--pre-mortem", "cookie domain edge case in mobile webview",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert "mobile webview" in pred["pre_mortem"]

    def test_tasks_add_reference_task_nonexistent_accepted(self, temp_portfolio):
        """Non-existent reference task IDs are accepted (warn-only design)."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Nonexistent ref",
                "--reference-task", "NONEXISTENT-001",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert "NONEXISTENT-001" in pred["reference_tasks"]


# ---------------------------------------------------------------------------
# 3. CLI flags on tasks edit — Phase 1.5 flags update stored predictions
# ---------------------------------------------------------------------------


class TestPhase15CLIEdit:
    def test_tasks_edit_success_criteria(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit SC test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--success-criteria", "latency <100ms",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert "latency <100ms" in pred["success_criteria"]

    def test_tasks_edit_confidence(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit confidence test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--confidence", "5",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        pred = json.loads(result.output)["data"]["predictions"]
        assert pred["confidence"] == 5

    def test_tasks_edit_confidence_out_of_range_rejected(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Edit bad confidence")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "edit", task.id,
                "--confidence", "0",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0 or "bad_confidence" in result.output


# ---------------------------------------------------------------------------
# 4. Surprise taxonomy validation
# ---------------------------------------------------------------------------


class TestSurpriseTaxonomy:
    def test_taxonomy_constant_has_all_expected_tags(self):
        expected = {
            "unknown_unknown",
            "scope_drift",
            "dependency",
            "tooling_friction",
            "complexity_misread",
            "assumption_broke",
            "external_blocker",
        }
        assert expected == SURPRISE_TAXONOMY

    def test_valid_surprise_tag_accepted_on_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Surprise valid test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "state", task.id, "done",
                "--surprise", "tooling_friction",
                "--surprise", "assumption_broke",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert "tooling_friction" in record["surprise_taxonomy"]
        assert "assumption_broke" in record["surprise_taxonomy"]

    def test_invalid_surprise_tag_rejected(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Bad surprise tag test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "state", task.id, "done",
                "--surprise", "totally_made_up_tag",
                "--project", "test",
            ],
        )
        assert result.exit_code != 0 or "bad_surprise_tag" in result.output

    def test_surprise_tag_on_block_shortcut(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Block surprise test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "block", task.id,
                "--surprise", "external_blocker",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert "external_blocker" in record["surprise_taxonomy"]


# ---------------------------------------------------------------------------
# 5. Process lesson — recursive meta-loop
# ---------------------------------------------------------------------------


class TestProcessLesson:
    def test_process_lesson_stored_in_reflection_event(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Process lesson test")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "tasks", "state", task.id, "done",
                "--process-lesson", "When auth touches mobile, ALWAYS read the mobile test suite first",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert "mobile test suite" in record["process_lesson"]

    def test_process_lesson_via_done_shortcut(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "Done shortcut process lesson")
        assert task is not None

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "done", task.id,
                "--process-lesson", "billing module always needs 1.5x multiplier",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert "billing module" in record["process_lesson"]

    def test_process_lesson_none_when_not_provided(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "No process lesson")
        assert task is not None

        runner = CliRunner()
        runner.invoke(
            main,
            ["done", task.id, "--project", "test"],
        )

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert record["process_lesson"] is None

    def test_surprise_taxonomy_empty_list_when_not_provided(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test", "No surprise tags")
        assert task is not None

        runner = CliRunner()
        runner.invoke(
            main,
            ["done", task.id, "--project", "test"],
        )

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert record["surprise_taxonomy"] == []


# ---------------------------------------------------------------------------
# 6. Reflection JSONL contains Phase 1.5 fields when provided
# ---------------------------------------------------------------------------


class TestReflectionEventPhase15Fields:
    def test_write_reflection_event_includes_process_lesson_and_surprise(self, temp_portfolio):
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Full Phase 1.5 reflection")
        assert task is not None

        predictions = Predictions(
            duration_min=60,
            success_criteria=["latency <200ms"],
            confidence=3,
            reference_tasks=["REF-001"],
            pre_mortem="scope will creep",
        )

        ref_file = write_reflection_event(
            portfolio_root,
            event="task_done",
            task_id=task.id,
            project_id="test",
            predictions=predictions,
            actuals=Actuals(duration_min=90),
            process_lesson="I always underestimate auth tasks — add 1.5x next time",
            surprise_taxonomy=["tooling_friction", "complexity_misread"],
        )

        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())

        # Phase 1 fields still present
        assert record["predictions"]["duration_min"] == 60
        assert record["deltas"]["duration_ratio"] == pytest.approx(1.5, rel=0.01)

        # Phase 1.5 fields in predictions block
        assert record["predictions"]["success_criteria"] == ["latency <200ms"]
        assert record["predictions"]["confidence"] == 3
        assert record["predictions"]["reference_tasks"] == ["REF-001"]
        assert record["predictions"]["pre_mortem"] == "scope will creep"

        # Phase 1.5 reflection meta fields
        assert "1.5x" in record["process_lesson"]
        assert "tooling_friction" in record["surprise_taxonomy"]
        assert "complexity_misread" in record["surprise_taxonomy"]

    def test_old_reflection_without_phase15_fields_loads_without_error(self, temp_portfolio):
        """Backward compat: old JSONL records (pre-Phase 1.5) don't crash on load."""
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Old reflection compat")
        assert task is not None

        # Manually write an old-style reflection record (no process_lesson, no surprise_taxonomy)
        old_record = {
            "event": "task_done",
            "task_id": task.id,
            "project_id": "test",
            "occurred_at": "2025-01-01T12:00:00Z",
            "predictions": {"duration_min": 30, "complexity": None, "files_changed": None,
                           "files_scope": [], "frameworks": [], "pitfalls": None, "hypothesis": None},
            "actuals": {"duration_min": 45, "complexity": None, "files_changed": None, "files_touched": []},
            "deltas": {"duration_ratio": 1.5},
            "note": "old note",
            "meta_reflection": "old meta",
        }

        ref_dir = portfolio_root / "reflections"
        ref_dir.mkdir(exist_ok=True)
        ref_file = ref_dir / f"{task.id}.jsonl"
        ref_file.write_text(json.dumps(old_record) + "\n", encoding="utf-8")

        # Should load without KeyError
        loaded = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert loaded.get("process_lesson") is None  # key absent = None via .get()
        assert loaded.get("surprise_taxonomy") is None  # key absent = acceptable
        assert loaded["predictions"]["duration_min"] == 30

    def test_reflection_schema_includes_new_keys(self, temp_portfolio):
        """Reflection JSONL schema has process_lesson and surprise_taxonomy keys."""
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Schema check Phase 1.5")
        assert task is not None

        ref_file = write_reflection_event(
            portfolio_root,
            event="task_done",
            task_id=task.id,
            project_id="test",
            predictions=Predictions(),
            actuals=Actuals(),
        )
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert "process_lesson" in record
        assert "surprise_taxonomy" in record


# ---------------------------------------------------------------------------
# 7. Full applied-science loop integration test
# ---------------------------------------------------------------------------


class TestFullAppliedScienceLoop:
    def test_full_loop_add_start_done_with_all_phase15_flags(self, temp_portfolio):
        """End-to-end: add with Phase 1.5 flags, start, done with meta-loop flags."""
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        runner = CliRunner()

        # ADD with full Phase 1.5 framing
        result = runner.invoke(
            main,
            [
                "tasks", "add",
                "--title", "Migrate auth from sessions to JWT",
                "--predict-duration", "4h",
                "--predict-complexity", "m",
                "--hypothesis", "JWT cuts session-table contention by 50%",
                "--success-criteria", "P95 login latency <200ms",
                "--success-criteria", "Session table writes drop >=50%",
                "--predict-approach", "Drop-in JWT middleware, keep session table for refresh tokens",
                "--unknowns", "Whether refresh-token rotation gives audit-grade traceability",
                "--confidence", "3",
                "--reference-task", "CLAWP-042",
                "--pre-mortem", "cookie domain edge case in mobile webview",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output
        task_data = json.loads(result.output)["data"]
        task_id = task_data["id"]
        pred = task_data["predictions"]

        assert pred["confidence"] == 3
        assert "P95 login latency <200ms" in pred["success_criteria"]
        assert pred["approach"] == "Drop-in JWT middleware, keep session table for refresh tokens"
        assert "CLAWP-042" in pred["reference_tasks"]
        assert "mobile webview" in pred["pre_mortem"]

        # START
        result = runner.invoke(main, ["start", task_id, "--project", "test"])
        assert result.exit_code == 0, result.output

        # DONE with process-lesson and surprise
        result = runner.invoke(
            main,
            [
                "done", task_id,
                "--note", "Shipped; PR #128 merged",
                "--reflect-note", "constraint conflict didn't materialize but mobile webview cookie issue did",
                "--meta-reflect", "I should have read the mobile auth tests before predicting",
                "--process-lesson", "When auth touches mobile, ALWAYS read the mobile test suite before predicting duration",
                "--surprise", "tooling_friction",
                "--surprise", "assumption_broke",
                "--project", "test",
            ],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task_id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip().splitlines()[-1])

        assert record["event"] == "task_done"
        assert "mobile test suite" in record["process_lesson"]
        assert set(record["surprise_taxonomy"]) == {"tooling_friction", "assumption_broke"}
        assert "mobile webview" in record["note"]
        assert record["predictions"]["confidence"] == 3
        assert "CLAWP-042" in record["predictions"]["reference_tasks"]
