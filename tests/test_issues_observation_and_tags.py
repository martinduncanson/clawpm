"""Tests for `clawpm issues add` observation type + --tag flag, and matching `issues list` filters."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main


@pytest.fixture
def temp_portfolio():
    temp_dir = tempfile.mkdtemp(prefix="clawpm_issues_test_")
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
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "alpha"\nname = "Alpha"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    (project_meta / "tasks").mkdir()
    (project_meta / "tasks" / "done").mkdir()
    (project_meta / "tasks" / "blocked").mkdir()

    (portfolio_root / "work_log.jsonl").touch()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    yield {"root": portfolio_root, "project_dir": project_dir}

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


class TestObservationType:
    def test_observation_type_accepted(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "issues", "add",
                "--project", "alpha",
                "--type", "observation",
                "--severity", "low",
                "--summary", "depth>2 subagent nesting observed",
                "--tag", "depth-warning",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["entry"]["type"] == "observation"
        assert data["entry"]["tags"] == ["depth-warning"]
        assert data["entry"]["summary"] == "depth>2 subagent nesting observed"

    def test_doctrine_form_works_verbatim(self, temp_portfolio):
        """Global CLAUDE.md doctrine form: --type observation --severity low --tag depth-warning."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "issues", "add",
                "--project", "alpha",
                "--type", "observation",
                "--severity", "low",
                "--tag", "depth-warning",
                "--summary", "Depth>2 subagent nesting smell",
            ],
        )
        assert result.exit_code == 0, result.output

    def test_multiple_tags(self, temp_portfolio):
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "issues", "add",
                "--project", "alpha",
                "--type", "observation",
                "--tag", "ergonomic",
                "--tag", "dogfood",
                "--summary", "doctor --project missing",
            ],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert sorted(data["entry"]["tags"]) == ["dogfood", "ergonomic"]

    def test_tags_default_empty_list(self, temp_portfolio):
        """Entries without --tag still have tags: [] in the persisted form."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["issues", "add", "--project", "alpha", "--summary", "bare entry"],
        )
        assert result.exit_code == 0, result.output
        # Verify persisted form
        issues_file = temp_portfolio["project_dir"] / ".agent" / "issues.jsonl"
        line = issues_file.read_text(encoding="utf-8").strip().splitlines()[-1]
        entry = json.loads(line)
        assert entry["tags"] == []

    def test_bug_type_still_works(self, temp_portfolio):
        """Backward compatibility: existing types still accepted."""
        runner = CliRunner()
        for t in ["bug", "ux", "docs", "feature"]:
            result = runner.invoke(
                main,
                ["issues", "add", "--project", "alpha", "--type", t, "--summary", f"a {t}"],
            )
            assert result.exit_code == 0, f"type={t}: {result.output}"


class TestListFilters:
    def _seed(self, runner, temp_portfolio):
        runner.invoke(main, ["issues", "add", "--project", "alpha", "--type", "bug", "--summary", "real bug"])
        runner.invoke(main, ["issues", "add", "--project", "alpha", "--type", "observation", "--tag", "ergonomic", "--summary", "doctor gap"])
        runner.invoke(main, ["issues", "add", "--project", "alpha", "--type", "observation", "--tag", "depth-warning", "--summary", "depth>2"])
        runner.invoke(main, ["issues", "add", "--project", "alpha", "--type", "observation", "--tag", "ergonomic", "--tag", "depth-warning", "--summary", "both tags"])

    def test_filter_by_tag(self, temp_portfolio):
        runner = CliRunner()
        self._seed(runner, temp_portfolio)
        result = runner.invoke(main, ["issues", "list", "--project", "alpha", "--tag", "depth-warning"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 2
        for issue in data["issues"]:
            assert "depth-warning" in issue["tags"]

    def test_filter_by_type_observation(self, temp_portfolio):
        runner = CliRunner()
        self._seed(runner, temp_portfolio)
        result = runner.invoke(main, ["issues", "list", "--project", "alpha", "--type", "observation"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 3
        assert all(i["type"] == "observation" for i in data["issues"])

    def test_filter_by_type_and_tag_combined(self, temp_portfolio):
        runner = CliRunner()
        self._seed(runner, temp_portfolio)
        result = runner.invoke(
            main,
            ["issues", "list", "--project", "alpha", "--type", "observation", "--tag", "ergonomic"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 2
        for issue in data["issues"]:
            assert issue["type"] == "observation"
            assert "ergonomic" in issue["tags"]

    def test_multiple_tag_filters_are_or_not_and(self, temp_portfolio):
        """--tag a --tag b returns entries matching ANY of a or b."""
        runner = CliRunner()
        self._seed(runner, temp_portfolio)
        result = runner.invoke(
            main,
            ["issues", "list", "--project", "alpha", "--tag", "ergonomic", "--tag", "depth-warning"],
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 3

    def test_legacy_entries_without_tags_load(self, temp_portfolio):
        """Pre-tag-flag entries (no tags field) still load and list."""
        issues_file = temp_portfolio["project_dir"] / ".agent" / "issues.jsonl"
        issues_file.parent.mkdir(exist_ok=True)
        legacy = {"ts": "2026-05-01T00:00:00Z", "type": "bug", "severity": "low", "actual": "old entry", "fixed": False}
        issues_file.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(main, ["issues", "list", "--project", "alpha"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["issues"][0]["type"] == "bug"

    def test_legacy_entries_excluded_by_tag_filter(self, temp_portfolio):
        """Entries without tags must NOT match a --tag filter."""
        issues_file = temp_portfolio["project_dir"] / ".agent" / "issues.jsonl"
        issues_file.parent.mkdir(exist_ok=True)
        legacy = {"ts": "2026-05-01T00:00:00Z", "type": "bug", "severity": "low", "actual": "old entry", "fixed": False}
        issues_file.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

        runner = CliRunner()
        runner.invoke(main, ["issues", "add", "--project", "alpha", "--type", "observation", "--tag", "ergonomic", "--summary", "new"])
        result = runner.invoke(main, ["issues", "list", "--project", "alpha", "--tag", "ergonomic"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["count"] == 1
        assert data["issues"][0]["summary"] == "new"
