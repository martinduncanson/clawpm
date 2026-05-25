"""Tests for cross-platform locked append (CLAWP-032).

Two layers:
1. Unit: locked_append + append_jsonl_line basic behaviour, parent-mkdir,
   file-handle lifecycle.
2. Integration: concurrent writers (threads on POSIX/Windows AND subprocesses
   to exercise the cross-process lock) prove the lock prevents byte
   interleaving. Each writer emits N JSONL lines; post-run, EVERY line must
   parse cleanly and the total count must equal sum(N_per_writer).

The integration test is the calibration target: without `locked_append`, a
parallel-writer fixture on Windows reliably produces corrupted JSONL within
~100 lines per writer. With the helper, corruption count is 0 across
thousands of lines.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from clawpm.concurrency import append_jsonl_line, locked_append


# ---------------------------------------------------------------------------
# Unit: locked_append context manager
# ---------------------------------------------------------------------------


class TestLockedAppendBasics:
    def test_writes_a_line(self, tmp_path):
        p = tmp_path / "log.jsonl"
        with locked_append(p) as fh:
            fh.write('{"ts": "1"}\n')
        assert p.read_text(encoding="utf-8") == '{"ts": "1"}\n'

    def test_appends_across_calls(self, tmp_path):
        p = tmp_path / "log.jsonl"
        with locked_append(p) as fh:
            fh.write('{"ts": "1"}\n')
        with locked_append(p) as fh:
            fh.write('{"ts": "2"}\n')
        lines = p.read_text(encoding="utf-8").splitlines()
        assert lines == ['{"ts": "1"}', '{"ts": "2"}']

    def test_creates_parent_dir(self, tmp_path):
        p = tmp_path / "a" / "b" / "c" / "log.jsonl"
        with locked_append(p) as fh:
            fh.write('{"x": 1}\n')
        assert p.exists()

    def test_handle_closed_after_context(self, tmp_path):
        p = tmp_path / "log.jsonl"
        with locked_append(p) as fh:
            captured = fh
        assert captured.closed

    def test_handle_closed_after_exception(self, tmp_path):
        p = tmp_path / "log.jsonl"
        captured = None
        with pytest.raises(RuntimeError):
            with locked_append(p) as fh:
                captured = fh
                raise RuntimeError("boom")
        assert captured is not None and captured.closed


# ---------------------------------------------------------------------------
# Unit: append_jsonl_line convenience
# ---------------------------------------------------------------------------


class TestAppendJsonlLine:
    def test_appends_newline_if_missing(self, tmp_path):
        p = tmp_path / "log.jsonl"
        append_jsonl_line(p, '{"x": 1}')
        assert p.read_text(encoding="utf-8") == '{"x": 1}\n'

    def test_preserves_existing_newline(self, tmp_path):
        # Caller pre-formatted; don't double-newline.
        p = tmp_path / "log.jsonl"
        append_jsonl_line(p, '{"x": 1}\n')
        assert p.read_text(encoding="utf-8") == '{"x": 1}\n'

    def test_multiple_appends_one_line_each(self, tmp_path):
        p = tmp_path / "log.jsonl"
        for i in range(5):
            append_jsonl_line(p, json.dumps({"i": i}))
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 5
        assert [json.loads(line)["i"] for line in lines] == [0, 1, 2, 3, 4]


# ---------------------------------------------------------------------------
# Integration: concurrent writers must not corrupt the file
# ---------------------------------------------------------------------------


class TestConcurrentWriters:
    """The acceptance test for CLAWP-032.

    Without `locked_append`, parallel writers on Windows interleave bytes
    within JSONL records — silently corrupting the file. With the helper,
    every line is intact.
    """

    def _writer(self, path: Path, writer_id: int, n_lines: int) -> None:
        """Append n_lines tagged with writer_id."""
        for i in range(n_lines):
            entry = {"writer": writer_id, "line": i, "payload": "x" * 200}
            append_jsonl_line(path, json.dumps(entry))

    def test_threads_no_interleave(self, tmp_path):
        # 8 threads × 200 lines = 1600 total lines, ~220 bytes each.
        # Without the lock this reliably corrupts on Windows; with it, 0.
        p = tmp_path / "concurrent.jsonl"
        n_threads = 8
        n_lines = 200
        threads = [
            threading.Thread(target=self._writer, args=(p, wid, n_lines))
            for wid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Parse every line — corruption manifests as JSONDecodeError or
        # a mismatched writer/line tuple.
        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n_threads * n_lines, (
            f"Expected {n_threads * n_lines} lines, got {len(lines)} — "
            "concurrent writers lost data (lock failed)."
        )

        seen: set[tuple[int, int]] = set()
        for raw in lines:
            entry = json.loads(raw)  # raises if any byte interleaving
            seen.add((entry["writer"], entry["line"]))

        # Every (writer_id, line_no) combination should appear exactly once.
        expected = {(w, l) for w in range(n_threads) for l in range(n_lines)}
        assert seen == expected, (
            f"Missing/duplicate entries: "
            f"missing={expected - seen}, extra={seen - expected}"
        )

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows-specific append-atomicity check; POSIX append is "
               "already atomic up to PIPE_BUF (4KB) per write() per O_APPEND.",
    )
    def test_subprocess_no_interleave_windows(self, tmp_path):
        """Cross-process lock check on Windows.

        Spawns 4 Python subprocesses that each append 100 lines. Validates
        that the file ends with 400 intact lines, no interleaving. This
        exercises the OS-level lock (msvcrt.locking is enforced across
        processes, unlike pure-Python threading.Lock).
        """
        import subprocess

        p = tmp_path / "subprocess-test.jsonl"
        n_procs = 4
        n_lines = 100
        # Inline writer script that uses the helper. The cwd must be the repo
        # root for clawpm.concurrency to be importable.
        repo_root = Path(__file__).parent.parent
        writer_script = (
            "import sys, json\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, r'" + str(repo_root / "src") + "')\n"
            "from clawpm.concurrency import append_jsonl_line\n"
            "wid, n, path = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]\n"
            "for i in range(n):\n"
            "    append_jsonl_line(Path(path), json.dumps({'writer': wid, 'line': i, 'payload': 'x'*200}))\n"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", writer_script, str(wid), str(n_lines), str(p)],
                stderr=subprocess.PIPE,
            )
            for wid in range(n_procs)
        ]
        for proc in procs:
            _, err = proc.communicate(timeout=30)
            assert proc.returncode == 0, f"writer subprocess failed: {err.decode('utf-8', errors='replace')}"

        lines = p.read_text(encoding="utf-8").splitlines()
        assert len(lines) == n_procs * n_lines

        seen: set[tuple[int, int]] = set()
        for raw in lines:
            entry = json.loads(raw)
            seen.add((entry["writer"], entry["line"]))
        expected = {(w, l) for w in range(n_procs) for l in range(n_lines)}
        assert seen == expected
