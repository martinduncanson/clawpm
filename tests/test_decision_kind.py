"""Tests for CLAWP-111-001 — a task can be a decision.

Covers the four success criteria:
  1. kind: Literal["build", "decision"], default "build", omitted from
     frontmatter when at the default so existing task files round-trip
     byte-for-byte.
  2. `done` on a kind: decision task without --resolution errors with
     decision_needs_resolution and leaves the task open.
  3. `done --resolution "..."` on a decision leaf with a parent sets
     resolution/resolved_at in the child's frontmatter AND appends one line
     to the parent's body under "## Decisions so far".
  4. A closed decision task's task_done reflection event carries
     kind: decision.
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
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import add_subtask, add_task, edit_task, get_task


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_portfolio():
    """Portfolio with a single project "test-proj" (no git repo needed)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_decision_test_")
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
    proj_dir = projects_dir / "test-proj"
    proj_dir.mkdir()
    dot_proj = proj_dir / ".project"
    dot_proj.mkdir()
    (dot_proj / "settings.toml").write_text(
        'id = "test-proj"\nname = "Test Project"\nstatus = "active"\n',
        encoding="utf-8",
    )
    tasks_dir = dot_proj / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()
    (portfolio_root / "work_log.jsonl").touch()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)

    yield {"root": portfolio_root, "config": config}

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1. kind field — default, persistence, omit-when-default round trip
# ---------------------------------------------------------------------------


class TestKindField:
    def test_task_defaults_kind_to_build(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Plain task")
        assert task is not None
        assert task.kind == "build"
        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.kind == "build"

    def test_add_task_persists_kind_decision(self, temp_portfolio):
        """A decision task's kind survives a load/save round trip."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Ship v2 or not?", kind="decision")
        assert task is not None
        assert task.kind == "decision"
        raw = task.file_path.read_text(encoding="utf-8")
        assert "kind: decision" in raw

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.kind == "decision"

    def test_build_task_frontmatter_omits_kind_key(self, temp_portfolio):
        """A "build" (default) task never gets a spurious kind: key on disk."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Plain task 2")
        raw = task.file_path.read_text(encoding="utf-8")
        assert "kind" not in raw

    def test_existing_fixture_without_kind_stays_omitted_after_unrelated_edit(
        self, temp_portfolio
    ):
        """An existing (pre-CLAWP-111-shaped) task file never gains a kind:
        key from an edit that doesn't touch it — the "no frontmatter diff on
        existing fixtures" contract."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Legacy-shaped task")
        raw_before = task.file_path.read_text(encoding="utf-8")
        assert "kind" not in raw_before

        edited = edit_task(config, "test-proj", task.id, priority=9)
        assert edited is not None
        assert edited.kind == "build"
        raw_after = edited.file_path.read_text(encoding="utf-8")
        assert "kind" not in raw_after

    def test_decision_kind_persists_through_unrelated_edit(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Decision task", kind="decision")
        edited = edit_task(config, "test-proj", task.id, priority=2)
        assert edited is not None
        assert edited.kind == "decision"

    def test_edit_task_kind_replace_semantics(self, temp_portfolio):
        """--kind build on a decision task pops the key (mirrors delegability)."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Reclassify me", kind="decision")
        edited = edit_task(config, "test-proj", task.id, kind="build")
        assert edited is not None
        assert edited.kind == "build"
        raw = edited.file_path.read_text(encoding="utf-8")
        assert "kind" not in raw


# ---------------------------------------------------------------------------
# 2. done requires --resolution on a decision task
# ---------------------------------------------------------------------------


