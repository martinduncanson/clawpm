"""Tests for the doctor encoding-risk check (CLAWP-011).

Three rule categories:
- nonascii-in-print: print/click.echo args with non-ASCII literals
- missing-encoding-kwarg: open/read_text/write_text without encoding=
- unconfigured-stdout: module has print/echo but no stdout reconfigure
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.encoding_check import (
    _is_nonascii,
    check_file,
    scan_path,
)


# ---------------------------------------------------------------------------
# Unit: _is_nonascii
# ---------------------------------------------------------------------------


class TestIsNonAscii:
    def test_ascii_returns_false(self):
        assert _is_nonascii("hello world") is False
        assert _is_nonascii("a + b = c") is False
        assert _is_nonascii("") is False

    def test_nonascii_returns_true(self):
        assert _is_nonascii("→") is True
        assert _is_nonascii("✓ done") is True
        assert _is_nonascii("café") is True
        assert _is_nonascii("○ bullet") is True


# ---------------------------------------------------------------------------
# Unit: check_file — rule-by-rule
# ---------------------------------------------------------------------------


def _write_file(tmp_path: Path, name: str, content: str) -> Path:
    """Write a .py file to tmp_path; returns the Path. Always UTF-8."""
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestRuleNonAsciiInPrint:
    def test_print_with_arrow_glyph_flagged(self, tmp_path):
        # Add a stdout reconfigure so we isolate THIS rule (otherwise
        # unconfigured-stdout would also fire).
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'if hasattr(sys.stdout, "reconfigure"):\n'
            '    sys.stdout.reconfigure(encoding="utf-8")\n'
            'print("→ task done")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules

    def test_click_echo_with_glyph_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys, click\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'click.echo("✓ ok")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules

    def test_ascii_only_print_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'print("hello world")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" not in rules

    def test_fstring_with_glyph_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'name = "task"\n'
            'print(f"→ {name}")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules


class TestRuleMissingEncodingKwarg:
    def test_open_without_encoding_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            "    return open(p)\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" in rules

    def test_open_with_encoding_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            '    return open(p, encoding="utf-8")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_read_text_without_encoding_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "from pathlib import Path\n"
            "def f(p):\n"
            "    return Path(p).read_text()\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" in rules

    def test_write_text_with_encoding_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "from pathlib import Path\n"
            "def f(p, t):\n"
            '    Path(p).write_text(t, encoding="utf-8")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_read_bytes_not_flagged(self, tmp_path):
        # read_bytes/write_bytes don't take encoding; correct to skip
        f = _write_file(tmp_path, "x.py",
            "from pathlib import Path\n"
            "def f(p):\n"
            "    return Path(p).read_bytes()\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules


class TestRuleUnconfiguredStdout:
    def test_module_with_print_no_reconfigure_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" in rules

    def test_module_with_print_AND_reconfigure_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" not in rules

    def test_module_with_hasattr_guarded_reconfigure_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'if hasattr(sys.stdout, "reconfigure"):\n'
            '    sys.stdout.reconfigure(encoding="utf-8")\n'
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" not in rules

    def test_pure_library_module_no_print_not_flagged(self, tmp_path):
        # Module doesn't print anything → no need to reconfigure stdout
        f = _write_file(tmp_path, "x.py",
            "def add(a, b):\n"
            "    return a + b\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" not in rules


# ---------------------------------------------------------------------------
# check_file edge cases
# ---------------------------------------------------------------------------


class TestCheckFileEdgeCases:
    def test_syntax_error_returns_empty(self, tmp_path):
        f = _write_file(tmp_path, "x.py", "def broken(:\n")  # syntax error
        assert check_file(f) == []

    def test_empty_file_returns_empty(self, tmp_path):
        f = _write_file(tmp_path, "x.py", "")
        assert check_file(f) == []

    def test_unreadable_file_returns_empty(self, tmp_path):
        # Nonexistent file → graceful empty
        assert check_file(tmp_path / "nonexistent.py") == []


# ---------------------------------------------------------------------------
# scan_path
# ---------------------------------------------------------------------------


class TestScanPath:
    def test_skips_venv_and_pycache(self, tmp_path):
        # Set up a project with risk in src/ AND in .venv/ — only src/ should be scanned
        (tmp_path / "src").mkdir()
        _write_file(tmp_path / "src", "good.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            "print('hello')\n"
        )
        (tmp_path / ".venv").mkdir()
        _write_file(tmp_path / ".venv", "bad.py",
            'print("→ should not be scanned")\n'
        )
        findings = scan_path(tmp_path)
        # The .venv file would have multiple findings if scanned
        # The src/ file has no findings (clean)
        files_with_findings = {f["file"] for f in findings}
        assert not any(".venv" in f for f in files_with_findings)

    def test_single_file_path(self, tmp_path):
        f = _write_file(tmp_path, "x.py", 'print("→ glyph")\n')
        findings = scan_path(f)
        assert any(r["rule"] == "nonascii-in-print" for r in findings)

    def test_non_py_file_skipped(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text('print("→ glyph")\n', encoding="utf-8")
        findings = scan_path(f)
        assert findings == []


# ---------------------------------------------------------------------------
# CLI wiring: --check-encoding flag
# ---------------------------------------------------------------------------


class TestDoctorCheckEncodingFlag:
    def test_flag_default_off_skips_encoding_check(self, tmp_path, monkeypatch):
        """Without --check-encoding, no encoding_risks key effort beyond an empty list."""
        (tmp_path / "portfolio.toml").write_text(
            f'portfolio_root = "{tmp_path.as_posix()}"\n'
            f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n",
            encoding="utf-8",
        )
        (tmp_path / "projects").mkdir()
        (tmp_path / "work_log.jsonl").touch()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        # Field present but empty list — additive shape, doesn't break consumers
        assert data.get("encoding_risks") == []

    def test_flag_on_returns_encoding_risks_key(self, tmp_path, monkeypatch):
        (tmp_path / "portfolio.toml").write_text(
            f'portfolio_root = "{tmp_path.as_posix()}"\n'
            f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n",
            encoding="utf-8",
        )
        (tmp_path / "projects").mkdir()
        (tmp_path / "work_log.jsonl").touch()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--check-encoding"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "encoding_risks" in data
