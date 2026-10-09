"""Task moves are atomic renames and never leave two copies (CLAWP-143).

``shutil.move`` falls back to copy-then-delete, which can leave a task in both
its old and new state. Moves now use one ``os.rename``: it either happens or
the source is untouched. An existing destination is refused.
"""

from __future__ import annotations

import errno
import os
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


def _copies(tasks_dir: Path, task_id: str) -> list[Path]:
    return [
        p for p in tasks_dir.rglob("*")
        if p.name in (f"{task_id}.md", f"{task_id}.progress.md", task_id)
    ]


def _fail_rename(exc: OSError):
    def _rename(src, dst, *a, **k):
        raise exc
    return _rename


def _form(tmp_path, monkeypatch, form):
    tasks_dir = _make_portfolio(tmp_path, monkeypatch)
    config = load_portfolio_config(tmp_path)
    task = add_task(config, "mv143", "Task")
    if form == "dir":
        add_subtask(config, "mv143", task.id, "Child")
        src = tasks_dir / task.id
    else:
        src = tasks_dir / f"{task.id}.md"
    assert src.exists()
    return tasks_dir, config, task, src


@pytest.mark.parametrize("form", ["file", "dir"])
class TestAtomicMove:
    def test_failed_rename_leaves_source_intact(self, tmp_path, monkeypatch, form):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, form)
        monkeypatch.setattr(
            tasks_module.os, "rename", _fail_rename(OSError(errno.EIO, "disk on fire"))
        )
        with pytest.raises(OSError, match="disk on fire"):
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        assert _copies(tasks_dir, task.id) == [src]
        assert not (tasks_dir / "done" / src.name).exists()

    def test_cross_device_raises_loudly_with_one_copy(self, tmp_path, monkeypatch, form):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, form)
        monkeypatch.setattr(
            tasks_module.os, "rename",
            _fail_rename(OSError(errno.EXDEV, "cross-device link")),
        )
        with pytest.raises(OSError) as ei:
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        assert any("source is untouched" in n for n in ei.value.__notes__)
        assert _copies(tasks_dir, task.id) == [src]

    def test_existing_destination_is_refused(self, tmp_path, monkeypatch, form):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, form)
        stale = tasks_dir / "done" / src.name
        if form == "dir":
            stale.mkdir()
            (stale / "stale.txt").write_text("keep\n", encoding="utf-8")
        else:
            stale.write_text("keep\n", encoding="utf-8")
        with pytest.raises(FileExistsError, match="already exists"):
            change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        assert src.exists()
        if form == "dir":
            assert not (stale / src.name).exists(), "must not nest"
            assert (stale / "stale.txt").read_text(encoding="utf-8") == "keep\n"
        else:
            assert stale.read_text(encoding="utf-8") == "keep\n"

    def test_transient_sharing_violation_retries_and_converges(
        self, tmp_path, monkeypatch, form
    ):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, form)
        real = os.rename
        calls = {"n": 0}

        def _flaky(s, d, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                err = OSError("transient sharing violation")
                err.winerror = 32  # ERROR_SHARING_VIOLATION: retried
                raise err
            return real(s, d, *a, **k)

        monkeypatch.setattr(tasks_module.os, "rename", _flaky)
        assert change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        assert calls["n"] == 2
        dest = tasks_dir / "done" / src.name
        assert dest.exists() and not src.exists()
        assert not (dest / src.name).exists(), "retry must not nest"


class TestOtherSites:
    def test_archive_failed_rename_leaves_source(self, tmp_path, monkeypatch):
        tasks_dir, config, task, _ = _form(tmp_path, monkeypatch, "file")
        assert change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        done_file = tasks_dir / "done" / f"{task.id}.md"
        os.utime(done_file, (1_000_000, 1_000_000))
        monkeypatch.setattr(
            tasks_module.os, "rename", _fail_rename(OSError(errno.EIO, "boom"))
        )
        with pytest.raises(OSError, match="boom"):
            archive_done_tasks(config, "mv143", older_than_days=1)
        assert done_file.exists()
        assert not (tasks_dir / "done" / "archive" / done_file.name).exists()

    def test_archive_dir_form_moves_atomically(self, tmp_path, monkeypatch):
        tasks_dir, config, task, _ = _form(tmp_path, monkeypatch, "dir")
        assert change_task_state(config, "mv143", task.id, TaskState.DONE, force=True)
        done_dir = tasks_dir / "done" / task.id
        os.utime(done_dir, (1_000_000, 1_000_000))
        for p in done_dir.rglob("*"):
            os.utime(p, (1_000_000, 1_000_000))
        archive_done_tasks(config, "mv143", older_than_days=1)
        assert len([p for p in _copies(tasks_dir, task.id) if p.is_dir()]) == 1

    def test_split_failed_rename_leaves_leaf(self, tmp_path, monkeypatch):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, "file")
        monkeypatch.setattr(
            tasks_module.os, "rename", _fail_rename(OSError(errno.EIO, "boom"))
        )
        with pytest.raises(OSError, match="boom"):
            split_task(config, "mv143", task.id)
        assert src.exists()
        assert not (tasks_dir / task.id / "_task.md").exists()

    def test_split_converges_file_form(self, tmp_path, monkeypatch):
        tasks_dir, config, task, src = _form(tmp_path, monkeypatch, "file")
        assert split_task(config, "mv143", task.id)
        assert (tasks_dir / task.id / "_task.md").exists()
        assert not src.exists()
