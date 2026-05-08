"""Regression tests for two field-reported bugs.

Bug 1 — ID collision after tasks split creates parent directory
  After OPENW-004.md is converted to OPENW-004/_task.md, the next call to
  add_task must not re-issue OPENW-004.

Bug 2 — UnicodeEncodeError on Windows when title/body contains non-cp1252 chars
  add_task with a title containing U+2192 (→) must succeed, and the task must
  round-trip through get_task with the title intact.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import add_task, get_task, split_task


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_portfolio():
    """Minimal portfolio wired up to a temp directory."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_bugfix_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n",
        encoding="utf-8",
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()

    project_dir = projects_dir / "bugfix-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()

    (project_meta / "settings.toml").write_text(
        'id = "bugfix"\nname = "Bugfix Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
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
# Bug 1 — ID collision after split
# ---------------------------------------------------------------------------

class TestIdCollisionAfterSplit:
    """add_task must not re-use the number of a split (directory) parent task."""

    def test_no_collision_after_split_at_top_level(self, temp_portfolio):
        """
        Sequence: add BUGFI-000, split it → BUGFI-000/_task.md, then add a new
        task — it must receive BUGFI-001, not BUGFI-000 again.
        """
        config = temp_portfolio["config"]

        task0 = add_task(config, "bugfix", "First task")
        assert task0 is not None
        assert task0.id == "BUGFI-000"

        # Split: BUGFI-000.md → BUGFI-000/_task.md
        split = split_task(config, "bugfix", "BUGFI-000")
        assert split is not None
        assert split.file_path.name == "_task.md"
        assert split.file_path.parent.name == "BUGFI-000"

        # Next add must NOT re-use 000
        task1 = add_task(config, "bugfix", "Second task")
        assert task1 is not None
        assert task1.id == "BUGFI-001", (
            f"Expected BUGFI-001 but got {task1.id} — "
            "ID scanner missed parent directory BUGFI-000"
        )

    def test_no_collision_after_split_in_done(self, temp_portfolio):
        """
        Same check when the split parent is in done/.
        A done parent directory must still block its number from being reused.
        """
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]

        # Manually create a split parent task in done/
        done_dir = tasks_dir / "done" / "BUGFI-000"
        done_dir.mkdir(parents=True)
        (done_dir / "_task.md").write_text(
            "---\nid: BUGFI-000\npriority: 5\n---\n# Done parent\n",
            encoding="utf-8",
        )

        task = add_task(config, "bugfix", "New task after done parent")
        assert task is not None
        assert task.id == "BUGFI-001", (
            f"Expected BUGFI-001 but got {task.id} — "
            "ID scanner missed done/ parent directory BUGFI-000"
        )

    def test_no_collision_after_split_in_blocked(self, temp_portfolio):
        """Same check when the split parent is in blocked/."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]

        blocked_dir = tasks_dir / "blocked" / "BUGFI-002"
        blocked_dir.mkdir(parents=True)
        (blocked_dir / "_task.md").write_text(
            "---\nid: BUGFI-002\npriority: 5\n---\n# Blocked parent\n",
            encoding="utf-8",
        )

        task = add_task(config, "bugfix", "New task after blocked parent")
        assert task is not None
        assert task.id == "BUGFI-003", (
            f"Expected BUGFI-003 but got {task.id} — "
            "ID scanner missed blocked/ parent directory BUGFI-002"
        )


# ---------------------------------------------------------------------------
# Bug 2 — Unicode round-trip
# ---------------------------------------------------------------------------

class TestUnicodeTaskRoundTrip:
    """Tasks with non-cp1252 characters in title/body must survive write → read."""

    def test_unicode_arrow_in_title(self, temp_portfolio):
        """U+2192 (→) in title must not raise and must round-trip correctly."""
        config = temp_portfolio["config"]

        title = "Phase 4 \u2192 Silero"  # → arrow
        task = add_task(config, "bugfix", title)

        assert task is not None, "add_task returned None — likely UnicodeEncodeError"
        assert task.title == title, f"Title did not round-trip: {task.title!r}"
        assert task.state == TaskState.OPEN

        # Reload via get_task to confirm on-disk round-trip
        reloaded = get_task(config, "bugfix", task.id)
        assert reloaded is not None
        assert reloaded.title == title, f"Reloaded title mismatch: {reloaded.title!r}"

    def test_unicode_in_description(self, temp_portfolio):
        """CJK characters and emoji in description must round-trip."""
        config = temp_portfolio["config"]

        title = "Unicode description test"
        description = "日本語テスト 🔥 café naïve résumé"

        task = add_task(config, "bugfix", title, description=description)
        assert task is not None

        reloaded = get_task(config, "bugfix", task.id)
        assert reloaded is not None
        # Content includes the description text
        assert "日本語テスト" in reloaded.content
        assert "🔥" in reloaded.content

    def test_unicode_title_no_husk_on_disk(self, temp_portfolio):
        """After a successful write, no orphan .tmp file should remain."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]

        task = add_task(config, "bugfix", "Arrow \u2192 test")
        assert task is not None

        # Confirm no .tmp husk
        tmp_files = list(tasks_dir.glob("*.tmp"))
        assert tmp_files == [], f"Orphan .tmp files left behind: {tmp_files}"

    def test_multiple_unicode_tasks_get_sequential_ids(self, temp_portfolio):
        """Adding several Unicode-titled tasks must yield sequential IDs."""
        config = temp_portfolio["config"]

        t0 = add_task(config, "bugfix", "Step 1 \u2192 Start")
        t1 = add_task(config, "bugfix", "Step 2 \u2013 Middle")  # en-dash
        t2 = add_task(config, "bugfix", "Step 3 \u2014 End")    # em-dash

        assert t0.id == "BUGFI-000"
        assert t1.id == "BUGFI-001"
        assert t2.id == "BUGFI-002"
