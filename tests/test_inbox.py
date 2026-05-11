"""Tests for clawpm inbox — inter-agent messaging subsystem."""

from __future__ import annotations

import json
import os
import tempfile
import warnings
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.inbox import (
    send_message,
    read_inbox,
    ack_messages,
    get_thread,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_portfolio(tmp_path):
    """Minimal temporary portfolio root with portfolio.toml."""
    portfolio_root = tmp_path / "clawpm"
    portfolio_root.mkdir()
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    return portfolio_root


@pytest.fixture
def runner(tmp_portfolio, monkeypatch):
    """CLI runner with CLAWPM_PORTFOLIO pointed at tmp_portfolio."""
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_portfolio))
    return CliRunner()


# ---------------------------------------------------------------------------
# Unit tests — pure functions
# ---------------------------------------------------------------------------


class TestSendAndRead:
    def test_send_creates_inbox_dir(self, tmp_portfolio):
        """Sending a message creates ~/clawpm/inbox/ if absent."""
        assert not (tmp_portfolio / "inbox").exists()
        send_message(tmp_portfolio, to="worker", message="hello", from_agent="main")
        assert (tmp_portfolio / "inbox").is_dir()

    def test_send_creates_recipient_file(self, tmp_portfolio):
        """Inbox JSONL file is created under inbox/<agent-id>.jsonl."""
        event = send_message(tmp_portfolio, to="researcher", message="go fetch", from_agent="main")
        inbox_file = tmp_portfolio / "inbox" / "researcher.jsonl"
        assert inbox_file.exists()
        data = json.loads(inbox_file.read_text(encoding="utf-8").strip())
        assert data["msg_id"] == event["msg_id"]
        assert data["from"] == "main"
        assert data["to"] == "researcher"
        assert data["message"] == "go fetch"

    def test_send_returns_event_dict(self, tmp_portfolio):
        """send_message returns the full event dict with required keys."""
        ev = send_message(tmp_portfolio, to="agent-b", message="ping")
        for key in ("event", "msg_id", "ts", "from", "to", "message"):
            assert key in ev
        assert ev["event"] == "message"
        assert ev["msg_id"].startswith("INBOX-")

    def test_msg_id_format(self, tmp_portfolio):
        """msg_id follows INBOX-YYYYMMDD-<4hex> pattern."""
        import re
        ev = send_message(tmp_portfolio, to="x", message="test")
        assert re.match(r"^INBOX-\d{8}-[0-9a-f]{4}$", ev["msg_id"])

    def test_read_single_message(self, tmp_portfolio):
        """Read inbox returns the sent message."""
        ev = send_message(tmp_portfolio, to="agent-a", message="task context", from_agent="main")
        messages = read_inbox(tmp_portfolio, agent_id="agent-a", unacked_only=False)
        assert len(messages) == 1
        assert messages[0]["msg_id"] == ev["msg_id"]

    def test_read_multiple_messages_same_recipient(self, tmp_portfolio):
        """Multiple messages to same recipient all appear in read."""
        n = 5
        ids = []
        for i in range(n):
            ev = send_message(tmp_portfolio, to="worker", message=f"msg {i}")
            ids.append(ev["msg_id"])
        messages = read_inbox(tmp_portfolio, agent_id="worker", unacked_only=False)
        assert len(messages) == n
        returned_ids = {m["msg_id"] for m in messages}
        assert set(ids) == returned_ids

    def test_read_empty_inbox(self, tmp_portfolio):
        """Reading a non-existent inbox returns empty list."""
        result = read_inbox(tmp_portfolio, agent_id="nobody", unacked_only=False)
        assert result == []

    def test_optional_fields_stored(self, tmp_portfolio):
        """project, task, and in_reply_to are stored in the event."""
        ev = send_message(
            tmp_portfolio, to="sub", message="ctx",
            project="polymarket-arb", task="POLYM-007", in_reply_to="INBOX-20260508-aaaa",
        )
        messages = read_inbox(tmp_portfolio, agent_id="sub", unacked_only=False)
        assert messages[0]["project"] == "polymarket-arb"
        assert messages[0]["task"] == "POLYM-007"
        assert messages[0]["in_reply_to"] == "INBOX-20260508-aaaa"


