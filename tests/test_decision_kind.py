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
from clawpm.tasks import (
    _append_decision_to_parent,
    add_subtask,
    add_task,
    change_task_state,
    edit_task,
    get_task,
)


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

    def test_closing_same_decision_twice_does_not_duplicate_parent_line(
        self, temp_portfolio
    ):
        """MUST-FIX (review): a retry or a plain re-run of `done --resolution`
        on an already-closed decision must not duplicate its line under the
        parent's Decisions-so-far heading — the second run REPLACES it."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test-proj", "Parent epic 4")
        child = add_subtask(
            config, "test-proj", parent.id, "Pick a message bus", kind="decision",
        )

        runner = CliRunner()
        r1 = runner.invoke(
            main, ["-p", "test-proj", "done", child.id, "--resolution", "RabbitMQ."],
        )
        assert r1.exit_code == 0, r1.output

        r2 = runner.invoke(
            main,
            ["-p", "test-proj", "done", child.id, "--resolution", "Actually, Kafka."],
        )
        assert r2.exit_code == 0, r2.output

        parent_reloaded = get_task(config, "test-proj", parent.id)
        body = parent_reloaded.file_path.read_text(encoding="utf-8")
        assert body.count(f"({child.id}):") == 1
        assert f"- [Pick a message bus]({child.id}): Actually, Kafka." in body
        assert "RabbitMQ." not in body

        reloaded_child = get_task(config, "test-proj", child.id)
        assert reloaded_child.resolution == "Actually, Kafka."

    def test_directory_form_decision_task_closes_and_updates_parent(
        self, temp_portfolio
    ):
        """The directory-task (_task.md) branch of change_task_state's
        decision handling — a decision task that itself has a child, so it
        splits into a directory. The decision is TOP-LEVEL (not itself a
        subtask) with its ``parent`` link set directly in frontmatter: a
        subtask that both takes children AND is itself nested under a
        parent hits a PRE-EXISTING, unrelated id-resolution gap in
        ``_candidate_task_paths`` (it only resolves one level of
        parent-prefixed nesting, not a subtask-of-a-subtask's own children) —
        out of scope for CLAWP-111-001; flagged separately."""
        config = temp_portfolio["config"]
        decision = add_task(config, "test-proj", "Top-level decision", kind="decision")
        real_parent = add_task(config, "test-proj", "Real parent for decisions")

        # Link decision -> real_parent by hand (add_task has no --parent flag;
        # only add_subtask sets it, and that would force physical nesting).
        text = decision.file_path.read_text(encoding="utf-8")
        text = text.replace("---\n", f"---\nparent: {real_parent.id}\n", 1)
        decision.file_path.write_text(text, encoding="utf-8")

        # Give the decision task its OWN child — this forces it into
        # directory (_task.md) form, exercising the other branch of
        # change_task_state's (d2) decision handling.
        grandchild = add_subtask(config, "test-proj", decision.id, "Sub-consideration")
        assert grandchild is not None

        runner = CliRunner()
        r0 = runner.invoke(main, ["-p", "test-proj", "done", grandchild.id])
        assert r0.exit_code == 0, r0.output

        mid = get_task(config, "test-proj", decision.id)
        assert mid.file_path.name == "_task.md"  # confirm directory form
        assert mid.parent == real_parent.id

        r1 = runner.invoke(
            main, ["-p", "test-proj", "done", decision.id, "--resolution", "Go big."],
        )
        assert r1.exit_code == 0, r1.output

        reloaded_decision = get_task(config, "test-proj", decision.id)
        assert reloaded_decision.state == TaskState.DONE
        assert reloaded_decision.resolution == "Go big."

        parent_reloaded = get_task(config, "test-proj", real_parent.id)
        body = parent_reloaded.file_path.read_text(encoding="utf-8")
        assert f"- [Top-level decision]({decision.id}): Go big." in body

    def test_child_closes_when_parent_file_deleted(self, temp_portfolio):
        """Vanished-parent lenient path, end to end: deleting the parent
        entirely before closing the child must not block the child's own
        (already durable) completion."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test-proj", "Parent about to vanish")
        child = add_subtask(
            config, "test-proj", parent.id, "Orphan-to-be decision", kind="decision",
        )
        parent_reloaded = get_task(config, "test-proj", parent.id)
        assert parent_reloaded is not None
        parent_reloaded.file_path.unlink()

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["-p", "test-proj", "done", child.id, "--resolution", "Proceed anyway."],
        )
        assert result.exit_code == 0, result.output

        reloaded_child = get_task(config, "test-proj", child.id)
        assert reloaded_child.state == TaskState.DONE
        assert reloaded_child.resolution == "Proceed anyway."

    def test_blocked_transition_does_not_require_resolution(self, temp_portfolio):
        """Gate scoping: only `done` requires --resolution. Blocking a
        decision task is unaffected."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Decision going to blocked", kind="decision")

        runner = CliRunner()
        result = runner.invoke(main, ["-p", "test-proj", "block", task.id])
        assert result.exit_code == 0, result.output

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.state == TaskState.BLOCKED
        assert reloaded.resolution is None

    def test_bulk_done_mixes_decision_and_build_task(self, temp_portfolio):
        """CLAWP-083 per-task isolation: a decision task missing --resolution
        fails for itself only; a build task in the same bulk call still
        completes."""
        config = temp_portfolio["config"]
        decision = add_task(config, "test-proj", "Bulk decision", kind="decision")
        build = add_task(config, "test-proj", "Bulk build")

        runner = CliRunner()
        result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "state", decision.id, build.id, "done"],
        )
        assert result.exit_code == 1, result.output

        payload = json.loads(result.output)
        by_id = {r["task_id"]: r for r in payload["results"]}
        assert by_id[decision.id]["ok"] is False
        assert by_id[decision.id]["error"] == "decision_needs_resolution"
        assert by_id[build.id]["ok"] is True
        assert by_id[build.id]["data"]["state"] == "done"

        reloaded_decision = get_task(config, "test-proj", decision.id)
        assert reloaded_decision.state == TaskState.OPEN
        reloaded_build = get_task(config, "test-proj", build.id)
        assert reloaded_build.state == TaskState.DONE

    def test_change_task_state_backstop_raises_without_resolution(
        self, temp_portfolio
    ):
        """Defense-in-depth: change_task_state's own gate fires for a caller
        that bypasses services.tasks.transition entirely (e.g. a hypothetical
        direct caller other than the CLI/MCP path)."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Direct call decision", kind="decision")

        with pytest.raises(ValueError, match="decision"):
            change_task_state(config, "test-proj", task.id, TaskState.DONE)

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.state == TaskState.OPEN
        assert reloaded.resolution is None

    def test_edit_kind_decision_on_already_done_resolutionless_task_refused(
        self, temp_portfolio
    ):
        """Review fix: `tasks edit --kind decision` must not be able to
        retroactively reclassify an already-done, resolution-less task —
        that combination can never arise through the done gate itself."""
        config = temp_portfolio["config"]
        task = add_task(config, "test-proj", "Ordinary finished work")
        runner = CliRunner()
        r0 = runner.invoke(main, ["-p", "test-proj", "done", task.id])
        assert r0.exit_code == 0, r0.output

        with pytest.raises(ValueError, match="resolution"):
            edit_task(config, "test-proj", task.id, kind="decision")

        reloaded = get_task(config, "test-proj", task.id)
        assert reloaded.kind == "build"


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


