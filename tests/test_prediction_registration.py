"""Tests for CLAWP-112-001 — prediction pre-registration.

Success criteria coverage:
  SC1: prediction_id minted + prediction_registered event, per write site.
       There are FOUR write sites, not three — the task's own pre-mortem
       ("three write sites means one gets missed") flagged this exact risk;
       reading tasks.py surfaced a fourth (add_subtask, reached via
       `tasks add --parent`) the planning pass didn't know about. All four
       get their own dedicated test:
         - tasks.add_task            (TestAddTaskRegistersPrediction)
         - tasks.add_subtask         (TestAddSubtaskRegistersPrediction)
         - tasks.edit_task first-set (TestEditTaskFirstWriteRegisters) —
           there is no separate `tasks predict` command in this codebase;
           `tasks edit --predict-*` is the only way to set predictions on an
           existing task, so it stands in for the task spec's "tasks predict".
         - emit_tree.emit_tree       (TestEmitTreeRegistersPrediction)
  SC2: tasks.edit_task revision appends prediction_revised with the full new
       snapshot, and preserves fields untouched by the edit — this is the
       CLAWP-108 wholesale-replace bug (tracked separately in
       .project/tasks/CLAWP-108.md), fixed here as required by the spec
       (calibration-metrics-spec.md §2.5: "this also forces the CLAWP-108
       ... bug to be fixed or fenced in the same PR").
  SC3: task_done/task_blocked carry prediction_id; legacy events (no
       prediction_id) resolve as legacy, registered at the task's `created`.
  SC4: closure = resolved_non_voided / registered, with open_predictions.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.emit_tree import parse_emit_document, emit_tree
from clawpm.models import Actuals, Predictions, TaskComplexity, TaskState
from clawpm.reflect import (
    compute_closure,
    resolve_prediction_registration,
    write_reflection_event,
)
from clawpm.tasks import add_subtask, add_task, change_task_state, edit_task, get_task


# ---------------------------------------------------------------------------
# Shared fixture (mirrors tests/test_reflect_phase1.py's temp_portfolio)
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_predreg_test_")
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
    (tasks_dir / "rejected").mkdir()

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


def _reflection_events(portfolio_root: Path, task_id: str) -> list[dict]:
    ref_file = portfolio_root / "reflections" / f"{task_id}.jsonl"
    if not ref_file.exists():
        return []
    return [
        json.loads(line)
        for line in ref_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# SC1 — one test per write site
# ---------------------------------------------------------------------------


class TestAddTaskRegistersPrediction:
    def test_add_task_mints_id_and_registers(self, temp_portfolio):
        config = temp_portfolio["config"]
        predictions = Predictions(duration_min=90, confidence=3, approach="do the thing")

        task = add_task(config, "test", "Some task", predictions=predictions)

        assert task is not None
        assert task.predictions.prediction_id, "prediction_id must be minted into frontmatter"

        events = _reflection_events(temp_portfolio["root"], task.id)
        registered = [e for e in events if e["event"] == "prediction_registered"]
        assert len(registered) == 1
        rec = registered[0]
        assert rec["prediction_id"] == task.predictions.prediction_id
        assert rec["task_id"] == task.id
        assert rec["project_id"] == "test"
        assert rec["registered_at"]
        assert rec["predictions"]["duration_min"] == 90
        assert rec["predictions"]["prediction_id"] == task.predictions.prediction_id
        assert rec["baseline_ref"]

    def test_add_task_without_predictions_writes_no_event(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Plain task with no predictions")
        assert task is not None
        assert task.predictions.is_empty()
        assert task.predictions.prediction_id is None
        assert _reflection_events(temp_portfolio["root"], task.id) == []


class TestAddSubtaskRegistersPrediction:
    def test_add_subtask_mints_id_and_registers(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test", "Parent task")
        assert parent is not None
        predictions = Predictions(duration_min=45, confidence=2)

        sub = add_subtask(config, "test", parent.id, "Child task", predictions=predictions)

        assert sub is not None
        assert sub.predictions.prediction_id

        events = _reflection_events(temp_portfolio["root"], sub.id)
        registered = [e for e in events if e["event"] == "prediction_registered"]
        assert len(registered) == 1
        assert registered[0]["prediction_id"] == sub.predictions.prediction_id
        assert registered[0]["task_id"] == sub.id
        assert registered[0]["predictions"]["duration_min"] == 45


class TestEditTaskFirstWriteRegisters:
    def test_edit_task_first_predictions_registers(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "No predictions yet")
        assert task is not None
        assert task.predictions.is_empty()

        edited = edit_task(
            config,
            "test",
            task.id,
            predictions=Predictions(confidence=4, approach="edit-time predict"),
        )

        assert edited is not None
        assert edited.predictions.prediction_id

        events = _reflection_events(temp_portfolio["root"], task.id)
        registered = [e for e in events if e["event"] == "prediction_registered"]
        revised = [e for e in events if e["event"] == "prediction_revised"]
        assert len(registered) == 1, "first predictions write must be a registration, not a revision"
        assert not revised
        assert registered[0]["prediction_id"] == edited.predictions.prediction_id
        assert registered[0]["predictions"]["approach"] == "edit-time predict"


class TestEmitTreeRegistersPrediction:
    def test_emit_tree_leaf_and_root_register(self, temp_portfolio):
        config = temp_portfolio["config"]
        raw = {
            "schema_version": 1,
            "root": {
                "title": "Root with predictions",
                "predictions": {"duration_min": 300, "confidence": 3},
            },
            "leaves": [
                {
                    "ref": "L1",
                    "parent_ref": None,
                    "title": "Leaf with predictions",
                    "leaf_key": "predreg-emit-L1",
                    "predictions": {"duration_min": 60, "confidence": 4},
                },
                {
                    "ref": "L2",
                    "parent_ref": None,
                    "title": "Leaf with no predictions",
                    "leaf_key": "predreg-emit-L2",
                },
            ],
        }
        doc = parse_emit_document(raw)
        result = emit_tree(config, "test", doc)

        assert not result.dry_run
        root_id = result.root_id

        root_task = get_task(config, "test", root_id)
        leaf1_id = f"{root_id}-001"
        leaf2_id = f"{root_id}-002"
        leaf1_task = get_task(config, "test", leaf1_id)
        leaf2_task = get_task(config, "test", leaf2_id)

        assert root_task.predictions.prediction_id
        assert leaf1_task.predictions.prediction_id
        assert leaf2_task.predictions.is_empty()

        root_events = _reflection_events(temp_portfolio["root"], root_id)
        leaf1_events = _reflection_events(temp_portfolio["root"], leaf1_id)
        leaf2_events = _reflection_events(temp_portfolio["root"], leaf2_id)

        root_registered = [e for e in root_events if e["event"] == "prediction_registered"]
        leaf1_registered = [e for e in leaf1_events if e["event"] == "prediction_registered"]

        assert len(root_registered) == 1
        assert root_registered[0]["prediction_id"] == root_task.predictions.prediction_id
        assert root_registered[0]["predictions"]["duration_min"] == 300

        assert len(leaf1_registered) == 1
        assert leaf1_registered[0]["prediction_id"] == leaf1_task.predictions.prediction_id
        assert leaf1_registered[0]["predictions"]["duration_min"] == 60

        # Leaf with no predictions block gets no event at all.
        assert leaf2_events == []


# ---------------------------------------------------------------------------
# SC2 — edit revision: full snapshot + CLAWP-108 field preservation
# ---------------------------------------------------------------------------


class TestEditTaskRevisionPreservesFields:
    def test_revision_appends_full_snapshot_and_preserves_unrelated_fields(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config,
            "test",
            "Task with rich predictions",
            predictions=Predictions(
                duration_min=120,
                complexity=TaskComplexity.M,
                confidence=3,
                pre_mortem="might hit a rate limit",
                files_scope=["src/foo/**"],
                filled_by="operator",
            ),
        )
        assert task is not None
        original_prediction_id = task.predictions.prediction_id
        assert original_prediction_id

        # CLAWP-108: editing ONLY --hypothesis must not null duration/
        # complexity/confidence/pre_mortem/scope/filled_by. Without the
        # merge-not-replace fix, this reproduces the bug: edit_task would
        # wholesale-replace the predictions block with a fresh Predictions()
        # carrying only `hypothesis`, dropping everything else silently.
        edited = edit_task(
            config,
            "test",
            task.id,
            predictions=Predictions(hypothesis="if we cache, latency drops"),
        )

        assert edited is not None
        assert edited.predictions.hypothesis == "if we cache, latency drops"
        # Unrelated fields preserved — this is the CLAWP-108 fix under test.
        assert edited.predictions.duration_min == 120
        assert edited.predictions.complexity == TaskComplexity.M
        assert edited.predictions.confidence == 3
        assert edited.predictions.pre_mortem == "might hit a rate limit"
        assert edited.predictions.files_scope == ["src/foo/**"]
        assert edited.predictions.filled_by == "operator"
        # prediction_id is carried forward, never re-minted on revision.
        assert edited.predictions.prediction_id == original_prediction_id

        events = _reflection_events(temp_portfolio["root"], task.id)
        registered = [e for e in events if e["event"] == "prediction_registered"]
        revised = [e for e in events if e["event"] == "prediction_revised"]
        assert len(registered) == 1
        assert len(revised) == 1

        rev = revised[0]
        assert rev["prediction_id"] == original_prediction_id
        assert rev["revised_at"]
        # The revision event carries the FULL new snapshot, not a diff.
        assert rev["predictions"]["hypothesis"] == "if we cache, latency drops"
        assert rev["predictions"]["duration_min"] == 120
        assert rev["predictions"]["complexity"] == "m"
        assert rev["predictions"]["confidence"] == 3
        assert rev["predictions"]["pre_mortem"] == "might hit a rate limit"
        assert rev["predictions"]["files_scope"] == ["src/foo/**"]
        assert rev["predictions"]["filled_by"] == "operator"

    def test_second_edit_appends_second_revision_not_a_new_registration(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", "Task", predictions=Predictions(duration_min=30, confidence=2)
        )
        edit_task(config, "test", task.id, predictions=Predictions(confidence=4))
        edit_task(config, "test", task.id, predictions=Predictions(duration_min=45))

        events = _reflection_events(temp_portfolio["root"], task.id)
        registered = [e for e in events if e["event"] == "prediction_registered"]
        revised = [e for e in events if e["event"] == "prediction_revised"]
        assert len(registered) == 1
        assert len(revised) == 2
        # Both revisions carry the same prediction_id as the original registration.
        pid = registered[0]["prediction_id"]
        assert all(e["prediction_id"] == pid for e in revised)
        # Latest revision's snapshot has both edits merged in.
        assert revised[-1]["predictions"]["confidence"] == 4
        assert revised[-1]["predictions"]["duration_min"] == 45


# ---------------------------------------------------------------------------
# SC3 — task_done/task_blocked carry prediction_id; legacy reader
# ---------------------------------------------------------------------------


class TestDoneEventCarriesPredictionId:
    def test_task_done_event_carries_prediction_id(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", "Task to complete", predictions=Predictions(duration_min=30)
        )
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.PROGRESS)
        write_reflection_event(
            temp_portfolio["root"],
            event="task_done",
            task_id=task.id,
            project_id="test",
            predictions=task.predictions,
            actuals=Actuals(duration_min=25),
        )

        events = _reflection_events(temp_portfolio["root"], task.id)
        done = [e for e in events if e["event"] == "task_done"]
        assert len(done) == 1
        assert done[0]["prediction_id"] == task.predictions.prediction_id


class TestResolvePredictionRegistration:
    def test_new_style_event_is_not_legacy(self):
        record = {"event": "task_done", "prediction_id": "abc123", "task_id": "T-1"}
        result = resolve_prediction_registration(record, task_created="2026-01-01")
        assert result == {"prediction_id": "abc123", "legacy": False, "registered_at": None}

    def test_legacy_event_without_prediction_id_resolves_at_created(self):
        record = {"event": "task_done", "task_id": "T-2"}
        result = resolve_prediction_registration(record, task_created="2026-01-01")
        assert result == {"prediction_id": None, "legacy": True, "registered_at": "2026-01-01"}

    def test_mixed_fixture_batch(self):
        records = [
            {"event": "task_done", "prediction_id": "p1", "task_id": "T-1"},
            {"event": "task_done", "task_id": "T-2"},  # legacy
            {"event": "task_blocked", "prediction_id": "p3", "task_id": "T-3"},
            {"event": "task_blocked", "task_id": "T-4"},  # legacy
        ]
        created_lookup = {"T-1": "2026-01-01", "T-2": "2026-02-01", "T-3": "2026-03-01", "T-4": "2026-04-01"}
        resolved = [
            resolve_prediction_registration(r, task_created=created_lookup[r["task_id"]])
            for r in records
        ]
        assert [r["legacy"] for r in resolved] == [False, True, False, True]
        assert resolved[1]["registered_at"] == "2026-02-01"
        assert resolved[3]["registered_at"] == "2026-04-01"
        assert resolved[0]["registered_at"] is None
        assert resolved[0]["prediction_id"] == "p1"


# ---------------------------------------------------------------------------
# SC4 — closure
# ---------------------------------------------------------------------------


class TestComputeClosure:
    def test_closure_matches_spec_example(self):
        result = compute_closure(n_registered=10, n_resolved_non_voided=6)
        assert result["closure"] == 0.6
        assert result["open_predictions"] == 4
        assert result["registered"] == 10
        assert result["resolved"] == 6

    def test_closure_accounts_for_voided(self):
        result = compute_closure(n_registered=10, n_resolved_non_voided=6, n_voided=2)
        assert result["closure"] == 0.6
        assert result["open_predictions"] == 2
        assert result["void_rate"] == 0.2

    def test_closure_no_registered_is_insufficient_data(self):
        result = compute_closure(n_registered=0, n_resolved_non_voided=0)
        assert result["closure"] is None
        assert result["insufficient_data"] is True
        assert result["open_predictions"] == 0
