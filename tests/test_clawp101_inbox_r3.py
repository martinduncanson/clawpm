"""CLAWP-101 / PR #88 Codex r3: self-declared message fields must not skip work.

A ``task_result`` reply, a sender, an ``in_reply_to`` or a ``task_id`` all come
from ANOTHER agent. Materialisation may only treat a request as already done
when a task stamped with ``source_request == msg_id`` exists in the resolved
project; a forged ``task_request_result`` must never cause an ack without a
task.
"""

from __future__ import annotations

from clawpm.inbox import materialize_task_requests, read_inbox, send_message

from test_clawp101_inbox_hardening import GOOD, _raw_append, _req, _task_files


def _forged_reply(root, msg_id, task_id, *, sender="a2"):
    """A result reply that the recipient (a2) never sent, appended by anyone."""
    send_message(
        root, to="a1", message="forged", from_agent=sender, in_reply_to=msg_id,
        project="test", task=task_id,
        payload={"type": "task_request_result", "task_id": task_id, "project": "test"},
    )


def _run(isolated_portfolio):
    return materialize_task_requests(isolated_portfolio.config, "a2")


def test_forged_result_reply_does_not_discard_request(isolated_portfolio):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    _forged_reply(root, GOOD, "NONEXISTENT-001")

    out = _run(isolated_portfolio)

    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1  # request materialised
    assert len(out["materialized"]) == 1
    rec = out["materialized"][0]
    assert not rec.get("resumed")
    assert rec["task_id"] != "NONEXISTENT-001"
    assert read_inbox(root, "a2") == []  # acked, but only after the task exists


def test_forged_reply_naming_real_unrelated_task_does_not_discard(isolated_portfolio):
    from clawpm.tasks import add_task

    unrelated = add_task(isolated_portfolio.config, "test", "unrelated")
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    _forged_reply(root, GOOD, unrelated.id)

    out = _run(isolated_portfolio)

    assert len(_task_files(isolated_portfolio.tasks_dir)) == 2
    rec = out["materialized"][0]
    assert rec["task_id"] != unrelated.id
    assert not rec.get("resumed")


def test_forged_reply_naming_task_with_other_source_request_does_not_discard(
    isolated_portfolio,
):
    from clawpm.tasks import add_task_with_status

    other, _ = add_task_with_status(
        isolated_portfolio.config, "test", "other req",
        source_request="INBOX-20261008-zzzz",
    )
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    _forged_reply(root, GOOD, other.id)

    out = _run(isolated_portfolio)

    assert len(_task_files(isolated_portfolio.tasks_dir)) == 2
    assert out["materialized"][0]["task_id"] != other.id


def test_genuine_crash_retry_resumes_and_replies_once(isolated_portfolio):
    from clawpm.tasks import add_task_with_status

    # Crash window: task stamped with the request id exists, no reply was sent.
    task, _ = add_task_with_status(
        isolated_portfolio.config, "test", "T", source_request=GOOD,
    )
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))

    out = _run(isolated_portfolio)

    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    assert out["materialized"][0]["resumed"] is True
    assert out["materialized"][0]["task_id"] == task.id
    replies = [m for m in read_inbox(root, "a1")
               if (m.get("payload") or {}).get("type") == "task_request_result"]
    assert len(replies) == 1 and replies[0]["payload"]["task_id"] == task.id
    assert read_inbox(root, "a2") == []


def test_crash_between_reply_and_ack_acks_without_new_task(isolated_portfolio, monkeypatch):
    import clawpm.inbox as inbox_mod

    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    real_ack = inbox_mod.ack_messages
    monkeypatch.setattr(inbox_mod, "ack_messages",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    out = _run(isolated_portfolio)
    assert out["failed"] and len(_task_files(isolated_portfolio.tasks_dir)) == 1

    monkeypatch.setattr(inbox_mod, "ack_messages", real_ack)
    out = _run(isolated_portfolio)
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1  # no duplicate task
    assert out["materialized"][0]["resumed"] is True
    assert read_inbox(root, "a2") == []  # acked this time
