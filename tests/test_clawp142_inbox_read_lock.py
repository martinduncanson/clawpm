"""CLAWP-142: inbox reads must tolerate a locked append landing mid-read.

On Windows ``locked_append`` holds a mandatory ``msvcrt`` byte-range lock on
byte 0 of the data file itself, so an unlocked reader on another handle gets
``PermissionError`` (errno 13, no ``winerror``) for as long as a writer holds
it. ``retry_transient`` cannot catch that (it keys off ``winerror``), so
``_read_events`` waits out the lock itself.
"""

from __future__ import annotations

import sys
import threading
import time

import pytest

import clawpm.inbox as inbox_mod
from clawpm.concurrency import locked_append
from clawpm.inbox import read_inbox, send_message

windows_only = pytest.mark.skipif(
    sys.platform != "win32", reason="mandatory byte-range lock is Windows-only"
)


@windows_only
def test_read_waits_for_held_append_lock(tmp_path):
    path = tmp_path / "a2.jsonl"
    path.write_text('{"event": "message", "msg_id": "M-1"}\n', encoding="utf-8")

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with locked_append(path) as fh:
            fh.write('{"event": "message", "msg_id": "M-2"}\n')
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(10)
    # Release shortly after the reader starts so it has to wait, not fail.
    threading.Timer(0.3, release.set).start()
    try:
        events = inbox_mod._read_events(path)
    finally:
        release.set()
        t.join(10)
    assert [e["msg_id"] for e in events] == ["M-1", "M-2"]


@windows_only
def test_read_gives_up_on_permanently_denied_file(tmp_path, monkeypatch):
    path = tmp_path / "a2.jsonl"
    path.write_text('{"event": "message", "msg_id": "M-1"}\n', encoding="utf-8")
    monkeypatch.setattr(inbox_mod, "_READ_LOCK_WAIT", 0.2)

    held = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with locked_append(path):
            held.set()
            release.wait(10)

    t = threading.Thread(target=hold)
    t.start()
    assert held.wait(10)
    try:
        with pytest.raises(PermissionError):
            inbox_mod._read_events(path)
    finally:
        release.set()
        t.join(10)


def test_concurrent_append_and_read_never_raise(isolated_portfolio):
    root = isolated_portfolio.root
    n = 40
    errors: list[BaseException] = []
    done = threading.Event()

    def writer() -> None:
        try:
            for i in range(n):
                send_message(root, to="a2", message=f"m{i}", from_agent="a1")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            done.set()

    def reader() -> None:
        try:
            while not done.is_set():
                read_inbox(root, "a2")
                time.sleep(0)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not errors, errors
    assert len(read_inbox(root, "a2")) == n
