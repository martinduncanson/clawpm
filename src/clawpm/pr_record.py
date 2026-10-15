"""Locked, surgical writer for a task's ``pr_verification`` record (CLAWP-151).

``record_pr_verification`` stores the outcome of a merged-mode ``verify_pr`` on
the task file as ONE top-level frontmatter line::

    pr_verification: {"pr": 7, "head": "<40 hex>", ...}

The JSON flow mapping is valid YAML, so ``Task.from_file`` can read it back.
Like ``tasks._set_updated_line`` this is a byte-level line splice, not a YAML
round-trip: comments, key order, body, BOM and CRLF/LF endings outside the
replaced value are preserved exactly.  The value's extent comes from the YAML
parser's event marks (never from line heuristics), and the writer refuses --
writing nothing -- whenever the frontmatter cannot be proven to be a plain
block mapping it can splice without disturbing any other key.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml

from clawpm.concurrency import file_lock, retry_transient
from clawpm.discovery import get_project_dir
from clawpm.models import PortfolioConfig
from clawpm.sessions import Scope
from clawpm.tasks import _candidate_task_paths, get_tasks_dir

RECORD_KEYS = ("pr", "head", "merge_commit", "verified_at", "remote", "repo")
_KEY = "pr_verification"
# The only raw spellings of the key this writer will replace (column 0).
_KEY_SPELLINGS = (_KEY, f'"{_KEY}"', f"'{_KEY}'")
# A task reference is a bare id (PREFIX-NNN[-NNN]); never a path.
_TASK_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.]*(?:-[A-Za-z0-9_.]+)*-\d+(?:-\d+)?")
_BOM = "﻿"
# Line breaks YAML honours beyond "\n": the parser's line numbers would no longer
# match a split on "\n", so frontmatter containing them is refused.
_ODD_BREAKS = ("\r", "\x85", " ", " ")


class PrRecordError(Exception):
    """The record was NOT written (``code`` says why); the task file is untouched."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_task_ref(task_id: str) -> None:
    """Raise ``bad_task_ref`` unless ``task_id`` is a bare task id (no separators, ``..`` or drive)."""
    if not isinstance(task_id, str) or ".." in task_id or not _TASK_REF_RE.fullmatch(task_id):
        raise PrRecordError("bad_task_ref", f"{task_id!r} is not a task id (expected PREFIX-NNN)")


def _refuse(message: str) -> PrRecordError:
    return PrRecordError("bad_frontmatter", message)


def _top_level_pairs(fm_text: str) -> list[tuple[Any, list[Any]]]:
    """Parse ``fm_text`` into ``[(key_event, value_events), ...]`` for the root mapping.

    Raises ``bad_frontmatter`` unless the document is exactly one block-style root
    mapping whose top-level keys are plain scalars with no duplicate (compared
    after the parser decodes escapes and resolves quoting).  Works on parser
    events, which keep anchors/aliases/tags visible (the composer hides them).
    """
    try:
        events = list(yaml.parse(fm_text, Loader=yaml.SafeLoader))
    except yaml.YAMLError as exc:
        raise _refuse(f"task frontmatter is not parseable YAML: {exc}")
    starts = [e for e in events if isinstance(e, yaml.DocumentStartEvent)]
    if len(starts) != 1:
        raise _refuse("task frontmatter is not a mapping")
    if starts[0].explicit or any(
        isinstance(e, yaml.DocumentEndEvent) and e.explicit for e in events
    ):
        raise _refuse("task frontmatter contains a YAML document marker")
    root_idx = events.index(starts[0]) + 1
    root = events[root_idx]
    if not isinstance(root, yaml.MappingStartEvent):
        raise _refuse("task frontmatter is not a mapping")
    if root.flow_style:
        raise _refuse("task frontmatter is a flow mapping; only a block mapping can be spliced")

    items: list[list[Any]] = []
    cur: list[Any] = []
    depth = 0
    for ev in events[root_idx + 1:]:
        if isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            cur.append(ev)
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            if depth == 0:
                break
            depth -= 1
            cur.append(ev)
            if depth == 0:
                items.append(cur)
                cur = []
        else:
            cur.append(ev)
            if depth == 0:
                items.append(cur)
                cur = []
    if len(items) % 2:
        raise _refuse("task frontmatter has a key without a value")

    pairs: list[tuple[Any, list[Any]]] = []
    seen: set[str] = set()
    for key_item, value_item in zip(items[0::2], items[1::2]):
        if len(key_item) != 1 or not isinstance(key_item[0], yaml.ScalarEvent):
            raise _refuse("task frontmatter has a top-level key that is not a plain scalar (alias or collection)")
        name = key_item[0].value
        if name in seen:
            raise _refuse(f"task frontmatter has more than one top-level '{name}' key")
        seen.add(name)
        pairs.append((key_item[0], value_item))
    return pairs


