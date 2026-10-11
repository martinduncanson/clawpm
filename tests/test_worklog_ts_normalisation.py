"""Work-log timestamps are normalised to aware UTC at load (CLAWP-112-002, PR #96 r3).

Legacy entries carry a trailing "Z" (loaded naive before this fix) next to
"+00:00" ones; mixing naive and aware datetimes made every sort/subtract
downstream raise TypeError and silently dropped the reflection event.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from click.testing import CliRunner

from clawpm.cli import main
from clawpm.models import TaskComplexity, Predictions, WorkLogAction, WorkLogEntry
from clawpm.reflect import _compute_actuals
from clawpm.tasks import add_task
from clawpm.worklog import get_worklog_path, read_entries

from tests.test_reflect_phase1 import temp_portfolio  # noqa: F401  (fixture)


def _row(ts: str, action: str, task: str = "T-1", project: str = "test") -> dict:
    return {"ts": ts, "project": project, "task": task, "action": action}


def _write_log(config, rows: list[dict]) -> None:
    path = get_worklog_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        ("\n".join(json.dumps(r) for r in rows) + "\n").encode("utf-8")
    )


class TestFromDictNormalises:
    def test_z_naive_and_offset_all_load_aware_utc(self):
        for raw in (
            "2026-01-01T00:00:00Z",
            "2026-01-01T00:00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T03:00:00+03:00",
        ):
            e = WorkLogEntry.from_dict(_row(raw, "start"))
            assert e.ts.tzinfo is not None
            assert e.ts.utcoffset() == timedelta(0)
            assert e.ts == datetime(2026, 1, 1, tzinfo=timezone.utc)

    def test_direct_construction_with_naive_ts_is_normalised(self):
        e = WorkLogEntry(
            ts=datetime(2026, 1, 1), project="p", action=WorkLogAction.START,
        )
        assert e.ts.tzinfo is not None

    def test_on_disk_format_unchanged_for_aware_utc(self):
        e = WorkLogEntry(
            ts=datetime(2026, 1, 1, tzinfo=timezone.utc), project="p",
            action=WorkLogAction.START,
        )
        assert e.to_dict()["ts"] == "2026-01-01T00:00:00+00:00"


class TestReadEntriesMixed:
    def test_mixed_z_offset_and_naive_does_not_raise(self, temp_portfolio):
        config = temp_portfolio["config"]
        _write_log(config, [
            _row("2026-01-01T00:00:00Z", "start"),
            _row("2026-01-01T00:30:00+00:00", "progress"),
            _row("2026-01-01T00:10:00", "progress"),
        ])
        entries = read_entries(config, project="test")
        assert [e.ts.minute for e in entries] == [30, 10, 0]  # newest first


class TestComputeActualsMixedStarts:
    def test_two_mixed_start_entries(self, temp_portfolio):
        config = temp_portfolio["config"]
        _write_log(config, [
            _row("2026-01-01T00:00:00Z", "start"),
            _row("2026-01-01T00:30:00+00:00", "start"),
        ])
        entries = read_entries(config, project="test")
        now = datetime(2026, 1, 1, 2, 0, tzinfo=timezone.utc)
        actuals = _compute_actuals("T-1", entries, now=now)
        assert actuals.duration_min == 120
        assert actuals.active_min == 45  # 30 gap + 15 wrap-up

    def test_hand_built_mixed_starts_without_reading_log(self):
        entries = [
            WorkLogEntry(ts=datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc),
                         project="p", action=WorkLogAction.START, task="T-1"),
            WorkLogEntry(ts=datetime(2026, 1, 1, 0, 0),
                         project="p", action=WorkLogAction.START, task="T-1"),
        ]
        now = datetime(2026, 1, 1, 1, 0, tzinfo=timezone.utc)
        assert _compute_actuals("T-1", entries, now=now).duration_min == 60


class TestTransitionsStillEmitReflection:
    def _task_with_mixed_log(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(
            config, "test", "Mixed log", complexity=TaskComplexity.M,
            predictions=Predictions(duration_min=60, complexity=TaskComplexity.M),
        )
        assert task is not None
        _write_log(config, [
            _row("2026-01-01T00:00:00Z", "start", task=task.id),
            _row("2026-01-01T00:30:00+00:00", "start", task=task.id),
            _row("2026-01-01T00:40:00", "progress", task=task.id),
        ])
        return task

    def _last_record(self, temp_portfolio, task):
        ref = temp_portfolio["root"] / "reflections" / f"{task.id}.jsonl"
        recs = [json.loads(l) for l in ref.read_text().strip().splitlines()]
        return [r for r in recs if r["event"] in ("task_done", "task_blocked")][-1]

    def test_done_emits_event_with_actuals(self, temp_portfolio):
        task = self._task_with_mixed_log(temp_portfolio)
        result = CliRunner().invoke(
            main, ["tasks", "state", task.id, "done", "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        rec = self._last_record(temp_portfolio, task)
        assert rec["event"] == "task_done"
        assert rec["actuals"]["duration_min"] is not None
        assert rec["actuals"]["active_min"] is not None

    def test_block_emits_event_with_actuals(self, temp_portfolio):
        task = self._task_with_mixed_log(temp_portfolio)
        result = CliRunner().invoke(
            main, ["block", task.id, "--project", "test"],
        )
        assert result.exit_code == 0, result.output
        rec = self._last_record(temp_portfolio, task)
        assert rec["event"] == "task_blocked"
        assert rec["actuals"]["active_min"] is not None
