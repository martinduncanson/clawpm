"""CLAWP-112-004 -- calibration block in `reflect summarize` + `reflect scorecard`.

Spec: docs/design/calibration-metrics-spec.md section 2.3.

Golden corpus (hand-computed, used by TestGoldenValues). 40 resolved events,
all complexity "m", predicted 100 min each, actual = active_min:

  group  conf  p     n   outcome   ratio r      ln r
  A      5     0.95  8   correct   1.0          0
  B      5     0.95  8   correct   2.0          +0.693147
  C      5     0.95  2   incorrect 0.1          -2.302585
  D      5     0.95  2   incorrect 4.0          +1.386294
  E      3     0.75  10  correct   0.5          -0.693147
  F      3     0.75  10  incorrect 0.25         -1.386294

  brier  = (16*0.05^2 + 4*0.95^2 + 10*0.25^2 + 10*0.75^2) / 40
         = (0.04 + 3.61 + 0.625 + 5.625) / 40 = 9.9 / 40 = 0.2475
  conf5 observed = 16/20 = 0.8, ratio = 0.8/0.95 = 0.8421
  conf3 observed = 10/20 = 0.5, ratio = 0.5/0.75 = 0.6667
  mae_log = (8*0.693147 + 2*2.302585 + 2*1.386294 + 10*0.693147
             + 10*1.386294) / 40 = 33.717352 / 40 = 0.8429
  bias: sorted ln r positions 20/21 both -0.693147 -> factor 0.5, "inflated"
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from clawpm.calibration import (
    P_BY_CONFIDENCE,
    compute_calibration,
    render_scorecard,
)
from clawpm.cli import main
from clawpm.models import Actuals, Predictions, TaskComplexity
from clawpm.reflect import (
    summarize_calibration,
    write_prediction_event,
    write_reflection_event,
)

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)

REQUIRED_KEYS = {
    "brier", "calibration_curve", "overconfidence_ratio", "mae_log", "bias",
    "bucket_bias_factor", "learning_velocity", "miss_category_distribution",
    "metareflection_due", "closure",
}


def _row(conf, held, ratio=None, complexity="m", project="p", miss=None,
         days_ago=1.0, task="T"):
    return {
        "project_id": project,
        "task_id": task,
        "occurred_at": NOW - timedelta(days=days_ago),
        "confidence": conf,
        "held": held,
        "duration_ratio": ratio,
        "complexity": complexity,
        "miss_category": miss,
        "legacy": False,
    }


def _golden_rows():
    rows = []
    rows += [_row(5, "correct", 1.0) for _ in range(8)]
    rows += [_row(5, "correct", 2.0) for _ in range(8)]
    rows += [_row(5, "incorrect", 0.1) for _ in range(2)]
    rows += [_row(5, "incorrect", 4.0) for _ in range(2)]
    rows += [_row(3, "correct", 0.5) for _ in range(10)]
    rows += [_row(3, "incorrect", 0.25) for _ in range(10)]
    return rows


LEDGER = {"registered": 10, "resolved": 6, "voided": 1, "legacy_resolved": 0}


class TestSchema:
    def test_all_keys_present_with_n_and_gate(self):
        c = compute_calibration(_golden_rows(), LEDGER, now=NOW)
        assert REQUIRED_KEYS <= set(c)
        assert set(c["bias"]) >= {"factor", "direction", "n"}
        assert set(c["learning_velocity"]) == {"90d", "365d"}
        assert set(c["closure"]) >= {"rate", "open_predictions", "void_rate"}
        for key in ("brier", "mae_log", "bias", "overconfidence_ratio"):
            assert "n" in c[key], key
            assert "insufficient_data" in c[key], key
        for cell in c["calibration_curve"]:
            assert "n" in cell and "insufficient_data" in cell

    def test_below_gate_is_null_and_flagged(self):
        rows = _golden_rows()[:19]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["brier"]["insufficient_data"] is True
        assert c["brier"]["value"] is None
        assert c["brier"]["n"] == 19
        assert c["mae_log"]["insufficient_data"] is True
        assert c["mae_log"]["value"] is None
        assert c["bias"]["factor"] is None
        assert c["bias"]["insufficient_data"] is True

    def test_exactly_twenty_passes_gate(self):
        rows = [_row(3, "correct", 1.0) for _ in range(20)]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["brier"]["insufficient_data"] is False
        assert c["brier"]["value"] == pytest.approx(0.0625, abs=1e-4)

    def test_empty_corpus_does_not_crash(self):
        c = compute_calibration([], {"registered": 0, "resolved": 0,
                                     "voided": 0, "legacy_resolved": 0}, now=NOW)
        assert REQUIRED_KEYS <= set(c)
        assert c["brier"]["insufficient_data"] is True
        assert c["closure"]["rate"] is None
        assert c["metareflection_due"] == []


class TestGoldenValues:
    def setup_method(self):
        self.c = compute_calibration(_golden_rows(), LEDGER, now=NOW)

    def test_p_map_is_spec_2_2(self):
        assert P_BY_CONFIDENCE == {1: 0.55, 2: 0.65, 3: 0.75, 4: 0.85, 5: 0.95}

    def test_brier(self):
        assert self.c["brier"]["n"] == 40
        assert self.c["brier"]["value"] == pytest.approx(0.2475, abs=1e-4)

    def test_calibration_curve(self):
        by_conf = {cell["confidence"]: cell for cell in self.c["calibration_curve"]}
        assert by_conf[5]["n"] == 20
        assert by_conf[5]["p"] == 0.95
        assert by_conf[5]["observed"] == pytest.approx(0.8, abs=1e-4)
        assert by_conf[3]["observed"] == pytest.approx(0.5, abs=1e-4)
        assert by_conf[4]["n"] == 0 and by_conf[4]["insufficient_data"] is True

    def test_overconfidence_headline_is_confidence_5(self):
        oc = self.c["overconfidence_ratio"]
        assert oc["headline"]["confidence"] == 5
        assert oc["headline"]["ratio"] == pytest.approx(0.8421, abs=1e-4)
        assert oc["by_confidence"]["3"]["ratio"] == pytest.approx(0.6667, abs=1e-4)
        assert oc["n"] == 20

    def test_overconfidence_headline_falls_back_to_highest_qualifying(self):
        rows = [_row(5, "correct", 1.0) for _ in range(5)]  # conf 5: n=5, gated
        rows += [_row(4, "correct", 1.0) for _ in range(12)]
        rows += [_row(4, "incorrect", 4.0) for _ in range(8)]  # conf 4: n=20
        rows += [_row(2, "correct", 1.0) for _ in range(25)]  # conf 2: n=25
        oc = compute_calibration(rows, LEDGER, now=NOW)["overconfidence_ratio"]
        assert oc["headline"]["confidence"] == 4
        assert oc["headline"]["ratio"] == pytest.approx(0.6 / 0.85, abs=1e-4)

    def test_mae_log(self):
        assert self.c["mae_log"]["n"] == 40
        assert self.c["mae_log"]["value"] == pytest.approx(0.8429, abs=1e-4)

    def test_bias(self):
        b = self.c["bias"]
        assert b["n"] == 40
        assert b["factor"] == pytest.approx(0.5, abs=1e-4)
        assert b["direction"] == "inflated"

    def test_bias_direction_optimistic(self):
        rows = [_row(3, "correct", 1.5) for _ in range(20)]
        b = compute_calibration(rows, LEDGER, now=NOW)["bias"]
        assert b["factor"] == pytest.approx(1.5, abs=1e-4)
        assert b["direction"] == "optimistic"

    def test_closure(self):
        cl = self.c["closure"]
        assert cl["rate"] == pytest.approx(0.6, abs=1e-4)
        assert cl["open_predictions"] == 3
        assert cl["void_rate"] == pytest.approx(0.1, abs=1e-4)
        assert cl["alarm"] is None  # 0.6 is not below 0.6

    def test_closure_alarm_below_threshold(self):
        led = {"registered": 10, "resolved": 5, "voided": 0, "legacy_resolved": 0}
        cl = compute_calibration([], led, now=NOW)["closure"]
        assert cl["rate"] == pytest.approx(0.5, abs=1e-4)
        assert cl["alarm"]

    def test_unset_confidence_excluded_and_counted(self):
        rows = _golden_rows() + [_row(None, "incorrect", 4.0) for _ in range(3)]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["brier"]["n"] == 40
        assert c["brier"]["n_unscored"] == 3
        assert c["brier"]["value"] == pytest.approx(0.2475, abs=1e-4)

    def test_unresolvable_rows_ignored_by_resolved_metrics(self):
        rows = _golden_rows() + [_row(5, "unresolvable", None) for _ in range(10)]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["brier"]["n"] == 40
        assert c["brier"]["value"] == pytest.approx(0.2475, abs=1e-4)


class TestBucketBiasFactor:
    def test_own_bucket_when_n_ge_20_else_global(self):
        rows = [_row(3, "correct", 0.5, complexity="m") for _ in range(20)]
        rows += [_row(3, "correct", 2.0, complexity="l") for _ in range(5)]
        bf = compute_calibration(rows, LEDGER, now=NOW)["bucket_bias_factor"]
        assert bf["m"]["factor"] == pytest.approx(0.5, abs=1e-4)
        assert bf["m"]["source"] == "complexity=m"
        # 'l' has n=5 -> falls back to the global factor (median over 25 = 0.5)
        assert bf["l"]["source"] == "global"
        assert bf["l"]["factor"] == pytest.approx(0.5, abs=1e-4)
        assert bf["global"]["n"] == 25

    def test_debiased_hit_rate_uses_bucket_factor(self):
        # r = 0.25 is outside the 0.5-2.0 band raw, inside once divided by 0.25.
        rows = [_row(3, "incorrect", 0.25) for _ in range(20)]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["duration_debiased"]["raw_hit_rate"] == pytest.approx(0.0, abs=1e-4)
        assert c["duration_debiased"]["hit_rate"] == pytest.approx(1.0, abs=1e-4)


class TestMetareflectionDue:
    def _due(self, rows):
        return compute_calibration(rows, LEDGER, now=NOW)["metareflection_due"]

    def test_three_in_window_same_project_fires(self):
        rows = [_row(3, "incorrect", 4.0, miss="wrong_signal", days_ago=d)
                for d in (1, 10, 29)]
        assert self._due(rows) == [
            {"project_id": "p", "miss_category": "wrong_signal", "count": 3}
        ]

    def test_two_occurrences_do_not_fire(self):
        rows = [_row(3, "incorrect", 4.0, miss="wrong_signal", days_ago=d)
                for d in (1, 10)]
        assert self._due(rows) == []

    def test_thirty_one_day_spread_does_not_fire(self):
        # Three occurrences, but the oldest is 31 days back: only 2 are inside
        # the trailing 30-day window.
        rows = [_row(3, "incorrect", 4.0, miss="wrong_signal", days_ago=d)
                for d in (1, 15, 31)]
        assert self._due(rows) == []

    def test_split_across_projects_does_not_fire(self):
        rows = [
            _row(3, "incorrect", 4.0, miss="noise", days_ago=1, project="a"),
            _row(3, "incorrect", 4.0, miss="noise", days_ago=2, project="a"),
            _row(3, "incorrect", 4.0, miss="noise", days_ago=3, project="b"),
        ]
        assert self._due(rows) == []

    def test_distribution_counts_resolved_incorrect_only(self):
        rows = (
            [_row(3, "incorrect", 4.0, miss="wrong_signal") for _ in range(3)]
            + [_row(3, "incorrect", 4.0, miss="noise")]
            + [_row(3, "correct", 1.0, miss="noise")]  # held: not counted
            + [_row(3, "incorrect", 4.0, miss=None)]   # no category: not counted
        )
        d = compute_calibration(rows, LEDGER, now=NOW)["miss_category_distribution"]
        assert d["counts"] == {"wrong_signal": 3, "noise": 1}
        assert d["n"] == 4


class TestLearningVelocity:
    def test_only_90d_and_365d_keys(self):
        lv = compute_calibration(_golden_rows(), LEDGER, now=NOW)["learning_velocity"]
        assert set(lv) == {"90d", "365d"}  # no 30d, per spec section 3

    def test_insufficient_below_three_qualifying_windows(self):
        lv = compute_calibration(_golden_rows(), LEDGER, now=NOW)["learning_velocity"]
        assert lv["90d"]["insufficient_data"] is True
        assert lv["90d"]["slope"] is None
        assert lv["365d"]["insufficient_data"] is True

    def test_improving_brier_gives_negative_slope(self):
        # 90-day windows stepped every 30 days. Put 20 events in each of three
        # disjoint 30-day slices so that windows ending now, now-30d, now-60d
        # each hold >= 20 events; later slices are more accurate.
        rows = []
        # slice 0 (newest, days 0-29): all hit at conf 3 -> Brier 0.0625
        rows += [_row(3, "correct", 1.0, days_ago=5 + i * 0.5) for i in range(20)]
        # slice 1 (days 30-59): 10 hit / 10 miss -> Brier 0.3125
        rows += [_row(3, "correct", 1.0, days_ago=35 + i * 0.5) for i in range(10)]
        rows += [_row(3, "incorrect", 4.0, days_ago=45 + i * 0.5) for i in range(10)]
        # slice 2 (days 60-89): all miss -> Brier 0.5625
        rows += [_row(3, "incorrect", 4.0, days_ago=65 + i * 0.5) for i in range(20)]
        lv = compute_calibration(rows, LEDGER, now=NOW)["learning_velocity"]["90d"]
        assert lv["insufficient_data"] is False
        assert lv["n"] >= 3
        assert lv["slope"] < 0  # newest windows are at the end of the index


# ---------------------------------------------------------------------------
# Loader + summarize integration (files on disk)
# ---------------------------------------------------------------------------


def _preds(duration=100, conf=3, scope=None, pid=None):
    return Predictions(
        duration_min=duration,
        complexity=TaskComplexity("m"),
        confidence=conf,
        files_scope=scope or [],
        prediction_id=pid,
    )


def _done(root, tid, *, actual=100, conf=3, project="proj", pid=None,
          miss=None, kind=None, preds=None, files_touched=None):
    write_reflection_event(
        root,
        event="task_done",
        task_id=tid,
        project_id=project,
        predictions=preds or _preds(conf=conf, pid=pid),
        actuals=Actuals(
            duration_min=actual,
            active_min=actual,
            complexity=None,
            files_touched=files_touched or [],
        ),
        miss_category=miss,
        kind=kind,
    )


def _register(root, tid, pid, project="proj", duration=100, conf=3):
    write_prediction_event(
        root,
        event="prediction_registered",
        task_id=tid,
        project_id=project,
        prediction_id=pid,
        predictions={"duration_min": duration, "complexity": "m",
                     "confidence": conf, "prediction_id": pid},
    )


class TestSummarizeIntegration:
    def test_summarize_carries_calibration_block(self, tmp_path):
        for i in range(25):
            _done(tmp_path, f"T-{i}", actual=100, conf=3)
        s = summarize_calibration(tmp_path, project_id="proj")
        assert REQUIRED_KEYS <= set(s["calibration"])
        assert s["calibration"]["brier"]["n"] == 25
        assert s["calibration"]["brier"]["value"] == pytest.approx(0.0625, abs=1e-4)
        # existing keys untouched
        assert s["total_done"] == 25

    def test_closure_from_ledger_ten_registered_six_resolved(self, tmp_path):
        for i in range(10):
            _register(tmp_path, f"R-{i}", f"pid{i:02d}")
        for i in range(6):
            _done(tmp_path, f"R-{i}", pid=f"pid{i:02d}")
        cl = summarize_calibration(tmp_path, "proj")["calibration"]["closure"]
        assert cl["rate"] == pytest.approx(0.6, abs=1e-4)
        assert cl["open_predictions"] == 4
        assert cl["void_rate"] == pytest.approx(0.0, abs=1e-4)

    def test_void_counts_in_closure_not_as_resolved(self, tmp_path):
        for i in range(4):
            _register(tmp_path, f"V-{i}", f"v{i}")
        for i in range(4):
            _done(tmp_path, f"V-{i}", pid=f"v{i}")
        ref = tmp_path / "reflections" / "V-0.jsonl"
        with ref.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "void", "task_id": "V-0",
                                 "project_id": "proj"}) + "\n")
        cl = summarize_calibration(tmp_path, "proj")["calibration"]["closure"]
        assert cl["rate"] == pytest.approx(0.75, abs=1e-4)
        assert cl["void_rate"] == pytest.approx(0.25, abs=1e-4)
        assert cl["open_predictions"] == 0

    def test_registered_snapshot_scored_not_post_hoc_revision(self, tmp_path):
        # Registered at duration=100, then revised AFTER work started to 400
        # (so the miss looks like a hit). Spec 2.5: score the registered one.
        tid, pid = "S-1", "snap1"
        _register(tmp_path, tid, pid, duration=100)
        # The done event below carries duration_min=400, so the first start was
        # ~400 min before it. A revision stamped at 'now' is therefore post-start.
        write_prediction_event(
            tmp_path, event="prediction_revised", task_id=tid,
            project_id="proj", prediction_id=pid,
            predictions={"duration_min": 400, "complexity": "m",
                         "confidence": 3, "prediction_id": pid},
            reason="rationalising",
        )
        _done(tmp_path, tid, actual=400, pid=pid,
              preds=_preds(duration=400, pid=pid))
        # actual 400 vs registered 100 -> r=4.0 -> incorrect.
        # Repeat to clear the n>=20 gate with the same shape.
        for i in range(19):
            t = f"S-{i + 2}"
            _register(tmp_path, t, f"snap{i + 2}", duration=100)
            write_prediction_event(
                tmp_path, event="prediction_revised", task_id=t,
                project_id="proj", prediction_id=f"snap{i + 2}",
                predictions={"duration_min": 400, "complexity": "m",
                             "confidence": 3, "prediction_id": f"snap{i + 2}"},
            )
            _done(tmp_path, t, actual=400, pid=f"snap{i + 2}",
                  preds=_preds(duration=400, pid=f"snap{i + 2}"))
        b = summarize_calibration(tmp_path, "proj")["calibration"]["brier"]
        # all 20 incorrect at p=0.75 -> Brier 0.5625 (a hit-by-revision would be 0.0625)
        assert b["n"] == 20
        assert b["value"] == pytest.approx(0.5625, abs=1e-4)

    def test_decision_kind_excluded_from_duration_axis_by_default(self, tmp_path):
        # Decision tasks: duration axis unresolvable unless --include-decisions.
        for i in range(20):
            _done(tmp_path, f"D-{i}", actual=900, conf=3, kind="decision")
        off = summarize_calibration(tmp_path, "proj")["calibration"]
        on = summarize_calibration(tmp_path, "proj", include_decisions=True)["calibration"]
        assert off["brier"]["n"] == 0
        assert on["brier"]["n"] == 20
        assert on["brier"]["value"] == pytest.approx(0.5625, abs=1e-4)

    def test_cross_project_same_task_id_kept(self, tmp_path):
        _done(tmp_path, "X-1", project="a", actual=100)
        _done(tmp_path, "X-1", project="b", actual=100)
        c = summarize_calibration(tmp_path, None)["calibration"]
        assert c["brier"]["n"] == 2


class TestCli:
    def _seed(self, root, n=25):
        for i in range(n):
            _done(root, f"C-{i}", project="test", actual=100 if i % 2 else 400,
                  miss="wrong_signal" if i % 2 == 0 else None)

    def test_scorecard_prints_exactly_five_lines(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        self._seed(root)
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        lines = [ln for ln in r.output.splitlines() if ln.strip()]
        assert len(lines) == 5, r.output
        assert lines[0].lower().startswith("closure")
        assert lines[1].lower().startswith("bias")
        assert lines[2].lower().startswith("brier")
        assert lines[3].lower().startswith("overconfidence")
        assert "miss" in lines[4].lower()

    def test_scorecard_json_returns_calibration_block(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        self._seed(root)
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test", "--json"])
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert REQUIRED_KEYS <= set(out)

    def test_scorecard_on_empty_corpus_still_five_lines(self, temp_portfolio_with_repo):
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        assert len([ln for ln in r.output.splitlines() if ln.strip()]) == 5

    def test_summarize_cli_has_calibration_and_include_decisions(
        self, temp_portfolio_with_repo
    ):
        root = temp_portfolio_with_repo["config"].portfolio_root
        self._seed(root)
        r = CliRunner().invoke(
            main, ["reflect", "summarize", "-p", "test", "--include-decisions"]
        )
        assert r.exit_code == 0, r.output
        out = json.loads(r.output)
        assert REQUIRED_KEYS <= set(out["data"]["calibration"])


class TestRenderScorecard:
    def test_five_lines_ascii_only(self):
        c = compute_calibration(_golden_rows(), LEDGER, now=NOW)
        lines = render_scorecard(c)
        assert len(lines) == 5
        assert all(ln.isascii() for ln in lines)

    def test_closure_alarm_shown(self):
        led = {"registered": 10, "resolved": 2, "voided": 0, "legacy_resolved": 0}
        lines = render_scorecard(compute_calibration([], led, now=NOW))
        assert "ALARM" in lines[0]

    def test_metareflection_listed_on_line_five(self):
        rows = [_row(3, "incorrect", 4.0, miss="missed_variable", days_ago=d)
                for d in (1, 2, 3)]
        lines = render_scorecard(compute_calibration(rows, LEDGER, now=NOW))
        assert "missed_variable" in lines[4]
        assert math.isfinite(len(lines))


class TestRound4Findings:
    """Codex gate r1 P2s 1-4 on PR #111."""

    def test_debias_keeps_full_precision(self):
        # 20 ratios of 1/30 and one of 2/30: the median factor is exactly
        # 1/30, so every debiased ratio is 1.0 (x20) or 2.0 -> hit rate 1.0.
        # A factor rounded to 4 dp pushes 2.0 to 2.0002 and a hit to a miss.
        rows = [_row(3, "correct", 1 / 30) for _ in range(20)]
        rows.append(_row(3, "correct", 2 / 30))
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["duration_debiased"]["hit_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_tiny_factor_does_not_divide_by_zero(self):
        rows = [_row(3, "correct", 1e-5) for _ in range(20)]
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["duration_debiased"]["n"] == 20
        assert c["duration_debiased"]["hit_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_malformed_confidence_rejected_and_counted(self):
        rows = [_row(3, "correct", 1.0) for _ in range(20)]
        rows.append(_row([], "correct", 1.0))
        rows.append(_row({"a": 1}, "incorrect", 1.0))
        rows.append(_row(True, "correct", 1.0))
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["brier"]["n"] == 20
        assert c["brier"]["n_unscored"] == 3

    def test_malformed_ratio_and_held_rejected_and_counted(self):
        rows = [_row(3, "correct", 1.0) for _ in range(20)]
        rows.append(_row(3, "correct", "fast"))
        rows.append(_row(3, ["x"], 1.0))
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["mae_log"]["n"] == 21  # the str ratio is rejected, the list-held row keeps its ratio
        assert c["data_quality"]["rejected_rows"] == 2

    def test_loader_counts_bad_lines_and_survives_bad_confidence(self, tmp_path):
        for i in range(3):
            _done(tmp_path, f"Q-{i}", actual=100)
        ref = tmp_path / "reflections" / "Q-0.jsonl"
        bad_done = {"event": "task_done", "task_id": "Q-9", "project_id": "proj",
                    "predictions": {"duration_min": 100, "confidence": []},
                    "actuals": {"duration_min": 100, "active_min": 100}}
        with ref.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write("[1, 2]\n")
            fh.write(json.dumps({"event": "mystery", "project_id": "proj"}) + "\n")
            fh.write(json.dumps(bad_done) + "\n")
        c = summarize_calibration(tmp_path, "proj")["calibration"]
        dq = c["data_quality"]
        assert dq["malformed_lines"] == 1
        assert dq["non_object"] == 1
        assert dq["unknown_events"] == 1

    def test_scorecard_shows_data_quality_line_only_when_nonzero(
        self, temp_portfolio_with_repo
    ):
        root = temp_portfolio_with_repo["config"].portfolio_root
        _done(root, "DQ-1", project="test")
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert len([ln for ln in r.output.splitlines() if ln.strip()]) == 5
        with (root / "reflections" / "DQ-1.jsonl").open("a", encoding="utf-8") as fh:
            fh.write("{broken\n")
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        lines = [ln for ln in r.output.splitlines() if ln.strip()]
        assert len(lines) == 5
        assert "malformed" in lines[2]

    def test_summarize_does_not_create_reflections_dir(self, tmp_path):
        assert not (tmp_path / "reflections").exists()
        summarize_calibration(tmp_path, None)
        assert not (tmp_path / "reflections").exists()

    def test_scorecard_does_not_create_reflections_dir(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        ref = root / "reflections"
        if ref.exists():
            import shutil
            shutil.rmtree(ref)
        before = sorted(p.name for p in root.iterdir())
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        assert not ref.exists()
        assert sorted(p.name for p in root.iterdir()) == before


class TestRound5Findings:
    """Codex gate r2 P2s 1-3 on PR #111."""

    def test_inclusive_band_survives_exp_log_rounding(self):
        # Factor is mathematically 0.1; exp(log(0.1)) lands a hair under and
        # would push 0.05 / factor below 0.5. All 21 ratios qualify.
        rows = [_row(3, "correct", 0.1) for _ in range(20)]
        rows.append(_row(3, "incorrect", 0.05))
        c = compute_calibration(rows, LEDGER, now=NOW)
        assert c["duration_debiased"]["hit_rate"] == pytest.approx(1.0, abs=1e-9)

    def test_other_projects_events_not_counted_in_scope(self, tmp_path):
        ref = tmp_path / "reflections"
        ref.mkdir()
        with (ref / "B-1.jsonl").open("w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "mystery", "project_id": "projB"}) + "\n")
            fh.write(json.dumps({"event": "task_done", "project_id": "projB",
                                 "prediction_id": []}) + "\n")
        dq = summarize_calibration(tmp_path, "projA")["calibration"]["data_quality"]
        assert dq["unknown_events"] == 0
        assert dq["malformed_lines"] == 0
        assert dq["non_object"] == 0
        # Unscoped request still sees them.
        dq = summarize_calibration(tmp_path, None)["calibration"]["data_quality"]
        assert dq["unknown_events"] == 1
        assert dq["malformed_lines"] == 1


class TestPerBucketBias:
    """Spec 2.3 `bias`: per-bucket exp(median(ln r)), same gate as global."""

    def _bias(self, rows):
        return compute_calibration(rows, LEDGER, now=NOW)["bias"]

    def test_median_of_logs_not_arithmetic_median(self):
        # Equal groups at 0.25 and 1: exp(median ln r) = exp(-0.693) = 0.5,
        # an arithmetic median of the ratios would say 0.625.
        rows = [_row(3, "correct", 0.25) for _ in range(10)]
        rows += [_row(3, "correct", 1.0) for _ in range(10)]
        b = self._bias(rows)
        assert b["by_complexity"]["m"]["n"] == 20
        assert b["by_complexity"]["m"]["factor"] == pytest.approx(0.5, abs=1e-4)
        assert b["by_complexity"]["m"]["direction"] == "inflated"

    def test_small_bucket_is_insufficient_with_null_values(self):
        rows = [_row(3, "correct", 0.5, complexity="m") for _ in range(20)]
        rows += [_row(3, "correct", 2.0, complexity="l") for _ in range(5)]
        by = self._bias(rows)["by_complexity"]
        assert by["l"] == {"n": 5, "insufficient_data": True,
                           "factor": None, "direction": None}
        assert by["m"]["insufficient_data"] is False

    def test_confidence_unset_and_agent_profile_buckets(self):
        rows = []
        for _ in range(20):
            r = _row(None, "correct", 0.5)
            r["agent_profile"] = "coder"
            rows.append(r)
        for _ in range(3):
            rows.append(_row(4, "correct", 2.0))
        b = self._bias(rows)
        assert b["by_confidence"]["unset"]["n"] == 20
        assert b["by_confidence"]["unset"]["factor"] == pytest.approx(0.5, abs=1e-4)
        assert b["by_confidence"]["4"]["insufficient_data"] is True
        assert b["by_agent_profile"]["coder"]["n"] == 20
        assert b["by_agent_profile"]["unspecified"]["n"] == 3

    def test_project_bucket_under_portfolio_scope(self):
        rows = [_row(3, "correct", 0.5, project="pa") for _ in range(20)]
        rows += [_row(3, "correct", 2.0, project="pb") for _ in range(20)]
        b = self._bias(rows)
        assert b["by_project"]["pa"]["factor"] == pytest.approx(0.5, abs=1e-4)
        assert b["by_project"]["pa"]["direction"] == "inflated"
        assert b["by_project"]["pb"]["factor"] == pytest.approx(2.0, abs=1e-4)
        assert b["by_project"]["pb"]["direction"] == "optimistic"
        assert b["n"] == 40

    def test_global_bias_fields_unchanged(self):
        b = self._bias(_golden_rows())
        assert b["n"] == 40 and b["factor"] == pytest.approx(0.5, abs=1e-4)

    def test_agent_profile_flows_from_done_event(self, tmp_path):
        for i in range(20):
            _done(tmp_path, f"AP-{i}", actual=50, conf=3)
        by = summarize_calibration(tmp_path, "proj")["calibration"]["bias"]
        assert "by_agent_profile" in by
        assert "unspecified" in by["by_agent_profile"]
