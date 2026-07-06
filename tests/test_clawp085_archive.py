"""Done-task archive/prune tests (CLAWP-085).

`done/` grows unboundedly and every list/next/reflect scan pays for it. The
archive command relocates stale DONE tasks into ``done/archive/`` — still on
disk, still resolvable, but skipped by default scans. Move-not-delete: nothing
is ever removed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.tasks import (
    archive_done_tasks,
    get_task,
    get_next_task,
    list_tasks,
    add_subtask,
    split_task,
    is_archived_path,
)
from clawpm.models import TaskState


def _make_portfolio(tmp_path: Path, monkeypatch, project_id: str = "clawpm") -> Path:
    """Register one project and point CLAWPM_PORTFOLIO at it. Returns tasks dir."""
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
    res = CliRunner().invoke(
        main, ["--format", "json", "tasks", "add", "--project", project_id, "--title", title]
    )
    assert res.exit_code == 0, res.output
    return json.loads(res.output)["data"]["id"]


def _done(project_id: str, task_id: str) -> None:
    res = CliRunner().invoke(
        main, ["--format", "json", "tasks", "state", task_id, "done", "--project", project_id]
    )
    assert res.exit_code == 0, res.output


def _age(path: Path, days: float) -> None:
    """Backdate a file's mtime by `days` so it qualifies for archiving."""
    old = time.time() - days * 86400
    os.utime(path, (old, old))


def _config():
    return load_portfolio_config()


