"""CLAWP-101 / PR #88 Codex r1 hardening.

Every field of a ``task_request`` message comes from ANOTHER agent, so it is
untrusted data. A bad request must be rejected loudly (reply + ack with a
reason) and must never block, or write outside the portfolio on behalf of,
later requests.
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
    materialize_task_requests,
    read_inbox,
    send_message,
)


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args))


def _json(r):
    assert r.exit_code == 0, r.output
    return json.loads(r.output)


def _task_files(tasks_dir: Path) -> list[Path]:
    return sorted(tasks_dir.rglob("*.md"))


def _raw_append(root: Path, agent: str, event: dict) -> None:
    d = root / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / f"{agent}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


GOOD = "INBOX-20261008-bbbb"
BAD = "INBOX-20261008-aaaa"


def _req(msg_id=BAD, **over):
    ev = {"event": "message", "msg_id": msg_id, "ts": "2026-10-08T00:00:00+00:00",
          "from": "a1", "to": "a2", "in_reply_to": None, "project": "test",
          "task": None, "message": "m",
          "payload": {"type": "task_request", "title": "T"}}
    ev.update(over)
    return ev


def _bad_then_good(root: Path, **payload_over):
    ev = _req()
    ev["payload"].update(payload_over)
    _raw_append(root, "a2", ev)
    _raw_append(root, "a2", _req(GOOD))


def _outside_project(root: Path) -> Path:
    outside = root.parent / "outside"
    meta = outside / ".project"
    (meta / "tasks").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        'id = "outside"\nname = "O"\nstatus = "active"\npriority = 3\n', encoding="utf-8")
    return outside


def _assert_bad_rejected_good_materialized(out, root):
    assert [r["msg_id"] for r in out["rejected"]] == [BAD]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]
    kinds = {r["in_reply_to"]: r["payload"]["type"] for r in read_inbox(root, "a1")}
    assert kinds[BAD] == "task_request_rejected"
    assert read_inbox(root, "a2") == []  # both acked


# --- P1: project must be a registered id -----------------------------------


@pytest.mark.parametrize("project", ["../../outside", "..\\..\\outside", "/abs/path", "a/b", 7, ["x"]])
def test_untrusted_project_cannot_escape_registry(isolated_portfolio, project):
    root = isolated_portfolio.root
    outside = _outside_project(root)
    _raw_append(root, "a2", _req(project=project))
    _raw_append(root, "a2", _req(GOOD))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert _task_files(outside / ".project" / "tasks") == []
    _assert_bad_rejected_good_materialized(out, root)


def test_payload_project_field_also_validated(isolated_portfolio):
    root = isolated_portfolio.root
    outside = _outside_project(root)
    ev = _req(project=None)
    ev["payload"]["project"] = "../../outside"
    _raw_append(root, "a2", ev)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert _task_files(outside / ".project" / "tasks") == []
    assert len(out["rejected"]) == 1


def test_default_project_also_validated(isolated_portfolio):
    root = isolated_portfolio.root
    outside = _outside_project(root)
    _raw_append(root, "a2", _req(project=None))
    out = materialize_task_requests(isolated_portfolio.config, "a2",
                                    default_project="../../outside")
    assert _task_files(outside / ".project" / "tasks") == []
    assert out["materialized"] == []


# --- P1: sender id must not steer the reply path ----------------------------


@pytest.mark.parametrize("sender", ["../../outside", "..\\x", "a/b", "/abs", "a:b", 5, ["a"]])
def test_untrusted_sender_cannot_redirect_reply(isolated_portfolio, sender):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(**{"from": sender}))
    _raw_append(root, "a2", _req(GOOD))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    for p in (root.parent, root.parent.parent):
        assert not (p / "outside.jsonl").exists()
    assert {p.name for p in (root / "inbox").iterdir()} <= {"a1.jsonl", "a2.jsonl"}
    assert [r["msg_id"] for r in out["rejected"]] == [BAD]
    assert "sender" in out["rejected"][0]["reason"]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]
    assert read_inbox(root, "a2") == []  # acked, cannot wedge the drain


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "", "..", "a:b", "x" * 200])
def test_inbox_file_rejects_bad_agent_ids_everywhere(isolated_portfolio, bad):
    root = isolated_portfolio.root
    with pytest.raises(ValueError):
        send_message(root, to=bad, message="m", from_agent="a1")
    with pytest.raises(ValueError):
        read_inbox(root, bad)


def test_cli_bad_agent_id_is_clean_error(isolated_portfolio):
    r = _invoke("inbox", "materialize", "--agent", "../x")
    assert r.exit_code != 0
    assert not isinstance(r.exception, (OSError, KeyError, AttributeError))


# --- P2: crash between add_task and reply must not duplicate ----------------


def test_crash_after_add_task_does_not_duplicate(isolated_portfolio, monkeypatch):
    import clawpm.inbox as inbox_mod

    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    real = inbox_mod.send_message

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(inbox_mod, "send_message", boom)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    assert out["failed"] and out["materialized"] == []

    monkeypatch.setattr(inbox_mod, "send_message", real)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1  # reused, not duplicated
    assert len(out["materialized"]) == 1 and out["materialized"][0]["resumed"] is True
    reply = read_inbox(root, "a1")[0]
    assert reply["payload"]["task_id"] == out["materialized"][0]["task_id"]
    assert read_inbox(root, "a2") == []


def test_idempotency_key_is_additive(isolated_portfolio):
    from clawpm.tasks import add_task

    plain = add_task(isolated_portfolio.config, "test", "plain")
    assert "source_request" not in plain.file_path.read_text(encoding="utf-8")
    _raw_append(isolated_portfolio.root, "a2", _req(GOOD))
    _json(_invoke("inbox", "materialize", "--agent", "a2"))
    texts = [p.read_text(encoding="utf-8") for p in _task_files(isolated_portfolio.tasks_dir)]
    assert sum(GOOD in x for x in texts) == 1


def test_key_survives_state_change_so_retry_still_dedups(isolated_portfolio, monkeypatch):
    import clawpm.inbox as inbox_mod
    from clawpm.models import TaskState
    from clawpm.tasks import change_task_state

    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(GOOD))
    real = inbox_mod.send_message
    monkeypatch.setattr(inbox_mod, "send_message",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert out["failed"]
    tid = _task_files(isolated_portfolio.tasks_dir)[0].stem
    change_task_state(isolated_portfolio.config, "test", tid, TaskState.DONE)
    monkeypatch.setattr(inbox_mod, "send_message", real)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1
    assert out["materialized"][0]["resumed"] is True


# --- P2: structured criteria that fail to parse -----------------------------


@pytest.mark.parametrize("crit", ["{}", '{"gradeable_signal": "x"}'])
def test_bad_structured_success_criteria_reject_not_abort(isolated_portfolio, crit):
    root = isolated_portfolio.root
    _bad_then_good(root, success_criteria=[crit])
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    _assert_bad_rejected_good_materialized(out, root)


def test_build_payload_rejects_bad_structured_criteria():
    with pytest.raises(TaskRequestError):
        build_task_request_payload(title="T", success_criteria=["{}"])


# --- class audit: every other untrusted field -------------------------------


@pytest.mark.parametrize("field,value", [
    ("title", "line1\nline2"),
    ("title", "x" * 5000),
    ("scope", ["../../etc/passwd"]),
    ("scope", ["/etc/passwd"]),
    ("scope", ["C:\\Windows"]),
    ("depends", ["../../x"]),
    ("depends", ["a/b"]),
    ("tags", ["ok", "bad\nnewline"]),
])
def test_other_untrusted_fields_rejected_not_written(isolated_portfolio, field, value):
    root = isolated_portfolio.root
    _bad_then_good(root, **{field: value})
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    _assert_bad_rejected_good_materialized(out, root)
    assert len(_task_files(isolated_portfolio.tasks_dir)) == 1


@pytest.mark.parametrize("bad_id", [None, "", 5, ["x"], "../evil", "a b"])
def test_bad_msg_id_does_not_abort_drain(isolated_portfolio, bad_id):
    root = isolated_portfolio.root
    _raw_append(root, "a2", _req(bad_id))
    _raw_append(root, "a2", _req(GOOD))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]
    assert len(out["rejected"]) == 1


def test_non_dict_json_lines_do_not_abort_drain(isolated_portfolio):
    root = isolated_portfolio.root
    d = root / "inbox"
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "a2.jsonl", "a", encoding="utf-8") as fh:
        fh.write('[1, 2]\n"str"\n42\nnull\n')
    _raw_append(root, "a2", _req(GOOD))
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert len(out["materialized"]) == 1


def test_unexpected_exception_does_not_block_later_requests(isolated_portfolio, monkeypatch):
    import clawpm.tasks as tasks_mod

    root = isolated_portfolio.root
    _raw_append(root, "a2", _req())
    _raw_append(root, "a2", _req(GOOD))
    real = tasks_mod.add_task_with_status
    state = {"n": 0}

    def flaky(*a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("boom")
        return real(*a, **k)

    monkeypatch.setattr(tasks_mod, "add_task_with_status", flaky)
    out = _json(_invoke("inbox", "materialize", "--agent", "a2"))
    assert [f["msg_id"] for f in out["failed"]] == [BAD]
    assert [m["msg_id"] for m in out["materialized"]] == [GOOD]
