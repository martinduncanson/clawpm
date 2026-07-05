"""CLAWP-072 CLI ergonomics regressions.

Covers:
- 072-003: top-level `doctor --project` mirrors `project doctor -p`.
- 072-005: `python -m clawpm` module invocation works.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

import clawpm
from clawpm.cli import main


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(f'''
portfolio_root = "{portfolio_root.as_posix()}"
project_roots = ["{(portfolio_root / 'projects').as_posix()}"]

[defaults]
status = "active"
''')
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text('''
id = "test"
name = "Test Project"
status = "active"
priority = 3
''')
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    yield {"root": portfolio_root}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


def test_doctor_project_flag_matches_project_doctor(temp_portfolio):
    """072-003: `clawpm doctor --project test` == `clawpm project doctor -p test`."""
    runner = CliRunner()
    top = runner.invoke(main, ["-p", "test", "doctor", "--project", "test"])
    grp = runner.invoke(main, ["-p", "test", "project", "doctor", "-p", "test"])
    assert top.exit_code == grp.exit_code
    assert top.output == grp.output


def test_doctor_no_flag_still_scans_portfolio(temp_portfolio):
    """072-003: omitting the flag preserves whole-portfolio behaviour."""
    runner = CliRunner()
    r = runner.invoke(main, ["doctor"])
    assert r.exit_code == 0, r.output


def _module_env():
    src_dir = str(Path(clawpm.__file__).resolve().parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = src_dir + os.pathsep + env.get("PYTHONPATH", "")
    # Pin width so click wraps help text identically to CliRunner's default,
    # otherwise the terminal-width-dependent line wrapping diverges.
    env["COLUMNS"] = "80"
    return env


def test_python_m_clawpm_help_exits_zero():
    """072-005: `python -m clawpm --help` exits 0 and lists the CLI commands."""
    r = subprocess.run(
        [sys.executable, "-m", "clawpm", "--help"],
        capture_output=True, text=True, env=_module_env(),
    )
    assert r.returncode == 0, r.stderr
    assert "Usage:" in r.stdout
    # `python -m clawpm` must expose the exact same command surface as the
    # installed `clawpm` entry point. (Exact help-body byte matching is brittle:
    # click truncates each command's short-help to the detected terminal width,
    # which differs between a captured subprocess and CliRunner.)
    for name in main.commands:
        assert name in r.stdout, f"module help missing command {name!r}"
