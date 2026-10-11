"""CLAWP-112-003 — --miss-category on done, the six-value taxonomy, the
unclassified-miss nudge, and the per-axis outcome resolver (spec 2.1)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import (
    Actuals,
    MISS_CATEGORIES,
    Predictions,
    SURPRISE_TAXONOMY,
    TaskComplexity,
)
from clawpm.reflect import _compute_deltas, resolve_axes
from clawpm.tasks import add_task

SIX = ["wrong_signal", "wrong_weight", "missed_variable", "operator_override", "noise", "structural"]


def _axes(preds: Predictions, acts: Actuals, kind: str | None = None) -> dict:
    return resolve_axes(preds, acts, _compute_deltas(preds, acts), kind=kind)


def _record(root, task_id: str) -> dict:
    ref_file = root / "reflections" / f"{task_id}.jsonl"
    return json.loads(ref_file.read_text(encoding="utf-8").strip().splitlines()[-1])


@pytest.fixture
def stub_actuals(monkeypatch):
    """Pin the actuals the done path computes (it reads the work_log + clock)."""

    def _set(**kw):
        monkeypatch.setattr(
            "clawpm.reflect._compute_actuals",
            lambda *a, **k: Actuals(**kw),
        )

    return _set


def _done(args: list[str]):
    return CliRunner().invoke(main, args)


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


def test_miss_categories_is_the_six_values():
    assert MISS_CATEGORIES == frozenset(SIX)
    assert isinstance(MISS_CATEGORIES, frozenset)


# ---------------------------------------------------------------------------
# resolve_axes (spec 2.1)
# ---------------------------------------------------------------------------


class TestResolveAxes:
    def test_duration_band_edges_are_correct(self):
        lo = _axes(Predictions(duration_min=100), Actuals(active_min=50))
        hi = _axes(Predictions(duration_min=100), Actuals(active_min=200))
        assert lo["duration"] == "correct" and hi["duration"] == "correct"

    def test_duration_outside_band_is_incorrect(self):
        assert _axes(Predictions(duration_min=100), Actuals(active_min=49))["duration"] == "incorrect"
        assert _axes(Predictions(duration_min=100), Actuals(active_min=201))["duration"] == "incorrect"

    def test_duration_prefers_active_over_wallclock(self):
        axes = _axes(Predictions(duration_min=100), Actuals(active_min=90, duration_min=9000))
        assert axes["duration"] == "correct"
        assert axes["duration_source"] == "active"

    def test_duration_falls_back_to_wallclock_and_says_so(self):
        axes = _axes(Predictions(duration_min=100), Actuals(duration_min=90))
        assert axes["duration"] == "correct"
        assert axes["duration_source"] == "wallclock"

    def test_duration_unresolvable_when_either_side_missing_or_zero(self):
        assert _axes(Predictions(), Actuals(active_min=10))["duration"] == "unresolvable"
        assert _axes(Predictions(duration_min=10), Actuals())["duration"] == "unresolvable"
        assert _axes(Predictions(duration_min=10), Actuals(active_min=0))["duration"] == "unresolvable"

    def test_decision_kind_excludes_duration(self):
        axes = _axes(Predictions(duration_min=10), Actuals(active_min=5000), kind="decision")
        assert axes["duration"] == "unresolvable"

    def test_scope_axis(self):
        preds = Predictions(files_scope=["src/*"])
        ok = _axes(preds, Actuals(files_touched=["src/a.py"], files_changed=1))
        bad = _axes(preds, Actuals(files_touched=["docs/a.md"], files_changed=1))
        none = _axes(Predictions(), Actuals(files_touched=["x"], files_changed=1))
        assert (ok["scope"], bad["scope"], none["scope"]) == ("correct", "incorrect", "unresolvable")

    def test_iterations_axis(self):
        preds = Predictions(predicted_iterations=2)
        assert _axes(preds, Actuals(iterations=2))["iterations"] == "correct"
        assert _axes(preds, Actuals(iterations=3))["iterations"] == "incorrect"
        assert _axes(preds, Actuals())["iterations"] == "unresolvable"

    # A predicted 0 is a real prediction ("no revision cycles expected"), not a
    # missing one: only None means missing (Codex PR #102 P2).
    def test_iterations_zero_prediction_is_scored_not_skipped(self):
        preds = Predictions(predicted_iterations=0)
        assert _axes(preds, Actuals(iterations=1))["iterations"] == "incorrect"
        assert _axes(preds, Actuals(iterations=0))["iterations"] == "correct"
        assert _axes(preds, Actuals())["iterations"] == "unresolvable"

    def test_iterations_zero_actual_is_scored_against_a_positive_prediction(self):
        assert _axes(Predictions(predicted_iterations=2), Actuals(iterations=0))["iterations"] == "correct"

    def test_iterations_zero_prediction_miss_makes_held_incorrect(self):
        assert _axes(Predictions(predicted_iterations=0), Actuals(iterations=1))["held"] == "incorrect"

    def test_zero_denominator_ratios_degrade_to_none_not_error(self):
        # duration/files_changed/iterations predicted as 0 can't form a ratio;
        # the ratio is None while the raw values still ride along.
        preds = Predictions(duration_min=0, files_changed=0, predicted_iterations=0)
        acts = Actuals(duration_min=5, files_changed=2, iterations=1)
        deltas = _compute_deltas(preds, acts)
        assert deltas["duration_ratio"] is None
        assert deltas["files_changed_ratio"] is None
        assert deltas["iterations_ratio"] is None
        assert deltas["iterations_predicted"] == 0 and deltas["iterations_actual"] == 1

    def test_duration_zero_prediction_is_unresolvable(self):
        assert _axes(Predictions(duration_min=0), Actuals(active_min=10))["duration"] == "unresolvable"

    def test_zero_prediction_survives_the_predictions_round_trip(self):
        p = Predictions(duration_min=0, files_changed=0, predicted_iterations=0, confidence=None)
        back = Predictions.from_dict(p.to_dict())
        assert (back.duration_min, back.files_changed, back.predicted_iterations) == (0, 0, 0)
        assert not back.is_empty()

    def test_complexity_unresolvable_without_independent_actual(self):
        axes = _axes(Predictions(complexity=TaskComplexity.M), Actuals())
        assert axes["complexity"] == "unresolvable"

    def test_complexity_scored_only_when_independently_supplied(self):
        preds = Predictions(complexity=TaskComplexity.M)
        assert _axes(preds, Actuals(complexity=TaskComplexity.M))["complexity"] == "correct"
        assert _axes(preds, Actuals(complexity=TaskComplexity.L))["complexity"] == "incorrect"

    def test_held_correct_when_every_resolvable_axis_correct(self):
        axes = _axes(Predictions(duration_min=100), Actuals(active_min=100))
        assert axes["held"] == "correct"

    def test_held_incorrect_when_any_resolvable_axis_incorrect(self):
        preds = Predictions(duration_min=100, files_scope=["src/*"])
        acts = Actuals(active_min=100, files_touched=["docs/a.md"], files_changed=1)
        assert _axes(preds, acts)["held"] == "incorrect"

    def test_held_unresolvable_when_no_axis_resolves(self):
        assert _axes(Predictions(), Actuals())["held"] == "unresolvable"


# ---------------------------------------------------------------------------
# CLI: --miss-category validation + persistence
# ---------------------------------------------------------------------------


class TestMissCategoryCli:
    @pytest.mark.parametrize("cat", SIX)
    def test_valid_category_persists_on_event(self, isolated_portfolio, cat):
        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        r = _done(["done", task.id, "--miss-category", cat, "--project", "test"])
        assert r.exit_code == 0, r.output
        assert _record(p.root, task.id)["miss_category"] == cat

    def test_default_is_null(self, isolated_portfolio):
        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        assert _done(["done", task.id, "--project", "test"]).exit_code == 0
        assert _record(p.root, task.id)["miss_category"] is None

    def test_invalid_value_rejected_and_nothing_written(self, isolated_portfolio):
        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        r = _done(["done", task.id, "--miss-category", "bogus", "--project", "test"])
        assert r.exit_code != 0
        assert "bad_miss_category" in r.output
        assert not (p.root / "reflections" / f"{task.id}.jsonl").exists()
        # task did not move
        assert list(p.tasks_dir.joinpath("done").glob(f"{task.id}*")) == []

    def test_tasks_state_accepts_flag_too(self, isolated_portfolio):
        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        r = _done(["tasks", "state", task.id, "done", "--miss-category", "noise", "--project", "test"])
        assert r.exit_code == 0, r.output
        assert _record(p.root, task.id)["miss_category"] == "noise"

    def test_service_backstop_rejects_bad_value(self, isolated_portfolio):
        from clawpm.services.tasks import transition

        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        with pytest.raises(ValueError, match="miss_category"):
            transition(p.config, project_id="test", task_id=task.id, new_state="done", miss_category="bogus")

    def test_coexists_with_surprise_taxonomy(self, isolated_portfolio):
        p = isolated_portfolio
        task = add_task(p.config, "test", "t")
        r = _done([
            "done", task.id, "--miss-category", "missed_variable",
            "--surprise", "assumption_broke", "--surprise", "scope_drift", "--project", "test",
        ])
        assert r.exit_code == 0, r.output
        rec = _record(p.root, task.id)
        assert rec["miss_category"] == "missed_variable"
        assert rec["surprise_taxonomy"] == ["assumption_broke", "scope_drift"]
        assert {"assumption_broke", "scope_drift"} <= SURPRISE_TAXONOMY


# ---------------------------------------------------------------------------
# Nudge: incorrect + no category -> one line, still closes; else silent
# ---------------------------------------------------------------------------


class TestMissNudge:
    def _task(self, p, **pred):
        return add_task(p.config, "test", "t", predictions=Predictions(**pred))

    def test_json_warning_when_incorrect_and_uncategorised(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(active_min=1000)
        task = self._task(p, duration_min=10)
        r = _done(["done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        out = json.loads(r.stdout)
        warnings = out["data"]["warnings"]
        assert len(warnings) == 1 and warnings[0]["code"] == "miss_category_missing"
        for c in SIX:
            assert c in warnings[0]["message"]
        assert list(p.tasks_dir.joinpath("done").glob(f"{task.id}*"))  # still closed

    def test_zero_iteration_prediction_missed_nudges(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(iterations=1)
        task = self._task(p, predicted_iterations=0)
        r = _done(["done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        warnings = json.loads(r.stdout)["data"]["warnings"]
        assert len(warnings) == 1 and warnings[0]["code"] == "miss_category_missing"

    def test_zero_iteration_prediction_met_is_silent(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(iterations=0)
        task = self._task(p, predicted_iterations=0)
        r = _done(["done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        assert "warnings" not in json.loads(r.stdout)["data"]

    def test_text_mode_nudge_goes_to_stderr_single_line(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(active_min=1000)
        task = self._task(p, duration_min=10)
        r = CliRunner().invoke(main, ["--format", "text", "done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        nudge = [ln for ln in r.stderr.splitlines() if "--miss-category" in ln]
        assert len(nudge) == 1
        assert all(c in nudge[0] for c in SIX)
        assert "--miss-category" not in r.stdout

    def test_no_nudge_when_category_given(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(active_min=1000)
        task = self._task(p, duration_min=10)
        r = _done(["done", task.id, "--miss-category", "noise", "--project", "test"])
        assert r.exit_code == 0, r.output
        assert "warnings" not in json.loads(r.stdout)["data"]

    def test_no_nudge_when_held_correct(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals(active_min=10)
        task = self._task(p, duration_min=10)
        r = _done(["done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        assert "warnings" not in json.loads(r.stdout)["data"]

    def test_no_nudge_when_unresolvable(self, isolated_portfolio, stub_actuals):
        p = isolated_portfolio
        stub_actuals()
        task = self._task(p)
        r = _done(["done", task.id, "--project", "test"])
        assert r.exit_code == 0, r.output
        assert "warnings" not in json.loads(r.stdout)["data"]
