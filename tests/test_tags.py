"""Tests for CLAWP-069 — cross-cutting workstream tags.

Covers:
  1. normalize_tags — lowercase / strip / dedupe / drop-blanks / defensive types
  2. Task schema + persistence: add_task / add_subtask / edit_task round-trips,
     to_dict exposure, legacy-file backward-compat.
  3. Filter semantics (filters.by_tags / apply_filters): OR (default), AND
     (--all-tags), empty-set matches nothing, case-insensitive, composability.
  4. CLI: tasks add/edit --tag, --clear-tags, tasks list --tag[/--all-tags],
     tasks tags discovery command, and text rendering in list + detail.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.filters import apply_filters, by_tags
from clawpm.models import Task, TaskState, normalize_tags
from clawpm.tasks import (
    add_subtask,
    add_task,
    distinct_tags,
    edit_task,
    get_task,
    list_tasks,
)


@pytest.fixture
def temp_portfolio():
    """Minimal portfolio with one project."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_tags_test_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n'
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    proj_dir = projects_dir / "test-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "test-proj"\n'
        'name = "Test Project"\n'
        f'repo_path = "{proj_dir.as_posix()}"\n'
    )
    yield portfolio_root


def _mktask(state: TaskState = TaskState.OPEN, tags=None, **kw) -> Task:
    return Task(
        id=kw.pop("id", "T-1"),
        title=kw.pop("title", "t"),
        state=state,
        tags=tags or [],
        **kw,
    )


# ---------------------------------------------------------------------------
# 1. normalize_tags
# ---------------------------------------------------------------------------

class TestNormalizeTags:
    def test_lowercases_and_strips(self):
        assert normalize_tags(["  Concurrency ", "MCP"]) == ["concurrency", "mcp"]

    def test_dedupes_preserving_order(self):
        assert normalize_tags(["a", "b", "A", "b"]) == ["a", "b"]

    def test_drops_blanks(self):
        assert normalize_tags(["", "  ", "x"]) == ["x"]

    def test_none_yields_empty(self):
        assert normalize_tags(None) == []

    def test_scalar_string_promoted(self):
        assert normalize_tags("q3-roadmap") == ["q3-roadmap"]

    def test_non_str_items_ignored(self):
        assert normalize_tags(["ok", 5, None, {"x": 1}]) == ["ok"]

    def test_non_iterable_yields_empty(self):
        assert normalize_tags(42) == []