class TestArchiveCore:
    def test_old_done_task_is_moved_to_archive(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        done_file = tasks_dir / "done" / f"{tid}.md"
        assert done_file.exists()
        _age(done_file, 120)

        moved = archive_done_tasks(_config(), "clawpm", older_than_days=90)
        assert [m["id"] for m in moved] == [tid]
        # Moved, not deleted: gone from done/ root, present in done/archive/.
        assert not done_file.exists()
        assert (tasks_dir / "done" / "archive" / f"{tid}.md").exists()

    def test_recent_done_task_is_not_archived(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "fresh done task")
        _done("clawpm", tid)
        # mtime is ~now — well within the 90d window.
        moved = archive_done_tasks(_config(), "clawpm", older_than_days=90)
        assert moved == []
        assert (tasks_dir / "done" / f"{tid}.md").exists()

    def test_dry_run_moves_nothing(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        done_file = tasks_dir / "done" / f"{tid}.md"
        _age(done_file, 120)

        moved = archive_done_tasks(_config(), "clawpm", older_than_days=90, dry_run=True)
        assert [m["id"] for m in moved] == [tid]
        # Nothing actually moved.
        assert done_file.exists()
        assert not (tasks_dir / "done" / "archive").exists()

    def test_directory_task_archived_wholesale(self, tmp_path, monkeypatch):
        """A done directory-task (parent + subtasks) moves as a whole dir."""
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        done_dir = tasks_dir / "done"
        parent_dir = done_dir / "CLAWP-900"
        parent_dir.mkdir()
        (parent_dir / "_task.md").write_text("---\nid: CLAWP-900\n---\n# parent\n", encoding="utf-8")
        (parent_dir / "CLAWP-900-001.md").write_text(
            "---\nid: CLAWP-900-001\nparent: CLAWP-900\n---\n# child\n", encoding="utf-8"
        )
        # Age the WHOLE tree: the age signal is the newest mtime across the dir,
        # so a fresh subtask would otherwise keep the parent out of the archive.
        _age(parent_dir / "_task.md", 200)
        _age(parent_dir / "CLAWP-900-001.md", 200)

        moved = archive_done_tasks(_config(), "clawpm", older_than_days=90)
        assert [m["id"] for m in moved] == ["CLAWP-900"]
        assert not parent_dir.exists()
        assert (done_dir / "archive" / "CLAWP-900" / "_task.md").exists()
        # Subtask travelled with the directory.
        assert (done_dir / "archive" / "CLAWP-900" / "CLAWP-900-001.md").exists()


class TestArchiveResolutionAndScans:
    def test_archived_task_still_resolves_as_done(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        task = get_task(_config(), "clawpm", tid)
        assert task is not None
        assert task.state == TaskState.DONE

    def test_show_marks_archived(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        res = CliRunner().invoke(
            main, ["--format", "json", "tasks", "show", tid, "--project", "clawpm"]
        )
        assert res.exit_code == 0, res.output
        assert json.loads(res.output)["archived"] is True

    def test_default_scans_exclude_archive(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        # No filter and DONE filter both skip archive by default.
        assert [t.id for t in list_tasks(_config(), "clawpm")] == []
        assert [t.id for t in list_tasks(_config(), "clawpm", state_filter=TaskState.DONE)] == []

    def test_include_archived_folds_them_back(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        got = list_tasks(_config(), "clawpm", state_filter=TaskState.DONE, include_archived=True)
        assert [t.id for t in got] == [tid]

    def test_archived_id_not_reused_by_auto_numbering(self, tmp_path, monkeypatch):
        """The critical corruption guard: an archived task's number is not re-minted."""
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        first = _add("clawpm", "task zero")  # CLAWP-000
        _done("clawpm", first)
        _age(tasks_dir / "done" / f"{first}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        # done/ root is now empty; naive numbering would reissue CLAWP-000.
        second = _add("clawpm", "task one")
        assert second != first, "auto-numbering reused an archived id"
        assert second == "CLAWP-001"


class TestArchiveCli:
    def test_cli_archive_reports_moved(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)

        res = CliRunner().invoke(
            main,
            ["--format", "json", "tasks", "archive", "--project", "clawpm", "--older-than", "90d"],
        )
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert data["count"] == 1
        assert data["archived"][0]["id"] == tid
        assert data["dry_run"] is False

    def test_cli_dry_run_moves_nothing(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)

        res = CliRunner().invoke(
            main,
            ["--format", "json", "tasks", "archive", "--project", "clawpm", "--dry-run"],
        )
        assert res.exit_code == 0, res.output
        assert (tasks_dir / "done" / f"{tid}.md").exists()
        assert not (tasks_dir / "done" / "archive").exists()

    def test_cli_bad_older_than_errors(self, tmp_path, monkeypatch):
        _make_portfolio(tmp_path, monkeypatch)
        res = CliRunner().invoke(
            main,
            ["--format", "json", "tasks", "archive", "--project", "clawpm", "--older-than", "banana"],
        )
        assert res.exit_code != 0


class TestArchiveConsumerConsistency:
    """Round-2 review fixes: every ID/dep/rollup surface stays archive-aware."""

    def _archive_first_task(self, tasks_dir):
        _age(tasks_dir / "done" / "CLAWP-000.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

    def test_get_next_task_treats_archived_dep_as_satisfied(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        a = _add("clawpm", "dep")            # CLAWP-000
        _done("clawpm", a)
        self._archive_first_task(tasks_dir)
        # B depends on the now-archived DONE task A.
        res = CliRunner().invoke(
            main,
            ["--format", "json", "tasks", "add", "--project", "clawpm",
             "--title", "B", "--depends", a],
        )
        assert res.exit_code == 0, res.output
        b = json.loads(res.output)["data"]["id"]
        assert b == "CLAWP-001"  # numbering skipped the archived CLAWP-000

        nxt = get_next_task(_config(), "clawpm")
        assert nxt is not None and nxt.id == b, "archived DONE dep should satisfy"

    def test_add_subtask_refuses_archived_parent(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        p = _add("clawpm", "parent")
        _done("clawpm", p)
        self._archive_first_task(tasks_dir)
        with pytest.raises(ValueError, match="archived"):
            add_subtask(_config(), "clawpm", p, "child")

    def test_split_task_refuses_archived_parent(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        p = _add("clawpm", "parent")
        _done("clawpm", p)
        self._archive_first_task(tasks_dir)
        with pytest.raises(ValueError, match="archived"):
            split_task(_config(), "clawpm", p)

    def test_emit_predict_parent_id_skips_archived_root(self, tmp_path, monkeypatch):
        from types import SimpleNamespace
        from clawpm.emit_tree import _predict_parent_id

        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        a = _add("clawpm", "root")           # CLAWP-000
        _done("clawpm", a)
        self._archive_first_task(tasks_dir)
        # done/ is now empty; a naive scan would re-predict CLAWP-000.
        doc = SimpleNamespace(root=SimpleNamespace(attach_to=None))
        assert _predict_parent_id(doc, _config(), "clawpm") == "CLAWP-001"

    def test_existing_child_nums_counts_archived_children(self, tmp_path, monkeypatch):
        from clawpm.emit_tree import _existing_child_nums

        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        # Archived directory-task parent with a child inside.
        archive_parent = tasks_dir / "done" / "archive" / "CLAWP-500"
        archive_parent.mkdir(parents=True)
        (archive_parent / "_task.md").write_text("---\nid: CLAWP-500\n---\n", encoding="utf-8")
        (archive_parent / "CLAWP-500-001.md").write_text(
            "---\nid: CLAWP-500-001\nparent: CLAWP-500\n---\n", encoding="utf-8"
        )
        assert 1 in _existing_child_nums(tasks_dir, "CLAWP-500")


class TestArchiveRobustness:
    def test_destination_exists_is_skipped_not_clobbered(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old done task")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        # Pre-seed a colliding archived file so the mover must not clobber it.
        archive_dir = tasks_dir / "done" / "archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / f"{tid}.md").write_text("ARCHIVED-ALREADY", encoding="utf-8")

        results = archive_done_tasks(_config(), "clawpm", older_than_days=90)
        rec = next(r for r in results if r["id"] == tid)
        assert rec.get("skipped") == "destination_exists"
        # The pre-existing archived file is untouched; the source stays put.
        assert (archive_dir / f"{tid}.md").read_text(encoding="utf-8") == "ARCHIVED-ALREADY"
        assert (tasks_dir / "done" / f"{tid}.md").exists()

    def test_is_archived_path_specific_to_done_archive(self):
        from pathlib import Path
        # A real done/archive/ silo → archived.
        assert is_archived_path(Path("/p/.project/tasks/done/archive/CLAWP-1.md"))
        assert is_archived_path(Path("/p/.project/tasks/done/archive/CLAWP-1/_task.md"))
        # A repo checked out under a dir literally named "archive" but the task
        # is live in tasks/ → NOT archived (Codex P3 false-positive guard).
        assert not is_archived_path(Path("/home/archive/proj/.project/tasks/CLAWP-1.md"))
        assert not is_archived_path(Path("/p/.project/tasks/done/CLAWP-1.md"))
        assert not is_archived_path(None)


class TestArchiveRound3:
    """Round-3 review fixes: nested-dir path shapes, cascade, dir-task mtime,
    dry-run preview, emit-tree attach_to + idempotency."""

    def test_nested_archived_directory_subtask_resolves(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        # A decomposed archived subtask: done/archive/CLAWP-500/CLAWP-500-001/_task.md
        nested = tasks_dir / "done" / "archive" / "CLAWP-500" / "CLAWP-500-001"
        nested.mkdir(parents=True)
        (nested / "_task.md").write_text(
            "---\nid: CLAWP-500-001\nparent: CLAWP-500\n---\n# nested\n", encoding="utf-8"
        )
        task = get_task(_config(), "clawpm", "CLAWP-500-001")
        assert task is not None and task.state == TaskState.DONE

    def test_cascade_unblocks_with_archived_dep(self, tmp_path, monkeypatch):
        from clawpm.tasks import cascade_unblock_dependents
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        a = _add("clawpm", "dep A")           # CLAWP-000
        _done("clawpm", a)
        _age(tasks_dir / "done" / f"{a}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)
        # B (CLAWP-001) is the just-completed trigger; C depends on both A
        # (archived) and B and starts blocked.
        b = _add("clawpm", "dep B")           # CLAWP-001
        res = CliRunner().invoke(
            main, ["--format", "json", "tasks", "add", "--project", "clawpm",
                   "--title", "C", "--depends", a, "--depends", b],
        )
        c = json.loads(res.output)["data"]["id"]
        r = CliRunner().invoke(main, ["--format", "json", "tasks", "state", c, "blocked", "--project", "clawpm"])
        assert r.exit_code == 0, r.output
        assert get_task(_config(), "clawpm", c).state == TaskState.BLOCKED

        # Directly exercise the cascade with B not yet in done_ids-free path:
        # mark B done on disk WITHOUT the CLI auto-cascade, then cascade.
        from clawpm.tasks import change_task_state
        change_task_state(_config(), "clawpm", b, TaskState.DONE)
        moved = cascade_unblock_dependents(_config(), "clawpm", b)
        assert any(m["task_id"] == c for m in moved), "archived dep must not block cascade"
        assert get_task(_config(), "clawpm", c).state == TaskState.OPEN

    def test_dir_task_with_recent_subtask_not_archived(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        parent_dir = tasks_dir / "done" / "CLAWP-700"
        parent_dir.mkdir(parents=True)
        (parent_dir / "_task.md").write_text("---\nid: CLAWP-700\n---\n", encoding="utf-8")
        (parent_dir / "CLAWP-700-001.md").write_text(
            "---\nid: CLAWP-700-001\nparent: CLAWP-700\n---\n", encoding="utf-8"
        )
        # _task.md is stale but the subtask was touched recently → newest mtime
        # is within the window → the whole dir stays put.
        _age(parent_dir / "_task.md", 300)
        # child left at ~now
        moved = archive_done_tasks(_config(), "clawpm", older_than_days=90)
        assert moved == []
        assert parent_dir.exists()

    def test_dry_run_previews_destination_exists_skip(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        tid = _add("clawpm", "old")
        _done("clawpm", tid)
        _age(tasks_dir / "done" / f"{tid}.md", 120)
        archive_dir = tasks_dir / "done" / "archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / f"{tid}.md").write_text("ALREADY", encoding="utf-8")

        results = archive_done_tasks(_config(), "clawpm", older_than_days=90, dry_run=True)
        rec = next(r for r in results if r["id"] == tid)
        # Dry-run preview matches what the real run would do: skip, not archive.
        assert rec.get("skipped") == "destination_exists"

    def test_emit_attach_to_refuses_archived_parent(self, tmp_path, monkeypatch):
        from clawpm.emit_tree import emit_tree, EmitValidationError, parse_emit_document
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        p = _add("clawpm", "parent")
        _done("clawpm", p)
        _age(tasks_dir / "done" / f"{p}.md", 120)
        archive_done_tasks(_config(), "clawpm", older_than_days=90)

        doc = parse_emit_document({
            "schema_version": 1,
            "root": {"attach_to": p},
            "leaves": [{"ref": "x", "title": "new child", "leaf_key": "k1"}],
        })
        with pytest.raises(EmitValidationError, match="archived"):
            emit_tree(_config(), "clawpm", doc)

    def test_resolve_idempotency_sees_archived_leaf(self, tmp_path, monkeypatch):
        from clawpm.emit_tree import _resolve_idempotency, parse_emit_document
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        # An emitted-then-archived standalone child carrying a leaf_key.
        archive_dir = tasks_dir / "done" / "archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / "CLAWP-600-001.md").write_text(
            "---\nid: CLAWP-600-001\nparent: CLAWP-600\nleaf_key: leafA\n---\n", encoding="utf-8"
        )
        doc = parse_emit_document({
            "schema_version": 1,
            "root": {"attach_to": "CLAWP-600"},
            "leaves": [{"ref": "a", "title": "t", "leaf_key": "leafA"}],
        })
        assert "leafA" in _resolve_idempotency(_config(), "clawpm", "CLAWP-600", doc.leaves)

    def test_is_archived_path_case_insensitive(self):
        from pathlib import Path
        # A case-preserving Windows FS (or external creation) can surface mixed
        # case; the freeze guard must still fire on the done/archive silo.
        assert is_archived_path(Path("/p/.project/tasks/Done/Archive/CLAWP-1.md"))
        assert is_archived_path(Path("/p/.project/tasks/DONE/ARCHIVE/CLAWP-1/_task.md"))

    def test_archive_skips_mixed_case_silo_dir(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        # An externally-created "Archive" dir under done/ must be treated as the
        # silo, never as a task to move into itself.
        (tasks_dir / "done" / "Archive").mkdir(parents=True)
        (tasks_dir / "done" / "Archive" / "CLAWP-050.md").write_text(
            "---\nid: CLAWP-050\n---\n", encoding="utf-8"
        )
        results = archive_done_tasks(_config(), "clawpm", older_than_days=0)
        # The silo dir itself is not reported as an archived/errored entry.
        assert all(r["id"] != "Archive" for r in results)
