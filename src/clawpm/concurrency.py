"""Cross-platform exclusive file locking for concurrent JSONL appends (CLAWP-032).

Background: clawpm's append-mode JSONL writers (work_log.jsonl, reflections/*.jsonl,
inbox/*.jsonl, .agent/issues.jsonl) use ``open(path, "a")`` + ``f.write(...)``.

On POSIX, ``O_APPEND`` makes single ``write()`` calls atomic up to ``PIPE_BUF``
(typically 4096 bytes on Linux). Two processes appending concurrently get
their bytes serialised correctly — each line ends up intact.

On Windows, append is NOT atomic. Two processes appending to the same file at
the same instant can interleave bytes within a single ``write()`` call, producing
a corrupted JSONL line (e.g. ``{"ts": "2026-05-25T10:00:00Z", "ac{"ts": "2026...``).
This is a silent corruption — the file stays "valid" from an OS perspective but
the JSONL becomes unparseable from the interleave point onward.

This module provides ``locked_append``, a context manager that wraps an
append-mode file handle with an exclusive advisory lock:
- POSIX: ``fcntl.flock`` with ``LOCK_EX`` (or a ``LOCK_NB`` poll when bounded)
- Windows: ``msvcrt.locking`` with ``LK_NBLCK`` polled in a backoff loop

The lock is **advisory** — only writers using this helper observe it. Any
external process bypassing the helper can still cause corruption. That's
acceptable for clawpm's use case (all writers are inside the package); for
external interop, a sentinel-file or proper IPC lock would be needed.

Why not portalocker?
- One more dependency for a 30-line problem.
- ``msvcrt`` + ``fcntl`` are stdlib on all supported platforms.
- portalocker's API has more knobs than we need.
"""

from __future__ import annotations

import errno
import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, IO, Iterator, TypeVar


class LockTimeout(TimeoutError):
    """Raised when a lock cannot be acquired within its timeout.

    Subclasses ``TimeoutError`` (hence ``OSError``), so existing callers that
    catch ``OSError`` keep working, while the message carries the lock path and
    timeout for diagnostics instead of a bare errno (CLAWP-066 / Grok review).
    """


# Byte range we lock on Windows. Locking 1 byte at offset 0 is the canonical
# whole-file lock pattern in Win32 — other ``msvcrt.locking`` calls on the same
# file will block until we unlock. The locked range need not contain real data;
# Win32 ``LockFileEx`` semantics allow locking past EOF.
_LOCK_OFFSET = 0
_LOCK_LENGTH = 1

# Default ceiling (seconds) for acquiring a contended lock. Windows' msvcrt
# ``LK_LOCK`` mode retries only 10×1s then raises — too short for a large parent
# rollup on a slow/AV-scanned filesystem, where a concurrent writer would get a
# spurious acquisition failure instead of waiting, defeating the multi-session
# safety this lock exists for (Codex review). We poll ``LK_NBLCK`` ourselves up
# to this bound so a waiter genuinely waits, while still erroring on a truly
# stuck lock rather than hanging forever.
_LOCK_ACQUIRE_TIMEOUT = 120.0
# Cap on the poll backoff between acquisition attempts.
_LOCK_POLL_MAX = 0.5
# Errnos that mean "the lock is held by someone else, try again" — the only
# errors the acquire poll loop should retry. Anything else (EBADF, EINVAL, a
# permanent permission fault on the sentinel itself) is a genuine failure that
# must surface immediately, NOT spin for the full timeout (Grok review). POSIX
# LOCK_NB raises EAGAIN/EWOULDBLOCK (→ BlockingIOError); Windows msvcrt LK_NBLCK
# raises EACCES or EDEADLK on a held range.
_LOCK_CONTENTION_ERRNOS = frozenset({
    errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK,
})


if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


