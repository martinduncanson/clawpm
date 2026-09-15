"""Tests for actuals.active_min (CLAWP-112-002).

``active_min`` sums ``min(gap_to_next, 60)`` minutes over consecutive
work_log entries for a task, plus a flat 15-minute wrap-up credit for the
final entry, then clamps to ``duration_min`` (see ``_compute_actuals`` in
``clawpm.reflect``). Two things are pinned here:

1. The exact arithmetic, against a hand-built log (unit test).
2. The documented invariant ``active_min <= duration_min`` holding against a
   FIXTURE COPY of the real reflection/work_log corpus
   (``tests/fixtures/reflection_corpus_sample.json``) — not just synthetic
   data. The fixture was extracted once from ``~/clawpm/{reflections,
   work_log.jsonl}`` via a disposable script; this test never touches the
   live corpus.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clawpm.models import WorkLogAction, WorkLogEntry
from clawpm.reflect import _compute_actuals, _compute_active_min

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "reflection_corpus_sample.json"


def _entry(ts: str, action: WorkLogAction, task: str = "T-1") -> WorkLogEntry:
    return WorkLogEntry(
        ts=datetime.fromisoformat(ts), project="test", action=action, task=task,
    )


class TestComputeActiveMinUnit:
    """Hand-built log — exact expected value (success criterion 1, part 1)."""

    def test_gaps_capped_plus_wrapup_credit(self):
        entries = [
            _entry("2026-01-01T00:00:00+00:00", WorkLogAction.START),
            # gap 10min (uncapped)
            _entry("2026-01-01T00:10:00+00:00", WorkLogAction.PROGRESS),
            # gap 80min -> capped at 60
            _entry("2026-01-01T01:30:00+00:00", WorkLogAction.PROGRESS),
            # gap 10min (uncapped)
            _entry("2026-01-01T01:40:00+00:00", WorkLogAction.DONE),
        ]
        # 10 + min(80, 60) + 10 = 80, plus 15 wrap-up credit = 95
        assert _compute_active_min(entries) == 95

    def test_single_entry_is_just_the_wrapup_credit(self):
        entries = [_entry("2026-01-01T00:00:00+00:00", WorkLogAction.START)]
        assert _compute_active_min(entries) == 15

    def test_no_entries_is_none(self):
        assert _compute_active_min([]) is None

    def test_unsorted_and_mixed_timezone_entries_handled(self):
        """Real work_log data interleaves entries out of order and mixes
        naive/aware/offset timestamps (commit timestamps carry local tz
        offsets) — the helper must sort by absolute instant, not append
        order or raw offset string."""
        entries = [
            # Listed out of order on purpose. In UTC-equivalent instants:
            # commit=2025-12-31T22:00:00Z, start=2026-01-01T00:00:00Z,
            # progress=2026-01-01T00:30:00Z — so the true chronological
            # order is commit -> start -> progress, despite appearing last
            # in this list and despite the +03:00 offset making its local
            # clock time read "later" than the others.
            _entry("2026-01-01T01:00:00+03:00", WorkLogAction.COMMIT),
            _entry("2026-01-01T00:00:00+00:00", WorkLogAction.START),
            _entry("2026-01-01T00:30:00+00:00", WorkLogAction.PROGRESS),
        ]
        # gap(commit->start) = 120min -> capped 60; gap(start->progress) = 30min.
        # total = 60 + 30 + 15 wrap-up = 105
        assert _compute_active_min(entries) == 105


class TestComputeActualsActiveMinClamp:
    def test_active_min_clamped_to_short_duration(self):
        """A single-entry task completed in 5 minutes must not report 15
        minutes of 'active' time — active_min is clamped to duration_min."""
        entries = [_entry("2026-01-01T00:00:00+00:00", WorkLogAction.START, task="T-1")]
        now = datetime(2026, 1, 1, 0, 5, tzinfo=timezone.utc)
        actuals = _compute_actuals("T-1", entries, now=now)
        assert actuals.duration_min == 5
        assert actuals.active_min == 5  # clamped down from the raw 15

    def test_active_min_none_when_no_entries(self):
        actuals = _compute_actuals("T-1", [])
        assert actuals.active_min is None

    def test_active_min_le_duration_min_when_ample_headroom(self):
        entries = [
            _entry("2026-01-01T00:00:00+00:00", WorkLogAction.START, task="T-1"),
            _entry("2026-01-01T00:20:00+00:00", WorkLogAction.DONE, task="T-1"),
        ]
        now = datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)  # 300 min elapsed
        actuals = _compute_actuals("T-1", entries, now=now)
        assert actuals.duration_min == 300
        # gap 20 + wrap-up 15 = 35, well under 300 -> no clamp needed
        assert actuals.active_min == 35


class TestActiveMinRealCorpusProperty:
    """Property-check against a fixture copy of the real corpus.

    Raw (unclamped) active_min DOES exceed the historically-recorded
    duration_min on a majority of short real tasks (the flat 15-minute
    credit alone exceeds a <15-minute wall-clock task) — verified when the
    fixture was built. The clamp in ``_compute_actuals`` is what makes the
    documented invariant hold; this test proves it holds through the real,
    messy, sometimes-out-of-order, mixed-timezone entry patterns actually
    present in the corpus, not just tidy synthetic logs.
    """

    @pytest.fixture(scope="class")
    def corpus(self):
        if not FIXTURE_PATH.exists():
            pytest.skip(f"fixture missing: {FIXTURE_PATH}")
        return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    def test_fixture_is_nonempty(self, corpus):
        assert len(corpus) >= 20, "fixture should carry a meaningful real sample"

    def test_active_min_never_exceeds_duration_min(self, corpus):
        checked = 0
        for task in corpus:
            entries = [
                _entry(e["ts"], WorkLogAction(e["action"]), task=e["task"])
                for e in task["work_log_entries"]
            ]
            start_entries = sorted(
                (e for e in entries if e.action == WorkLogAction.START),
                key=lambda e: e.ts,
            )
            if not start_entries:
                continue  # no start entry -> duration_min is None, nothing to clamp against
            first_start = start_entries[0].ts
            # Replay the historical moment 'done' actually happened: now =
            # first_start + the ACTUAL recorded duration, so _compute_actuals
            # reproduces the same duration_min the real event recorded.
            now = first_start + timedelta(minutes=task["actual_duration_min"])

            actuals = _compute_actuals(task["task_id"], entries, now=now)

            assert actuals.duration_min is not None
            assert actuals.active_min is not None
            assert actuals.active_min <= actuals.duration_min, (
                f"{task['task_id']}: active_min={actuals.active_min} > "
                f"duration_min={actuals.duration_min}"
            )
            checked += 1
        assert checked >= 20, "property check should exercise most of the fixture"
