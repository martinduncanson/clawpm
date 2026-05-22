"""Tests for outcome iteration log (CLAWP-019)."""

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
from clawpm.models import Actuals, Predictions, SuccessCriterion, TaskState
from clawpm.reflect import (
    _compute_actuals,
    _compute_deltas,
    count_iterations_for_task,
    write_iteration_event,
)
from clawpm.tasks import add_task, change_task_state, get_task
from clawpm.worklog import add_entry
from clawpm.models import WorkLogAction


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_iter_test_")
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
# write_iteration_event + count_iterations_for_task
# ---------------------------------------------------------------------------


class TestIterationEvent:
    def test_write_creates_file(self, temp_portfolio):
        root = temp_portfolio["root"]
        path = write_iteration_event(
            root, "TEST-001", "test",
            verdict_ok=False, verdict_reason="missing X",
        )
        assert path.exists()
        line = json.loads(path.read_text(encoding="utf-8").strip())
        assert line["event"] == "iteration_event"
        assert line["task_id"] == "TEST-001"
        assert line["verdict"]["ok"] is False
        assert line["verdict"]["reason"] == "missing X"
        assert line["verdict"]["impossible"] is False

    def test_count_iterations_zero_when_no_file(self, temp_portfolio):
        assert count_iterations_for_task(temp_portfolio["root"], "TEST-001") == 0

    def test_count_iterations_filters_by_project(self, temp_portfolio):
        """Codex round-6 P2: reflection JSONL is keyed by task_id alone,
        so two projects sharing a task_id write to the same file. Counter
        MUST filter by project_id to avoid polluting one project's
        iteration count with another's events."""
        root = temp_portfolio["root"]
        # Two projects, same task_id, 3 and 2 events respectively
        for _ in range(3):
            write_iteration_event(
                root, "SHARED-001", "proj_a",
                verdict_ok=False, verdict_reason="A",
            )
        for _ in range(2):
            write_iteration_event(
                root, "SHARED-001", "proj_b",
                verdict_ok=False, verdict_reason="B",
            )
        # Project-filtered counts return only the project's own events
        assert count_iterations_for_task(root, "SHARED-001", project_id="proj_a") == 3
        assert count_iterations_for_task(root, "SHARED-001", project_id="proj_b") == 2
        # Legacy unfiltered behaviour: counts every event in the file
        assert count_iterations_for_task(root, "SHARED-001") == 5

    def test_count_iterations_counts_only_iteration_events(self, temp_portfolio):
        root = temp_portfolio["root"]
        write_iteration_event(root, "TEST-001", "test", verdict_ok=False, verdict_reason="r1")
        write_iteration_event(root, "TEST-001", "test", verdict_ok=False, verdict_reason="r2")
        write_iteration_event(root, "TEST-001", "test", verdict_ok=True, verdict_reason="done")

        # Sneak a non-iteration line in
        ref_file = root / "reflections" / "TEST-001.jsonl"
        with open(ref_file, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event": "task_done", "task_id": "TEST-001"}) + "\n")

        assert count_iterations_for_task(root, "TEST-001") == 3

    def test_impossible_verdict_recorded(self, temp_portfolio):
        root = temp_portfolio["root"]
        write_iteration_event(
            root, "TEST-001", "test",
            verdict_ok=False, verdict_reason="no creds",
            verdict_impossible=True,
        )
        line = json.loads(
            (root / "reflections" / "TEST-001.jsonl")
            .read_text(encoding="utf-8").strip()
        )
        assert line["verdict"]["impossible"] is True


# ---------------------------------------------------------------------------
# _compute_actuals + deltas with iteration count
# ---------------------------------------------------------------------------


class TestActualsAndDeltas:
    def test_actuals_iterations_populated_from_reflection_jsonl(self, temp_portfolio):
        root = temp_portfolio["root"]
        # Seed 4 iteration events
        for i in range(4):
            write_iteration_event(
                root, "TEST-001", "test",
                verdict_ok=(i == 3),  # last one passes
                verdict_reason=f"iter {i}",
            )
        actuals = _compute_actuals(
            "TEST-001",
            task_complexity=None,
            log_entries=[],
            portfolio_root=root,
        )
        assert actuals.iterations == 4

    def test_actuals_iterations_none_when_no_jsonl(self, temp_portfolio):
        actuals = _compute_actuals(
            "TEST-001",
            task_complexity=None,
            log_entries=[],
            portfolio_root=temp_portfolio["root"],
        )
        assert actuals.iterations is None

    def test_actuals_iterations_none_without_portfolio_root(self, temp_portfolio):
        """Backwards compat: when called without portfolio_root, iterations stays None."""
        actuals = _compute_actuals(
            "TEST-001", task_complexity=None, log_entries=[],
        )
        assert actuals.iterations is None

    def test_deltas_iterations_ratio(self):
        p = Predictions(predicted_iterations=2)
        a = Actuals(iterations=5)
        d = _compute_deltas(p, a)
        assert d["iterations_predicted"] == 2
        assert d["iterations_actual"] == 5
        assert d["iterations_ratio"] == 2.5

    def test_deltas_iterations_ratio_none_when_no_prediction(self):
        p = Predictions()
        a = Actuals(iterations=5)
        d = _compute_deltas(p, a)
        assert d["iterations_ratio"] is None
        assert d["iterations_actual"] == 5


# ---------------------------------------------------------------------------
# Predictions backwards-compat with new field
# ---------------------------------------------------------------------------


class TestPredictionsField:
    def test_default_predicted_iterations_none(self):
        p = Predictions()
        assert p.predicted_iterations is None
        assert p.is_empty()

    def test_predictions_to_dict_includes_field(self):
        p = Predictions(predicted_iterations=3)
        d = p.to_dict()
        assert d["predicted_iterations"] == 3
        assert not p.is_empty()

    def test_predictions_round_trip(self):
        p = Predictions(predicted_iterations=2)
        d = p.to_dict()
        p2 = Predictions.from_dict(d)
        assert p2.predicted_iterations == 2


# ---------------------------------------------------------------------------
# Integration: eval-stop writes iteration events
# ---------------------------------------------------------------------------


class TestEvalStopWritesIteration:
    def test_eval_stop_writes_iteration_event(self, temp_portfolio, monkeypatch):
        from clawpm.judges import stop_condition as sc_mod

        def fake_invoker(prompt: str) -> str:
            return '{"ok": false, "reason": "evidence missing"}'

        monkeypatch.setattr(sc_mod, "_default_judge_invoker", fake_invoker)

        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="Iter",
            predictions=Predictions(
                success_criteria=["c1"],
                predicted_iterations=1,
            ),
        )

        transcript = temp_portfolio["root"] / "transcript.txt"
        transcript.write_text("FAILED", encoding="utf-8")

        # Invoke eval-stop twice (two iterations)
        for _ in range(2):
            r = CliRunner().invoke(
                main,
                ["-p", "test", "hook", "eval-stop", "--task", task.id,
                 "--transcript-file", str(transcript)],
            )
            assert r.exit_code == 0

        # Check the reflection file
        ref_file = temp_portfolio["root"] / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        events = [
            json.loads(line)
            for line in ref_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(events) == 2
        assert all(e["event"] == "iteration_event" for e in events)


class TestDoneWithIterationsActual:
    def test_done_surfaces_iterations_in_reflection(self, temp_portfolio, monkeypatch):
        """A task with predicted_iterations + observed iterations gets a delta event."""
        from clawpm.judges import stop_condition as sc_mod

        def fake_invoker(prompt: str) -> str:
            return '{"ok": false, "reason": "still working"}'

        monkeypatch.setattr(sc_mod, "_default_judge_invoker", fake_invoker)

        config = temp_portfolio["config"]
        task = add_task(
            config, "test", title="DoneIter",
            predictions=Predictions(
                success_criteria=["c1"],
                predicted_iterations=1,
            ),
        )
        change_task_state(config, "test", task.id, TaskState.PROGRESS)

        # Need a START work_log entry so the done flow has a duration anchor.
        # The change_task_state above doesn't write work_log (that's done by
        # cli.tasks_state), so simulate via add_entry:
        add_entry(config, project="test", action=WorkLogAction.START, task=task.id, auto=True)

        transcript = temp_portfolio["root"] / "transcript.txt"
        transcript.write_text("WIP", encoding="utf-8")

        # Fire 3 iterations
        for _ in range(3):
            CliRunner().invoke(
                main,
                ["-p", "test", "hook", "eval-stop", "--task", task.id,
                 "--transcript-file", str(transcript)],
            )

        # Now mark done via the CLI (so the done flow runs)
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "state", task.id, "done"]
        )
        assert r.exit_code == 0, r.output

        # Inspect the reflection JSONL — last line should be task_done with
        # iterations_actual = 3 in the deltas
        ref_file = temp_portfolio["root"] / "reflections" / f"{task.id}.jsonl"
        lines = [
            json.loads(line)
            for line in ref_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        done_events = [l for l in lines if l.get("event") == "task_done"]
        assert len(done_events) == 1
        ev = done_events[0]
        assert ev["deltas"]["iterations_actual"] == 3
        assert ev["deltas"]["iterations_predicted"] == 1
        assert ev["deltas"]["iterations_ratio"] == 3.0


# ---------------------------------------------------------------------------
# CLI flag: --predict-iterations
# ---------------------------------------------------------------------------


class TestEndToEndDispatchSpine:
    """Codex-review hardening: the headline integration loop —
    dispatch → eval-stop fires N times → done → auto-teardown + correct
    iterations_actual in reflection — exercised as one continuous flow."""

    def test_full_dispatch_spine(self, temp_portfolio, monkeypatch, tmp_path):
        from clawpm.judges import stop_condition as sc_mod
        from clawpm.dispatch import settings_path

        # Need a project with a real repo_path for dispatch to work
        # idiomatically; bolt it on via settings.toml edit.
        config = temp_portfolio["config"]
        project_dir = config.project_roots[0] / "test-project"
        repo_dir = tmp_path / "repo"
        repo_dir.mkdir()
        (project_dir / ".project" / "settings.toml").write_text(
            f'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
            f'repo_path = "{repo_dir.as_posix()}"\n'
        )

        verdicts = iter([
            '{"ok": false, "reason": "iter 1 - not yet"}',
            '{"ok": false, "reason": "iter 2 - still working"}',
            '{"ok": true, "reason": "iter 3 - rubric satisfied"}',
        ])
        def fake_invoker(prompt: str) -> str:
            return next(verdicts)

        monkeypatch.setattr(sc_mod, "_default_judge_invoker", fake_invoker)

        # 1. Add a task with predicted_iterations=1 (we'll observe 3)
        task = add_task(
            config, "test", title="Spine",
            predictions=Predictions(
                success_criteria=[SuccessCriterion(
                    criterion="lands cleanly",
                    gradeable_signal="judge ok",
                )],
                predicted_iterations=1,
            ),
        )

        # 2. Dispatch into repo dir
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "dispatch", task.id,
             "--target-dir", str(repo_dir)],
        )
        assert r.exit_code == 0, r.output
        assert settings_path(repo_dir).exists()

        # Pre-condition for done: a start log entry so duration is computed
        add_entry(config, project="test", action=WorkLogAction.START, task=task.id, auto=True)

        # 3. Simulate 3 Stop-hook fires
        transcript = tmp_path / "transcript.txt"
        transcript.write_text("subagent transcript", encoding="utf-8")
        for _ in range(3):
            r = CliRunner().invoke(
                main,
                ["-p", "test", "hook", "eval-stop",
                 "--task", task.id,
                 "--transcript-file", str(transcript)],
            )
            assert r.exit_code == 0, r.output

        # 4. Operator marks done — auto-teardown + iteration count appear
        r = CliRunner().invoke(
            main, ["-p", "test", "tasks", "state", task.id, "done"]
        )
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]

        # Settings file teardown happened
        assert "dispatch_teardowns" in data
        assert not settings_path(repo_dir).exists()

        # Reflection JSONL has 3 iteration_events + 1 task_done with
        # iterations_actual=3
        ref_file = temp_portfolio["root"] / "reflections" / f"{task.id}.jsonl"
        events = [
            json.loads(line)
            for line in ref_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        iter_events = [e for e in events if e["event"] == "iteration_event"]
        done_events = [e for e in events if e["event"] == "task_done"]
        assert len(iter_events) == 3
        assert len(done_events) == 1
        assert done_events[0]["deltas"]["iterations_actual"] == 3
        assert done_events[0]["deltas"]["iterations_predicted"] == 1
        assert done_events[0]["deltas"]["iterations_ratio"] == 3.0


class TestPredictIterationsFlag:
    def test_tasks_add_predict_iterations(self, temp_portfolio):
        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "add",
             "-t", "Iter", "--predict-iterations", "2"],
        )
        assert r.exit_code == 0, r.output
        tid = json.loads(r.output)["data"]["id"]

        r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "show", tid])
        pred = json.loads(r2.output)["predictions"]
        assert pred["predicted_iterations"] == 2

    def test_tasks_edit_predict_iterations(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", title="Edit me")

        r = CliRunner().invoke(
            main,
            ["-p", "test", "tasks", "edit", task.id,
             "--predict-iterations", "5"],
        )
        assert r.exit_code == 0, r.output

        r2 = CliRunner().invoke(main, ["-p", "test", "tasks", "show", task.id])
        pred = json.loads(r2.output)["predictions"]
        assert pred["predicted_iterations"] == 5
