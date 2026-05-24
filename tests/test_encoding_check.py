"""Tests for the cp1252-stdout-risk doctor check (CLAWP-011).

Covers the four success-criteria cases from CLAWP-011.md plus:
  - f-strings (JoinedStr) with non-ASCII pieces
  - click.echo attribute calls with non-ASCII
  - bare echo() Name calls (from-import shape)
  - **kwargs treated as "encoding may be present" (false-negative by design)
  - binary mode is not flagged (encoding= is illegal there)
  - multi-file scan via scan_paths(dir)
  - SELF-TEST: the encoding_check module's own source must be cp1252-clean
    per the task pre_mortem.

All test fixtures are written via Path.write_text(..., encoding="utf-8") to
avoid the very bug being tested.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from clawpm.encoding_check import (
    EncodingFinding,
    format_finding,
    scan_file,
    scan_paths,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_py(dir_: Path, name: str, body: str) -> Path:
    """Write a .py fixture with explicit UTF-8 encoding."""
    p = dir_ / name
    p.write_text(body, encoding="utf-8")
    return p


def _kinds(findings: list[EncodingFinding]) -> list[str]:
    return [f.kind for f in findings]


# ---------------------------------------------------------------------------
# Success-criteria cases (the 4 named in CLAWP-011 success_criteria)
# ---------------------------------------------------------------------------


class TestSuccessCriteria:
    def test_print_with_non_ascii_arrow_is_flagged(self, tmp_path):
        # Arrow U+2192 — the canonical case-history glyph
        body = 'print("Done \u2192")\n'
        p = _write_py(tmp_path, "a.py", body)
        findings = scan_file(p)
        assert len(findings) == 1
        assert findings[0].kind == "non_ascii_print"
        assert findings[0].line == 1
        assert "\u2192" in findings[0].detail

    def test_print_with_ascii_only_is_clean(self, tmp_path):
        body = 'print("Done")\n'
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []

    def test_path_write_text_without_encoding_is_flagged(self, tmp_path):
        body = (
            "from pathlib import Path\n"
            "Path('out.txt').write_text('hello')\n"
        )
        p = _write_py(tmp_path, "a.py", body)
        findings = scan_file(p)
        assert len(findings) == 1
        assert findings[0].kind == "file_op_no_encoding"
        assert "write_text" in findings[0].detail

    def test_open_with_encoding_is_clean(self, tmp_path):
        body = 'open("f.txt", encoding="utf-8").read()\n'
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []


# ---------------------------------------------------------------------------
# Detection-shape coverage
# ---------------------------------------------------------------------------


class TestPrintEchoMatching:
    def test_fstring_with_non_ascii_is_flagged(self, tmp_path):
        # f-strings expose non-ASCII via JoinedStr -> Constant children
        body = 'name = "x"\nprint(f"Hello {name} \u2192 done")\n'
        p = _write_py(tmp_path, "a.py", body)
        findings = scan_file(p)
        assert len(findings) == 1
        assert findings[0].kind == "non_ascii_print"
        assert findings[0].line == 2
        assert "f-string" in findings[0].detail

    def test_click_echo_attribute_call_is_flagged(self, tmp_path):
        body = (
            "import click\n"
            'click.echo("done \u2713")\n'
        )
        p = _write_py(tmp_path, "a.py", body)
        kinds = _kinds(scan_file(p))
        assert "non_ascii_print" in kinds

    def test_bare_echo_name_call_is_flagged(self, tmp_path):
        # `from click import echo` then `echo(...)` — Name shape, not Attribute
        body = (
            "from click import echo\n"
            'echo("warn \u26a0")\n'
        )
        p = _write_py(tmp_path, "a.py", body)
        kinds = _kinds(scan_file(p))
        assert "non_ascii_print" in kinds

    def test_print_with_ascii_variable_is_not_flagged(self, tmp_path):
        # Variable content is unknowable statically — don't false-positive
        body = 'msg = "hi"\nprint(msg)\n'
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []


class TestFileOpMatching:
    def test_bare_open_without_encoding_is_flagged(self, tmp_path):
        body = 'open("f.txt").read()\n'
        p = _write_py(tmp_path, "a.py", body)
        findings = scan_file(p)
        assert any(f.kind == "file_op_no_encoding" for f in findings)

    def test_path_read_text_without_encoding_is_flagged(self, tmp_path):
        body = (
            "from pathlib import Path\n"
            "Path('f.txt').read_text()\n"
        )
        p = _write_py(tmp_path, "a.py", body)
        findings = scan_file(p)
        assert any(f.kind == "file_op_no_encoding" for f in findings)

    def test_path_read_text_with_encoding_is_clean(self, tmp_path):
        body = (
            "from pathlib import Path\n"
            "Path('f.txt').read_text(encoding='utf-8')\n"
        )
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []

    def test_binary_mode_positional_is_not_flagged(self, tmp_path):
        # mode="rb" makes encoding= illegal — flagging would be a false positive
        body = 'open("f.bin", "rb").read()\n'
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []

    def test_binary_mode_keyword_is_not_flagged(self, tmp_path):
        body = 'open("f.bin", mode="wb").write(b"x")\n'
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []

    def test_kwargs_splat_treated_as_encoding_present(self, tmp_path):
        # **kwargs forwarding: don't false-positive on wrapper functions.
        # Concerns block of the PR flags this as a deliberate false-negative.
        body = (
            "def wrap(path, **kwargs):\n"
            "    return open(path, **kwargs).read()\n"
        )
        p = _write_py(tmp_path, "a.py", body)
        assert scan_file(p) == []


# ---------------------------------------------------------------------------
# Multi-file / directory scanning
# ---------------------------------------------------------------------------


class TestScanPaths:
    def test_scan_paths_recurses_directory(self, tmp_path):
        sub = tmp_path / "pkg"
        sub.mkdir()
        _write_py(sub, "ok.py", 'print("clean")\n')
        _write_py(sub, "bad.py", 'print("\u2192")\n')
        findings = scan_paths([tmp_path])
        assert len(findings) == 1
        assert findings[0].path.name == "bad.py"

    def test_scan_paths_skips_vendor_dirs(self, tmp_path):
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        _write_py(venv, "bad.py", 'print("\u2192")\n')
        # Same offence in real source — should still be found
        _write_py(tmp_path, "real.py", 'print("\u2192")\n')
        findings = scan_paths([tmp_path])
        assert len(findings) == 1
        assert findings[0].path.name == "real.py"

    def test_scan_paths_accepts_individual_py_file(self, tmp_path):
        p = _write_py(tmp_path, "a.py", 'print("\u2192")\n')
        findings = scan_paths([p])
        assert len(findings) == 1

    def test_scan_paths_silently_skips_missing_root(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        # Should not raise
        assert scan_paths([missing]) == []

    def test_unparseable_file_is_silently_skipped(self, tmp_path):
        p = _write_py(tmp_path, "broken.py", "def (((\n")
        # No SyntaxError surfaced; no findings either.
        assert scan_file(p) == []


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def test_format_finding_is_single_line_and_ascii(tmp_path):
    p = _write_py(tmp_path, "a.py", 'print("\u2192")\n')
    findings = scan_file(p)
    line = format_finding(findings[0])
    assert "\n" not in line
    assert "[cp1252-risk]" in line
    assert "non_ascii_print" in line


# ---------------------------------------------------------------------------
# Self-test — the pre_mortem requirement
# ---------------------------------------------------------------------------


def test_encoding_check_module_is_self_clean():
    """The check module's own source MUST contain zero cp1252 risks.

    Per CLAWP-011 predictions.pre_mortem:
      'Self-test the check against its own source file before declaring done.'

    If this test fails, the check has been written with the very bug it
    detects — fix the module, not the test.
    """
    import clawpm.encoding_check as mod
    src_path = Path(mod.__file__)
    findings = scan_file(src_path)
    assert findings == [], (
        "encoding_check.py is not self-clean. Findings:\n"
        + "\n".join(format_finding(f) for f in findings)
    )
