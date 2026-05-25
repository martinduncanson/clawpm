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
    _is_print_like_call,
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

    def test_multiarg_print_second_arg_glyph_flagged(self, tmp_path):
        # ast.walk traverses all node.args; lock this in.
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'print("ok", "→ done")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules

    def test_logger_echo_not_flagged_as_print_like(self, tmp_path):
        # PRE-REVIEW P1: domain objects with .echo/.print methods must not
        # false-positive. logger.echo is NOT stdout.
        f = _write_file(tmp_path, "x.py",
            "def f(logger):\n"
            '    logger.echo("→ glyph in ascii-only-stdout-receiver")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        # Neither rule 1 nor rule 3 should fire — logger isn't a stdout writer
        assert "nonascii-in-print" not in rules
        assert "unconfigured-stdout" not in rules

    def test_rich_console_print_not_flagged(self, tmp_path):
        # Rich's console.print handles its own encoding. The scanner should
        # not flag it as print-like unless the receiver is click/typer/sys.
        f = _write_file(tmp_path, "x.py",
            "def f(console):\n"
            '    console.print("→ Rich handles this")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" not in rules
        assert "unconfigured-stdout" not in rules

    def test_click_secho_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys, click\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'click.secho("✓ ok", fg="green")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules

    def test_typer_echo_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "import sys, typer\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'typer.echo("→ task")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules

    def test_sys_stdout_write_with_glyph_flagged(self, tmp_path):
        # sys.stdout.write is the canonical low-level stdout-writer.
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            'sys.stdout.reconfigure(encoding="utf-8")\n'
            'sys.stdout.write("→ glyph")\n'
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

    def test_open_binary_mode_positional_not_flagged(self, tmp_path):
        # PRE-REVIEW P1: open(p, "rb") has no encoding by design.
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            '    return open(p, "rb")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_open_binary_mode_kwarg_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            '    return open(p, mode="wb")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_open_text_mode_explicit_still_flagged_without_encoding(self, tmp_path):
        # open(p, "r") is text mode → encoding= IS required
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            '    return open(p, "r")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" in rules

    def test_path_open_binary_mode_not_flagged(self, tmp_path):
        f = _write_file(tmp_path, "x.py",
            "from pathlib import Path\n"
            "def f(p):\n"
            '    return Path(p).open("rb")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_with_open_no_encoding_flagged(self, tmp_path):
        # `with open(p) as f:` — ast.walk reaches the Call inside With.items
        f = _write_file(tmp_path, "x.py",
            "def f(p):\n"
            "    with open(p) as h:\n"
            "        return h.read()\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" in rules

    def test_zipfile_open_not_flagged(self, tmp_path):
        # Codex PR#5 round-1 P1: `zipfile.ZipFile(p).open(name)` is not a
        # text-file API — encoding= isn't a valid kwarg on it. Must not flag.
        f = _write_file(tmp_path, "x.py",
            "import zipfile\n"
            "def f(p, name):\n"
            "    return zipfile.ZipFile(p).open(name)\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_socket_open_not_flagged(self, tmp_path):
        # Generic `obj.open(...)` on a non-pathlib receiver must not flag.
        f = _write_file(tmp_path, "x.py",
            "def f(conn):\n"
            "    return conn.open()\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" not in rules

    def test_pathlib_path_open_no_encoding_still_flagged(self, tmp_path):
        # The pathlib narrowing must not drop the real signal: Path(p).open()
        # without encoding= is exactly what we want flagged.
        f = _write_file(tmp_path, "x.py",
            "from pathlib import Path\n"
            "def f(p):\n"
            "    return Path(p).open()\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "missing-encoding-kwarg" in rules

    def test_kwargs_forward_silences_finding(self, tmp_path):
        # PR#5 round-2 (borrowed from PR#8): wrapper functions that forward
        # **kwargs through to open() may legitimately have encoding= flowing
        # in via the dict. Don't flag.
        f = _write_file(tmp_path, "x.py",
            "def wrapper(p, **kwargs):\n"
            "    return open(p, **kwargs)\n"
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

    def test_try_guarded_reconfigure_not_flagged(self, tmp_path):
        # try: sys.stdout.reconfigure(...) except AttributeError: pass — common
        # defensive pattern. Must be recognised, not flagged.
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            "try:\n"
            '    sys.stdout.reconfigure(encoding="utf-8")\n'
            "except AttributeError:\n"
            "    pass\n"
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" not in rules

    def test_reconfigure_inside_function_still_flagged(self, tmp_path):
        # Lock in the conservative scope: reconfigure inside a function does
        # not protect module-level prints. If someone "helpfully" walks the
        # whole tree, this test catches the regression.
        f = _write_file(tmp_path, "x.py",
            "import sys\n"
            "def setup():\n"
            '    sys.stdout.reconfigure(encoding="utf-8")\n'
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" in rules

    def test_non_sys_stdout_reconfigure_does_not_satisfy(self, tmp_path):
        # Codex PR#5 round-1 P2: `process.stdout.reconfigure(...)` on a
        # subprocess handle reconfigures the subprocess's stream, not the
        # host's. Module-level prints in this module are still at risk —
        # must remain flagged.
        f = _write_file(tmp_path, "x.py",
            "import subprocess\n"
            "process = subprocess.Popen(['x'])\n"
            'process.stdout.reconfigure(encoding="utf-8")\n'
            "def main():\n"
            "    print('hello')\n"
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" in rules

    def test_logger_only_module_not_flagged(self, tmp_path):
        # logger.info / logger.echo is NOT stdout — module with only logging
        # should not be flagged for missing reconfigure.
        f = _write_file(tmp_path, "x.py",
            "import logging\n"
            "logger = logging.getLogger(__name__)\n"
            "def f():\n"
            '    logger.info("→ logged not printed")\n'
        )
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unconfigured-stdout" not in rules


# ---------------------------------------------------------------------------
# check_file edge cases
# ---------------------------------------------------------------------------


class TestCheckFileEdgeCases:
    def test_syntax_error_surfaces_unparseable_finding(self, tmp_path):
        # PRE-REVIEW P1: surface, don't swallow. A .py file that won't parse
        # is itself signal worth a structured finding.
        f = _write_file(tmp_path, "x.py", "def broken(:\n")
        findings = check_file(f)
        rules = [r["rule"] for r in findings]
        assert "unparseable-source" in rules
        # And only that rule fires (we don't try to scan the unparseable AST)
        unparseable = next(r for r in findings if r["rule"] == "unparseable-source")
        assert "syntax error" in unparseable["evidence"].lower()

    def test_empty_file_returns_empty(self, tmp_path):
        f = _write_file(tmp_path, "x.py", "")
        assert check_file(f) == []

    def test_unreadable_file_surfaces_unreadable_finding(self, tmp_path):
        # Nonexistent file → structured finding (not silent skip)
        findings = check_file(tmp_path / "nonexistent.py")
        rules = [r["rule"] for r in findings]
        assert "unreadable-source" in rules


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

    def test_site_packages_skipped(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "site-packages").mkdir()
        _write_file(tmp_path / "src" / "site-packages", "bad.py",
            'print("→ should not be scanned")\n'
        )
        findings = scan_path(tmp_path)
        files_with_findings = {f["file"] for f in findings}
        assert not any("site-packages" in f for f in files_with_findings)

    def test_scan_truncation_surfaces_finding(self, tmp_path):
        # Create more files than max_files; expect truncation marker
        for i in range(5):
            _write_file(tmp_path, f"f{i}.py", "x = 1\n")
        findings = scan_path(tmp_path, max_files=2)
        rules = [r["rule"] for r in findings]
        assert "scan-truncated" in rules

    def test_ancestor_named_build_not_skipped(self, tmp_path):
        # Codex PR#5 round-1 P1: a project located UNDER a directory named
        # "build" (e.g. CI runners use `/build/<workspace>/...`) was
        # silently skipped because absolute path.parts matched skip_dirs.
        # Filter must apply to parts RELATIVE to root, not absolute.
        ancestor_build = tmp_path / "build"
        ancestor_build.mkdir()
        proj = ancestor_build / "myproj"
        proj.mkdir()
        _write_file(proj, "risky.py", 'print("→ glyph in real source")\n')
        findings = scan_path(proj)
        rules = [r["rule"] for r in findings]
        assert "nonascii-in-print" in rules, findings

    def test_two_sided_skip_assertion(self, tmp_path):
        # Defends against "scanner broken entirely" regressions: a risky
        # file in src/ MUST be found, a risky file in .venv/ MUST NOT.
        (tmp_path / "src").mkdir()
        _write_file(tmp_path / "src", "risky.py",
            'print("→ glyph in real source")\n'
        )
        (tmp_path / ".venv").mkdir()
        _write_file(tmp_path / ".venv", "bad.py",
            'print("→ should not be scanned")\n'
        )
        findings = scan_path(tmp_path)
        files = {f["file"] for f in findings}
        assert any("src/risky.py" in f for f in files), files
        assert not any(".venv" in f for f in files), files


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

    def test_exit_code_zero_when_only_encoding_findings(self, tmp_path, monkeypatch):
        # PRE-REVIEW P1: lock contract — encoding findings are advisory,
        # not failures. Without --strict, doctor must exit 0 even with risks.
        # Set up a project with a risky .py file.
        proj_root = tmp_path / "projects"
        proj_root.mkdir()
        proj = proj_root / "demo"
        proj.mkdir()
        (proj / ".project").mkdir()
        (proj / ".project" / "settings.toml").write_text(
            'id = "demo"\n'
            'name = "demo"\n'
            f'repo_path = "{proj.as_posix()}"\n'
            'status = "active"\n',
            encoding="utf-8",
        )
        (proj / "risky.py").write_text(
            'print("→ glyph")\n',
            encoding="utf-8",
        )
        (tmp_path / "portfolio.toml").write_text(
            f'portfolio_root = "{tmp_path.as_posix()}"\n'
            f'project_roots = ["{proj_root.as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n",
            encoding="utf-8",
        )
        (tmp_path / "work_log.jsonl").touch()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--check-encoding"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert len(data["encoding_risks"]) > 0
        # Every finding has the documented shape
        for er in data["encoding_risks"]:
            assert {"project_id", "file", "line", "rule", "evidence"} <= set(er.keys())


# ---------------------------------------------------------------------------
# _is_print_like_call discrimination (PRE-REVIEW P1)
# ---------------------------------------------------------------------------


class TestIsPrintLikeCallDiscrimination:
    """Locks in the receiver-tightening: only click/typer/sys roots trigger."""

    def _call_from(self, code: str):
        import ast as _ast
        tree = _ast.parse(code)
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                return node
        raise AssertionError("no Call in code")

    def test_bare_print_is_print_like(self):
        assert _is_print_like_call(self._call_from('print("x")')) is True

    def test_click_echo_is_print_like(self):
        assert _is_print_like_call(self._call_from('click.echo("x")')) is True

    def test_typer_echo_is_print_like(self):
        assert _is_print_like_call(self._call_from('typer.echo("x")')) is True

    def test_sys_stdout_write_is_print_like(self):
        assert _is_print_like_call(self._call_from('sys.stdout.write("x")')) is True

    def test_logger_echo_not_print_like(self):
        assert _is_print_like_call(self._call_from('logger.echo("x")')) is False

    def test_console_print_not_print_like(self):
        assert _is_print_like_call(self._call_from('console.print("x")')) is False

    def test_file_write_not_print_like(self):
        assert _is_print_like_call(self._call_from('f.write("x")')) is False

    def test_unrelated_method_not_print_like(self):
        # `.send()`, `.put()`, etc. — totally outside the rule
        assert _is_print_like_call(self._call_from('queue.put("x")')) is False
