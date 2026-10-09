"""CLAWP-101 / PR #88 Codex r2: concurrent materialisers + agent-id validation.

(1) Two materialisers for one request must not create two tasks: the
``source_request`` recheck belongs inside ``add_task``'s lock, before the id is
allocated.
(2) Agent ids become filenames, so Windows device names (``NUL``, ``CON.foo``,
``COM1``...) and ``$``-before-trailing-newline matches must be refused, and the
same ``$`` hole closed in every other validator regex of the module.
"""

from __future__ import annotations

import threading

import pytest

import clawpm.tasks as tasks_mod
from clawpm.inbox import (
    materialize_task_requests,
    read_inbox,
    send_message,
    validate_agent_id,
)

from test_clawp101_inbox_hardening import GOOD, BAD, _raw_append, _req, _task_files


# --- (1) concurrent materialisers ------------------------------------------


def test_concurrent_materializers_create_one_task(isolated_portfolio, monkeypatch):
    sent = send_message(
        isolated_portfolio.root, to="a2", message="please", from_agent="a1",
        project="test", payload={"type": "task_request", "title": "Race"},
    )

    # Barrier-controlled: both threads finish the pre-check (no task yet) before
    # either one is allowed to create.
    barrier = threading.Barrier(2, timeout=10)
    real_find = tasks_mod.find_task_by_source_request

    def gated_find(*args, **kwargs):
        found = real_find(*args, **kwargs)
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return found

    monkeypatch.setattr(tasks_mod, "find_task_by_source_request", gated_find)

    results: list = [None, None]
    errors: list = []

    def run(i: int) -> None:
        try:
            results[i] = materialize_task_requests(isolated_portfolio.config, "a2")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not errors, errors

    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    recs = [r["materialized"][0] for r in results if r and r["materialized"]]
    assert len(recs) == 2, [r['failed'] for r in results]
    assert recs[0]["task_id"] == recs[1]["task_id"]
    assert sorted(bool(r.get("resumed")) for r in recs) == [False, True]
    assert all(r["msg_id"] == sent["msg_id"] for r in recs)


def test_add_task_reuses_task_for_same_source_request(isolated_portfolio):
    from clawpm.tasks import add_task

    first = add_task(isolated_portfolio.config, "test", "T", source_request="INBOX-X-1")
    second = add_task(isolated_portfolio.config, "test", "T", source_request="INBOX-X-1")
    other = add_task(isolated_portfolio.config, "test", "T", source_request="INBOX-X-2")
    assert second.id == first.id
    assert other.id != first.id
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 2


# --- (2) agent-id validation ------------------------------------------------

DEVICE_NAMES = (
    ["NUL", "CON", "PRN", "AUX"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)


@pytest.mark.parametrize("stem", DEVICE_NAMES)
@pytest.mark.parametrize("variant", [
    "{}", "{}.foo", "{}.jsonl", "{}.a.b",
])
@pytest.mark.parametrize("case", [str.upper, str.lower, str.title])
def test_device_names_refused(stem, variant, case):
    name = variant.format(case(stem))
    with pytest.raises(ValueError):
        validate_agent_id(name)


@pytest.mark.parametrize("bad", [
    "a\n", "\na", "a\r\n", "a\nb", "a ", " a", "a.", "CON.", "CON ", "nul\n", "",
])
def test_newline_trailing_space_and_dot_refused(bad):
    with pytest.raises(ValueError):
        validate_agent_id(bad)


@pytest.mark.parametrize("ok", ["a1", "main", "w-88-r2", "console", "nullable", "com10",
                                "lpt0", "CON@host", "x.y", "COM1x"])
def test_ordinary_ids_still_accepted(ok):
    assert validate_agent_id(ok) == ok


def test_send_to_device_name_refused_nothing_written(isolated_portfolio):
    with pytest.raises(ValueError):
        send_message(isolated_portfolio.root, to="NUL", message="x", from_agent="a1")
    with pytest.raises(ValueError):
        send_message(isolated_portfolio.root, to="a2", message="x", from_agent="CON.foo")
    inbox = isolated_portfolio.root / "inbox"
    assert not inbox.exists() or list(inbox.iterdir()) == []


@pytest.mark.parametrize("sender", ["NUL", "con.foo", "COM1", "a\n"])
def test_materialize_rejects_bad_sender_loudly_and_continues(isolated_portfolio, sender):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(BAD, **{"from": sender}))
    _raw_append(root, "a2", _req(GOOD))
    out = materialize_task_requests(isolated_portfolio.config, "a2")
    assert [r["msg_id"] for r in out["rejected"]] == [BAD]
    assert "malformed sender" in out["rejected"][0]["reason"]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]
    assert read_inbox(root, "a2") == []  # bad one acked, never wedges the drain
    inbox = root / "inbox"
    assert sorted(p.name for p in inbox.iterdir()) == ["a1.jsonl", "a2.jsonl"]


def test_materialize_refuses_device_named_agent(isolated_portfolio):
    with pytest.raises(ValueError):
        materialize_task_requests(isolated_portfolio.config, "NUL")


# --- other validators: `$` accepted a trailing newline ---------------------


def test_msg_id_with_trailing_newline_rejected(isolated_portfolio):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(BAD + "\n"))
    _raw_append(root, "a2", _req(GOOD))
    out = materialize_task_requests(isolated_portfolio.config, "a2")
    assert len(out["rejected"]) == 1 and "malformed msg_id" in out["rejected"][0]["reason"]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]


def test_project_with_trailing_newline_rejected(isolated_portfolio):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(BAD, project="test\n"))
    _raw_append(root, "a2", _req(GOOD))
    out = materialize_task_requests(isolated_portfolio.config, "a2")
    assert [r["msg_id"] for r in out["rejected"]] == [BAD]
    assert "malformed project" in out["rejected"][0]["reason"]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]


def test_depends_with_trailing_newline_rejected(isolated_portfolio):
    root = isolated_portfolio.root
    p = {"type": "task_request", "title": "T", "depends": ["TEST-001\n"]}
    _raw_append(root, "a2", _req(BAD, payload=p))
    out = materialize_task_requests(isolated_portfolio.config, "a2")
    assert [r["msg_id"] for r in out["rejected"]] == [BAD]
    assert _task_files(isolated_portfolio.tasks_dir) == []