# ---------------------------------------------------------------------------
# CLI-level --kind coverage (CLAWP-072-006 precedent: a flag can silently
# fail to reach the service layer if only tested via the Python API).
# ---------------------------------------------------------------------------


class TestKindViaCli:
    def test_cli_tasks_add_kind_decision(self, temp_portfolio):
        config = temp_portfolio["config"]
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "-p", "test-proj", "tasks", "add",
                "-t", "CLI decision task", "--kind", "decision",
            ],
        )
        assert result.exit_code == 0, result.output
        task_id = json.loads(result.output)["data"]["id"]

        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.kind == "decision"
        raw = reloaded.file_path.read_text(encoding="utf-8")
        assert "kind: decision" in raw

    def test_cli_tasks_add_subtask_kind_decision(self, temp_portfolio):
        config = temp_portfolio["config"]
        runner = CliRunner()
        parent_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "add", "-t", "CLI parent"],
        )
        parent_id = json.loads(parent_result.output)["data"]["id"]

        child_result = runner.invoke(
            main,
            [
                "-p", "test-proj", "tasks", "add",
                "-t", "CLI decision subtask",
                "--parent", parent_id, "--kind", "decision",
            ],
        )
        assert child_result.exit_code == 0, child_result.output
        child_id = json.loads(child_result.output)["data"]["id"]

        reloaded = get_task(config, "test-proj", child_id)
        assert reloaded is not None
        assert reloaded.kind == "decision"

    def test_cli_tasks_edit_kind_decision(self, temp_portfolio):
        config = temp_portfolio["config"]
        runner = CliRunner()
        add_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "add", "-t", "CLI reclass task"],
        )
        task_id = json.loads(add_result.output)["data"]["id"]

        edit_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "edit", task_id, "--kind", "decision"],
        )
        assert edit_result.exit_code == 0, edit_result.output

        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.kind == "decision"

    def test_cli_tasks_edit_kind_build_pops_key(self, temp_portfolio):
        config = temp_portfolio["config"]
        runner = CliRunner()
        add_result = runner.invoke(
            main,
            [
                "-p", "test-proj", "tasks", "add",
                "-t", "CLI un-reclass task", "--kind", "decision",
            ],
        )
        task_id = json.loads(add_result.output)["data"]["id"]

        edit_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "edit", task_id, "--kind", "build"],
        )
        assert edit_result.exit_code == 0, edit_result.output

        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.kind == "build"
        raw = reloaded.file_path.read_text(encoding="utf-8")
        assert "kind" not in raw

    def test_cli_tasks_edit_kind_decision_on_already_done_task_refused(
        self, temp_portfolio
    ):
        """Same review fix as the direct edit_task test, exercised through
        the CLI: the friendly error surfaces via --format json, exit_code!=0,
        and the on-disk kind is untouched."""
        config = temp_portfolio["config"]
        runner = CliRunner()
        add_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "add", "-t", "CLI finished work"],
        )
        task_id = json.loads(add_result.output)["data"]["id"]
        done_result = runner.invoke(main, ["-p", "test-proj", "done", task_id])
        assert done_result.exit_code == 0, done_result.output

        edit_result = runner.invoke(
            main, ["-p", "test-proj", "tasks", "edit", task_id, "--kind", "decision"],
        )
        assert edit_result.exit_code != 0

        reloaded = get_task(config, "test-proj", task_id)
        assert reloaded is not None
        assert reloaded.kind == "build"


