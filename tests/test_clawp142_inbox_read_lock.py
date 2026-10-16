"""CLAWP-142: inbox reads must tolerate a locked append landing mid-read.

On Windows ``locked_append`` holds a mandatory ``msvcrt`` byte-range lock on
byte 0 of the data file itself, so an unlocked reader on another handle gets
``PermissionError`` (errno 13, no ``winerror``) for as long as a writer holds
it. ``retry_transient`` cannot catch that (it keys off ``winerror``), so
``_read_events`` waits out the lock itself.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time

import pytest

import clawpm.concurrency as concurrency_mod
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


def test_concurrent_append_and_read_never_raise(isolated_portfolio, monkeypatch):
    root = isolated_portfolio.root
    n = 40
    errors: list[BaseException] = []
    done = threading.Event()
    # Set when any reader exits or errors, and again in the final cleanup: the
    # writer's barrier wait polls it, so a failing run releases the lock and
    # stops at once instead of waiting out the barrier timeout per append.
    cancel = threading.Event()
    collided = threading.Event()
    collisions: list[int] = []
    unforced: list[int] = []
    pending: set[int] = set()  # reader threads denied and not yet recovered
    pending_lock = threading.Lock()

    if sys.platform == "win32":
        # Force the overlap: each append holds its byte-0 lock until a reader
        # has actually been denied, so the retry path runs on every append
        # instead of depending on scheduler luck.
        real_locked_append = concurrency_mod.locked_append
        real_read_once = inbox_mod._read_events_once

        @contextlib.contextmanager
        def barrier_append(*args, **kwargs):
            collided.clear()
            with real_locked_append(*args, **kwargs) as fh:
                yield fh
                deadline = time.monotonic() + 10
                while not collided.wait(0.05):
                    if cancel.is_set():
                        return
                    if time.monotonic() >= deadline:
                        unforced.append(1)
                        return
            # The lock is released.  Hold the next append back until every
            # reader that was denied has completed a read: otherwise the
            # next append retakes the lock at once and a denied reader can
            # lose every race for the gap until its _READ_LOCK_WAIT expires,
            # which on a slow runner fails the test for scheduling reasons.
            deadline = time.monotonic() + 10
            while True:
                with pending_lock:
                    if not pending:
                        return
                if cancel.is_set() or time.monotonic() >= deadline:
                    return
                time.sleep(0.005)

        def counting_read_once(path):
            ident = threading.get_ident()
            try:
                events = real_read_once(path)
            except PermissionError:
                with pending_lock:
                    pending.add(ident)
                collisions.append(1)
                collided.set()
                raise
            with pending_lock:
                pending.discard(ident)
            return events

        monkeypatch.setattr(concurrency_mod, "locked_append", barrier_append)
        monkeypatch.setattr(inbox_mod, "_read_events_once", counting_read_once)

    def writer() -> None:
        try:
            for i in range(n):
                if cancel.is_set():
                    break
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
        finally:
            cancel.set()

    threads = [threading.Thread(target=writer)] + [
        threading.Thread(target=reader) for _ in range(3)
    ]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
    finally:
        # Every worker must be gone before the monkeypatch/fixture teardown.
        cancel.set()
        for t in threads:
            if t.is_alive():
                t.join(30)
    assert not any(t.is_alive() for t in threads), "worker outlived the test"
    assert not errors, errors
    if sys.platform == "win32":
        assert not unforced, "a writer released its lock before any reader collided"
        assert len(collisions) >= n
    assert len(read_inbox(root, "a2")) == n
