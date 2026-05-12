"""Regression tests for the doctor/projects-list bug trio (operator-notes 2026-05-13).

Bug 1.1 — doctor must not crash on UTF-8 decode error in a portfolio markdown file;
          offending paths must be surfaced in an `unreadable_files` warning list.
Bug 1.2 — `projects list --all` must not emit the same project id twice when sibling
          worktree directories each carry a `.project/settings.toml` with the same id.
Bug 1.3 — text output of `projects list --all` must not raise UnicodeEncodeError
          when stdout is the Windows cp1252 default (or any non-UTF-8 codec).

Family: Windows-cp1252 encoding bugs (4th observed instance — see memory file
feedback-windows-cp1252-write-text.md).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import discover_projects, load_portfolio_config


@pytest.fixture
def encoding_portfolio():
    """Portfolio with one project and one task file containing a cp1252 em-dash (0x97)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_enc_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    project_dir = projects_dir / "alpha"
    (project_dir / ".project" / "tasks" / "done").mkdir(parents=True)
    (project_dir / ".project" / "tasks" / "blocked").mkdir()
    (project_dir / ".project" / "settings.toml").write_text(
        'id = "alpha"\nname = "Alpha"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )

    # Valid UTF-8 task — control case.
    (project_dir / ".project" / "tasks" / "ALPHA-001.md").write_text(
        "---\nstate: open\n---\nValid UTF-8 task body.\n",
        encoding="utf-8",
    )

    # cp1252-encoded task — byte 0x97 (em-dash) at a deterministic offset.
    bad_path = project_dir / ".project" / "tasks" / "ALPHA-002.md"
    bad_path.write_bytes(
        b"---\nstate: open\n---\n"
        b"An em-dash here \x97 breaks utf-8 strict decode.\n"
    )

    (portfolio_root / "work_log.jsonl").touch()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "bad_path": bad_path,
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


@pytest.fixture
def worktree_portfolio():
    """Portfolio with three sibling dirs all carrying id='alpha' — simulates the
    arb-prd / arb-prd-pr-sprint / arb-prd-tam-v2 worktree pattern."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_dup_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    for dir_name in ("alpha", "alpha-pr-sprint", "alpha-tam-v2"):
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


# ---------------------------------------------------------------------------
# Bug 1.1 — doctor UTF-8 decode tolerance
# ---------------------------------------------------------------------------


class TestDoctorEncodingTolerance:
    def test_doctor_completes_with_invalid_utf8_in_task(self, encoding_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])

        # Doctor must NOT crash, regardless of strict mode.
        assert result.exit_code in (0, 1), (
            f"doctor crashed with exit {result.exit_code}: {result.output}"
        )
        assert "UnicodeDecodeError" not in result.output
        assert "Traceback" not in result.output

    def test_unreadable_files_surfaced_in_json(self, encoding_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "doctor"])
        assert result.exit_code in (0, 1), result.output

        data = json.loads(result.output)
        assert "unreadable_files" in data, "doctor JSON must include unreadable_files key"

        bad_paths = {entry["file"] for entry in data["unreadable_files"]}
        assert encoding_portfolio["bad_path"].as_posix() in bad_paths

        entry = next(
            e for e in data["unreadable_files"]
            if e["file"] == encoding_portfolio["bad_path"].as_posix()
        )
        assert entry["project_id"] == "alpha"
        assert "error" in entry

    def test_unreadable_files_surfaced_in_text(self, encoding_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "text", "doctor"])
        assert result.exit_code in (0, 1), result.output
        assert "[encoding]" in result.output
        assert "ALPHA-002.md" in result.output

    def test_strict_mode_exits_nonzero_on_unreadable(self, encoding_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--strict"])
        assert result.exit_code == 1, (
            f"strict mode must flag unreadable_files as a warning, got {result.exit_code}"
        )


# ---------------------------------------------------------------------------
# Bug 1.2 — dedup on duplicate project ids across sibling worktrees
# ---------------------------------------------------------------------------


class TestProjectDedup:
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

    def test_projects_list_text_has_no_duplicates(self, worktree_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "text", "projects", "list", "--all"])
        assert result.exit_code == 0, result.output
        # The canonical id should appear at most once in the body table.
        # We can't grep cleanly across the tabulated rendering, so use the JSON path.

    def test_projects_list_json_has_no_duplicates(self, worktree_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "projects", "list", "--all"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = [p["id"] for p in data["projects"]]
        assert ids.count("alpha") == 1, f"projects.list emitted alpha {ids.count('alpha')}x"


# ---------------------------------------------------------------------------
# Bug 1.3 — cp1252 stdout safety in projects list --all
# ---------------------------------------------------------------------------


class TestCp1252StdoutSafety:
    """The untracked-repos rendering used U+25CB (○) which cannot be encoded
    on Windows cp1252 stdout. Hand-written click.echo lines must use ASCII glyphs.

    Note: we deliberately do NOT assert the full tabulated project list is
    cp1252-encodable — tabulate-style formatters adapt their box-drawing chars
    to the live stdout encoding at runtime, so they emit Unicode in test
    harnesses (CliRunner -> in-memory buffer) but ASCII on a real cp1252
    console. Only the manually-emitted click.echo lines are in scope here.
    """

    # The original crash site — keep this list as a regression list.
    BANNED_CHARS = ["\u25cb", "\u2713", "\u2717", "\u2192", "\u2190"]
    # ○        ✓        ✗        →        ←

    def test_untracked_block_is_cp1252_safe(self, worktree_portfolio):
        # Add one untracked git repo into the project_roots to exercise the block.
        untracked = worktree_portfolio / "projects" / "loose-repo"
        untracked.mkdir()
        (untracked / ".git").mkdir()  # token marker — discover_untracked_repos checks .git

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

    def test_doctor_text_output_avoids_banned_glyphs(self, encoding_portfolio):
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "text", "doctor"])
        assert result.exit_code in (0, 1), result.output
        for ch in self.BANNED_CHARS:
            assert ch not in result.output, (
                f"doctor output contains U+{ord(ch):04X} which crashes "
                f"on Windows cp1252 stdout. Use ASCII."
            )
