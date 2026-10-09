"""State-change moves must never leave two copies of a task (CLAWP-143).

``shutil.move`` falls back to copy-then-delete on OSError. If the copy lands
and the source delete (or anything after the copy) fails, the task exists in
both its old and new state. These tests simulate that with a move that copies
and then raises, and require exactly ONE surviving copy (the source) plus a
loud error.
"""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path

import pytest

import clawpm.tasks as tasks_module
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import (
    add_subtask,
    add_task,
    archive_done_tasks,
    change_task_state,
    split_task,
)


def _make_portfolio(tmp_path: Path, monkeypatch, project_id: str = "mv143") -> Path:
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    meta = tmp_path / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return tasks_dir


def _copy_then_raise(src, dst, *a, **k):
    """Mimic shutil.move's cross-volume fallback dying after the copy landed."""
    src, dst = Path(src), Path(dst)
    if src.is_dir():
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    raise OSError(errno.EIO, "simulated source delete failure")


def _copy_then_partial_delete_then_raise(src, dst, *a, **k):
    """Copy a directory, delete part of the source, then die (rmtree half-done)."""
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst)
    (src / "extra.txt").unlink()
    raise OSError(errno.EIO, "simulated partial source delete")


def _copies(tasks_dir: Path, task_id: str) -> list[Path]:
    """Every path under the task store that is this task's file or directory."""
    found = []
    for p in tasks_dir.rglob("*"):
        if p.name in (f"{task_id}.md", f"{task_id}.progress.md", task_id):
            found.append(p)
    return found


class TestFileFormMove:
    def test_failed_move_leaves_only_the_source(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "Leaf task")
        src = tasks_dir / f"{task.id}.md"
        assert src.exists()
        before = src.read_bytes()

        monkeypatch.setattr(tasks_module.shutil, "move", _copy_then_raise)
        with pytest.raises(OSError, match="simulated source delete failure"):
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)

        assert _copies(tasks_dir, task.id) == [src], "exactly one copy, in the source state"
        assert src.read_bytes() == before
        assert not (tasks_dir / "done" / f"{task.id}.md").exists()

    def test_retry_after_failure_converges(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "Leaf task")

        with monkeypatch.context() as m:
            m.setattr(tasks_module.shutil, "move", _copy_then_raise)
            with pytest.raises(OSError):
                change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)

        assert change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        assert [p.relative_to(tasks_dir) for p in _copies(tasks_dir, task.id)] == [
            Path("done") / f"{task.id}.md"
        ]


class TestDirFormMove:
    def _dir_task(self, config, tasks_dir):
        parent = add_task(config, "mv143", "Parent")
        add_subtask(config, "mv143", parent.id, "Child")
        assert (tasks_dir / parent.id / "_task.md").exists()
        return parent

    def test_failed_move_leaves_only_the_source_tree(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        parent = self._dir_task(config, tasks_dir)
        src = tasks_dir / parent.id
        listing = sorted(str(p.relative_to(src)) for p in src.rglob("*"))

        monkeypatch.setattr(tasks_module.shutil, "move", _copy_then_raise)
        with pytest.raises(OSError, match="simulated source delete failure"):
            change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        assert _copies(tasks_dir, parent.id) == [src]
        assert sorted(str(p.relative_to(src)) for p in src.rglob("*")) == listing
        assert not (tasks_dir / "done" / parent.id).exists()

    def test_partially_deleted_source_is_healed_not_lost(self, tmp_path, monkeypatch):
        """Copy done, source half-deleted: the destination holds the only copy of
        the missing file. Rollback must restore it into the source, not discard it."""
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        parent = self._dir_task(config, tasks_dir)
        src = tasks_dir / parent.id
        (src / "extra.txt").write_text("only copy\n", encoding="utf-8")

        monkeypatch.setattr(
            tasks_module.shutil, "move", _copy_then_partial_delete_then_raise
        )
        with pytest.raises(OSError, match="simulated partial source delete"):
            change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        assert _copies(tasks_dir, parent.id) == [src]
        assert (src / "extra.txt").read_text(encoding="utf-8") == "only copy\n"

    def test_unrestorable_source_keeps_destination_and_says_so(
        self, tmp_path, monkeypatch
    ):
        """If healing the source fails, never delete the destination: raise loudly."""
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        parent = self._dir_task(config, tasks_dir)
        src = tasks_dir / parent.id
        (src / "extra.txt").write_text("only copy\n", encoding="utf-8")

        monkeypatch.setattr(
            tasks_module.shutil, "move", _copy_then_partial_delete_then_raise
        )
        real_copy2 = shutil.copy2

        def _boom(*a, **k):
            raise OSError(errno.EIO, "restore failed")

        monkeypatch.setattr(tasks_module.shutil, "copy2", _boom)
        with pytest.raises(RuntimeError, match="extra.txt|left in place|by hand"):
            change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        dest = tasks_dir / "done" / parent.id
        assert (dest / "extra.txt").read_text(encoding="utf-8") == "only copy\n"
        monkeypatch.setattr(tasks_module.shutil, "copy2", real_copy2)


class TestOtherMoveSites:
    def test_split_task_failed_move_leaves_only_the_source(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "To split")
        src = tasks_dir / f"{task.id}.md"

        monkeypatch.setattr(tasks_module.shutil, "move", _copy_then_raise)
        with pytest.raises(OSError, match="simulated source delete failure"):
            split_task(config, "mv143", task.id)

        assert src.exists()
        assert not (tasks_dir / task.id / "_task.md").exists()

    def test_archive_failed_move_leaves_only_the_source(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "Old done")
        assert change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        done_file = tasks_dir / "done" / f"{task.id}.md"
        os.utime(done_file, (1_000_000, 1_000_000))

        monkeypatch.setattr(tasks_module.shutil, "move", _copy_then_raise)
        with pytest.raises(OSError, match="simulated source delete failure"):
            archive_done_tasks(config, "mv143", older_than_days=1)

        assert done_file.exists()
        assert not (tasks_dir / "done" / "archive" / f"{task.id}.md").exists()


class TestPreexistingDestinationUntouched:
    def test_rollback_never_deletes_a_destination_it_did_not_create(
        self, tmp_path, monkeypatch
    ):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "Leaf task")
        stale = tasks_dir / "done" / f"{task.id}.md"
        stale.write_text("pre-existing stale file\n", encoding="utf-8")

        def _raise_only(src, dst, *a, **k):
            raise OSError(errno.EIO, "boom")

        monkeypatch.setattr(tasks_module.shutil, "move", _raise_only)
        with pytest.raises(OSError, match="boom"):
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)

        assert stale.read_text(encoding="utf-8") == "pre-existing stale file\n"


