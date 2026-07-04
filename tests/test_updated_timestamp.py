"""Tests for the `updated` task-frontmatter timestamp (CLAWP-086).

Every mutating path stamps `updated: <ISO date>`; `add` sets it equal to
`created`; doctor's stale-task check prefers `updated` over the (lying) file
mtime.
"""

import json
import os
import re
import shutil
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.frontmatter import parse_frontmatter
from clawpm.models import Task, TaskState, TaskComplexity
from clawpm.tasks import (
    add_task,
    add_subtask,
    change_task_state,
    edit_task,
    get_task,
    split_task,
)

from click.testing import CliRunner

_OLD = "2000-01-01"


@pytest.fixture
def temp_portfolio():
    """A temporary portfolio with one test project (id ``test``)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_upd_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'''
portfolio_root = "{portfolio_root.as_posix()}"
project_roots = ["{(portfolio_root / 'projects').as_posix()}"]

[defaults]
status = "active"
'''
    )

    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        '''
id = "test"
name = "Test Project"
status = "active"
priority = 3
'''
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


def _updated_on_disk(path: Path) -> str | None:
    fm, _ = parse_frontmatter(path.read_text(encoding="utf-8"))
    return fm.get("updated") if isinstance(fm, dict) else None


def _backdate(path: Path) -> None:
    """Rewrite the file's `updated` stamp to an old date to prove a re-stamp."""
    text = path.read_text(encoding="utf-8")
    new_text, n = re.subn(
        r"updated: '?\d{4}-\d{2}-\d{2}'?", f"updated: '{_OLD}'", text
    )
    assert n == 1, f"expected exactly one updated line in {path}, got {n}"
    path.write_text(new_text, encoding="utf-8")


