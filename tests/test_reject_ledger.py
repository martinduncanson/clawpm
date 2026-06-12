"""Tests for CLAWP-053 — rejected terminal task state (won't-do ledger).

TDD suite: these tests are written before the implementation and define
the contract for the rejected state feature.

Success criteria:
  SC-1: A task can be moved to 'rejected' terminal state with a required
        rationale; rejected tasks are excluded from default listings but
        queryable.
  SC-2: A CLI/JSON surface returns the reject set for a project; a fixture
        proves a second pass can read it to dedup (machine-consumable set).
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
from clawpm.tasks import add_task, change_task_state, list_tasks, get_task


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_portfolio():
    """Minimal portfolio with one project, matches pattern from test_subtasks."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_reject_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "\n[defaults]\nstatus = \"active\"\n"
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()

    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n'
    )

    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)

    yield {
        "root": portfolio_root,
        "project_dir": project_dir,
        "tasks_dir": tasks_dir,
        "config": load_portfolio_config(portfolio_root),
    }

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir)


# ---------------------------------------------------------------------------
# SC-1: State value and enum membership
# ---------------------------------------------------------------------------

class TestTaskStateEnum:
    """TaskState.REJECTED exists in the enum."""

    def test_rejected_in_enum(self):
        assert TaskState.REJECTED == TaskState("rejected")

    def test_rejected_value(self):
        assert TaskState.REJECTED.value == "rejected"

    def test_rejected_is_not_done(self):
        assert TaskState.REJECTED != TaskState.DONE

    def test_rejected_is_not_blocked(self):
        assert TaskState.REJECTED != TaskState.BLOCKED


# ---------------------------------------------------------------------------
# SC-1: Storage — rejected/ subdirectory on disk
# ---------------------------------------------------------------------------

class TestRejectedStorageLocation:
    """Rejected tasks live under tasks/rejected/ on disk."""

    def test_rejected_dir_created_on_transition(self, temp_portfolio):
        config = temp_portfolio["config"]

        task = add_task(config, "test", "Idea A")
        assert task is not None

        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Too costly compared to alternatives",
        )
        assert result is not None
        assert result.state == TaskState.REJECTED

        # File must be in tasks/rejected/
        assert result.file_path is not None
        assert "rejected" in result.file_path.parts

    def test_rejected_rationale_stored_in_frontmatter(self, temp_portfolio):
        config = temp_portfolio["config"]
        rationale_text = "Does not align with Q3 goals"

        task = add_task(config, "test", "Feature X")
        assert task is not None

        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale=rationale_text,
        )
        assert result is not None
        assert result.rationale == rationale_text

        # Re-read from disk — rationale must survive round-trip
        reloaded = get_task(config, "test", task.id)
        assert reloaded is not None
        assert reloaded.rationale == rationale_text

    def test_from_file_detects_rejected_state(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Rejected idea")
        assert task is not None

        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Not feasible",
        )
        assert result is not None

        # from_file must return REJECTED when path contains 'rejected'
        assert result.state == TaskState.REJECTED


# ---------------------------------------------------------------------------
# SC-1: Rationale is REQUIRED — missing rationale must error
# ---------------------------------------------------------------------------

