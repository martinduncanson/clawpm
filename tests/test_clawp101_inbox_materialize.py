"""CLAWP-101 -- structured inbox payload + `inbox materialize`.

a1 sends a ``task_request`` payload to a2's inbox (portfolio root, outside any
project's git tree); a2 materializes it into a real task from its own context.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.inbox import (
    TaskRequestError,
    build_task_request_payload,
    read_inbox,
    send_message,
)


def _task_files(tasks_dir: Path) -> list[Path]:
    return sorted(p for p in tasks_dir.rglob("*.md"))


def _snapshot(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _invoke(*args: str, input: str | None = None):
    r = CliRunner().invoke(main, list(args), input=input)
    return r


def _json(r):
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


# --- schema is additive ----------------------------------------------------


def test_plain_message_has_no_payload_key(isolated_portfolio):
    ev = send_message(isolated_portfolio.root, to="a2", message="hi", from_agent="a1")
    assert "payload" not in ev
    assert read_inbox(isolated_portfolio.root, "a2")[0].get("payload") is None


def test_send_message_accepts_payload(isolated_portfolio):
    ev = send_message(
        isolated_portfolio.root, to="a2", message="m", from_agent="a1",
        payload={"type": "task_request", "title": "T"},
    )
    stored = read_inbox(isolated_portfolio.root, "a2")[0]
    assert stored["payload"] == {"type": "task_request", "title": "T"}
    assert ev["payload"] == stored["payload"]


def test_cli_plain_send_unchanged(isolated_portfolio):
    out = _json(_invoke("inbox", "send", "--to", "a2", "--message", "hello"))
    assert out["msg_id"].startswith("INBOX-")
    assert "payload" not in read_inbox(isolated_portfolio.root, "a2")[0]


# --- payload builder / validation -----------------------------------------


def test_build_payload_requires_title():
    with pytest.raises(TaskRequestError):
        build_task_request_payload(title=None)
    with pytest.raises(TaskRequestError):
        build_task_request_payload(title="   ")


@pytest.mark.parametrize("kw", [
    {"priority": 0}, {"priority": 11}, {"confidence": 6}, {"complexity": "xxl"},
])
def test_build_payload_rejects_bad_values(kw):
    with pytest.raises(TaskRequestError):
        build_task_request_payload(title="T", **kw)


def test_cli_task_request_requires_title(isolated_portfolio):
    r = _invoke("inbox", "send", "--to", "a2", "--message", "x", "--task-request")
    assert r.exit_code != 0
    assert read_inbox(isolated_portfolio.root, "a2") == []


def test_cli_task_request_builds_payload(isolated_portfolio):
    _json(_invoke(
        "inbox", "send", "--to", "a2", "--from", "a1", "--message", "please",
        "--project", "test", "--task-request", "--title", "Do the thing",
        "--priority", "3", "--complexity", "s", "--predict-duration", "2h",
        "--success-criteria", "tests pass", "--success-criteria", "docs updated",
        "--scope", "src/x/*.py", "--confidence", "4",
    ))
    msg = read_inbox(isolated_portfolio.root, "a2")[0]
    p = msg["payload"]
    assert p["type"] == "task_request"
    assert p["title"] == "Do the thing"
    assert p["priority"] == 3
    assert p["complexity"] == "s"
    assert p["predict_duration"] == "2h"
    assert p["success_criteria"] == ["tests pass", "docs updated"]
    assert p["scope"] == ["src/x/*.py"]
    assert p["confidence"] == 4


# --- materialize: the round trip -------------------------------------------


def _send_request(root, title="Handoff task", **extra):
    payload = {"type": "task_request", "title": title, "priority": 2,
               "success_criteria": ["it works"], "predict_duration": "1h", **extra}
    return send_message(root, to="a2", message="please do", from_agent="a1",
                        project="test", payload=payload)


def test_roundtrip_creates_scoped_task_with_no_a1_project_writes(isolated_portfolio):
    proj = isolated_portfolio.project_dir / ".project"
    before = _snapshot(proj)

    # a1 side: only an inbox send. Project tree must be byte-identical after.
    sent = _send_request(isolated_portfolio.root)
    assert _snapshot(proj) == before

    # a2 side
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert len(out["materialized"]) == 1
    rec = out["materialized"][0]
    assert rec["msg_id"] == sent["msg_id"]
    assert rec["project"] == "test"
    task_id = rec["task_id"]

    files = _task_files(isolated_portfolio.tasks_dir)
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert task_id in text and "Handoff task" in text
    assert "it works" in text  # success criteria persisted
    assert "priority: 2" in text

    # acked
    assert read_inbox(isolated_portfolio.root, "a2") == []
    # replied to a1 via in_reply_to with the new id
    replies = read_inbox(isolated_portfolio.root, "a1")
    assert len(replies) == 1
    assert replies[0]["in_reply_to"] == sent["msg_id"]
    assert replies[0]["from"] == "a2"
    assert task_id in replies[0]["message"]
    assert replies[0]["payload"]["type"] == "task_request_result"
    assert replies[0]["payload"]["task_id"] == task_id


def test_materialize_is_idempotent(isolated_portfolio):
    _send_request(isolated_portfolio.root)
    _json(_invoke("inbox", "materialize", "--agent", "a2"))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["materialized"] == []
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1


def test_dry_run_is_side_effect_free(isolated_portfolio):
    _send_request(isolated_portfolio.root)
    snap = _snapshot(isolated_portfolio.root)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2", "--dry-run"))
    assert out["dry_run"] is True
    assert len(out["would_create"]) == 1
    assert out["would_create"][0]["title"] == "Handoff task"
    assert out["would_create"][0]["project"] == "test"
    assert out["materialized"] == []
    assert _snapshot(isolated_portfolio.root) == snap  # inbox + projects untouched
    assert _task_files(isolated_portfolio.tasks_dir) == []


def test_plain_and_other_type_messages_left_pending(isolated_portfolio):
    send_message(isolated_portfolio.root, to="a2", message="just text", from_agent="a1")
    send_message(isolated_portfolio.root, to="a2", message="x", from_agent="a1",
                 payload={"type": "something_else"})
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["materialized"] == []
    assert len(read_inbox(isolated_portfolio.root, "a2")) == 2


def test_invalid_payload_is_rejected_replied_and_acked(isolated_portfolio):
    bad = send_message(isolated_portfolio.root, to="a2", message="x", from_agent="a1",
                       project="test", payload={"type": "task_request", "priority": 99,
                                                 "title": "T"})
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["materialized"] == []
    assert out["rejected"][0]["msg_id"] == bad["msg_id"]
    assert _task_files(isolated_portfolio.tasks_dir) == []
    assert read_inbox(isolated_portfolio.root, "a2") == []
    reply = read_inbox(isolated_portfolio.root, "a1")[0]
    assert reply["in_reply_to"] == bad["msg_id"]
    assert reply["payload"]["type"] == "task_request_rejected"


def test_unknown_project_left_pending_and_reported(isolated_portfolio):
    m = send_message(isolated_portfolio.root, to="a2", message="x", from_agent="a1",
                     project="no-such-project",
                     payload={"type": "task_request", "title": "T"})
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["materialized"] == []
    assert out["failed"][0]["msg_id"] == m["msg_id"]
    assert [x["msg_id"] for x in read_inbox(isolated_portfolio.root, "a2")] == [m["msg_id"]]
    assert read_inbox(isolated_portfolio.root, "a1") == []


def test_payload_cannot_inject_unknown_fields(isolated_portfolio):
    """Only whitelisted keys reach add_task; extras are ignored, not executed."""
    _send_request(isolated_portfolio.root, task_id="HIJACK-999", tags=["ok"],
                  evil="rm -rf")
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["materialized"][0]["task_id"] != "HIJACK-999"
