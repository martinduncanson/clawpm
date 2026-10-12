"""CLAWP-112-003 (Codex PR #102 r2 P2) — a counted/measured 0 stays 0 in actuals.

Spec 2.1: zero is known, not missing. ``_compute_actuals`` must report a counted
0 (iterations, files_changed) as 0 and keep ``None`` only for "could not measure".
These tests exercise the real ``_compute_actuals`` (no stubbing) both directly and
through the ``transition`` close path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from clawpm.models import Predictions, WorkLogAction, WorkLogEntry
from clawpm.reflect import (
    _compute_actuals,
    _compute_deltas,
    resolve_axes,
    write_iteration_event,
)
from clawpm.services.tasks import transition
from clawpm.tasks import add_task


def _entry(task_id: str, files):
    return WorkLogEntry(
        ts=datetime.now(timezone.utc),
        project="test",
        action=WorkLogAction.PROGRESS,
        task=task_id,
        files_changed=files,
    )


def _record(root, task_id: str) -> dict:
    ref_file = root / "reflections" / f"{task_id}.jsonl"
    return json.loads(ref_file.read_text(encoding="utf-8").strip().splitlines()[-1])


@pytest.fixture
def captured_axes(monkeypatch):
    """Record what resolve_axes returns on the real close path."""
    seen: list[dict] = []

    def _wrap(*a, **k):
        out = resolve_axes(*a, **k)
        seen.append(out)
        return out

    monkeypatch.setattr("clawpm.reflect.resolve_axes", _wrap)
    return seen


class TestComputeActualsZero:
    def test_zero_counted_iterations_stay_zero(self, isolated_portfolio):
        actuals = _compute_actuals(
            "TEST-001", log_entries=[], portfolio_root=isolated_portfolio.root
        )
        assert actuals.iterations == 0

    def test_iterations_none_without_portfolio(self):
        assert _compute_actuals("TEST-001", log_entries=[]).iterations is None

    def test_other_projects_iterations_do_not_count(self, isolated_portfolio):
        root = isolated_portfolio.root
        write_iteration_event(root, "TEST-001", "other", True, "x")
        actuals = _compute_actuals(
            "TEST-001", log_entries=[], portfolio_root=root, project_id="test"
        )
        assert actuals.iterations == 0

    def test_measured_empty_files_list_is_zero(self):
        # filter_files_changed returns [] when every changed file was filtered
        # out: the diff WAS measured and found nothing. Not the same as unknown.
        actuals = _compute_actuals("T-1", [_entry("T-1", [])])
        assert actuals.files_changed == 0
        assert actuals.files_touched == []

    def test_unmeasured_files_stay_none(self):
        actuals = _compute_actuals("T-1", [_entry("T-1", None)])
        assert actuals.files_changed is None
        assert _compute_actuals("T-1", []).files_changed is None

    def test_other_tasks_empty_list_does_not_count_as_measured(self):
        actuals = _compute_actuals("T-1", [_entry("T-2", [])])
        assert actuals.files_changed is None

    def test_zero_files_yields_zero_ratio_not_none(self):
        actuals = _compute_actuals("T-1", [_entry("T-1", [])])
        deltas = _compute_deltas(Predictions(files_changed=3), actuals)
        assert deltas["files_changed_ratio"] == 0.0


class TestCloseResolvesZero:
    def test_zero_predicted_zero_counted_resolves_correct(
        self, isolated_portfolio, captured_axes
    ):
        p = isolated_portfolio
        task = add_task(
            p.config, "test", "t", predictions=Predictions(predicted_iterations=0)
        )
        res = transition(p.config, project_id="test", task_id=task.id, new_state="done")
        assert "reflection_errors" not in res
        assert _record(p.root, task.id)["actuals"]["iterations"] == 0
        assert captured_axes[-1]["iterations"] == "correct"
        assert captured_axes[-1]["held"] == "correct"

    def test_zero_predicted_one_counted_resolves_incorrect(
        self, isolated_portfolio, captured_axes
    ):
        p = isolated_portfolio
        task = add_task(
            p.config, "test", "t", predictions=Predictions(predicted_iterations=0)
        )
        write_iteration_event(p.root, task.id, "test", True, "once")
        transition(p.config, project_id="test", task_id=task.id, new_state="done")
        assert captured_axes[-1]["held"] == "incorrect"
