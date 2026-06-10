"""Auto-ID allocation regression tests (CLAWP-047).

The headline bug: a project id whose ``upper()[:5]`` prefix contains a hyphen
(``arb-prd`` -> ``ARB-P``) broke the ``.md`` number parser — it did
``f.stem.split("-")[1]`` which grabbed ``"P"`` from ``ARB-P-000``, raised
ValueError, skipped EVERY file, and collapsed every new task to ``ARB-P-000``,
silently overwriting prior tasks. The directory scan beside it used an anchored
regex and was correct; the fix unifies the two.

Non-hyphenated prefixes (``clawpm`` -> ``CLAWP``) were never affected — which is
why the project dogfooding clawpm never saw it but ``arb-prd`` did.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main


def _make_portfolio(tmp_path: Path, monkeypatch, project_id: str) -> Path:
    """Register a single project (dir name == id, so it's the canonical dir) and
    point CLAWPM_PORTFOLIO at it. Returns the project's tasks dir."""
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    proj_meta = tmp_path / "projects" / project_id / ".project"
    tasks_dir = proj_meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (proj_meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return tasks_dir


def _add(project_id: str, title: str) -> str:
    """Run `clawpm tasks add` and return the allocated id."""
    res = CliRunner().invoke(
        main, ["--format", "json", "tasks", "add", "--project", project_id, "--title", title]
    )
    assert res.exit_code == 0, res.output
    return json.loads(res.output)["data"]["id"]


class TestHyphenatedPrefixCollision:
    def test_sequential_ids_not_collision(self, tmp_path, monkeypatch):
        # prefix = "arb-prd".upper()[:5] = "ARB-P" (hyphen at index 3).
        _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        ids = [_add("arb-prd", f"epic {i}") for i in range(3)]
        assert ids == ["ARB-P-000", "ARB-P-001", "ARB-P-002"], ids
        # The headline invariant: no two epics share an id.
        assert len(set(ids)) == 3

    def test_counts_done_and_progress_files(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        # A completed task in done/ and an in-progress task (.progress.md stem
        # is "ARB-P-003.progress") must both be counted for hyphenated prefixes.
        (tasks_dir / "done" / "ARB-P-005.md").write_text("---\nid: ARB-P-005\n---\n", encoding="utf-8")
        (tasks_dir / "ARB-P-003.progress.md").write_text("---\nid: ARB-P-003\n---\n", encoding="utf-8")
        assert _add("arb-prd", "next") == "ARB-P-006"

    def test_subtask_files_do_not_pollute_top_level(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch, "arb-prd")
        # A stray subtask-shaped file at the top level must NOT be read as
        # top-level number 1 (anchored pattern rejects the extra segment).
        (tasks_dir / "ARB-P-000-001.md").write_text("---\nid: ARB-P-000-001\n---\n", encoding="utf-8")
        assert _add("arb-prd", "first real") == "ARB-P-000"


class TestNonHyphenatedPrefixUnaffected:
    def test_plain_prefix_still_sequential(self, tmp_path, monkeypatch):
        # The common case (no hyphen in the first 5 chars) must keep working.
        _make_portfolio(tmp_path, monkeypatch, "test")
        ids = [_add("test", f"t{i}") for i in range(2)]
        assert ids == ["TEST-000", "TEST-001"], ids
