"""Tests for dependency cascade auto-unblock (CLAWP-020)."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import (
    add_task,
    cascade_unblock_dependents,
    change_task_state,
    get_task,
)


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_cascade_test_")
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
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Direct function tests
# ---------------------------------------------------------------------------


def _add_with_depends(config, project_id, title, depends):
    """add_task accepts depends; helper just keeps the test body terse."""
    return add_task(config, project_id, title=title, depends=depends)


class TestCascadeFunction:
    def test_single_dep_satisfied_promotes_blocked_to_open(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])

        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

        # Complete the parent, then cascade
        change_task_state(config, "test", parent.id, TaskState.DONE)
        transitions = cascade_unblock_dependents(config, "test", parent.id)

        assert len(transitions) == 1
        assert transitions[0]["task_id"] == child.id
        assert transitions[0]["from_state"] == "blocked"
        assert transitions[0]["to_state"] == "open"
        assert transitions[0]["trigger"] == parent.id
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_multi_dep_only_promotes_when_all_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="Dep A")
        b = add_task(config, "test", title="Dep B")
        child = _add_with_depends(config, "test", "Child", depends=[a.id, b.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)

        # Complete A only — child must remain blocked.
        change_task_state(config, "test", a.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", a.id)
        assert trans == []
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

        # Complete B — child cascades.
        change_task_state(config, "test", b.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", b.id)
        assert len(trans) == 1
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_no_cascade_for_unrelated_done(self, temp_portfolio):
        config = temp_portfolio["config"]
        unrelated = add_task(config, "test", title="Unrelated")
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)

        change_task_state(config, "test", unrelated.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", unrelated.id)
        assert trans == []
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

    def test_open_task_with_dep_not_touched(self, temp_portfolio):
        """Tasks already in OPEN aren't moved; we only promote from BLOCKED."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = _add_with_depends(config, "test", "Child", depends=[parent.id])
        # child stays OPEN (operator chose not to mark blocked)

        change_task_state(config, "test", parent.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", parent.id)
        assert trans == []
        # Child was already OPEN, still OPEN
        assert get_task(config, "test", child.id).state == TaskState.OPEN

    def test_shallow_cascade_with_malformed_dep_graph(self, temp_portfolio):
        """Cascade is shallow: a cycle in the dep graph still terminates.

        The cascade visits each task at most once via the outer for-loop
        and only acts on direct-dependent edges of the completed task.
        There is no recursive descent. This test exercises a malformed
        ``A -> B -> A`` graph and asserts the call terminates with the
        expected single direct-dependent transition.
        """
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A")
        # b's depends references a; we then manually edit a to depend on b
        b = _add_with_depends(config, "test", "B", depends=[a.id])
        # Hand-edit a.md to add depends: [b.id] (simulating malformed graph)
        a_path = temp_portfolio["tasks_dir"] / f"{a.id}.md"
        text = a_path.read_text(encoding="utf-8")
        text = text.replace(
            f"id: {a.id}",
            f"depends:\n- {b.id}\nid: {a.id}",
        )
        a_path.write_text(text, encoding="utf-8")

        change_task_state(config, "test", b.id, TaskState.BLOCKED)
        change_task_state(config, "test", a.id, TaskState.BLOCKED)

        change_task_state(config, "test", a.id, TaskState.DONE, force=True)
        trans = cascade_unblock_dependents(config, "test", a.id)
        # Direct dependent of A (= B) is examined; B's only dep is A which
        # is now done, so B cascades. The cycle on A's side is irrelevant
        # because cascade is shallow.
        assert len(trans) == 1
        assert trans[0]["task_id"] == b.id

    def test_missing_dep_prevents_cascade(self, temp_portfolio):
        """Codex P1 fix: a missing dependency must NOT count as satisfied.

        A blocked task depending on `A` and a typoed `B-NOTEXIST` must
        stay blocked even when `A` is completed — dependency contract
        cannot be silently weakened by typos.
        """
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A")
        child = add_task(
            config, "test", title="Child", depends=[a.id, "TEST-9999"]
        )
        change_task_state(config, "test", child.id, TaskState.BLOCKED)

        change_task_state(config, "test", a.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", a.id)
        assert trans == []
        assert get_task(config, "test", child.id).state == TaskState.BLOCKED

    def test_indirect_dependent_does_not_cascade(self, temp_portfolio):
        """Shallow cascade: an indirect dependent (C deps on B deps on A) is
        NOT promoted when A is completed — only B's direct cascade gets it
        on the next done. This pins down the shallow-by-design contract."""
        config = temp_portfolio["config"]
        a = add_task(config, "test", title="A")
        b = _add_with_depends(config, "test", "B", depends=[a.id])
        c = _add_with_depends(config, "test", "C", depends=[b.id])
        change_task_state(config, "test", b.id, TaskState.BLOCKED)
        change_task_state(config, "test", c.id, TaskState.BLOCKED)

        change_task_state(config, "test", a.id, TaskState.DONE)
        trans = cascade_unblock_dependents(config, "test", a.id)
        # B (direct dep) cascades; C (indirect) does NOT — c.depends is [b]
        # not [a], so c is not a direct dependent of a.
        ids = {t["task_id"] for t in trans}
        assert ids == {b.id}
        assert get_task(config, "test", c.id).state == TaskState.BLOCKED


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCascadeCLI:
    def test_cli_done_emits_cascade_in_output(self, temp_portfolio):
        runner = CliRunner()
        # Create parent + blocked child
        r = runner.invoke(main, ["-p", "test", "tasks", "add", "-t", "Parent"])
        assert r.exit_code == 0
        parent_id = json.loads(r.output)["data"]["id"]

        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add", "-t", "Child", "-d", parent_id],
        )
        assert r.exit_code == 0
        child_id = json.loads(r.output)["data"]["id"]

        r = runner.invoke(
            main, ["-p", "test", "tasks", "state", child_id, "blocked"]
        )
        assert r.exit_code == 0

        # Done parent → cascade fires
        r = runner.invoke(
            main, ["-p", "test", "tasks", "state", parent_id, "done"]
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "cascade_unblocks" in payload["data"]
        assert len(payload["data"]["cascade_unblocks"]) == 1
        cu = payload["data"]["cascade_unblocks"][0]
        assert cu["task_id"] == child_id
        assert cu["from_state"] == "blocked"
        assert cu["to_state"] == "open"
        assert cu["trigger"] == parent_id

    def test_cli_emits_work_log_entry(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(main, ["-p", "test", "tasks", "add", "-t", "Parent"])
        parent_id = json.loads(r.output)["data"]["id"]
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "add", "-t", "Child", "-d", parent_id],
        )
        child_id = json.loads(r.output)["data"]["id"]
        runner.invoke(
            main, ["-p", "test", "tasks", "state", child_id, "blocked"]
        )
        runner.invoke(
            main, ["-p", "test", "tasks", "state", parent_id, "done"]
        )

        # Inspect work_log.jsonl
        worklog = temp_portfolio["root"] / "work_log.jsonl"
        assert worklog.exists()
        lines = [
            json.loads(line)
            for line in worklog.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cascade_entries = [
            e for e in lines if e.get("action") == "cascade_unblock"
        ]
        assert len(cascade_entries) == 1
        assert cascade_entries[0]["task"] == child_id
        assert parent_id in cascade_entries[0]["summary"]


# ---------------------------------------------------------------------------
# Doctor stale-blocked check
# ---------------------------------------------------------------------------


def _backdate_blocked_task(blocked_path: Path, old_date_iso: str) -> None:
    """Rewrite a blocked task file's `updated` stamp to `old_date_iso` and
    backdate its mtime 48h, for the stale-blocked doctor check. Shared by
    both tests in ``TestDoctorStaleBlocked`` so the write-and-restamp
    mechanics can't drift between them.
    """
    import re as _re
    import time as _time

    _txt = blocked_path.read_text(encoding="utf-8")
    _txt, _n = _re.subn(
        r"updated: '?\d{4}-\d{2}-\d{2}'?", f"updated: '{old_date_iso}'", _txt
    )
    assert _n == 1
    blocked_path.write_text(_txt, encoding="utf-8")
    old_ts = _time.time() - (48 * 3600)
    os.utime(blocked_path, (old_ts, old_ts))


class TestDoctorStaleBlocked:
    def test_doctor_flags_blocked_with_done_deps(self, temp_portfolio, monkeypatch):
        """A task in blocked/ whose deps are all done is stale-blocked."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = add_task(
            config, "test", title="Child", depends=[parent.id]
        )

        # Mark child blocked, parent done, but DON'T run cascade
        # (simulating historical state from before cascade landed).
        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        change_task_state(config, "test", parent.id, TaskState.DONE)

        # Backdate the child so it's past the 24h cutoff. Since CLAWP-086 the
        # stale-blocked check prefers the `updated` frontmatter stamp over the
        # (lying) file mtime, so backdate BOTH: rewrite `updated` to 2 days ago
        # (the authoritative signal — the block above stamped it to today) and
        # the mtime as the legacy fallback.
        #
        # CLAWP-123: backdate using UTC's current date, not the LOCAL
        # `date.today()` — doctor's own stale-blocked reader deliberately
        # interprets a date-only `updated` stamp as END-OF-DAY UTC
        # (project.py, CLAWP-086: "so a task blocked late on day D isn't
        # falsely reported the next morning"). `date.today()` and "UTC's
        # current date" are only the same calendar day when the test
        # happens to run in UTC or during the part of the day both agree
        # on — on a positive-UTC-offset machine, local `date.today()` can
        # already be tomorrow's UTC date for part of the day, understating
        # the 2-day backdate to under the 24h cutoff and flaking this test
        # (see test_local_date_today_backdating_can_understate_utc_staleness
        # below for a deterministic reproduction of exactly this). Deriving
        # the backdate from UTC's date instead matches the reader's own
        # interpretation exactly, so the gap is always >= 24h regardless of
        # the test machine's timezone or time of day.
        child_blocked_path = (
            temp_portfolio["tasks_dir"] / "blocked" / f"{child.id}.md"
        )
        assert child_blocked_path.exists()
        _old_date = (
            datetime.now(timezone.utc).date() - timedelta(days=2)
        ).isoformat()
        _backdate_blocked_task(child_blocked_path, _old_date)

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        sb_ids = [sb["task_id"] for sb in payload.get("stale_blocked", [])]
        assert child.id in sb_ids

    def _fake_local_date_one_day_ahead_of_utc(self, monkeypatch) -> date:
        """Simulate a machine whose LOCAL calendar date already reads one
        day ahead of the REAL current UTC date -- realistic for large
        positive UTC offsets shortly after local midnight (e.g. UTC+14).

        Rebinds this test MODULE's own `date` name (not the builtin type
        itself, which is immutable and can't be monkeypatched directly --
        confirmed: ``date.today = ...`` raises ``TypeError``) to a
        subclass whose ``today()`` is faked. `datetime.now(timezone.utc)`
        -- what doctor's own stale-blocked reader and this test's
        `real_utc_today` both actually call -- is left completely
        untouched, so the two tests below exercise the REAL
        interpretation gap between two genuinely different clock reads,
        not a fully mocked clock. Returns the real UTC date the fake is
        anchored to, for the tests' own assertions.
        """
        real_utc_today = datetime.now(timezone.utc).date()

        class _FakeDate(date):
            @classmethod
            def today(cls):
                return real_utc_today + timedelta(days=1)

        monkeypatch.setattr(sys.modules[__name__], "date", _FakeDate)
        return real_utc_today

    def test_local_date_today_backdating_can_understate_utc_staleness(
        self, temp_portfolio, monkeypatch
    ):
        """CLAWP-123: deterministic reproduction of the flake in
        ``test_doctor_flags_blocked_with_done_deps`` before its fix
        (backdating via LOCAL `date.today()` instead of UTC's current
        date) -- a permanent record of the confirmed bug pattern, so a
        future edit can't silently reintroduce it into that test.

        Doctor's stale-blocked reader treats a date-only `updated` stamp
        as END-OF-DAY UTC on that date (project.py, CLAWP-086). The
        pre-fix test computed its 2-day backdate from `date.today()` --
        the LOCAL calendar date. Those two are the same calendar day only
        part of the time on a positive-UTC-offset machine: for part of
        each day, local `date.today()` is already "tomorrow" relative to
        UTC's current date, understating a "2 days ago" backdate to only
        ONE real UTC day back once reinterpreted as UTC end-of-day --
        under the 24h STALE_BLOCKED_HOURS cutoff for virtually any time
        of day the check runs, so the task is NOT flagged and the
        pre-fix test flaked.
        """
        real_utc_today = self._fake_local_date_one_day_ahead_of_utc(monkeypatch)

        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = add_task(config, "test", title="Child", depends=[parent.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        change_task_state(config, "test", parent.id, TaskState.DONE)

        # The pre-CLAWP-123 computation: 2 LOCAL days back.
        child_blocked_path = (
            temp_portfolio["tasks_dir"] / "blocked" / f"{child.id}.md"
        )
        assert child_blocked_path.exists()
        _old_date = (date.today() - timedelta(days=2)).isoformat()
        # With the faked local date one day ahead of UTC, this backdates
        # to only `real_utc_today - 1` -- end-of-day UTC on YESTERDAY,
        # not two real days ago.
        assert _old_date == (real_utc_today - timedelta(days=1)).isoformat()
        _backdate_blocked_task(child_blocked_path, _old_date)

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        sb_ids = [sb["task_id"] for sb in payload.get("stale_blocked", [])]
        assert child.id not in sb_ids, (
            "expected the pre-CLAWP-123 backdating pattern to UNDER-detect "
            "staleness here (that's the bug this task fixed) -- if this "
            "now fails, either doctor's reader semantics changed or this "
            "reproduction no longer models the original flake"
        )

    def test_utc_date_backdating_is_immune_to_the_local_date_understatement(
        self, temp_portfolio, monkeypatch
    ):
        """CLAWP-123: the FIX (`test_doctor_flags_blocked_with_done_deps`'s
        current backdating, derived from ``datetime.now(timezone.utc)
        .date()`` rather than `date.today()`) correctly detects staleness
        even under the EXACT SAME adversarial local-date fake that
        defeats the pre-fix pattern in the test above -- proving the fix
        is robust against the specific mechanism that caused the flake,
        not just coincidentally passing on this machine's timezone.
        """
        self._fake_local_date_one_day_ahead_of_utc(monkeypatch)

        config = temp_portfolio["config"]
        parent = add_task(config, "test", title="Parent")
        child = add_task(config, "test", title="Child", depends=[parent.id])
        change_task_state(config, "test", child.id, TaskState.BLOCKED)
        change_task_state(config, "test", parent.id, TaskState.DONE)

        child_blocked_path = (
            temp_portfolio["tasks_dir"] / "blocked" / f"{child.id}.md"
        )
        assert child_blocked_path.exists()
        # The FIXED computation: 2 UTC days back. `date.today()` is faked
        # in this test too (via the same helper), but this call never
        # reaches it -- proving the fix doesn't merely avoid the fake by
        # accident.
        _old_date = (
            datetime.now(timezone.utc).date() - timedelta(days=2)
        ).isoformat()
        _backdate_blocked_task(child_blocked_path, _old_date)

        runner = CliRunner()
        r = runner.invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        sb_ids = [sb["task_id"] for sb in payload.get("stale_blocked", [])]
        assert child.id in sb_ids, (
            "the UTC-date-based backdate should be immune to the "
            "local-date-ahead-of-UTC scenario that broke the old pattern"
        )
