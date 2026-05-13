"""Regression tests for:

* Duplicate project rows in ``clawpm projects list --all`` when sibling worktree
  directories all carry ``.project/settings.toml`` with the same ``id``.
* Non-ASCII glyphs in hand-written ``click.echo`` lines crashing on Windows
  ``cp1252`` stdout (``UnicodeEncodeError`` on U+25CB / U+2713 / U+2717 / U+2192).

Both bugs were observed on real portfolios with multiple worktree-style clones
and on Windows consoles using the default ``cp1252`` codec. See the PR
description for the full reproduction.
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
from clawpm.discovery import discover_projects, load_portfolio_config


@pytest.fixture
def worktree_portfolio():
    """Portfolio with three sibling dirs all carrying id='alpha' — simulates
    the ``foo / foo-worktree-a / foo-worktree-b`` worktree pattern."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_dedup_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    for dir_name in ("alpha", "alpha-pr-sprint", "alpha-experiment"):
        d = projects_dir / dir_name / ".project"
        (d / "tasks").mkdir(parents=True)
        (d / "settings.toml").write_text(
            'id = "alpha"\nname = "Alpha"\nstatus = "active"\npriority = 3\n',
            encoding="utf-8",
        )

    (portfolio_root / "work_log.jsonl").touch()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    yield portfolio_root

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


class TestProjectDedup:
    """``discover_projects`` must return one row per id, preferring the
    canonical directory (name == id) over worktree-style siblings."""

    def test_discover_projects_dedups_by_id(self, worktree_portfolio):
        config = load_portfolio_config(worktree_portfolio)
        projects = discover_projects(config)
        ids = [p.id for p in projects]
        assert ids.count("alpha") == 1, (
            f"discover_projects emitted alpha {ids.count('alpha')}x — expected 1. ids={ids}"
        )

    def test_canonical_dir_wins_over_worktree(self, worktree_portfolio):
        config = load_portfolio_config(worktree_portfolio)
        projects = discover_projects(config)
        alpha = next(p for p in projects if p.id == "alpha")
        assert alpha.project_dir is not None
        assert alpha.project_dir.name == "alpha", (
            f"Expected canonical dir 'alpha' to win, got '{alpha.project_dir.name}'"
        )

    def test_fallback_is_deterministic_when_no_canonical_dir(self, tmp_path):
        """When no sibling dir's name matches the project id, the chosen
        winner must be deterministic — sort by directory name before iterating
        so ``Path.iterdir()`` order doesn't decide which settings.toml wins."""
        portfolio_root = tmp_path
        (portfolio_root / "portfolio.toml").write_text(
            f'portfolio_root = "{portfolio_root.as_posix()}"\n'
            f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n',
            encoding="utf-8",
        )
        projects_dir = portfolio_root / "projects"
        projects_dir.mkdir()

        # Three worktree-style dirs, all with id="beta", NONE named "beta".
        # Sorted order: beta-pr -> beta-tam -> beta-wip. First wins.
        for dir_name in ("beta-wip", "beta-pr", "beta-tam"):
            d = projects_dir / dir_name / ".project"
            (d / "tasks").mkdir(parents=True)
            (d / "settings.toml").write_text(
                'id = "beta"\nname = "Beta"\nstatus = "active"\npriority = 3\n',
                encoding="utf-8",
            )

        old_env = os.environ.get("CLAWPM_PORTFOLIO")
        os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
        try:
            config = load_portfolio_config(portfolio_root)
            projects = discover_projects(config)
            beta = next(p for p in projects if p.id == "beta")
            assert beta.project_dir is not None
            assert beta.project_dir.name == "beta-pr", (
                f"Expected sorted-first 'beta-pr' to win, got '{beta.project_dir.name}'. "
                f"Path.iterdir() order is not guaranteed — discover_projects must sort."
            )
        finally:
            if old_env:
                os.environ["CLAWPM_PORTFOLIO"] = old_env
            else:
                os.environ.pop("CLAWPM_PORTFOLIO", None)

    def test_projects_list_json_has_no_duplicates(self, worktree_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "projects", "list", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = [p["id"] for p in data["projects"]]
        assert ids.count("alpha") == 1, f"projects.list emitted alpha {ids.count('alpha')}x"


class TestCp1252StdoutSafety:
    """Hand-written ``click.echo`` lines must use ASCII glyphs only, otherwise
    Windows cp1252 stdout raises ``UnicodeEncodeError`` mid-render and abandons
    the rest of the output.

    We don't assert the full tabulated table is ASCII-only — tabulate-style
    formatters adapt their box-drawing chars to live stdout encoding at runtime
    (they emit Unicode in test harnesses but ASCII on real cp1252 consoles).
    Only hand-written click.echo paths are in scope.
    """

    # Chars previously found in hand-written click.echo lines that crash cp1252.
    BANNED_CHARS = ["\u25cb", "\u2713", "\u2717", "\u2192", "\u2190"]
    # ○        ✓        ✗        →        ←

    def test_untracked_block_is_cp1252_safe(self, worktree_portfolio):
        # Add an untracked git repo into project_roots to exercise the block.
        untracked = worktree_portfolio / "projects" / "loose-repo"
        untracked.mkdir()
        (untracked / ".git").mkdir()

        runner = CliRunner()
        result = runner.invoke(main, ["--format", "text", "projects", "list", "--all"])
        assert result.exit_code == 0, result.output

        if "Untracked git repos" not in result.output:
            pytest.skip("untracked-repo discovery did not surface the fixture")

        untracked_block = result.output.split("Untracked git repos", 1)[1]
        for ch in self.BANNED_CHARS:
            assert ch not in untracked_block, (
                f"untracked-repo block contains U+{ord(ch):04X} which crashes "
                f"on Windows cp1252 stdout. Use ASCII."
            )