@contextmanager
def locked_append(path: Path, encoding: str = "utf-8") -> Iterator[IO[str]]:
    """Yield a text-mode append file handle holding an exclusive advisory lock.

    Usage::

        with locked_append(path) as f:
            f.write(json.dumps(entry) + "\\n")

    The lock is acquired before the handle yields and released after the
    ``with`` block exits (success or exception). ``flush()`` + ``fsync()``
    run before unlock to guarantee the write is durable before another
    writer can append.

    Parent directory is created if missing.

    Raises:
        OSError: if the lock cannot be acquired within ``_LOCK_ACQUIRE_TIMEOUT``
        (both platforms poll a non-blocking acquire up to that ceiling; see
        ``_acquire``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fh = open(path, "a", encoding=encoding)
    try:
        _acquire(fh, lock_desc=str(path))
        try:
            yield fh
            # Flush + fsync BEFORE releasing the lock so the durable write
            # completes before another writer can append. Without this,
            # writer A's data may still be in the page cache when writer B
            # acquires the lock and starts writing.
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                # fsync can fail on some filesystems / pipes — we've still
                # flushed to OS buffers, which is what append-atomicity needs.
                pass
        finally:
            _release(fh)
    finally:
        fh.close()


def _acquire(
    fh: IO[str],
    timeout: float | None = _LOCK_ACQUIRE_TIMEOUT,
    lock_desc: str = "lock",
) -> None:
    """Acquire exclusive lock on the file handle, waiting up to ``timeout`` secs.

    Platform dispatch:
    - Windows: poll ``msvcrt.locking(fd, LK_NBLCK, n)`` — the NON-blocking mode,
      which fails immediately if the range is held — in a backoff loop we
      control, rather than ``LK_LOCK`` whose internal 10×1s cap would surface a
      spurious failure on a long-held lock (Codex review). We seek to offset 0
      first so the lock range is deterministic across writers; then seek back to
      EOF for the append.
    - POSIX: ``fcntl.flock(fd, LOCK_EX)`` blocks whole-file. With a ``timeout``
      we poll ``LOCK_EX | LOCK_NB`` instead so the wait is bounded; ``timeout``
      of None blocks indefinitely (the historical behaviour).

    Raises ``OSError`` if the lock can't be acquired within ``timeout``.
    """
    if sys.platform == "win32":
        # Save EOF position; the file was opened in "a"/"a+" mode so pos is EOF.
        eof = fh.tell()
        fh.seek(_LOCK_OFFSET)
        try:
            deadline = None if timeout is None else time.monotonic() + timeout
            delay = 0.02
            while True:
                try:
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_LENGTH)
                    return
                except OSError as exc:
                    # Retry ONLY genuine contention; a permanent fault on the
                    # sentinel must fail fast, not spin for the whole timeout.
                    if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise LockTimeout(
                            f"could not acquire {lock_desc} within {timeout}s "
                            "(held by another session)"
                        ) from exc
                    time.sleep(min(delay, _LOCK_POLL_MAX))
                    delay = min(delay * 2, _LOCK_POLL_MAX)
        finally:
            # Restore EOF position regardless of outcome, so the caller's write()
            # goes to the right place if we re-raise.
            fh.seek(eof)
    else:
        if timeout is None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            return
        deadline = time.monotonic() + timeout
        delay = 0.02
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except OSError as exc:
                # Retry ONLY genuine contention (EAGAIN/EWOULDBLOCK); any other
                # error is a real failure that must surface immediately.
                if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire {lock_desc} within {timeout}s "
                        "(held by another session)"
                    ) from exc
                time.sleep(min(delay, _LOCK_POLL_MAX))
                delay = min(delay * 2, _LOCK_POLL_MAX)


def _release(fh: IO[str]) -> None:
    """Release the exclusive lock on the file handle.

    Mirror of ``_acquire``. Failures here are non-fatal — the OS will reclaim
    the lock on file close anyway, but explicit unlock is cleaner under
    short-held locks where the same process may need to re-acquire.
    """
    if sys.platform == "win32":
        eof = fh.tell()
        fh.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_LENGTH)
        except OSError:
            pass
        finally:
            fh.seek(eof)
    else:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


# Per-THREAD reentrancy bookkeeping: maps the normalised lock path to the
# nesting depth this thread currently holds. The OS lock (msvcrt/fcntl) is
# acquired only at depth 0->1; nested acquires of the SAME path on the SAME
# thread just bump the depth. This makes file_lock reentrant within a thread
# (so a locked mutator can call another locked helper on the same path without
# self-deadlock — e.g. add_subtask -> split_task) while cross-thread and
# cross-process contention is still serialised by the OS lock (CLAWP-066).
_held_locks = threading.local()


def _held_depths() -> dict[str, int]:
    depths = getattr(_held_locks, "depths", None)
    if depths is None:
        depths = {}
        _held_locks.depths = depths
    return depths


@contextmanager
def file_lock(
    lock_path: Path, timeout: float | None = _LOCK_ACQUIRE_TIMEOUT
) -> Iterator[None]:
    """Hold an exclusive advisory lock across an arbitrary critical section.

    Distinct from ``locked_append``: this guards a code block, not an append
    write.  The lock file is a dedicated sentinel — never a data file.

    Granularity is per-project (per tasks-dir):
    ``lock_path`` should be ``<tasks_dir>/.clawpm-tasks.lock``.  This serialises
    mutations *within one project's task tree* while letting different projects
    proceed concurrently.

    **REENTRANCY (CLAWP-066):** reentrant per-thread on the same lock path. A
    function holding the lock may call another function that re-enters
    ``file_lock`` on the same path — the nested acquire just bumps a thread-local
    depth and the OS lock is released only when the outermost block exits. This
    is what lets ``add_subtask`` (locked) call ``split_task`` (also locked) on
    the same path without deadlock. Cross-thread and cross-process callers still
    contend on the OS lock as normal.

    Usage::

        with file_lock(tasks_dir / ".clawpm-tasks.lock"):
            # scan → create critical section
            ...

    ``timeout`` bounds how long a contended acquire waits (default
    ``_LOCK_ACQUIRE_TIMEOUT``); pass None to wait indefinitely on POSIX. The
    wait is a poll loop, so a long-held lock (large rollup, slow/AV filesystem)
    is waited out rather than failing at Windows' 10s ``LK_LOCK`` cap.

    Raises:
        LockTimeout: if the lock cannot be acquired within ``timeout``.
    """
    # normcase + abspath so the reentrancy key is CANONICAL: two call sites that
    # spell the same lock path differently (Windows drive-case, / vs \, an
    # unresolved vs resolved prefix) must map to the SAME key, or a nested acquire
    # (add_subtask → split_task) would see depth 0, take the real-acquire path,
    # and self-deadlock on the non-reentrant OS lock (Grok review). normcase is a
    # no-op on POSIX.
    key = os.path.normcase(os.path.abspath(str(lock_path)))
    depths = _held_depths()
    if depths.get(key, 0) > 0:
        # Reentrant acquire on this thread — the OS lock is already held.
        depths[key] += 1
        try:
            yield
        finally:
            depths[key] -= 1
            if depths[key] <= 0:
                depths.pop(key, None)
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")  # create-or-open; "a+" keeps existing content
    # Single try whose finally keys off `acquired`: the depth marker is set only
    # after _acquire succeeds, and the matching release+pop is guaranteed by the
    # same finally — so there is no window (even for an async exception between
    # statements) where depth>0 is left stranded over a released/absent OS lock
    # (Grok review). pop is in an inner finally so it runs even if _release
    # raised; fh.close() backstops the OS release.
    acquired = False
    try:
        _acquire(fh, timeout, lock_desc=key)
        acquired = True
        depths[key] = 1
        yield
    finally:
        if acquired:
            try:
                _release(fh)
            finally:
                depths.pop(key, None)
        fh.close()


_T = TypeVar("_T")

# Windows error codes for the transient sharing/access faults an antivirus
# scanner or the search indexer briefly raises against a freshly written or
# renamed file: ERROR_ACCESS_DENIED (5), ERROR_SHARING_VIOLATION (32), and
# ERROR_LOCK_VIOLATION (33). An atomic rename (``os.replace`` / ``shutil.move``)
# issued the instant after a write can hit these even with the per-project lock
# held — the lock serialises *clawpm's* writers, but a third-party scanner holds
# its own handle. 32 and 33 are the two codes Win32 surfaces for "another handle
# is on this file"; both are transient here. The fix is a bounded retry, NOT a
# wider lock.
_TRANSIENT_WINERRORS = frozenset({5, 32, 33})


def _is_transient_fs_error(exc: BaseException) -> bool:
    """True for the transient Windows sharing/access errors worth retrying.

    Deliberately narrow: retried iff ``winerror`` is in the sharing/access set
    {5, 32, 33}. Everything else (FileExistsError, FileNotFoundError,
    cross-device EXDEV, real EACCES on POSIX, or a PermissionError whose
    ``winerror`` is absent/None or outside the set) is a genuine condition the
    caller must see — never retried.

    Known ambiguity (accepted trade-off): ERROR_ACCESS_DENIED (5) is raised both
    transiently by an AV scanner/indexer holding a freshly renamed file AND by a
    *permanent* ACL denial. The two are indistinguishable from the error alone,
    so a permanent deny surfacing as winerror 5 gets a bounded retry (≤ attempts,
    ~exponential backoff) before the real exception propagates correctly. The
    bounded cost is worth catching the common transient case; the caller still
    sees the genuine failure.
    """
    if not isinstance(exc, OSError):
        return False
    # Retry only on the specific known-transient Windows codes. A PermissionError
    # with no transient winerror (e.g. winerror=None, or POSIX EACCES) fails fast.
    if getattr(exc, "winerror", None) in _TRANSIENT_WINERRORS:
        return True
    return False


def retry_transient(
    fn: Callable[..., _T], *args: object, attempts: int = 5, base_delay: float = 0.02
) -> _T:
    """Call ``fn(*args)``, retrying ONLY on transient FS sharing/access faults.

    For atomic renames/moves performed under concurrent access on Windows
    (CLAWP-051). Re-raises immediately on any non-transient error and after the
    final attempt; backs off exponentially (``base_delay`` × 2ⁿ) between tries.
    Sleeps only on failure, so the happy path is unaffected.

    ``fn`` is always invoked at least once: a non-positive ``attempts`` is
    clamped to 1 so the contract ("calls fn, retrying only transients") holds
    rather than falling through to the unreachable guard below (Grok review).
    """
    attempts = max(1, attempts)
    for attempt in range(attempts):
        try:
            return fn(*args)
        except OSError as exc:
            if attempt == attempts - 1 or not _is_transient_fs_error(exc):
                raise
            time.sleep(base_delay * (2**attempt))
    # Unreachable: the loop either returns or raises on the final attempt.
    raise AssertionError("retry_transient exhausted without returning or raising")


def append_jsonl_line(path: Path, line: str, encoding: str = "utf-8") -> None:
    """Atomic-append helper for the canonical JSONL writer pattern.

    Equivalent to::

        with locked_append(path, encoding) as f:
            f.write(line if line.endswith("\\n") else line + "\\n")

    Provided as a convenience for callers that don't need the handle for
    anything else. Ensures every appended line ends with ``\\n`` so external
    readers using line-based iteration see complete records.
    """
    if not line.endswith("\n"):
        line = line + "\n"
    with locked_append(path, encoding) as fh:
        fh.write(line)
