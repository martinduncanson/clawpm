"""Tests for the Semble content-shape advisory (CLAWP-036).

Covers the semble.py census helpers and the doctor advisory wiring,
including the load-bearing case: a repo with substantial code AND docs
must surface BOTH the codegraph and semble advisories (they're
independent, neither suppresses the other).
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm import semble as sb
from clawpm.cli import main
from clawpm.discovery import load_portfolio_config


@pytest.fixture
def temp_portfolio_with_repo():
    """Portfolio with one active project pointing at a real git repo."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_semble_test_")
    portfolio_root = Path(temp_dir)
    repo_dir = portfolio_root / "repo"
    repo_dir.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo_dir)], check=True)
    (repo_dir / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "add", "README.md"], check=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a",
         "-C", str(repo_dir), "commit", "-q", "-m", "init"], check=True,
    )

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{portfolio_root.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    project_meta = repo_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo_dir.as_posix()}"\n'
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)
    yield {"root": portfolio_root, "repo_dir": repo_dir, "config": config}
    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    import shutil
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# semble.py census helpers
# ---------------------------------------------------------------------------


class TestDocCensus:
    def test_count_doc_files_counts_prose(self, tmp_path):
        for i in range(5):
            (tmp_path / f"doc{i}.md").write_text("x", encoding="utf-8")
        (tmp_path / "guide.rst").write_text("x", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
        assert sb.count_doc_files(tmp_path) == 7

    def test_count_doc_files_ignores_code(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        for i in range(5):
            (tmp_path / f"mod{i}.py").write_text("x", encoding="utf-8")
        assert sb.count_doc_files(tmp_path) == 1

    def test_count_doc_files_skips_vendored_trees(self, tmp_path):
        (tmp_path / "real.md").write_text("x", encoding="utf-8")
        nm = tmp_path / "node_modules" / "pkg"
        nm.mkdir(parents=True)
        (nm / "bundled.md").write_text("x", encoding="utf-8")
        assert sb.count_doc_files(tmp_path) == 1

    def test_count_doc_files_empty_for_missing_dir(self, tmp_path):
        assert sb.count_doc_files(tmp_path / "nope") == 0

    def test_is_doc_indexed_false_for_empty(self, tmp_path):
        assert sb.is_doc_indexed(tmp_path) is False

    def test_is_doc_indexed_true_when_marker_present(self, tmp_path):
        (tmp_path / sb.SEMBLE_INDEX_NAME).mkdir()
        assert sb.is_doc_indexed(tmp_path) is True


# ---------------------------------------------------------------------------
# doctor advisory wiring
# ---------------------------------------------------------------------------


def _seed_docs(repo_dir: Path, n: int) -> None:
    docs = repo_dir / "docs"
    docs.mkdir(exist_ok=True)
    for i in range(n):
        (docs / f"page{i}.md").write_text("content", encoding="utf-8")


def _seed_code(repo_dir: Path, n: int) -> None:
    src = repo_dir / "src"
    src.mkdir(exist_ok=True)
    for i in range(n):
        (src / f"file{i}.py").write_text("x", encoding="utf-8")


class TestDoctorSembleAdvisory:
    def test_advisory_for_doc_heavy_project(self, temp_portfolio_with_repo):
        _seed_docs(temp_portfolio_with_repo["repo_dir"], 35)  # above 30
        r = CliRunner().invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert "semble_advice" in payload
        assert any(a["project_id"] == "test" for a in payload["semble_advice"])

    def test_no_advisory_below_threshold(self, temp_portfolio_with_repo):
        _seed_docs(temp_portfolio_with_repo["repo_dir"], 10)  # below 30
        r = CliRunner().invoke(main, ["doctor"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert not any(
            a["project_id"] == "test" for a in payload.get("semble_advice", [])
        )

    def test_no_advisory_when_already_indexed(self, temp_portfolio_with_repo):
        repo_dir = temp_portfolio_with_repo["repo_dir"]
        _seed_docs(repo_dir, 35)
        (repo_dir / sb.SEMBLE_INDEX_NAME).mkdir()
        r = CliRunner().invoke(main, ["doctor"])
        assert r.exit_code == 0
        payload = json.loads(r.output)
        assert not any(
            a["project_id"] == "test" for a in payload.get("semble_advice", [])
        )

    def test_code_and_docs_get_both_advisories(self, temp_portfolio_with_repo):
        """Load-bearing: a mixed repo (lots of code AND docs) must surface
        BOTH codegraph and semble advice — they're independent."""
        repo_dir = temp_portfolio_with_repo["repo_dir"]
        _seed_code(repo_dir, 60)   # above codegraph threshold (50)
        _seed_docs(repo_dir, 35)   # above semble threshold (30)
        r = CliRunner().invoke(main, ["doctor"])
        assert r.exit_code == 0, r.output
        payload = json.loads(r.output)
        assert any(
            a["project_id"] == "test" for a in payload.get("codegraph_advice", [])
        ), "codegraph advisory missing on mixed repo"
        assert any(
            a["project_id"] == "test" for a in payload.get("semble_advice", [])
        ), "semble advisory missing on mixed repo"

    def test_text_mode_surfaces_advisory(self, temp_portfolio_with_repo):
        _seed_docs(temp_portfolio_with_repo["repo_dir"], 35)
        r = CliRunner().invoke(main, ["-f", "text", "doctor"])
        assert r.exit_code == 0, r.output
        assert "No issues found" not in r.output
        assert "[ADVICE]" in r.output
        assert "semble" in r.output