class TestAck:
    def test_ack_removes_from_unacked_view(self, tmp_portfolio):
        """After acking, message no longer appears in --unacked read."""
        ev = send_message(tmp_portfolio, to="worker", message="do this", from_agent="main")
        msg_id = ev["msg_id"]

        # Unacked — should appear
        before = read_inbox(tmp_portfolio, agent_id="worker", unacked_only=True)
        assert any(m["msg_id"] == msg_id for m in before)

        ack_messages(tmp_portfolio, msg_ids=[msg_id], acked_by="worker")

        # Still unacked read — should be gone
        after = read_inbox(tmp_portfolio, agent_id="worker", unacked_only=True)
        assert not any(m["msg_id"] == msg_id for m in after)

    def test_ack_message_still_in_all_mode(self, tmp_portfolio):
        """Acked message still shows with unacked_only=False."""
        ev = send_message(tmp_portfolio, to="worker", message="do this", from_agent="main")
        ack_messages(tmp_portfolio, msg_ids=[ev["msg_id"]], acked_by="worker")
        all_msgs = read_inbox(tmp_portfolio, agent_id="worker", unacked_only=False)
        assert any(m["msg_id"] == ev["msg_id"] for m in all_msgs)

    def test_ack_returns_acked_list(self, tmp_portfolio):
        """ack_messages returns {"acked": [...], "ts": ...}."""
        ev1 = send_message(tmp_portfolio, to="w", message="a")
        ev2 = send_message(tmp_portfolio, to="w", message="b")
        result = ack_messages(tmp_portfolio, msg_ids=[ev1["msg_id"], ev2["msg_id"]], acked_by="w")
        assert set(result["acked"]) == {ev1["msg_id"], ev2["msg_id"]}
        assert "ts" in result

    def test_ack_nonexistent_warns_not_errors(self, tmp_portfolio):
        """Acking a non-existent msg_id warns but does not raise."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = ack_messages(tmp_portfolio, msg_ids=["INBOX-00000000-ffff"], acked_by="main")
        assert any("not found" in str(warning.message) for warning in w)
        assert result["acked"] == ["INBOX-00000000-ffff"]  # still recorded

    def test_ack_appended_to_acker_inbox(self, tmp_portfolio):
        """Ack event is appended to the acker's own inbox file."""
        ev = send_message(tmp_portfolio, to="agent-b", message="x", from_agent="agent-a")
        ack_messages(tmp_portfolio, msg_ids=[ev["msg_id"]], acked_by="agent-b")
        inbox_file = tmp_portfolio / "inbox" / "agent-b.jsonl"
        lines = inbox_file.read_text(encoding="utf-8").strip().split("\n")
        events = [json.loads(l) for l in lines]
        acks = [e for e in events if e.get("event") == "ack"]
        assert len(acks) == 1
        assert acks[0]["msg_id_ref"] == ev["msg_id"]


class TestFilters:
    def test_filter_since(self, tmp_portfolio):
        """--since filters out older messages."""
        ev = send_message(tmp_portfolio, to="x", message="old msg")
        # Future date — nothing should pass
        result = read_inbox(tmp_portfolio, agent_id="x", unacked_only=False, since="2099-01-01")
        assert len(result) == 0

    def test_filter_since_passes_recent(self, tmp_portfolio):
        """--since with past date passes all messages."""
        send_message(tmp_portfolio, to="x", message="msg1")
        send_message(tmp_portfolio, to="x", message="msg2")
        result = read_inbox(tmp_portfolio, agent_id="x", unacked_only=False, since="2000-01-01")
        assert len(result) == 2

    def test_filter_from(self, tmp_portfolio):
        """--from filters to only messages from the specified sender."""
        send_message(tmp_portfolio, to="target", message="from main", from_agent="main")
        send_message(tmp_portfolio, to="target", message="from worker", from_agent="worker")
        result = read_inbox(tmp_portfolio, agent_id="target", unacked_only=False, from_filter="worker")
        assert len(result) == 1
        assert result[0]["from"] == "worker"


