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
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


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
