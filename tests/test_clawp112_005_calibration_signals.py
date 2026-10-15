"""CLAWP-112-005 round A -- rolling-30-day closure alarm + `reflect summarize` text alarm.

Spec: docs/design/calibration-metrics-spec.md section 2.3.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from clawpm.calibration import CLOSURE_ALARM_BELOW, compute_calibration, render_scorecard
from clawpm.cli import main
from clawpm.reflect import summarize_calibration, write_prediction_event

from test_agent_dispatch import temp_portfolio_with_repo  # noqa: F401

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
PRE_KEYS = {"registered", "resolved", "voided", "legacy_resolved", "rate",
            "open_predictions", "void_rate", "insufficient_data", "alarm"}


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def _append(root, task_id, rec):
    d = root / "reflections"
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{task_id}.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def _register(root, tid, days_ago, project="proj"):
    _append(root, tid, {
        "event": "prediction_registered", "task_id": tid, "project_id": project,
        "prediction_id": f"pid-{tid}", "registered_at": _iso(NOW - timedelta(days=days_ago)),
        "predictions": {"duration_min": 100, "complexity": "m", "confidence": 3,
                        "prediction_id": f"pid-{tid}"},
    })


def _resolve(root, tid, days_ago=0.5, project="proj"):
    _append(root, tid, {
        "event": "task_done", "task_id": tid, "project_id": project,
        "prediction_id": f"pid-{tid}", "occurred_at": _iso(NOW - timedelta(days=days_ago)),
        "predictions": {"duration_min": 100, "complexity": "m", "confidence": 3,
                        "prediction_id": f"pid-{tid}"},
        "actuals": {"duration_min": 100, "active_min": 100},
    })


def _ledger(reg30, res30, reg=None, res=None):
    return {"registered": reg if reg is not None else reg30,
            "resolved": res if res is not None else res30,
            "voided": 0, "legacy_resolved": 0,
            "registered_30d": reg30, "resolved_30d": res30, "voided_30d": 0}


class TestWindowBoundary:
    def test_059_alarms(self):
        c = compute_calibration([], _ledger(100, 59), now=NOW)["closure"]
        assert c["window_30d"]["rate"] == 0.59
        assert c["alarm"] is not None

    def test_060_does_not_alarm(self):
        # Existing comparison is strict (`rate < CLOSURE_ALARM_BELOW`).
        assert CLOSURE_ALARM_BELOW == 0.6
        c = compute_calibration([], _ledger(100, 60), now=NOW)["closure"]
        assert c["window_30d"]["rate"] == 0.6
        assert c["alarm"] is None

    def test_window_rate_drives_alarm_not_all_time(self):
        # All-time 0.20 would alarm; the last 30 days are 9/10 -> no alarm.
        c = compute_calibration([], _ledger(10, 9, reg=100, res=20), now=NOW)["closure"]
        assert c["rate"] == 0.2
        assert c["alarm"] is None

    def test_no_window_registrations_falls_back_to_all_time(self):
        c = compute_calibration([], _ledger(0, 0, reg=10, res=3), now=NOW)["closure"]
        assert c["window_30d"] == {"registered": 0, "resolved": 0, "voided": 0, "rate": None}
        assert c["alarm"] is not None  # all-time 0.30

    def test_empty_ledger_has_window_key_and_no_alarm(self):
        c = compute_calibration([], {}, now=NOW)["closure"]
        assert c["window_30d"]["rate"] is None
        assert c["alarm"] is None


class TestWindowFromEvents:
    def test_old_unresolved_registration_outside_window_does_not_alarm(self, tmp_path):
        for i in range(5):
            _register(tmp_path, f"OLD-{i}", days_ago=40)  # never resolved, outside window
        for i in range(5):
            _register(tmp_path, f"NEW-{i}", days_ago=5)
            _resolve(tmp_path, f"NEW-{i}", days_ago=4)
        cl = summarize_calibration(tmp_path, "proj", now=NOW)["calibration"]["closure"]
        assert cl["registered"] == 10
        assert cl["rate"] == 0.5  # all-time would alarm
        assert cl["window_30d"] == {"registered": 5, "resolved": 5, "voided": 0, "rate": 1.0}
        assert cl["alarm"] is None

    def test_same_unresolved_registration_inside_window_alarms(self, tmp_path):
        for i in range(5):
            _register(tmp_path, f"OLD-{i}", days_ago=10)  # inside window, unresolved
        for i in range(5):
            _register(tmp_path, f"NEW-{i}", days_ago=5)
            _resolve(tmp_path, f"NEW-{i}", days_ago=4)
        cl = summarize_calibration(tmp_path, "proj", now=NOW)["calibration"]["closure"]
        assert cl["window_30d"]["registered"] == 10
        assert cl["window_30d"]["rate"] == 0.5
        assert cl["alarm"] is not None
        assert cl["alarm_basis"] == "window_30d"

    def test_legacy_corpus_without_window_registrations_keeps_all_time(self, tmp_path):
        for i in range(5):
            _register(tmp_path, f"OLD-{i}", days_ago=90)
        cl = summarize_calibration(tmp_path, "proj", now=NOW)["calibration"]["closure"]
        assert cl["window_30d"]["registered"] == 0
        assert cl["alarm"] is not None
        assert cl["alarm_basis"] == "all_time"

    def test_voided_counted_in_window(self, tmp_path):
        _register(tmp_path, "V-1", days_ago=3)
        _append(tmp_path, "V-1", {"event": "void", "task_id": "V-1", "project_id": "proj"})
        cl = summarize_calibration(tmp_path, "proj", now=NOW)["calibration"]["closure"]
        assert cl["window_30d"]["voided"] == 1
        assert cl["window_30d"]["resolved"] == 0


class TestCli:
    def _seed_low_closure(self, root, project="test"):
        for i in range(4):
            write_prediction_event(
                root, event="prediction_registered", task_id=f"Z-{i}",
                project_id=project, prediction_id=f"z{i}",
                predictions={"duration_min": 60, "complexity": "m",
                             "confidence": 3, "prediction_id": f"z{i}"},
            )

    def test_summarize_text_contains_alarm_and_exit_zero(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        self._seed_low_closure(root)
        r = CliRunner().invoke(main, ["--format", "text", "reflect", "summarize", "-p", "test"])
        assert r.exit_code == 0, r.output
        assert "ALARM" in r.output
        assert "closure 0.00 is below 0.6" in r.output

    def test_summarize_text_without_alarm_has_none(self, temp_portfolio_with_repo):
        r = CliRunner().invoke(main, ["--format", "text", "reflect", "summarize", "-p", "test"])
        assert r.exit_code == 0, r.output
        assert "ALARM" not in r.output

    def test_summarize_json_additive_keys(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        self._seed_low_closure(root)
        r = CliRunner().invoke(main, ["reflect", "summarize", "-p", "test"])
        assert r.exit_code == 0, r.output
        cl = json.loads(r.output)["data"]["calibration"]["closure"]
        assert PRE_KEYS <= set(cl)
        assert set(cl["window_30d"]) == {"registered", "resolved", "voided", "rate"}
        assert cl["window_30d"]["registered"] == 4

    def test_scorecard_five_lines_with_and_without_alarm(self, temp_portfolio_with_repo):
        root = temp_portfolio_with_repo["config"].portfolio_root
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        assert len([ln for ln in r.output.splitlines() if ln.strip()]) == 5
        assert "ALARM" not in r.output
        self._seed_low_closure(root)
        r = CliRunner().invoke(main, ["reflect", "scorecard", "-p", "test"])
        assert r.exit_code == 0, r.output
        lines = [ln for ln in r.output.splitlines() if ln.strip()]
        assert len(lines) == 5, r.output
        assert "ALARM: below 0.60" in lines[0]


class TestRenderScorecardAlarm:
    def test_alarm_text_uses_constant_and_stays_five_lines(self):
        block = compute_calibration([], _ledger(10, 1), now=NOW)
        lines = render_scorecard(block)
        assert len(lines) == 5
        assert f"ALARM: below {CLOSURE_ALARM_BELOW:.2f}" in lines[0]
