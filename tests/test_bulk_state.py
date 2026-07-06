"""Tests for bulk state operations via varargs task ids (CLAWP-083).

Every state command (`done`/`start`/`block`/`unblock` and `tasks state`) accepts
one OR many task ids. A single id preserves the historical output contract; many
ids run with per-task error isolation, emit an aggregate JSON envelope, and exit
non-zero if ANY transition failed.
"""

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_project(projects_dir: Path, pid: str, name: str) -> None:
    project_dir = projects_dir / f"{pid}-project"
    project_dir.mkdir()
    meta = project_dir / ".project"
    meta.mkdir()
    (meta / "settings.toml").write_text(
        f'id = "{pid}"\nname = "{name}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()
    (tasks_dir / "rejected").mkdir()


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_bulkstate_test_")
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
    _make_project(projects_dir, "test", "Test Project")
    _make_project(projects_dir, "other", "Other Project")

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)

    yield {"root": portfolio_root, "config": config}

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _add(runner, project: str, title: str) -> str:
    r = runner.invoke(main, ["-p", project, "tasks", "add", "-t", title])
    assert r.exit_code == 0, r.output
    return json.loads(r.output)["data"]["id"]


def _worklog(root: Path) -> list[dict]:
    wl = root / "work_log.jsonl"
    if not wl.exists():
        return []
    return [
        json.loads(line)
        for line in wl.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ---------------------------------------------------------------------------
# Single-task backward compatibility
# ---------------------------------------------------------------------------


class TestSingleTaskBackwardCompat:
    def test_done_single_preserves_output_shape(self, temp_portfolio):
        runner = CliRunner()
        tid = _add(runner, "test", "One")
        r = runner.invoke(main, ["-p", "test", "done", tid])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["data"]["id"] == tid
        assert payload["data"]["state"] == "done"
        # No batch envelope keys leak into the single-task shape.
        assert "results" not in payload
        assert "summary" not in payload

    def test_tasks_state_single_error_exit_1(self, temp_portfolio):
        runner = CliRunner()
        r = runner.invoke(main, ["-p", "test", "tasks", "state", "TEST-999", "done"])
        assert r.exit_code == 1
        payload = json.loads(r.output)
        assert payload["error"] == "task_not_found"


# ---------------------------------------------------------------------------
# Bulk success
# ---------------------------------------------------------------------------


class TestBulkSuccess:
    def test_done_multiple_transitions_each(self, temp_portfolio):
        runner = CliRunner()
        ids = [_add(runner, "test", f"T{i}") for i in range(3)]
        r = runner.invoke(main, ["-p", "test", "done", *ids])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["summary"] == {"total": 3, "succeeded": 3, "failed": 0}
        got = {res["task_id"]: res for res in payload["results"]}
        for tid in ids:
            assert got[tid]["ok"] is True
            assert got[tid]["data"]["state"] == "done"

    def test_tasks_state_bulk_progress(self, temp_portfolio):
        runner = CliRunner()
        ids = [_add(runner, "test", f"P{i}") for i in range(2)]
        r = runner.invoke(main, ["-p", "test", "tasks", "state", *ids, "progress"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["state"] == "progress"
        assert all(res["ok"] for res in payload["results"])

    def test_block_bulk(self, temp_portfolio):
        runner = CliRunner()
        ids = [_add(runner, "test", f"B{i}") for i in range(2)]
        r = runner.invoke(main, ["-p", "test", "block", *ids, "-n", "waiting"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert all(res["data"]["state"] == "blocked" for res in payload["results"])

    def test_worklog_and_reflection_fire_per_task(self, temp_portfolio):
        runner = CliRunner()
        root = temp_portfolio["root"]
        ids = [_add(runner, "test", f"W{i}") for i in range(3)]
        r = runner.invoke(main, ["-p", "test", "done", *ids])
        assert r.exit_code == 0, r.output

        done_entries = [
            e for e in _worklog(root)
            if e.get("action") == "done" and e.get("task") in ids
        ]
        assert {e["task"] for e in done_entries} == set(ids)

        # One reflection file per completed task.
        for tid in ids:
            rf = root / "reflections" / f"{tid}.jsonl"
            assert rf.exists(), f"missing reflection for {tid}"
            events = [json.loads(l) for l in rf.read_text(encoding="utf-8").splitlines() if l.strip()]
            assert any(ev.get("event") == "task_done" for ev in events)


# ---------------------------------------------------------------------------
# Mixed success / failure + honest exit codes
# ---------------------------------------------------------------------------


class TestMixedBatch:
    def test_mixed_success_and_failure_exit_nonzero(self, temp_portfolio):
        runner = CliRunner()
        good = _add(runner, "test", "Good")
        r = runner.invoke(main, ["-p", "test", "done", good, "TEST-998"])
        assert r.exit_code == 1
        payload = json.loads(r.output)
        assert payload["status"] == "error"
        assert payload["summary"] == {"total": 2, "succeeded": 1, "failed": 1}
        by_id = {res["task_id"]: res for res in payload["results"]}
        assert by_id[good]["ok"] is True
        assert by_id["TEST-998"]["ok"] is False
        assert by_id["TEST-998"]["error"] == "task_not_found"

    def test_one_failure_does_not_abort_the_rest(self, temp_portfolio):
        runner = CliRunner()
        a = _add(runner, "test", "A")
        b = _add(runner, "test", "B")
        # Missing id in the MIDDLE must not stop C from transitioning.
        c = _add(runner, "test", "C")
        r = runner.invoke(main, ["-p", "test", "done", a, "TEST-997", b, c])
        assert r.exit_code == 1
        payload = json.loads(r.output)
        assert payload["summary"]["succeeded"] == 3
        assert payload["summary"]["failed"] == 1
        succeeded_ids = {res["task_id"] for res in payload["results"] if res["ok"]}
        assert succeeded_ids == {a, b, c}


# ---------------------------------------------------------------------------
# Cross-project id safety
# ---------------------------------------------------------------------------


class TestCrossProjectSafety:
    def test_bulk_scoped_to_one_project(self, temp_portfolio):
        runner = CliRunner()
        # Same numeric id exists in two projects.
        test_id = _add(runner, "test", "In test")
        other_id = _add(runner, "other", "In other")
        assert test_id.split("-")[-1] == other_id.split("-")[-1]

        # Bulk-done in `test` must not touch the same-numbered task in `other`.
        r = runner.invoke(main, ["-p", "test", "done", test_id])
        assert r.exit_code == 0, r.output

        r_other = runner.invoke(main, ["-p", "other", "tasks", "show", other_id])
        assert r_other.exit_code == 0, r_other.output
        assert json.loads(r_other.output)["state"] == "open"


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


class TestDedup:
    def test_repeated_id_processed_once(self, temp_portfolio):
        runner = CliRunner()
        tid = _add(runner, "test", "Dup")
        # Three ids supplied -> batch shape; dedup collapses to one result.
        r = runner.invoke(main, ["-p", "test", "done", tid, tid, tid])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["summary"]["total"] == 1
        assert len(payload["results"]) == 1
        assert payload["results"][0]["ok"] is True


# ---------------------------------------------------------------------------
# Interactive-input-refusal policy: bulk reject
# ---------------------------------------------------------------------------


class TestBulkRejectRefusal:
    def test_bulk_reject_refused(self, temp_portfolio):
        runner = CliRunner()
        a = _add(runner, "test", "RA")
        b = _add(runner, "test", "RB")
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "state", a, b, "rejected", "-r", "nope"],
        )
        assert r.exit_code == 2
        payload = json.loads(r.output)
        assert payload["error"] == "bulk_reject_unsupported"
        # Neither task was mutated.
        for tid in (a, b):
            g = runner.invoke(main, ["-p", "test", "tasks", "show", tid])
            assert json.loads(g.output)["state"] == "open"

    def test_single_reject_still_works(self, temp_portfolio):
        runner = CliRunner()
        a = _add(runner, "test", "Solo reject")
        r = runner.invoke(
            main,
            ["-p", "test", "tasks", "state", a, "rejected", "-r", "superseded by X"],
        )
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["status"] == "ok"
        assert payload["data"]["state"] == "rejected"

    def test_single_reject_missing_rationale_errors(self, temp_portfolio):
        runner = CliRunner()
        a = _add(runner, "test", "No rationale")
        r = runner.invoke(main, ["-p", "test", "tasks", "state", a, "rejected"])
        assert r.exit_code == 1
        assert json.loads(r.output)["error"] == "rationale_required"


# ---------------------------------------------------------------------------
# unblock bulk with per-task isolation
# ---------------------------------------------------------------------------


class TestSecondaryFailureIsolation:
    """A durable primary state change must not be turned into a batch-aborting
    traceback by a failing secondary work-log append (Grok review, CLAWP-083)."""

    def test_worklog_append_failure_does_not_abort_batch(self, temp_portfolio, monkeypatch):
        import clawpm.cli.tasks as cli_mod

        runner = CliRunner()
        ids = [_add(runner, "test", f"L{i}") for i in range(3)]

        def _boom(*_a, **_k):
            raise OSError("simulated work_log append failure")

        monkeypatch.setattr(cli_mod, "add_entry", _boom)
        r = runner.invoke(main, ["-p", "test", "done", *ids])
        # All three still transition (state file moves are durable); the batch
        # is not aborted, and each carries a log_errors marker.
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert payload["summary"] == {"total": 3, "succeeded": 3, "failed": 0}
        for res in payload["results"]:
            assert res["ok"] is True
            assert res["data"]["state"] == "done"
            assert res["data"]["log_errors"], "expected a log_errors marker"

    def test_unexpected_exception_isolated_in_batch(self, temp_portfolio, monkeypatch):
        import clawpm.cli.tasks as cli_mod

        runner = CliRunner()
        a = _add(runner, "test", "A")
        b = _add(runner, "test", "B")
        real = cli_mod.change_task_state

        def flaky(config, project_id, task_id, new_state, **kw):
            if task_id == a:
                raise RuntimeError("boom unexpected")
            return real(config, project_id, task_id, new_state, **kw)

        monkeypatch.setattr(cli_mod, "change_task_state", flaky)
        r = runner.invoke(main, ["-p", "test", "done", a, b])
        # Unexpected exception on `a` is isolated; `b` still transitions.
        assert r.exit_code == 1
        payload = json.loads(r.output)
        by_id = {res["task_id"]: res for res in payload["results"]}
        assert by_id[a]["ok"] is False
        assert by_id[a]["error"] == "unexpected_error"
        assert by_id[a]["error_class"] == "RuntimeError"
        assert by_id[b]["ok"] is True

    def test_unexpected_exception_propagates_in_single(self, temp_portfolio, monkeypatch):
        import clawpm.cli.tasks as cli_mod

        runner = CliRunner()
        a = _add(runner, "test", "Solo")

        def boom(*_a, **_k):
            raise RuntimeError("boom")

        monkeypatch.setattr(cli_mod, "change_task_state", boom)
        r = runner.invoke(main, ["-p", "test", "done", a])
        # Single-task: preserve traceback semantics (exception propagates).
        assert r.exit_code != 0
        assert isinstance(r.exception, RuntimeError)

    def test_reflection_failure_marker(self, temp_portfolio, monkeypatch):
        import clawpm.reflect as reflect_mod

        runner = CliRunner()
        a = _add(runner, "test", "Refl")

        def boom(*_a, **_k):
            raise OSError("reflection write failed")

        monkeypatch.setattr(reflect_mod, "write_reflection_event", boom)
        r = runner.invoke(main, ["-p", "test", "done", a])
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data.get("reflection_errors"), "expected a reflection_errors marker"

    def test_dispatch_teardown_failure_marker(self, temp_portfolio, monkeypatch):
        # A failure building the dispatch-teardown candidate set must not turn
        # an already-durable done into a failed result (Codex P2 + Grok HIGH).
        import clawpm.dispatch as dispatch_mod

        runner = CliRunner()
        a = _add(runner, "test", "TD")

        def boom(*_a, **_k):
            raise OSError("dispatch registry read failed")

        monkeypatch.setattr(dispatch_mod, "active_dispatch_dirs", boom)
        r = runner.invoke(main, ["-p", "test", "done", a])
        assert r.exit_code == 0, r.output
        data = json.loads(r.output)["data"]
        assert data["state"] == "done"
        assert data.get("dispatch_teardown_errors"), "expected a teardown marker"

    def test_text_mode_surfaces_degraded_marker(self, temp_portfolio, monkeypatch):
        import clawpm.cli.tasks as cli_mod

        runner = CliRunner()
        ids = [_add(runner, "test", f"D{i}") for i in range(2)]

        def _boom(*_a, **_k):
            raise OSError("worklog append failure")

        monkeypatch.setattr(cli_mod, "add_entry", _boom)
        r = runner.invoke(main, ["--format", "text", "done", "-p", "test", *ids])
        assert r.exit_code == 0, r.output
        assert "degraded" in r.output


class TestUnblockBulk:
    def test_unblock_bulk_mixed(self, temp_portfolio):
        runner = CliRunner()
        root = temp_portfolio["root"]
        blocked1 = _add(runner, "test", "Blk1")
        blocked2 = _add(runner, "test", "Blk2")
        open_task = _add(runner, "test", "StillOpen")
        runner.invoke(main, ["-p", "test", "block", blocked1, blocked2])

        r = runner.invoke(main, ["-p", "test", "unblock", blocked1, blocked2, open_task])
        assert r.exit_code == 1  # open_task is not blocked -> one failure
        payload = json.loads(r.output)
        by_id = {res["task_id"]: res for res in payload["results"]}
        assert by_id[blocked1]["ok"] is True
        assert by_id[blocked2]["ok"] is True
        assert by_id[open_task]["ok"] is False
        assert by_id[open_task]["error"] == "not_blocked"

        # Both unblocked tasks returned to open, and each logged an unblock entry.
        unblock_entries = [
            e for e in _worklog(root) if e.get("action") == "unblock"
        ]
        assert {e["task"] for e in unblock_entries} == {blocked1, blocked2}

    def test_unblock_single_not_blocked_exit_1(self, temp_portfolio):
        runner = CliRunner()
        t = _add(runner, "test", "Open")
        r = runner.invoke(main, ["-p", "test", "unblock", t])
        assert r.exit_code == 1
        assert json.loads(r.output)["error"] == "not_blocked"

    def test_unblock_start_flag_bulk(self, temp_portfolio):
        runner = CliRunner()
        b1 = _add(runner, "test", "S1")
        b2 = _add(runner, "test", "S2")
        runner.invoke(main, ["-p", "test", "block", b1, b2])
        r = runner.invoke(main, ["-p", "test", "unblock", b1, b2, "--start"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert all(res["data"]["state"] == "progress" for res in payload["results"])