class TestRejectedRationaleRequired:
    """change_task_state(..., TaskState.REJECTED) without rationale raises."""

    def test_missing_rationale_raises_value_error(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Need rationale")
        assert task is not None

        with pytest.raises(ValueError, match="rationale"):
            change_task_state(config, "test", task.id, TaskState.REJECTED)

    def test_empty_rationale_raises_value_error(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Empty rationale")
        assert task is not None

        with pytest.raises(ValueError, match="rationale"):
            change_task_state(
                config, "test", task.id, TaskState.REJECTED, rationale="   "
            )


# ---------------------------------------------------------------------------
# SC-1: Default listing excludes rejected
# ---------------------------------------------------------------------------

class TestRejectedExcludedFromDefaultListings:
    """Rejected tasks must NOT appear in default list_tasks() output."""

    def test_rejected_excluded_from_default_list(self, temp_portfolio):
        config = temp_portfolio["config"]

        keep = add_task(config, "test", "Keep task")
        reject = add_task(config, "test", "Rejected idea")
        assert keep is not None
        assert reject is not None

        change_task_state(
            config, "test", reject.id, TaskState.REJECTED,
            rationale="Not worth the complexity",
        )

        # Default list_tasks (no filter) must not include rejected
        all_tasks = list_tasks(config, "test", state_filter=None)
        ids = {t.id for t in all_tasks}
        assert keep.id in ids
        assert reject.id not in ids

    def test_rejected_excluded_from_open_filter(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Idea Z")
        assert task is not None

        change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Out of scope",
        )

        open_tasks = list_tasks(config, "test", state_filter=TaskState.OPEN)
        assert task.id not in {t.id for t in open_tasks}

    def test_rejected_excluded_from_progress_filter(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Idea Y")
        assert task is not None

        change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Superseded",
        )

        progress_tasks = list_tasks(config, "test", state_filter=TaskState.PROGRESS)
        assert task.id not in {t.id for t in progress_tasks}

    def test_rejected_excluded_from_blocked_filter(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Idea W")
        assert task is not None

        change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Risk too high",
        )

        blocked_tasks = list_tasks(config, "test", state_filter=TaskState.BLOCKED)
        assert task.id not in {t.id for t in blocked_tasks}


# ---------------------------------------------------------------------------
# SC-1 + SC-2: Rejected tasks ARE queryable by state filter
# ---------------------------------------------------------------------------

class TestRejectedQueryable:
    """Rejected tasks can be retrieved with state_filter=TaskState.REJECTED."""

    def test_rejected_queryable_by_state_filter(self, temp_portfolio):
        config = temp_portfolio["config"]

        task1 = add_task(config, "test", "Rejected idea 1")
        task2 = add_task(config, "test", "Rejected idea 2")
        keep = add_task(config, "test", "Keeper")
        assert task1 and task2 and keep

        change_task_state(
            config, "test", task1.id, TaskState.REJECTED,
            rationale="Too expensive",
        )
        change_task_state(
            config, "test", task2.id, TaskState.REJECTED,
            rationale="Superseded by task1",
        )

        rejected_tasks = list_tasks(config, "test", state_filter=TaskState.REJECTED)
        rejected_ids = {t.id for t in rejected_tasks}

        assert task1.id in rejected_ids
        assert task2.id in rejected_ids
        assert keep.id not in rejected_ids

    def test_rejected_rationale_present_in_query_result(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Check rationale")
        assert task is not None

        rationale = "No ROI in current quarter"
        change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale=rationale,
        )

        rejected = list_tasks(config, "test", state_filter=TaskState.REJECTED)
        assert len(rejected) == 1
        assert rejected[0].rationale == rationale

    def test_project_scoped_isolation(self, temp_portfolio):
        """Reject-set query must be project-scoped — cross-project isolation."""
        portfolio_root = temp_portfolio["root"]
        config = temp_portfolio["config"]

        # Create a second project
        projects_dir = portfolio_root / "projects"
        proj2_dir = projects_dir / "project-two"
        proj2_dir.mkdir()
        meta2 = proj2_dir / ".project"
        meta2.mkdir()
        (meta2 / "settings.toml").write_text(
            'id = "proj2"\nname = "Project Two"\nstatus = "active"\npriority = 3\n'
        )
        tasks2 = meta2 / "tasks"
        tasks2.mkdir()
        (tasks2 / "done").mkdir()
        (tasks2 / "blocked").mkdir()

        # Reload config so project two is discovered
        config2 = load_portfolio_config(portfolio_root)

        t1 = add_task(config2, "test", "Test reject")
        t2 = add_task(config2, "proj2", "Proj2 reject")
        assert t1 and t2

        change_task_state(
            config2, "test", t1.id, TaskState.REJECTED,
            rationale="Cross-project isolation check — test",
        )
        change_task_state(
            config2, "proj2", t2.id, TaskState.REJECTED,
            rationale="Cross-project isolation check — proj2",
        )

        test_rejected = list_tasks(config2, "test", state_filter=TaskState.REJECTED)
        proj2_rejected = list_tasks(config2, "proj2", state_filter=TaskState.REJECTED)

        assert {t.id for t in test_rejected} == {t1.id}
        assert {t.id for t in proj2_rejected} == {t2.id}


# ---------------------------------------------------------------------------
# SC-2: to_dict() includes rationale (JSON surface)
# ---------------------------------------------------------------------------

class TestRejectedToDict:
    """to_dict() surfaces rationale so CLI --json output is machine-readable."""

    def test_to_dict_includes_rationale(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Dict surface check")
        assert task is not None

        rationale = "Architecture too brittle"
        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale=rationale,
        )
        assert result is not None

        d = result.to_dict()
        assert d["state"] == "rejected"
        assert d["rationale"] == rationale

    def test_to_dict_rationale_none_for_non_rejected(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Open task")
        assert task is not None

        d = task.to_dict()
        # rationale key absent or None for non-rejected tasks
        assert d.get("rationale") is None


# ---------------------------------------------------------------------------
# SC-2: CLI surface — `tasks list --state rejected --json`
# ---------------------------------------------------------------------------

class TestRejectedCLI:
    """CLI integration for rejected state."""

    def test_cli_tasks_state_rejected_requires_rationale(self, temp_portfolio):
        """CLI must reject state transition to 'rejected' when --rationale is absent."""
        runner = CliRunner()
        config = temp_portfolio["config"]

        task = add_task(config, "test", "CLI reject test")
        assert task is not None

        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "state", task.id, "rejected"],
            catch_exceptions=False,
        )
        # Must exit non-zero when --rationale is not provided
        assert result.exit_code != 0

    def test_cli_tasks_state_rejected_with_rationale(self, temp_portfolio):
        runner = CliRunner()
        config = temp_portfolio["config"]

        task = add_task(config, "test", "CLI reject ok")
        assert task is not None

        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "state", task.id, "rejected",
             "--rationale", "Lower value than estimated"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["data"]["state"] == "rejected"
        assert data["data"]["rationale"] == "Lower value than estimated"

    def test_cli_tasks_list_excludes_rejected_by_default(self, temp_portfolio):
        runner = CliRunner()
        config = temp_portfolio["config"]

        keep = add_task(config, "test", "Keep me")
        reject = add_task(config, "test", "Reject me")
        assert keep and reject

        change_task_state(
            config, "test", reject.id, TaskState.REJECTED,
            rationale="Not feasible",
        )

        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "list"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        task_ids = {t["id"] for t in data}
        assert keep.id in task_ids
        assert reject.id not in task_ids

    def test_cli_tasks_list_rejected_filter_returns_reject_set(self, temp_portfolio):
        runner = CliRunner()
        config = temp_portfolio["config"]

        task1 = add_task(config, "test", "Reject A")
        task2 = add_task(config, "test", "Reject B")
        keep = add_task(config, "test", "Keep C")
        assert task1 and task2 and keep

        change_task_state(config, "test", task1.id, TaskState.REJECTED,
                          rationale="Too slow")
        change_task_state(config, "test", task2.id, TaskState.REJECTED,
                          rationale="Duplicate of something else")

        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "list", "--state", "rejected"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = {t["id"] for t in data}
        assert task1.id in ids
        assert task2.id in ids
        assert keep.id not in ids

    def test_cli_reject_set_is_machine_consumable_for_dedup(self, temp_portfolio):
        """SC-2: a second pass can read the reject set as JSON to dedup candidates."""
        runner = CliRunner()
        config = temp_portfolio["config"]

        # Simulate a planning pass that rejected two ideas
        idea1 = add_task(config, "test", "Build feature Foo")
        idea2 = add_task(config, "test", "Refactor module Bar")
        assert idea1 and idea2

        change_task_state(config, "test", idea1.id, TaskState.REJECTED,
                          rationale="Foo is out of scope for Q3")
        change_task_state(config, "test", idea2.id, TaskState.REJECTED,
                          rationale="Bar refactor postponed — technical debt acceptable")

        # Second-pass planner: fetch reject set
        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "list", "--state", "rejected"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        reject_set = json.loads(result.output)

        # Planner receives a list of dicts with id + rationale
        assert isinstance(reject_set, list)
        assert len(reject_set) == 2

        by_id = {item["id"]: item for item in reject_set}
        assert idea1.id in by_id
        assert idea2.id in by_id

        # Each entry carries enough info for semantic dedup
        for entry in reject_set:
            assert "id" in entry
            assert "title" in entry
            assert "rationale" in entry
            assert entry["rationale"]  # non-empty

        # Simulate dedup: new candidate title matches a rejected task title
        new_candidate_titles = {"Build feature Foo", "Build feature Baz"}
        rejected_titles = {item["title"] for item in reject_set}
        already_rejected = new_candidate_titles & rejected_titles
        assert already_rejected == {"Build feature Foo"}

    def test_cli_tasks_list_all_excludes_rejected(self, temp_portfolio):
        """'--state all' still excludes rejected (only --state rejected returns them)."""
        runner = CliRunner()
        config = temp_portfolio["config"]

        task = add_task(config, "test", "Rejected")
        assert task is not None
        change_task_state(config, "test", task.id, TaskState.REJECTED,
                          rationale="Not now")

        result = runner.invoke(
            main,
            ["-p", "test", "tasks", "list", "--state", "all"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = {t["id"] for t in data}
        assert task.id not in ids

    def test_cli_tasks_state_rejected_is_in_choices(self, temp_portfolio):
        """'rejected' is a valid choice for `tasks state`."""
        runner = CliRunner()

        # --help should not crash and 'rejected' should appear as a valid choice
        result = runner.invoke(
            main,
            ["tasks", "state", "--help"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "rejected" in result.output

    def test_cli_tasks_list_rejected_is_in_choices(self, temp_portfolio):
        """'rejected' is a valid --state choice for `tasks list`."""
        result = CliRunner().invoke(
            main,
            ["tasks", "list", "--help"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert "rejected" in result.output


# ---------------------------------------------------------------------------
# Optional supersedes field
# ---------------------------------------------------------------------------

class TestRejectedSupersedes:
    """Optional supersedes link field is stored and surfaced."""

    def test_supersedes_stored_and_retrieved(self, temp_portfolio):
        config = temp_portfolio["config"]
        old_task = add_task(config, "test", "Old approach")
        new_task = add_task(config, "test", "New approach (winner)")
        assert old_task and new_task

        result = change_task_state(
            config, "test", old_task.id, TaskState.REJECTED,
            rationale="Replaced by a better design",
            supersedes=new_task.id,
        )
        assert result is not None
        assert result.supersedes == new_task.id

        # Round-trip
        reloaded = get_task(config, "test", old_task.id)
        assert reloaded is not None
        assert reloaded.supersedes == new_task.id

    def test_supersedes_absent_is_none(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "No supersedes")
        assert task is not None

        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Just no",
        )
        assert result is not None
        assert result.supersedes is None

    def test_supersedes_in_to_dict(self, temp_portfolio):
        config = temp_portfolio["config"]
        task = add_task(config, "test", "Dict supersedes check")
        linked = add_task(config, "test", "Linked task")
        assert task and linked

        result = change_task_state(
            config, "test", task.id, TaskState.REJECTED,
            rationale="Superseded",
            supersedes=linked.id,
        )
        assert result is not None
        d = result.to_dict()
        assert d["supersedes"] == linked.id
