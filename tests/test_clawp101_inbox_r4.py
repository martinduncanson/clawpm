"""CLAWP-101 / PR #88 Codex r4: the crash-retry key must be provably findable.

The dedupe stamp (``source_request`` in a task's frontmatter) only works if
(a) the serialised frontmatter still parses with the stamp in it, and (b) a scan
that cannot read a task file never concludes "absent".
"""

from __future__ import annotations

import pytest

from clawpm.frontmatter import split_frontmatter
from clawpm.inbox import (
    TaskRequestError,
    build_task_request_payload,
    materialize_task_requests,
    read_inbox,
)

from test_clawp101_inbox_hardening import GOOD, _raw_append, _req, _task_files


def _run(isolated_portfolio):
    return materialize_task_requests(isolated_portfolio.config, "a2")


# --- P2: '---' inside a frontmatter-bound field --------------------------------


@pytest.mark.parametrize("field,value", [
    ("scope", ["src/---/x"]),
    ("tags", ["a---b"]),
    ("depends", ["CLAWP---001"]),
    ("success_criteria", ["done --- really"]),
    ("predict_approach", "step one --- step two"),
    ("pre_mortem", "---"),
])
def test_fence_in_frontmatter_field_rejected_at_boundary(field, value):
    with pytest.raises(TaskRequestError, match="---"):
        build_task_request_payload(title="T", **{field: value})


def test_fence_in_scope_rejected_end_to_end_no_task(isolated_portfolio):
    root = isolated_portfolio.root
    ev = _req(GOOD)
    ev["payload"]["scope"] = ["src/---/x"]
    _raw_append(root, "a2", ev)

    out = _run(isolated_portfolio)

    assert [r["msg_id"] for r in out["rejected"]] == [GOOD]
    assert _task_files(isolated_portfolio.tasks_dir) == []
    assert read_inbox(root, "a2") == []  # permanent refusal, acked with a reason


def test_fence_in_body_fields_still_allowed(isolated_portfolio):
    # title and description land after the closing fence; a markdown rule there is fine.
    root = isolated_portfolio.root
    ev = _req(GOOD)
    ev["payload"].update(title="A --- B", description="intro\n\n---\n\nmore")
    _raw_append(root, "a2", ev)

    out = _run(isolated_portfolio)

    assert len(out["materialized"]) == 1
    (path,) = _task_files(isolated_portfolio.tasks_dir)
    fm, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert fm["source_request"] == GOOD


def test_msg_id_with_fence_rejected(isolated_portfolio):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req("INBOX---x"))

    out = _run(isolated_portfolio)

    assert len(out["rejected"]) == 1
    assert _task_files(isolated_portfolio.tasks_dir) == []


def test_add_task_refuses_stamp_it_cannot_round_trip(isolated_portfolio):
    # Defence in depth: whatever slips past the boundary, a stamp that does not
    # survive serialisation must fail loudly instead of writing an undedupable task.
    from clawpm.tasks import add_task_with_status

    with pytest.raises(ValueError, match="source_request"):
        add_task_with_status(
            isolated_portfolio.config, "test", "T",
            scope=["a/---/b"], source_request=GOOD,
        )
    assert _task_files(isolated_portfolio.tasks_dir) == []


# --- P2: an unreadable task file must not read as "no such task" ----------------


def _transient(winerror: int) -> PermissionError:
    exc = PermissionError(13, "sharing violation")
    exc.winerror = winerror  # attribute only exists natively on Windows
    return exc


def _stamped_task(isolated_portfolio):
    from clawpm.tasks import add_task_with_status

    task, _ = add_task_with_status(
        isolated_portfolio.config, "test", "T", source_request=GOOD,
    )
    return task


def _patch_reads(monkeypatch, task_path, make_exc, fail_times):
    from pathlib import Path

    real = Path.read_text
    calls = {"n": 0}

    def fake(self, *a, **k):
        if self.resolve() == task_path.resolve():
            calls["n"] += 1
            if fail_times is None or calls["n"] <= fail_times:
                raise make_exc()
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", fake)
    return calls


def test_unreadable_task_leaves_request_pending_no_duplicate(isolated_portfolio, monkeypatch):
    task = _stamped_task(isolated_portfolio)
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    _patch_reads(monkeypatch, task.file_path, lambda: _transient(32), None)  # never readable

    out = _run(isolated_portfolio)

    assert out["materialized"] == [] and out["rejected"] == []
    assert [f["msg_id"] for f in out["failed"]] == [GOOD]
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1  # no duplicate task
    assert [m["msg_id"] for m in read_inbox(root, "a2")] == [GOOD]  # still pending


def test_non_transient_read_error_also_fails_pending(isolated_portfolio, monkeypatch):
    task = _stamped_task(isolated_portfolio)
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    _patch_reads(monkeypatch, task.file_path, lambda: PermissionError(13, "denied"), None)

    out = _run(isolated_portfolio)

    assert [f["msg_id"] for f in out["failed"]] == [GOOD]
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    assert [m["msg_id"] for m in read_inbox(root, "a2")] == [GOOD]


def test_transient_read_error_is_retried_and_task_found(isolated_portfolio, monkeypatch):
    task = _stamped_task(isolated_portfolio)
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    calls = _patch_reads(monkeypatch, task.file_path, lambda: _transient(32), 2)

    out = _run(isolated_portfolio)

    assert calls["n"] >= 3
    assert out["materialized"][0]["resumed"] is True
    assert out["materialized"][0]["task_id"] == task.id
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    assert read_inbox(root, "a2") == []


def test_in_lock_recheck_also_fails_loudly(isolated_portfolio, monkeypatch):
    # The creation-lock recheck shares the scan, so it must not allocate either.
    from clawpm.tasks import add_task_with_status

    task = _stamped_task(isolated_portfolio)
    _patch_reads(monkeypatch, task.file_path, lambda: _transient(32), None)

    with pytest.raises(OSError):
        add_task_with_status(
            isolated_portfolio.config, "test", "T2", source_request=GOOD,
        )
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1


def test_file_vanishing_mid_scan_restarts_scan(isolated_portfolio, monkeypatch):
    # A concurrent state change renames the stamped file; one miss must not read as absent.
    from clawpm.tasks import find_task_by_source_request

    task = _stamped_task(isolated_portfolio)
    _patch_reads(monkeypatch, task.file_path, lambda: FileNotFoundError(2, "gone"), 1)

    found = find_task_by_source_request(isolated_portfolio.config, "test", GOOD)

    assert found is not None and found.id == task.id