class TestAddStampsUpdated:
    def test_add_sets_updated_equal_to_created(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "First task")
        assert task is not None
        today = date.today().isoformat()
        assert task.updated == today
        assert task.updated == task.created

    def test_from_file_roundtrip_preserves_updated(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Roundtrip")
        reloaded = get_task(cfg, "test", task.id)
        assert reloaded is not None
        assert reloaded.updated == task.updated
        # And the JSON surface exposes it.
        assert reloaded.to_dict()["updated"] == task.updated

    def test_subtask_add_sets_updated_equal_to_created(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        parent = add_task(cfg, "test", "Parent")
        child = add_subtask(cfg, "test", parent.id, "Child")
        assert child is not None
        assert child.updated == date.today().isoformat()
        assert child.updated == child.created


class TestMutatorsBumpUpdated:
    def test_edit_bumps_updated(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Editable")
        _backdate(task.file_path)
        assert _updated_on_disk(task.file_path) == _OLD

        edited = edit_task(cfg, "test", task.id, priority=1)
        assert edited is not None
        assert edited.updated == date.today().isoformat()
        assert _updated_on_disk(edited.file_path) != _OLD

    def test_state_change_bumps_updated(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Movable")
        _backdate(task.file_path)

        moved = change_task_state(cfg, "test", task.id, TaskState.DONE)
        assert moved is not None
        assert moved.state == TaskState.DONE
        assert moved.updated == date.today().isoformat()

    def test_split_bumps_updated(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Splittable")
        _backdate(task.file_path)

        split = split_task(cfg, "test", task.id)
        assert split is not None
        assert split.file_path.name == "_task.md"
        assert split.updated == date.today().isoformat()

    def test_dir_task_progress_stamps_updated(self, temp_portfolio):
        """A directory task's same-location PROGRESS transition still stamps."""
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "DirParent")
        split_task(cfg, "test", task.id)  # convert to a directory task
        tm = temp_portfolio["tasks_dir"] / task.id / "_task.md"
        _backdate(tm)
        # Directory tasks track PROGRESS in-place (no `.progress.md` rename), so
        # state stays OPEN by location — but the stamp must still fire.
        moved = change_task_state(cfg, "test", task.id, TaskState.PROGRESS)
        assert moved is not None
        assert moved.updated == date.today().isoformat()

    def test_log_attach_bumps_updated(self, temp_portfolio):
        """`clawpm log add --task X` (log-attach) bumps the task's updated."""
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Loggable")
        _backdate(task.file_path)
        result = CliRunner().invoke(
            main,
            ["log", "add", "--project", "test", "--task", task.id,
             "--action", "note", "--summary", "did stuff"],
        )
        assert result.exit_code == 0, result.output
        reloaded = get_task(cfg, "test", task.id)
        assert reloaded is not None
        assert reloaded.updated == date.today().isoformat()

    def test_crlf_file_no_mixed_endings(self, tmp_path):
        """Stamping a CRLF task file produces no doubled/mixed line endings."""
        from clawpm.tasks import _stamp_updated_file

        f = tmp_path / "crlf.md"
        f.write_bytes(b"---\r\nid: x\r\nupdated: '2020-01-01'\r\n---\r\n# T\r\n")
        _stamp_updated_file(f)
        raw = f.read_bytes()
        assert b"\r\r" not in raw  # no doubled CR
        assert f"updated: '{date.today().isoformat()}'".encode() in raw
        # Uniform line endings, no MIX: every LF is part of a CRLF (Windows
        # write_text) OR none are (Linux LF). A mixed result (0 < crlf < lf)
        # would mean the surgical edit left a lone LF among CRLF lines.
        lf_total = raw.count(b"\n")
        crlf = raw.count(b"\r\n")
        assert crlf == lf_total or crlf == 0
        # No lone LF (LF not preceded by CR) unless the whole file is LF-only.
        if crlf:
            assert not any(
                raw[i:i + 1] == b"\n" and (i == 0 or raw[i - 1:i] != b"\r")
                for i in range(len(raw))
            )

    def test_decompose_stamps_child_and_bumps_parent(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        parent = add_task(cfg, "test", "Decomposable")
        _backdate(parent.file_path)  # backdate the pre-split .md

        child = add_subtask(cfg, "test", parent.id, "Sub A")
        assert child is not None
        today = date.today().isoformat()
        assert child.updated == today

        # Parent was split into a directory task; its _task.md must be bumped.
        parent_after = get_task(cfg, "test", parent.id)
        assert parent_after is not None
        assert parent_after.file_path.name == "_task.md"
        assert parent_after.updated == today

    def test_state_change_preserves_frontmatter_comments(self, temp_portfolio):
        """Surgical stamp: a state move keeps comments/order and only bumps updated."""
        cfg = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        f = tasks_dir / "test-200.md"
        f.write_text(
            "---\n"
            "id: test-200\n"
            "priority: 5\n"
            "# operator note: do not lose this\n"
            "created: '2026-01-01'\n"
            "updated: '2026-01-01'\n"
            "---\n"
            "# Commented task\n\nbody\n",
            encoding="utf-8",
        )
        moved = change_task_state(cfg, "test", "test-200", TaskState.DONE)
        assert moved is not None
        done_text = (tasks_dir / "done" / "test-200.md").read_text(encoding="utf-8")
        today = date.today().isoformat()
        assert "# operator note: do not lose this" in done_text
        assert f"updated: '{today}'" in done_text
        assert "created: '2026-01-01'" in done_text  # untouched

    def test_state_change_inserts_updated_when_absent(self, temp_portfolio):
        """A legacy file with no `updated` key gets one inserted on state move."""
        cfg = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        f = tasks_dir / "test-201.md"
        f.write_text(
            "---\nid: test-201\ncreated: '2024-01-01'\n---\n# Legacy\n",
            encoding="utf-8",
        )
        moved = change_task_state(cfg, "test", "test-201", TaskState.DONE)
        assert moved is not None
        assert moved.updated == date.today().isoformat()
        done_text = (tasks_dir / "done" / "test-201.md").read_text(encoding="utf-8")
        assert f"updated: '{date.today().isoformat()}'" in done_text

    def test_repeated_stamp_is_idempotent_single_line(self, temp_portfolio):
        """Two state moves leave exactly one `updated:` line (no duplicate)."""
        from clawpm.tasks import _set_updated_line

        text = "---\nid: x\nupdated: '2020-01-01'\n---\n# T\n"
        once = _set_updated_line(text, "2026-07-04")
        twice = _set_updated_line(once, "2026-07-05")
        assert twice.count("updated:") == 1
        assert "updated: '2026-07-05'" in twice
        # Space-less form is still replaced, not duplicated.
        spaceless = _set_updated_line("---\nid: x\nupdated:'2020-01-01'\n---\n#T\n", "2026-07-04")
        assert spaceless.count("updated:") == 1
        # Space-BEFORE-colon (valid YAML) is also replaced, not duplicated.
        spaced = _set_updated_line("---\nid: x\nupdated : '2020-01-01'\n---\n#T\n", "2026-07-04")
        assert spaced.count("updated") == 1

    def test_reject_bumps_updated(self, temp_portfolio):
        cfg = temp_portfolio["config"]
        task = add_task(cfg, "test", "Rejectable")
        _backdate(task.file_path)

        rejected = change_task_state(
            cfg, "test", task.id, TaskState.REJECTED, rationale="not worth it"
        )
        assert rejected is not None
        assert rejected.state == TaskState.REJECTED
        assert rejected.updated == date.today().isoformat()
        assert rejected.rationale == "not worth it"


class TestLegacyTasks:
    def test_unquoted_date_coerced_to_str(self, temp_portfolio):
        """An unquoted ISO `updated` (YAML → datetime.date) loads as an ISO str."""
        tasks_dir = temp_portfolio["tasks_dir"]
        f = tasks_dir / "test-901.md"
        f.write_text(
            "---\nid: test-901\nupdated: 2026-07-04\n---\n# Unquoted\n",
            encoding="utf-8",
        )
        task = Task.from_file(f)
        assert task.updated == "2026-07-04"
        assert isinstance(task.updated, str)
        # to_dict feeds JSON — must be serialisable (a date object would raise).
        json.dumps(task.to_dict())

    def test_unquoted_created_coerced_to_str(self, temp_portfolio):
        """`created` gets the same date→str coercion as `updated`."""
        tasks_dir = temp_portfolio["tasks_dir"]
        f = tasks_dir / "test-902.md"
        f.write_text(
            "---\nid: test-902\ncreated: 2024-01-01\n---\n# C\n", encoding="utf-8"
        )
        task = Task.from_file(f)
        assert task.created == "2024-01-01"
        assert isinstance(task.created, str)
        json.dumps(task.to_dict())

    def test_missing_updated_is_none(self, temp_portfolio):
        """A pre-CLAWP-086 task file (no `updated`) loads with updated=None."""
        tasks_dir = temp_portfolio["tasks_dir"]
        legacy = tasks_dir / "test-900.md"
        legacy.write_text(
            "---\nid: test-900\ncreated: 2024-01-01\n---\n# Legacy\n",
            encoding="utf-8",
        )
        task = Task.from_file(legacy)
        assert task.updated is None
        assert task.to_dict()["updated"] is None


class TestOtherFrontmatterWriters:
    """Non-tasks.py writers of task frontmatter must also stamp (CLAWP-086)."""

    def test_doctor_state_rewrite_stamps_updated(self, temp_portfolio):
        from clawpm.doctor_apply import _rewrite_frontmatter_state

        tasks_dir = temp_portfolio["tasks_dir"]
        f = tasks_dir / "test-010.md"
        f.write_text(
            "---\nid: test-010\ncreated: 2024-01-01\n---\n# Drift\n",
            encoding="utf-8",
        )
        _rewrite_frontmatter_state(f, "done")
        fm, _ = parse_frontmatter(f.read_text(encoding="utf-8"))
        assert fm["state"] == "done"
        assert fm["updated"] == date.today().isoformat()

    def test_emit_render_sets_updated_equal_to_created(self):
        from clawpm.emit_tree import _render_task_content

        content = _render_task_content(
            task_id="TEST-000",
            title="Emitted",
            parent_id=None,
            leaf=None,
            baseline_ref="ts:2026-01-01T00:00:00+00:00",
        )
        fm, _ = parse_frontmatter(content)
        today = date.today().isoformat()
        assert fm["updated"] == today
        assert fm["updated"] == fm["created"]


class TestDoctorPrefersUpdated:
    def test_stale_updated_flagged_despite_fresh_mtime(self, temp_portfolio):
        """`updated` 8 days ago + fresh mtime → stale (proves updated wins)."""
        tasks_dir = temp_portfolio["tasks_dir"]
        old = (date.today() - timedelta(days=8)).isoformat()
        prog = tasks_dir / "test-001.progress.md"
        prog.write_text(
            f"---\nid: test-001\nupdated: {old}\n---\n# Prog\n", encoding="utf-8"
        )
        # mtime is now (freshly written) — if doctor used mtime this is NOT stale.

        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stale_ids = [s["task_id"] for s in data.get("stale_tasks", [])]
        assert "test-001" in stale_ids

    def test_fresh_updated_not_flagged_despite_stale_mtime(self, temp_portfolio):
        """`updated` today + 8-day-old mtime → NOT stale (proves updated wins)."""
        tasks_dir = temp_portfolio["tasks_dir"]
        today = date.today().isoformat()
        prog = tasks_dir / "test-002.progress.md"
        prog.write_text(
            f"---\nid: test-002\nupdated: {today}\n---\n# Prog\n", encoding="utf-8"
        )
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(prog, (old_ts, old_ts))

        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stale_ids = [s["task_id"] for s in data.get("stale_tasks", [])]
        assert "test-002" not in stale_ids

    def _seed_blocked(self, tasks_dir, updated_scalar):
        """A DONE dep + a blocked task depending on it (all deps resolved)."""
        (tasks_dir / "done" / "test-050.md").write_text(
            "---\nid: test-050\n---\n# Dep\n", encoding="utf-8"
        )
        bf = tasks_dir / "blocked" / "test-051.md"
        upd = f"updated: {updated_scalar}\n" if updated_scalar else ""
        bf.write_text(
            f"---\nid: test-051\ndepends:\n- test-050\n{upd}---\n# Blocked\n",
            encoding="utf-8",
        )
        return bf

    def test_blocked_stale_uses_updated_despite_fresh_mtime(self, temp_portfolio):
        old = (date.today() - timedelta(days=3)).isoformat()  # > 24h
        bf = self._seed_blocked(temp_portfolio["tasks_dir"], old)
        # mtime fresh (just written) — if the check used mtime it'd NOT be stale.
        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = [s["task_id"] for s in data.get("stale_blocked", [])]
        assert "test-051" in ids

    def test_blocked_fresh_updated_not_flagged_despite_stale_mtime(self, temp_portfolio):
        today = date.today().isoformat()
        bf = self._seed_blocked(temp_portfolio["tasks_dir"], today)
        old_ts = (datetime.now(timezone.utc) - timedelta(days=3)).timestamp()
        os.utime(bf, (old_ts, old_ts))
        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        ids = [s["task_id"] for s in data.get("stale_blocked", [])]
        assert "test-051" not in ids

    def test_legacy_no_updated_falls_back_to_mtime(self, temp_portfolio):
        """No `updated` stamp → doctor falls back to mtime (legacy behaviour)."""
        tasks_dir = temp_portfolio["tasks_dir"]
        prog = tasks_dir / "test-003.progress.md"
        prog.write_text("---\nid: test-003\n---\n# Prog\n", encoding="utf-8")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).timestamp()
        os.utime(prog, (old_ts, old_ts))

        result = CliRunner().invoke(main, ["doctor"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        stale_ids = [s["task_id"] for s in data.get("stale_tasks", [])]
        assert "test-003" in stale_ids