class TestDoneRequiresResolution:
    def test_done_without_resolution_errors_and_stays_open(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Undecided", kind="decision")

        runner = CliRunner()
        result = runner.invoke(
            main, ["-p", "test-proj", "done", task.id],
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["error"] == "decision_needs_resolution"

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.state == TaskState.OPEN
        assert reloaded.resolution is None

    def test_done_with_blank_resolution_errors(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Undecided 2", kind="decision")

        runner = CliRunner()
        result = runner.invoke(
            main, ["-p", "test-proj", "done", task.id, "--resolution", "   "],
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["error"] == "decision_needs_resolution"

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.state == TaskState.OPEN

    def test_build_task_done_without_resolution_still_succeeds(self, temp_portfolio):
        """Regression: the guard must not broaden to ordinary build tasks."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Ordinary work")

        runner = CliRunner()
        result = runner.invoke(main, ["-p", "test-proj", "done", task.id])
        assert result.exit_code == 0, result.output

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.state == TaskState.DONE

    def test_tasks_state_done_without_resolution_also_guarded(self, temp_portfolio):
        """The guard covers BOTH done entry points (shortcuts.done AND
        tasks state), not just one — this was the task's own pre-mortem."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Undecided via tasks state", kind="decision")

        runner = CliRunner()
        result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "state", task.id, "done"],
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["error"] == "decision_needs_resolution"

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.state == TaskState.OPEN


# ---------------------------------------------------------------------------
# 3. done --resolution closes a decision and updates the parent
# ---------------------------------------------------------------------------


class TestDoneWithResolution:
    def test_resolution_sets_frontmatter_on_the_decision_task(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Standalone decision", kind="decision")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-p", "test-proj", "done", task.id, "--resolution", "We go with option B."],
        )
        assert result.exit_code == 0, result.output

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded is not None
        assert reloaded.state == TaskState.DONE
        assert reloaded.resolution == "We go with option B."
        assert reloaded.resolved_at is not None

    def test_resolution_appends_to_parent_decisions_so_far(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test-proj", "Parent epic")
        assert parent is not None
        child = add_subtask(
            config, "test-proj", parent.id, "Pick a database", kind="decision",
        )
        assert child is not None
        assert child.kind == "decision"

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-p", "test-proj", "done", child.id,
                "--resolution", "Postgres, for the JSONB support.",
            ],
        )
        assert result.exit_code == 0, result.output

        parent_reloaded = get_task(config, "test-proj", parent.id)
        assert parent_reloaded is not None
        body = parent_reloaded.file_path.read_text(encoding="utf-8")
        assert "## Decisions so far" in body
        expected_line = (
            f"- [Pick a database]({child.id}): Postgres, for the JSONB support."
        )
        assert expected_line in body
        # Exactly one line was appended for this decision.
        assert body.count(expected_line) == 1

    def test_resolution_only_first_line_appears_in_parent(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test-proj", "Parent epic 2")
        child = add_subtask(
            config, "test-proj", parent.id, "Pick a cloud", kind="decision",
        )
        multi_line_resolution = "AWS.\nRationale: existing Terraform footprint."

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-p", "test-proj", "done", child.id, "--resolution", multi_line_resolution],
        )
        assert result.exit_code == 0, result.output

        parent_reloaded = get_task(config, "test-proj", parent.id)
        body = parent_reloaded.file_path.read_text(encoding="utf-8")
        assert f"- [Pick a cloud]({child.id}): AWS." in body
        assert "Rationale: existing Terraform footprint." not in body

    def test_two_decisions_under_one_parent_share_one_heading(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test-proj", "Parent epic 3")
        child_a = add_subtask(
            config, "test-proj", parent.id, "Pick a queue", kind="decision",
        )
        child_b = add_subtask(
            config, "test-proj", parent.id, "Pick a cache", kind="decision",
        )

        runner = CliRunner()
        for child, resolution in (
            (child_a, "Kafka."), (child_b, "Redis."),
        ):
            result = runner.invoke(
                main,
                ["-p", "test-proj", "done", child.id, "--resolution", resolution],
            )
            assert result.exit_code == 0, result.output

        parent_reloaded = get_task(config, "test-proj", parent.id)
        body = parent_reloaded.file_path.read_text(encoding="utf-8")
        assert body.count("## Decisions so far") == 1
        assert f"- [Pick a queue]({child_a.id}): Kafka." in body
        assert f"- [Pick a cache]({child_b.id}): Redis." in body

    def test_decision_without_parent_does_not_error(self, temp_portfolio):
        """A parentless decision task closes cleanly (no parent to update)."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Orphan decision", kind="decision")
        runner = CliRunner()
        result = runner.invoke(
            main, ["-p", "test-proj", "done", task.id, "--resolution", "Yes."],
        )
        assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# 4. task_done reflection event carries kind: decision
# ---------------------------------------------------------------------------


class TestReflectionEventKindTag:
    def test_task_done_reflection_event_carries_kind_decision(self, temp_portfolio):
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test-proj", "Reflected decision", kind="decision")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-p", "test-proj", "done", task.id, "--resolution", "Go."],
        )
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert record["event"] == "task_done"
        assert record["kind"] == "decision"

    def test_build_task_reflection_event_has_no_kind_key(self, temp_portfolio):
        """Regression: an ordinary task's reflection event keeps its current
        shape — no spurious kind key."""
        config = temp_portfolio["config"]
        portfolio_root = temp_portfolio["root"]
        task = add_task(config, "test-proj", "Reflected build task")

        runner = CliRunner()
        result = runner.invoke(main, ["-p", "test-proj", "done", task.id])
        assert result.exit_code == 0, result.output

        ref_file = portfolio_root / "reflections" / f"{task.id}.jsonl"
        assert ref_file.exists()
        record = json.loads(ref_file.read_text(encoding="utf-8").strip())
        assert record["event"] == "task_done"
        assert "kind" not in record
