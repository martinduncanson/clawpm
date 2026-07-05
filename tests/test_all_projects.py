"""Tests for CLAWP-084 — cross-project ``tasks list --all-projects``.

Covers:
  1. Aggregation across every ACTIVE project, each row carrying ``project_id``.
  2. Same-numeric-id AND same-full-id tasks in different projects are NOT
     conflated (cross-project id-isolation class) — the row count and the
     per-row ``project_id`` stay correct even when two projects mint the
     identical task id.
  3. Composable filters (CLAWP-069/082) apply per-project under --all-projects.
  4. Default (no flag) is unchanged: single project, no ``project_id`` field.
  5. --all-projects and --project are mutually exclusive.
  6. Inactive projects are excluded; portfolio-priority ordering.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.tasks import add_task


def _make_portfolio(tmp_path: Path, monkeypatch, projects: list[dict]) -> SimpleNamespace:
    """Build a throwaway portfolio with N projects.

    Each entry in ``projects`` is a dict with keys: ``id``, ``name`` (optional),
    ``priority`` (optional, default 5), ``prefix`` (optional explicit
    task_prefix), ``status`` (optional, default "active").
    """
    root = tmp_path / "portfolio"
    root.mkdir()
    projects_dir = root / "projects"
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{projects_dir.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    projects_dir.mkdir()
    for spec in projects:
        pid = spec["id"]
        name = spec.get("name", pid.title())
        priority = spec.get("priority", 5)
        status = spec.get("status", "active")
        prefix = spec.get("prefix")
        project_dir = projects_dir / f"{pid}-project"
        meta = project_dir / ".project"
        meta.mkdir(parents=True)
        toml = (
            f'id = "{pid}"\nname = "{name}"\n'
            f'status = "{status}"\npriority = {priority}\n'
        )
        if prefix:
            toml += f'task_prefix = "{prefix}"\n'
        (meta / "settings.toml").write_text(toml, encoding="utf-8")
        for sub in ("progress", "done", "blocked"):
            (meta / "tasks" / sub).mkdir(parents=True)

    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", root.as_posix())
    config = load_portfolio_config(root)
    return SimpleNamespace(root=root, config=config)


def _env(port: SimpleNamespace) -> dict:
    return {"CLAWPM_PORTFOLIO": port.root.as_posix()}


class TestAllProjectsAggregation:
    def test_spans_every_active_project(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
            {"id": "beta", "priority": 5, "prefix": "BETA"},
        ])
        add_task(port.config, "alpha", "Alpha one")
        add_task(port.config, "beta", "Beta one")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects"], env=_env(port)
        )
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        titles = {row["title"] for row in rows}
        assert titles == {"Alpha one", "Beta one"}
        # Every row carries its owning project explicitly.
        by_title = {row["title"]: row["project_id"] for row in rows}
        assert by_title == {"Alpha one": "alpha", "Beta one": "beta"}

    def test_portfolio_priority_ordering(self, tmp_path, monkeypatch):
        # beta has the more urgent PROJECT priority, so its task sorts first
        # even though both tasks share the default task priority.
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 9, "prefix": "ALPHA"},
            {"id": "beta", "priority": 1, "prefix": "BETA"},
        ])
        add_task(port.config, "alpha", "Alpha one")
        add_task(port.config, "beta", "Beta one")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects"], env=_env(port)
        )
        assert r.exit_code == 0, r.output
        order = [row["project_id"] for row in json.loads(r.output)]
        assert order == ["beta", "alpha"]

    def test_inactive_project_excluded(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "prefix": "ALPHA"},
            {"id": "archived", "prefix": "ARCH", "status": "archived"},
        ])
        add_task(port.config, "alpha", "Alpha one")
        add_task(port.config, "archived", "Archived one")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects"], env=_env(port)
        )
        assert r.exit_code == 0, r.output
        titles = {row["title"] for row in json.loads(r.output)}
        assert titles == {"Alpha one"}


class TestSameIdNotConflated:
    def test_same_full_id_two_projects_not_conflated(self, tmp_path, monkeypatch):
        # Force BOTH projects onto the SAME explicit prefix so each mints the
        # IDENTICAL full id (SAME-001). Relying on id alone WOULD conflate them;
        # the explicit project_id must keep them distinct.
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "SAME"},
            {"id": "beta", "priority": 5, "prefix": "SAME"},
        ])
        a = add_task(port.config, "alpha", "Alpha task")
        b = add_task(port.config, "beta", "Beta task")
        # Sanity: both minted the literally identical full id.
        shared_id = a.id
        assert a.id == b.id and shared_id.startswith("SAME-")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects"], env=_env(port)
        )
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        # TWO rows despite the shared id — not collapsed to one.
        assert len(rows) == 2
        assert all(row["id"] == shared_id for row in rows)
        # Each row is disambiguated by project_id + title.
        scoped = {(row["project_id"], row["title"]) for row in rows}
        assert scoped == {("alpha", "Alpha task"), ("beta", "Beta task")}

    def test_same_full_id_text_mode_shows_both(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "SAME"},
            {"id": "beta", "priority": 5, "prefix": "SAME"},
        ])
        add_task(port.config, "alpha", "Alpha task")
        add_task(port.config, "beta", "Beta task")

        r = CliRunner().invoke(
            main, ["--format", "text", "tasks", "list", "--all-projects"],
            env=_env(port),
        )
        assert r.exit_code == 0, r.output
        # Both project scopes are rendered even though the id is identical.
        assert "alpha" in r.output and "beta" in r.output
        assert "Alpha task" in r.output and "Beta task" in r.output


class TestFiltersComposeAcrossProjects:
    def test_tag_filter_spans_projects(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
            {"id": "beta", "priority": 5, "prefix": "BETA"},
        ])
        add_task(port.config, "alpha", "Alpha urgent", tags=["urgent"])
        add_task(port.config, "alpha", "Alpha calm")
        add_task(port.config, "beta", "Beta urgent", tags=["urgent"])
        add_task(port.config, "beta", "Beta calm")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects", "--tag", "urgent"],
            env=_env(port),
        )
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        scoped = {(row["project_id"], row["title"]) for row in rows}
        assert scoped == {("alpha", "Alpha urgent"), ("beta", "Beta urgent")}

    def test_priority_filter_spans_projects(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
            {"id": "beta", "priority": 5, "prefix": "BETA"},
        ])
        add_task(port.config, "alpha", "Alpha hot", priority=1)
        add_task(port.config, "alpha", "Alpha cold", priority=9)
        add_task(port.config, "beta", "Beta hot", priority=2)
        add_task(port.config, "beta", "Beta cold", priority=8)

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects", "--priority", "<=3"],
            env=_env(port),
        )
        assert r.exit_code == 0, r.output
        titles = {row["title"] for row in json.loads(r.output)}
        assert titles == {"Alpha hot", "Beta hot"}

    def test_limit_applies_globally(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "priority": 1, "prefix": "ALPHA"},
            {"id": "beta", "priority": 5, "prefix": "BETA"},
        ])
        for i in range(3):
            add_task(port.config, "alpha", f"Alpha {i}")
            add_task(port.config, "beta", f"Beta {i}")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--all-projects", "--limit", "2"],
            env=_env(port),
        )
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        assert len(rows) == 2
        # Global sort puts the higher-priority PROJECT (alpha) first.
        assert all(row["project_id"] == "alpha" for row in rows)


class TestDefaultUnchanged:
    def test_single_project_has_no_project_id_field(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "prefix": "ALPHA"},
            {"id": "beta", "prefix": "BETA"},
        ])
        add_task(port.config, "alpha", "Alpha one")
        add_task(port.config, "beta", "Beta one")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--project", "alpha"], env=_env(port)
        )
        assert r.exit_code == 0, r.output
        rows = json.loads(r.output)
        titles = {row["title"] for row in rows}
        assert titles == {"Alpha one"}
        # No schema change without the flag: project_id key is absent.
        assert all("project_id" not in row for row in rows)


class TestMutualExclusion:
    def test_project_and_all_projects_conflict(self, tmp_path, monkeypatch):
        port = _make_portfolio(tmp_path, monkeypatch, [
            {"id": "alpha", "prefix": "ALPHA"},
        ])
        add_task(port.config, "alpha", "Alpha one")

        r = CliRunner().invoke(
            main, ["tasks", "list", "--project", "alpha", "--all-projects"],
            env=_env(port),
        )
        assert r.exit_code != 0
        assert "cannot be combined" in r.output
