"""CLAWP-109: the CLI must not glob-expand its own arguments on Windows.

Root cause: ``click.Command.main`` defaults ``windows_expand_args=True``. On
Windows it then runs every argv entry through glob / expanduser / expandvars, so
``--scope "src/**"`` reached the command as a list of file paths. These tests
drive the real entry-point path (``main()`` reading ``sys.argv``), not
``CliRunner`` (which bypasses argv handling), so they fail before the fix.
"""

from __future__ import annotations

import json
import os
import sys

import click
import pytest

from clawpm.cli import main

_ON_WINDOWS = os.name == "nt"


def _run_entry_point(monkeypatch, capsys, argv: list[str]) -> tuple[int, str, str]:
    """Run ``main()`` exactly as the console script does; return (code, out, err)."""
    monkeypatch.setattr(sys, "argv", ["clawpm", *argv])
    code = 0
    try:
        main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _globbable_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    (work / "src" / "sub").mkdir(parents=True)
    (work / "src" / "a.py").write_text("x", encoding="utf-8")
    (work / "src" / "sub" / "b.py").write_text("x", encoding="utf-8")
    monkeypatch.chdir(work)
    return work


def test_entry_point_disables_click_windows_arg_expansion(monkeypatch):
    """Platform-independent guard: the group must pass windows_expand_args=False."""
    seen: dict = {}

    def recorder(self, args=None, prog_name=None, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(click.core.Command, "main", recorder)
    main()
    assert seen.get("windows_expand_args") is False


@pytest.mark.skipif(not _ON_WINDOWS, reason="click only expands argv on Windows")
def test_tasks_add_scope_double_star_stored_verbatim(isolated_portfolio, tmp_path, monkeypatch, capsys):
    _globbable_cwd(tmp_path, monkeypatch)
    code, out, err = _run_entry_point(
        monkeypatch, capsys,
        ["--project", "test", "tasks", "add", "--title", "globby", "--scope", "src/**"],
    )
    assert code == 0, (out, err)
    task_files = list(isolated_portfolio.tasks_dir.rglob("*.md"))
    assert task_files, f"exit 0 but no task artefact written: out={out!r} err={err!r}"
    text = task_files[0].read_text(encoding="utf-8")
    assert "src/**" in text
    assert "a.py" not in text


@pytest.mark.skipif(not _ON_WINDOWS, reason="click only expands argv on Windows")
def test_free_text_double_star_reaches_command_verbatim(isolated_portfolio, tmp_path, monkeypatch, capsys):
    _globbable_cwd(tmp_path, monkeypatch)
    code, out, err = _run_entry_point(
        monkeypatch, capsys,
        ["--project", "test", "tasks", "add", "--title", "src/**", "--scope", "src/*"],
    )
    assert code == 0, (out, err)
    data = json.loads(out)["data"]
    assert data["title"] == "src/**"
    # A single-match pattern used to be silently rewritten to a file path.
    assert data["scope"] == ["src/*"]