# ---------------------------------------------------------------------------
# 2. Schema + persistence
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_task_defaults_tags_to_empty(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "No tags")
        assert task is not None
        assert task.tags == []

    def test_add_task_persists_tags_roundtrip(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Tagged task",
            tags=["concurrency", "mcp"],
        )
        assert task is not None
        assert task.tags == ["concurrency", "mcp"]
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.tags == ["concurrency", "mcp"]

    def test_add_task_normalizes_on_write(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(
            config, "test-proj", "Messy tags",
            tags=["  Concurrency", "MCP", "concurrency", ""],
        )
        assert task is not None
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.tags == ["concurrency", "mcp"]

    def test_to_dict_includes_tags(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Dict task", tags=["infra"])
        d = task.to_dict()
        assert d["tags"] == ["infra"]

    def test_edit_task_replaces_tags(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Editable", tags=["old"])
        updated = edit_task(config, "test-proj", task.id, tags=["new", "shiny"])
        assert updated is not None
        assert updated.tags == ["new", "shiny"]
        assert get_task(config, "test-proj", task.id).tags == ["new", "shiny"]

    def test_edit_clear_tags_removes(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Clearable", tags=["a", "b"])
        updated = edit_task(config, "test-proj", task.id, clear_tags=True)
        assert updated is not None
        assert updated.tags == []
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.tags == []
        # Field is gone from frontmatter, not just empty.
        assert "tags:" not in reloaded.file_path.read_text(encoding="utf-8")

    def test_edit_without_tags_flag_preserves(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Keep tags", tags=["keep"])
        # Editing an unrelated field must not touch tags.
        updated = edit_task(config, "test-proj", task.id, priority=2)
        assert updated.tags == ["keep"]

    def test_edit_blank_tag_is_noop_not_clear(self, temp_portfolio):
        """`--tag ""` (normalises to empty) must NOT wipe existing tags — only
        --clear-tags clears (Codex + Grok review)."""
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Keep on blank", tags=["keep"])
        updated = edit_task(config, "test-proj", task.id, tags=[""])
        assert updated is not None
        assert updated.tags == ["keep"]
        assert get_task(config, "test-proj", task.id).tags == ["keep"]

    def test_add_subtask_persists_tags(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        parent = add_task(config, "test-proj", "Parent")
        child = add_subtask(
            config, "test-proj", parent.id, "Child", tags=["mcp"],
        )
        assert child is not None
        assert child.tags == ["mcp"]
        assert get_task(config, "test-proj", child.id).tags == ["mcp"]

    def test_legacy_file_without_tags_loads(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        task = add_task(config, "test-proj", "Legacy")
        # Simulate an old file that predates the tags field.
        text = task.file_path.read_text(encoding="utf-8")
        assert "tags:" not in text
        reloaded = Task.from_file(task.file_path)
        assert reloaded.tags == []


# ---------------------------------------------------------------------------
# 3. Filter semantics
# ---------------------------------------------------------------------------

class TestFilterSemantics:
    def _tasks(self):
        return [
            _mktask(id="A", tags=["concurrency", "mcp"]),
            _mktask(id="B", tags=["mcp"]),
            _mktask(id="C", tags=["q3-roadmap"]),
            _mktask(id="D", tags=[]),
        ]

    def test_or_matches_any(self):
        got = apply_filters(self._tasks(), [by_tags(["mcp", "q3-roadmap"])])
        assert {t.id for t in got} == {"A", "B", "C"}

    def test_single_tag(self):
        got = apply_filters(self._tasks(), [by_tags(["concurrency"])])
        assert {t.id for t in got} == {"A"}

    def test_all_tags_and(self):
        got = apply_filters(
            self._tasks(), [by_tags(["concurrency", "mcp"], match_all=True)]
        )
        assert {t.id for t in got} == {"A"}

    def test_all_tags_none_match(self):
        got = apply_filters(
            self._tasks(), [by_tags(["mcp", "q3-roadmap"], match_all=True)]
        )
        assert got == []

    def test_case_insensitive_filter(self):
        got = apply_filters(self._tasks(), [by_tags(["Concurrency", "MCP"])])
        assert {t.id for t in got} == {"A", "B"}

    def test_empty_tag_set_matches_nothing(self):
        got = apply_filters(self._tasks(), [by_tags([])])
        assert got == []

    def test_no_filters_passthrough(self):
        tasks = self._tasks()
        assert apply_filters(tasks, []) == tasks

    def test_filters_compose_with_and(self):
        # Two tag filters AND together: a task must satisfy both.
        got = apply_filters(
            self._tasks(), [by_tags(["mcp"]), by_tags(["concurrency"])]
        )
        assert {t.id for t in got} == {"A"}


# ---------------------------------------------------------------------------
# 4. CLI
# ---------------------------------------------------------------------------

class TestCli:
    def _add(self, runner, temp_portfolio, title, *tags):
        args = ["tasks", "add", "--project", "test-proj", "--title", title]
        for t in tags:
            args += ["--tag", t]
        r = runner.invoke(main, args, env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        return json.loads(r.output)["data"]

    def test_cli_add_tag_persists(self, temp_portfolio):
        runner = CliRunner()
        data = self._add(runner, temp_portfolio, "CLI tagged", "concurrency", "mcp")
        assert data["tags"] == ["concurrency", "mcp"]

    def test_cli_edit_tag_replaces(self, temp_portfolio):
        runner = CliRunner()
        data = self._add(runner, temp_portfolio, "Editable", "old")
        r = runner.invoke(main, [
            "tasks", "edit", data["id"], "--project", "test-proj",
            "--tag", "new",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["tags"] == ["new"]

    def test_cli_clear_tags(self, temp_portfolio):
        runner = CliRunner()
        data = self._add(runner, temp_portfolio, "Clearable", "a", "b")
        r = runner.invoke(main, [
            "tasks", "edit", data["id"], "--project", "test-proj", "--clear-tags",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        assert json.loads(r.output)["data"]["tags"] == []

    def test_cli_tag_and_clear_conflict(self, temp_portfolio):
        runner = CliRunner()
        data = self._add(runner, temp_portfolio, "Conflict", "a")
        r = runner.invoke(main, [
            "tasks", "edit", data["id"], "--project", "test-proj",
            "--tag", "x", "--clear-tags",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 1
        assert "conflicting_flags" in r.output

    def test_cli_list_filter_or(self, temp_portfolio):
        runner = CliRunner()
        self._add(runner, temp_portfolio, "A", "concurrency", "mcp")
        self._add(runner, temp_portfolio, "B", "mcp")
        self._add(runner, temp_portfolio, "C", "q3-roadmap")
        r = runner.invoke(main, [
            "tasks", "list", "--project", "test-proj", "--tag", "mcp",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"A", "B"}

    def test_cli_list_filter_all_tags_and(self, temp_portfolio):
        runner = CliRunner()
        self._add(runner, temp_portfolio, "A", "concurrency", "mcp")
        self._add(runner, temp_portfolio, "B", "mcp")
        r = runner.invoke(main, [
            "tasks", "list", "--project", "test-proj",
            "--tag", "concurrency", "--tag", "mcp", "--all-tags",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        titles = {t["title"] for t in json.loads(r.output)}
        assert titles == {"A"}

    def test_cli_tags_command_counts(self, temp_portfolio):
        runner = CliRunner()
        self._add(runner, temp_portfolio, "A", "concurrency", "mcp")
        self._add(runner, temp_portfolio, "B", "mcp")
        self._add(runner, temp_portfolio, "C", "q3-roadmap")
        r = runner.invoke(main, [
            "tasks", "tags", "--project", "test-proj",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        pairs = {d["tag"]: d["count"] for d in json.loads(r.output)}
        assert pairs == {"mcp": 2, "concurrency": 1, "q3-roadmap": 1}
        # mcp (count 2) sorts first.
        assert json.loads(r.output)[0]["tag"] == "mcp"

    def test_cli_list_text_renders_tags(self, temp_portfolio):
        runner = CliRunner()
        self._add(runner, temp_portfolio, "Tagged", "concurrency")
        r = runner.invoke(main, [
            "-f", "text", "tasks", "list", "--project", "test-proj",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        assert "#concurrency" in r.output

    def test_cli_show_text_renders_tags(self, temp_portfolio):
        runner = CliRunner()
        data = self._add(runner, temp_portfolio, "Detailed", "infra")
        r = runner.invoke(main, [
            "-f", "text", "tasks", "show", data["id"], "--project", "test-proj",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        assert "infra" in r.output

    def test_distinct_tags_excludes_done_when_asked(self, temp_portfolio):
        config = load_portfolio_config(temp_portfolio)
        t = add_task(config, "test-proj", "Done one", tags=["shipped"])
        add_task(config, "test-proj", "Open one", tags=["active"])
        from clawpm.tasks import change_task_state
        change_task_state(config, "test-proj", t.id, TaskState.DONE)
        with_done = dict(distinct_tags(config, "test-proj", include_done=True))
        without_done = dict(distinct_tags(config, "test-proj", include_done=False))
        assert "shipped" in with_done
        assert "shipped" not in without_done
        assert "active" in without_done

    def test_distinct_tags_includes_rejected(self, temp_portfolio):
        """Tags on rejected (won't-do) tasks are part of the tag universe and
        must surface in discovery (Codex P2 + Grok)."""
        config = load_portfolio_config(temp_portfolio)
        t = add_task(config, "test-proj", "Wont do", tags=["dropped"])
        from clawpm.tasks import change_task_state
        change_task_state(
            config, "test-proj", t.id, TaskState.REJECTED, rationale="nope",
        )
        with_terminal = dict(distinct_tags(config, "test-proj", include_done=True))
        active_only = dict(distinct_tags(config, "test-proj", include_done=False))
        assert with_terminal.get("dropped") == 1
        assert "dropped" not in active_only

    def test_list_text_escapes_markup_in_tags(self, temp_portfolio):
        """A tag containing Rich markup metacharacters must not break or
        restyle text rendering (Codex P3)."""
        runner = CliRunner()
        config = load_portfolio_config(temp_portfolio)
        # normalize_tags lowercases but preserves brackets/slashes.
        add_task(config, "test-proj", "Markup tag", tags=["[ops]"])
        r = runner.invoke(main, [
            "-f", "text", "tasks", "list", "--project", "test-proj",
        ], env={"CLAWPM_PORTFOLIO": str(temp_portfolio)})
        assert r.exit_code == 0, r.output
        # The literal bracketed tag survives instead of being swallowed as markup.
        assert "[ops]" in r.output
