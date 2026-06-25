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
- POSIX: ``fcntl.flock`` with ``LOCK_EX``
- Windows: ``msvcrt.locking`` with ``LK_LOCK`` (blocks with retry)

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

import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, IO, Iterator, TypeVar


# Byte range we lock on Windows. Locking 1 byte at offset 0 is the canonical
# whole-file lock pattern in Win32 — other ``msvcrt.locking`` calls on the same
# file will block until we unlock. The locked range need not contain real data;
# Win32 ``LockFileEx`` semantics allow locking past EOF.
_LOCK_OFFSET = 0
_LOCK_LENGTH = 1


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
        OSError: if the lock cannot be acquired (Windows ``msvcrt.locking``
        retries 10 times with 1-second delays before raising; POSIX
        ``fcntl.flock`` blocks indefinitely unless interrupted).
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fh = open(path, "a", encoding=encoding)
    try:
        _acquire(fh)
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


def _acquire(fh: IO[str]) -> None:
    """Acquire exclusive lock on the file handle.

    Platform dispatch:
    - Windows: ``msvcrt.locking(fd, LK_LOCK, n)`` locks ``n`` bytes from the
      current file position. We seek to offset 0 first so the lock range is
      deterministic across writers; then seek back to EOF for the append.
    - POSIX: ``fcntl.flock(fd, LOCK_EX)`` is whole-file. No position trickery.
    """
    if sys.platform == "win32":
        # Save EOF position; the file was opened in "a" mode so position is EOF.
        eof = fh.tell()
        fh.seek(_LOCK_OFFSET)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, _LOCK_LENGTH)
        finally:
            # Restore EOF position regardless of locking success, so the caller's
            # write() goes to the right place if we re-raise.
            fh.seek(eof)
    else:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


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


@contextmanager
def file_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock across an arbitrary critical section.

    Distinct from ``locked_append``: this guards a code block, not an append
    write.  The lock file is a dedicated sentinel — never a data file.

    Granularity is per-project (per tasks-dir):
    ``lock_path`` should be ``<tasks_dir>/.clawpm-tasks.lock``.  This serialises
    mutations *within one project's task tree* while letting different projects
    proceed concurrently.

    **DEADLOCK SAFETY:** ``fcntl.flock`` / ``msvcrt.locking`` are NOT reentrant
    across two handles to the same path within one process.  Never enter a nested
    ``file_lock`` on the same lock path while already holding it; the inner
    acquire will self-deadlock.  Keep each critical section flat — do not call
    functions that re-enter ``file_lock`` on the same path.

    Usage::

        with file_lock(tasks_dir / ".clawpm-tasks.lock"):
            # scan → create critical section
            ...

    Raises:
        OSError: if the lock cannot be acquired (same behaviour as ``_acquire``).
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")  # create-or-open; "a+" keeps existing content
    try:
        _acquire(fh)
        try:
            yield
        finally:
            _release(fh)
    finally:
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
    """
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