class TestThread:
    def test_thread_single_message(self, tmp_portfolio):
        """Thread of a message with no replies returns just that message."""
        ev = send_message(tmp_portfolio, to="agent-b", message="root")
        thread = get_thread(tmp_portfolio, ev["msg_id"])
        assert len(thread) == 1
        assert thread[0]["msg_id"] == ev["msg_id"]

    def test_thread_with_reply(self, tmp_portfolio):
        """Parent + child reply both appear in thread, sorted by (ts, msg_id)."""
        parent = send_message(tmp_portfolio, to="agent-b", message="parent", from_agent="main")
        child = send_message(
            tmp_portfolio, to="main", message="reply",
            from_agent="agent-b", in_reply_to=parent["msg_id"],
        )
        thread = get_thread(tmp_portfolio, parent["msg_id"])
        thread_ids = [m["msg_id"] for m in thread]
        # Both messages must be present
        assert parent["msg_id"] in thread_ids
        assert child["msg_id"] in thread_ids
        # Thread must be sorted ascending by (ts, msg_id)
        sort_keys = [(m.get("ts", ""), m.get("msg_id", "")) for m in thread]
        assert sort_keys == sorted(sort_keys)

    def test_thread_nonexistent_msg(self, tmp_portfolio):
        """get_thread for unknown msg_id returns empty list."""
        result = get_thread(tmp_portfolio, "INBOX-00000000-0000")
        assert result == []

    def test_thread_cross_agent(self, tmp_portfolio):
        """Cross-agent thread: A→B, B replies to A — both appear."""
        msg_a = send_message(tmp_portfolio, to="agent-b", message="task context", from_agent="main")
        msg_b = send_message(
            tmp_portfolio, to="main", message="results",
            from_agent="agent-b", in_reply_to=msg_a["msg_id"],
        )
        # Thread from either end should contain both
        thread_from_parent = get_thread(tmp_portfolio, msg_a["msg_id"])
        thread_from_child = get_thread(tmp_portfolio, msg_b["msg_id"])
        assert len(thread_from_parent) == 2
        assert len(thread_from_child) == 2


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_cli_send_returns_json(self, runner):
        result = runner.invoke(main, ["inbox", "send", "--to", "worker", "--message", "hello"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "msg_id" in data
        assert data["to"] == "worker"

    def test_cli_read_returns_sent_message(self, runner):
        send_result = runner.invoke(main, ["inbox", "send", "--to", "agent-x", "--message", "ping"])
        msg_id = json.loads(send_result.output)["msg_id"]
        read_result = runner.invoke(main, ["inbox", "read", "--agent", "agent-x", "--all"])
        assert read_result.exit_code == 0, read_result.output
        messages = json.loads(read_result.output)
        assert any(m["msg_id"] == msg_id for m in messages)

    def test_cli_ack_removes_from_unacked(self, runner):
        send_result = runner.invoke(main, ["inbox", "send", "--to", "ag", "--message", "do it"])
        msg_id = json.loads(send_result.output)["msg_id"]
        runner.invoke(main, ["inbox", "ack", msg_id, "--agent", "ag"])
        read_result = runner.invoke(main, ["inbox", "read", "--agent", "ag"])  # default: --unacked
        messages = json.loads(read_result.output)
        assert not any(m["msg_id"] == msg_id for m in messages)

    def test_cli_thread(self, runner, tmp_portfolio, monkeypatch):
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_portfolio))
        parent = send_message(tmp_portfolio, to="sub", message="dispatch context", from_agent="main")
        child = send_message(
            tmp_portfolio, to="main", message="done",
            from_agent="sub", in_reply_to=parent["msg_id"],
        )
        result = runner.invoke(main, ["inbox", "thread", parent["msg_id"]])
        assert result.exit_code == 0, result.output
        thread = json.loads(result.output)
        ids = [m["msg_id"] for m in thread]
        assert parent["msg_id"] in ids
        assert child["msg_id"] in ids

    def test_cli_send_with_project_and_task(self, runner):
        result = runner.invoke(
            main,
            ["inbox", "send", "--to", "w", "--message", "ctx",
             "--project", "myproj", "--task", "CLAWP-007"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "msg_id" in data