def _can_symlink(tmp_path: Path) -> bool:
    try:
        (tmp_path / "_probe_target").write_text("x", encoding="utf-8")
        os.symlink(tmp_path / "_probe_target", tmp_path / "_probe_link")
        return True
    except (OSError, NotImplementedError):
        return False


def _copy_symlinks_then_delete_entries_then_raise(src, dst, *a, **k):
    """shutil.move's real fallback copies with symlinks=True; then the source
    delete removes a directory, a file symlink and a dangling symlink and dies."""
    src, dst = Path(src), Path(dst)
    shutil.copytree(src, dst, symlinks=True)
    shutil.rmtree(src / "emptydir")
    (src / "filelink").unlink()
    (src / "dangling").unlink()
    raise OSError(errno.EIO, "simulated delete failure")


class TestSymlinkAndEmptyDirRecovery:
    def _setup(self, tmp_path, monkeypatch):
        if not _can_symlink(tmp_path):
            pytest.skip("symlinks unavailable on this host")
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        parent = add_task(config, "mv143", "Parent")
        add_subtask(config, "mv143", parent.id, "Child")
        src = tasks_dir / parent.id
        (src / "emptydir").mkdir()
        (src / "target.txt").write_text("t\n", encoding="utf-8")
        os.symlink("target.txt", src / "filelink")
        os.symlink("no-such-target", src / "dangling")
        return tasks_dir, config, parent, src

    def test_symlinks_and_empty_dirs_are_restored_as_themselves(
        self, tmp_path, monkeypatch
    ):
        tasks_dir, config, parent, src = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(
            tasks_module.shutil, "move", _copy_symlinks_then_delete_entries_then_raise
        )
        with pytest.raises(OSError, match="simulated delete failure"):
            change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        assert (src / "emptydir").is_dir()
        assert (src / "filelink").is_symlink()
        assert os.readlink(src / "filelink") == "target.txt"
        assert (src / "dangling").is_symlink()
        assert os.readlink(src / "dangling") == "no-such-target"
        assert not (tasks_dir / "done" / parent.id).exists()

    def test_destination_kept_when_an_entry_cannot_be_restored(
        self, tmp_path, monkeypatch
    ):
        tasks_dir, config, parent, src = self._setup(tmp_path, monkeypatch)
        def _no_symlink(*a, **k):
            raise OSError(errno.EPERM, "no symlink privilege")

        def _move_then_lose_symlink_privilege(src_, dst, *a, **k):
            try:
                _copy_symlinks_then_delete_entries_then_raise(src_, dst)
            finally:
                # Only recovery (not the simulated copy) sees the missing privilege.
                monkeypatch.setattr(tasks_module.os, "symlink", _no_symlink)

        monkeypatch.setattr(tasks_module.shutil, "move", _move_then_lose_symlink_privilege)
        with pytest.raises(RuntimeError, match="reconcile|by hand"):
            change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        dest = tasks_dir / "done" / parent.id
        assert os.path.lexists(dest / "filelink")
        assert os.path.lexists(dest / "dangling")


class TestTransientRetryDoesNotNest:
    @staticmethod
    def _transient():
        err = OSError("transient sharing violation")
        err.winerror = 32  # ERROR_SHARING_VIOLATION: retry_transient retries this
        return err

    def test_transient_after_copy_recovers_before_retry(self, tmp_path, monkeypatch):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        parent = add_task(config, "mv143", "Parent")
        add_subtask(config, "mv143", parent.id, "Child")
        real_move = shutil.move
        calls = {"n": 0}

        def _flaky(src, dst, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                shutil.copytree(src, dst)
                raise self._transient()
            return real_move(src, dst, *a, **k)

        monkeypatch.setattr(tasks_module.shutil, "move", _flaky)
        assert change_task_state(config, "mv143", parent.id, TaskState.DONE, force=True)

        dest = tasks_dir / "done" / parent.id
        assert calls["n"] == 2
        assert (dest / "_task.md").exists()
        assert not (dest / parent.id).exists(), "retry must not nest src inside dst"
        assert not (tasks_dir / parent.id).exists()
        assert [p for p in _copies(tasks_dir, parent.id) if p.is_dir()] == [dest]

    def test_persistent_transient_after_copy_leaves_one_whole_copy(
        self, tmp_path, monkeypatch
    ):
        tasks_dir = _make_portfolio(tmp_path, monkeypatch)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, "mv143", "Leaf")
        src = tasks_dir / f"{task.id}.md"

        def _always(src_, dst, *a, **k):
            shutil.copy2(src_, dst)
            raise self._transient()

        monkeypatch.setattr(tasks_module.shutil, "move", _always)
        with pytest.raises(OSError, match="transient sharing violation"):
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)

        assert _copies(tasks_dir, task.id) == [src]