# ---------------------------------------------------------------------------
# Direct unit coverage of _append_decision_to_parent (heading placement +
# the vanished-parent lenient path's own internal guard).
# ---------------------------------------------------------------------------


class TestAppendDecisionToParentUnit:
    def test_missing_parent_file_is_a_noop(self, tmp_path):
        missing = tmp_path / "GONE.md"
        _append_decision_to_parent(missing, "CHILD-1", "Some decision", "Resolution text.")
        assert not missing.exists()

    def test_insertion_stops_before_a_following_heading(self, tmp_path):
        """Only 'no heading yet' and 'heading with nothing after' were
        covered by the CLI-level tests — this covers 'heading followed by
        ANOTHER heading', where insertion must land before it, not after."""
        parent_path = tmp_path / "PARENT.md"
        parent_path.write_text(
            "---\n"
            "id: PARENT\n"
            "priority: 5\n"
            "---\n"
            "# Parent\n"
            "\n"
            "## Decisions so far\n"
            "\n"
            "- [Old one](OLD-1): Old resolution.\n"
            "\n"
            "## Notes\n"
            "\n"
            "Some notes here.\n",
            encoding="utf-8",
        )

        _append_decision_to_parent(parent_path, "NEW-1", "New decision", "New resolution.")

        body = parent_path.read_text(encoding="utf-8")
        decisions_idx = body.index("## Decisions so far")
        notes_idx = body.index("## Notes")
        new_line_idx = body.index("- [New decision](NEW-1): New resolution.")
        old_line_idx = body.index("- [Old one](OLD-1): Old resolution.")
        assert decisions_idx < old_line_idx < new_line_idx < notes_idx
        assert "Some notes here." in body

    def test_rerun_replaces_in_place_even_with_a_following_heading(self, tmp_path):
        """Idempotency (must-fix) combined with the following-heading case:
        re-closing the same child updates its line without disturbing the
        section after it."""
        parent_path = tmp_path / "PARENT.md"
        parent_path.write_text(
            "---\n"
            "id: PARENT\n"
            "---\n"
            "# Parent\n"
            "\n"
            "## Decisions so far\n"
            "\n"
            "- [Pick a bus](BUS-1): RabbitMQ.\n"
            "\n"
            "## Notes\n"
            "\n"
            "Untouched.\n",
            encoding="utf-8",
        )

        _append_decision_to_parent(parent_path, "BUS-1", "Pick a bus", "Actually, Kafka.")

        body = parent_path.read_text(encoding="utf-8")
        assert body.count("(BUS-1):") == 1
        assert "- [Pick a bus](BUS-1): Actually, Kafka." in body
        assert "RabbitMQ." not in body
        assert "Untouched." in body