def _value_extent(key_ev: Any, value_events: list[Any], fm_text: str, fm_lines: list[str]) -> tuple[int, int, str]:
    """Return ``(first_line, last_line, tail)`` of the existing key+value, 0-based within the frontmatter.

    ``tail`` is whatever follows the value on its last line (a trailing comment),
    kept so only the key and value are rewritten.  Refuses layouts it cannot
    replace exactly.
    """
    if key_ev.start_mark.column != 0:
        raise _refuse(f"'{_KEY}' key is indented; refusing to splice it")
    raw_key = fm_text[key_ev.start_mark.index:key_ev.end_mark.index]
    if raw_key not in _KEY_SPELLINGS:
        raise _refuse(f"task frontmatter holds a '{_KEY}' key in a spelling this writer cannot replace")
    flow_stack: list[bool] = []
    last: tuple[int, int] = (key_ev.end_mark.line, key_ev.end_mark.column)
    for ev in value_events:
        if isinstance(ev, yaml.AliasEvent) or getattr(ev, "anchor", None) or getattr(ev, "tag", None):
            raise _refuse(f"'{_KEY}' value uses an anchor, alias or tag; refusing to splice it")
        if isinstance(ev, yaml.ScalarEvent):
            if ev.style in ("|", ">"):
                raise _refuse(f"'{_KEY}' value contains a block scalar; refusing to splice it")
            last = max(last, (ev.end_mark.line, ev.end_mark.column))
        elif isinstance(ev, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            flow_stack.append(bool(ev.flow_style))
        elif isinstance(ev, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            if flow_stack.pop():
                last = max(last, (ev.end_mark.line, ev.end_mark.column))
    last_line, last_col = last
    tail = fm_lines[last_line][last_col:]
    return key_ev.start_mark.line, last_line, tail if tail.strip() else ""


def _splice(text: str, record: dict[str, Any]) -> str:
    """Return ``text`` with the ``pr_verification`` line set, or raise PrRecordError."""
    bom = _BOM if text.startswith(_BOM) else ""
    body = text[len(bom):]
    lines = body.split("\n")
    if not body.startswith("---") or lines[0].strip() != "---":
        raise PrRecordError("no_frontmatter", "task file has no leading '---' frontmatter fence")
    close_idx = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if close_idx is None:
        raise PrRecordError("no_frontmatter", "task file frontmatter fence is not terminated")
    fm_lines = [ln.rstrip("\r") for ln in lines[1:close_idx]]
    fm_text = "\n".join(fm_lines)
    if any(ch in fm_text for ch in _ODD_BREAKS):
        raise _refuse("task frontmatter contains a line separator other than LF/CRLF")
    try:
        parsed = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        raise _refuse(f"task frontmatter is not parseable YAML: {exc}")
    if not isinstance(parsed, dict):
        raise _refuse("task frontmatter is not a mapping")
    pairs = _top_level_pairs(fm_text)

    eol = "\r" if lines[0].endswith("\r") else ""
    new_value = f"{_KEY}: " + json.dumps(record, sort_keys=True, separators=(", ", ": "))

    existing = next(((k, v) for k, v in pairs if k.value == _KEY), None)
    if existing is not None:
        first, last, tail = _value_extent(existing[0], existing[1], fm_text, fm_lines)
        lines[1 + first:1 + last + 1] = [new_value + tail + eol]
    else:
        lines.insert(close_idx, new_value + eol)
    out = "\n".join(lines)

    # Re-parse before anything is committed to disk: exactly one key, the record
    # reads back, and no OTHER top-level key changed.
    check_lines = out.split("\n")
    check_close = next(i for i in range(1, len(check_lines)) if check_lines[i].strip() == "---")
    check_text = "\n".join(ln.rstrip("\r") for ln in check_lines[1:check_close])
    try:
        check = yaml.safe_load(check_text)
        check_pairs = _top_level_pairs(check_text)
    except (yaml.YAMLError, PrRecordError) as exc:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not parse cleanly: {exc}")
    if sum(1 for k, _ in check_pairs if k.value == _KEY) != 1:
        raise PrRecordError("verify_failed", f"spliced frontmatter does not hold exactly one '{_KEY}' key")
    if not isinstance(check, dict) or check.get(_KEY) != record:
        raise PrRecordError("verify_failed", "spliced pr_verification did not read back as the record")
    if {k: v for k, v in check.items() if k != _KEY} != {k: v for k, v in parsed.items() if k != _KEY}:
        raise PrRecordError("verify_failed", "splice changed a frontmatter key other than pr_verification")
    return bom + out


# Windows reparse tags that make a directory entry a link: IO_REPARSE_TAG_SYMLINK
# and IO_REPARSE_TAG_MOUNT_POINT (a junction).  ``S_ISLNK`` is False for a
# junction on Python 3.11, so the tag is the only reliable signal.
_LINK_REPARSE_TAGS = frozenset({0xA000000C, 0xA0000003})
_LOCK_NAME = ".clawpm-tasks.lock"


def _dirfd_supported(os_mod: Any = os) -> bool:
    """True where the fd-relative writer can run (POSIX); Windows has no directory fds.

    Read at call time (not import time) so branch selection is testable.
    ``os.replace`` never appears in ``supports_dir_fd`` (it takes ``src_dir_fd`` /
    ``dst_dir_fd``, gated by ``renameat``, which is listed as ``os.rename``), so it
    is checked through ``os.rename``.
    """
    return (
        os_mod.name == "posix"
        and hasattr(os_mod, "O_DIRECTORY")
        and hasattr(os_mod, "O_NOFOLLOW")
        and all(fn in os_mod.supports_dir_fd for fn in (os_mod.open, os_mod.stat, os_mod.unlink, os_mod.rename))
    )


def _ident(st: os.stat_result) -> tuple[int, int]:
    """Directory/file identity: (volume serial, file index) on Windows, (st_dev, st_ino) on POSIX."""
    return (st.st_dev, st.st_ino)


def _is_link(st: os.stat_result) -> bool:
    return stat.S_ISLNK(st.st_mode) or getattr(st, "st_reparse_tag", 0) in _LINK_REPARSE_TAGS


def _unsafe(message: str) -> PrRecordError:
    return PrRecordError("unsafe_store", message)


class _PinnedStore:
    """The verified tasks dir plus the identity of every component down to it.

    ``path`` is built ONCE, from the verified project dir, and is the only path
    any later step uses: nothing re-resolves it.  Identity is ``(st_dev, st_ino)``
    from ``os.lstat`` (Windows: volume serial + file index; a junction has its own
    index, so a swapped directory never compares equal).  ``recheck`` re-lstats the
    chain and raises ``unsafe_store`` on a mismatch, a link at ``path``, or any
    error.
    """

    def __init__(self, path: Path):
        self.path = path
        self._chain: list[tuple[Path, tuple[int, int]]] = []
        for comp in (*reversed(path.parents), path):
            st = self._lstat(comp)
            self._chain.append((comp, _ident(st)))
        st = self._lstat(path)
        if _is_link(st) or st.st_ino == 0:
            raise _unsafe(f"tasks directory {str(path)!r} is a link or has no stable identity")

    @property
    def tasks_ident(self) -> tuple[int, int]:
        return self._chain[-1][1]

    @staticmethod
    def _lstat(p: Path) -> os.stat_result:
        try:
            st = os.lstat(p)
        except OSError as exc:
            raise _unsafe(f"cannot stat {str(p)!r} while verifying the tasks directory: {exc}") from exc
        if not stat.S_ISDIR(st.st_mode):
            raise _unsafe(f"{str(p)!r} is not a real directory")
        return st

    def recheck(self) -> None:
        """Raise ``unsafe_store`` unless every component is still the verified directory."""
        for comp, ident in self._chain:
            st = self._lstat(comp)
            if _ident(st) != ident:
                raise _unsafe(f"{str(comp)!r} changed identity after verification; refusing to write")
        if _is_link(self._lstat(self.path)):
            raise _unsafe(f"tasks directory {str(self.path)!r} became a link; refusing to write")

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Pin the directory with an open handle for the write (Windows).

        An open handle inside a directory makes the OS refuse a rename or delete
        of it and of every ancestor (probed: WinError 5), so once pinned a swap
        cannot happen.  The handle must live in the VERIFIED directory, so after
        opening it ``fstat(handle)`` must equal ``lstat`` of the same path, with
        the chain identity checked both before and after (this is what catches a
        swap, open, swap-back race).
        """
        lock = self.path / _LOCK_NAME
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            raise _unsafe(f"cannot open {str(lock)!r} to pin the tasks directory: {exc}") from exc
        try:
            self.recheck()
            try:
                held, there = os.fstat(fd), os.lstat(lock)
            except OSError as exc:
                raise _unsafe(f"cannot stat {str(lock)!r}: {exc}") from exc
            if not stat.S_ISREG(there.st_mode) or _ident(held) != _ident(there):
                raise _unsafe(f"{str(lock)!r} is not the lock file of the verified tasks directory")
            self.recheck()
            yield
        finally:
            os.close(fd)

    def open_dirfd(self) -> int:
        """POSIX: open the tasks dir without following links; it must be the verified inode."""
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            raise _unsafe(f"cannot open tasks directory {str(self.path)!r}: {exc}") from exc
        if _ident(os.fstat(fd)) != self.tasks_ident:
            os.close(fd)
            raise _unsafe(f"{str(self.path)!r} is not the verified tasks directory")
        return fd


def _verified_tasks_dir(config: PortfolioConfig, project_id: str, scope: Scope | None) -> Path:
    """Return the REAL ``<verified project dir>/tasks``, built once and proven to be the tasks dir.

    Both sides of the comparison derive from the verified project dir, never
    from the tasks dir itself, so a ``tasks`` symlink/junction that points at
    another project's store (whose real path "contains" itself) is caught.  The
    returned path is the one every later step uses; it is never re-resolved.
    """
    kw = {} if scope is None else {"scope": scope}
    tasks_dir = get_tasks_dir(config, project_id, **kw)
    if tasks_dir is None:
        raise PrRecordError("task_not_found", f"no tasks directory for project {project_id!r}")
    project_dir = get_project_dir(config, project_id, **kw)
    if project_dir is None:
        raise PrRecordError("unsafe_store", f"cannot establish the verified project dir for {project_id!r}")
    verified = Path(project_dir).resolve() / "tasks"
    actual = os.path.normcase(str(Path(tasks_dir).resolve()))
    if actual != os.path.normcase(str(verified)) or Path(tasks_dir).is_symlink():
        raise PrRecordError(
            "unsafe_store",
            f"tasks directory {str(tasks_dir)!r} is not the real 'tasks' dir of project {project_id!r} "
            f"(resolves to {actual!r}); refusing to write through a link",
        )
    return verified


def _bad_ref(path: Path, why: str) -> PrRecordError:
    return PrRecordError("bad_task_ref", f"{str(path)!r} {why}")


def _locate(pin: _PinnedStore, task_id: str) -> tuple[Path, tuple[int, int]] | None:
    """Find the task file by lstat, component by component; never follows a link.

    Returns ``(path, file identity)`` or None.  Every directory between the
    tasks dir and the file must be a real directory and the file a regular file,
    else ``bad_task_ref``; nothing is re-resolved.
    """
    for cand in _candidate_task_paths(pin.path, task_id):
        cur = pin.path
        found = True
        for part in cand.relative_to(pin.path).parts:
            cur = cur / part
            try:
                st = os.lstat(cur)
            except (FileNotFoundError, NotADirectoryError):
                found = False
                break
            if _is_link(st):
                raise _bad_ref(cur, "is a link, not a real task file or state directory")
            if cur != cand and not stat.S_ISDIR(st.st_mode):
                found = False
                break
        if not found:
            continue
        if not stat.S_ISREG(st.st_mode):
            raise _bad_ref(cand, "is not a regular task file")
        return cand, _ident(st)
    return None


def _replace_file(pin: _PinnedStore, path: Path, file_ident: tuple[int, int], data: bytes) -> None:
    """Atomically replace regular file ``path`` with ``data`` via an exclusively created temp file.

    The temp file and the target are named from the verified path.  The store
    identity is re-checked before the temp file is made, right after it is
    written, and immediately before ``os.replace``; the target must still be the
    same regular file.  Any failure removes the temp file.
    """

    def check_target() -> os.stat_result:
        st = os.lstat(path)
        if _is_link(st) or not stat.S_ISREG(st.st_mode):
            raise _bad_ref(path, "is a link, not a regular task file")
        if _ident(st) != file_ident:
            raise _bad_ref(path, "was replaced while the record was being written; refusing to overwrite it")
        return st

    st = check_target()
    pin.recheck()
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
        except OSError:
            pass
        pin.recheck()
        check_target()
        pin.recheck()  # immediately before the swap: the narrowest window left
        retry_transient(os.replace, tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_via_path(pin: _PinnedStore, task_id: str, project_id: str, record: dict[str, Any]) -> Path:
    """Windows (no directory fds): path-based, pinned by an open handle plus identity re-checks."""
    with pin.hold():
        located = _locate(pin, task_id)
        if located is None:
            raise PrRecordError("task_not_found", f"task {task_id!r} not found in project {project_id!r}")
        path, file_ident = located
        # Bytes in, bytes out: text mode would fold/expand line endings.
        text = retry_transient(lambda: path.read_bytes()).decode("utf-8")
        _replace_file(pin, path, file_ident, _splice(text, record).encode("utf-8"))
    return path


def _write_via_dirfd(
    pin: _PinnedStore, dirfd: int, task_id: str, project_id: str, record: dict[str, Any]
) -> Path:
    """POSIX: every step is relative to the verified tasks dir's fd, so a swap cannot redirect it."""
    dflags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    for cand in _candidate_task_paths(pin.path, task_id):
        parts = cand.relative_to(pin.path).parts
        opened: list[int] = []
        try:
            cur = dirfd
            for part in parts[:-1]:
                try:
                    cur = os.open(part, dflags, dir_fd=cur)
                except FileNotFoundError:
                    break
                except OSError as exc:  # ELOOP/ENOTDIR: a link where a state dir should be
                    raise _bad_ref(cand, "passes through a link or non-directory") from exc
                opened.append(cur)
            else:
                name = parts[-1]
                try:
                    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=cur)
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise _bad_ref(cand, "is a link, not a regular task file") from exc
                with os.fdopen(fd, "rb") as fh:
                    st = os.fstat(fh.fileno())
                    if not stat.S_ISREG(st.st_mode):
                        raise _bad_ref(cand, "is not a regular task file")
                    data = fh.read()
                new = _splice(data.decode("utf-8"), record).encode("utf-8")
                _replace_in_dir(cur, name, st, new, cand)
                return cand
        finally:
            for fd_ in reversed(opened):
                os.close(fd_)
    raise PrRecordError("task_not_found", f"task {task_id!r} not found in project {project_id!r}")


def _replace_in_dir(dirfd: int, name: str, st: os.stat_result, data: bytes, shown: Path) -> None:
    tmp = f"{name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dirfd)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            try:
                os.fchmod(fh.fileno(), stat.S_IMODE(st.st_mode))
            except OSError:
                pass
        now = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
        if not stat.S_ISREG(now.st_mode) or _ident(now) != _ident(st):
            raise _bad_ref(shown, "was replaced while the record was being written; refusing to overwrite it")
        os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except OSError:
            pass
        raise


def record_pr_verification(
    config: PortfolioConfig,
    project_id: str,
    task_id: str,
    record: dict[str, Any],
    *,
    scope: Scope | None = None,
):
    """Write ``record`` as the task's ``pr_verification`` line; return the task file Path.

    ``record`` must carry exactly :data:`RECORD_KEYS`.  Raises
    :class:`PrRecordError` (file untouched) on a bad record, a missing task, an
    unsafe (linked or swapped) store, or frontmatter that is absent, non-mapping,
    unparseable or not a layout the splice supports.  Holds the project's
    ``.clawpm-tasks.lock`` across the read-splice-replace.

    The verified directory's IDENTITY is preserved through the write (nothing
    re-resolves by path).  POSIX: closed, every operation goes through a
    directory fd (``dir_fd``).  Windows: no directory fds, so an open handle
    pins the directory (the OS refuses to rename it or an ancestor) and the
    identity chain is re-checked before the temp file, after writing it and
    immediately before ``os.replace``; the residual is the lock file itself
    being a link and the stat-to-replace instant.
    """
    validate_task_ref(task_id)
    if set(record) != set(RECORD_KEYS):
        raise PrRecordError(
            "bad_record", f"record keys must be exactly {sorted(RECORD_KEYS)}, got {sorted(record)}"
        )
    pin = _PinnedStore(_verified_tasks_dir(config, project_id, scope))
    dirfd = pin.open_dirfd() if _dirfd_supported() else None
    try:
        pin.recheck()
        with file_lock(pin.path / _LOCK_NAME):
            # The lock file was opened by path: prove the lock is the verified dir's.
            pin.recheck()
            if dirfd is not None:
                return _write_via_dirfd(pin, dirfd, task_id, project_id, record)
            return _write_via_path(pin, task_id, project_id, record)
    finally:
        if dirfd is not None:
            os.close(dirfd)
