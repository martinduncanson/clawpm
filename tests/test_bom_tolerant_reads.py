"""CLAWP-144: a UTF-8 BOM (and CRLF) must not make task frontmatter look absent.

The BOM hid the leading fence from ``split_frontmatter`` / ``parse_frontmatter``
and from the surgical ``updated:`` / ``state:`` line writers, so a BOM-prefixed
task loaded with empty metadata. Read path is BOM-tolerant at the source.
"""

from __future__ import annotations

import pytest

from clawpm.frontmatter import FrontmatterError, parse_frontmatter, split_frontmatter
from clawpm.models import Task, TaskState
from clawpm.tasks import (
    add_task,
    change_task_state,
    edit_task,
    get_task,
    list_tasks,
    _sync_state_line,
)

BOM = b"\xef\xbb\xbf"


class TestFrontmatterParsers:
    def test_split_frontmatter_tolerates_bom(self):
        data, body = split_frontmatter("﻿---\nid: T-1\n---\n# Hi\n")
        assert data == {"id": "T-1"}
        assert body == "\n# Hi\n"

    def test_parse_frontmatter_tolerates_bom(self):
        data, body = parse_frontmatter("﻿---\nid: T-1\n---\nbody\n")
        assert data == {"id": "T-1"}
        assert body == "\nbody\n"

    def test_bom_without_fence_is_still_absent(self):
        with pytest.raises(FrontmatterError) as ei:
            split_frontmatter("﻿just text\n")
        assert ei.value.reason == "absent"


@pytest.fixture(params=["\n", "\r\n"], ids=["lf", "crlf"])
def bom_task(request, isolated_portfolio):
    """A task with real metadata whose file is rewritten as BOM (+ CRLF)."""
    task = add_task(
        isolated_portfolio.config, isolated_portfolio.project_id, "Bom task",
        priority=2, scope=["src/**"], tags=["alpha"],
    )
    assert task is not None and task.file_path is not None
    clean = task.file_path.read_bytes().decode("utf-8").replace("\r\n", "\n")
    task.file_path.write_bytes(
        BOM + clean.replace("\n", request.param).encode("utf-8")
    )
    return isolated_portfolio, task


class TestBomRoundTrip:
    def test_from_file_reads_metadata(self, bom_task):
        pf, task = bom_task
        loaded = Task.from_file(task.file_path)
        assert loaded.id == task.id
        assert loaded.priority == 2
        assert loaded.title == "Bom task"

    def test_list_and_show_see_metadata(self, bom_task):
        pf, task = bom_task
        listed = [t for t in list_tasks(pf.config, pf.project_id) if t.id == task.id]
        assert len(listed) == 1 and listed[0].priority == 2
        shown = get_task(pf.config, pf.project_id, task.id)
        assert shown is not None and shown.priority == 2

    def test_edit_keeps_metadata(self, bom_task):
        pf, task = bom_task
        updated = edit_task(pf.config, pf.project_id, task.id, priority=4)
        assert updated is not None
        assert updated.priority == 4
        assert updated.id == task.id
        assert updated.scope == ["src/**"]
        assert updated.tags == ["alpha"]

    def test_state_change_stamps_updated_and_syncs_state(self, bom_task):
        pf, task = bom_task
        # Pin a stale stamp and a state line, preserving BOM + line endings.
        raw = task.file_path.read_bytes().decode("utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        raw = raw.replace("created: '", f"updated: '2000-01-01'{nl}created: '", 1)
        task.file_path.write_bytes(raw.encode("utf-8"))

        moved = change_task_state(pf.config, pf.project_id, task.id, TaskState.PROGRESS)
        assert moved is not None and moved.file_path is not None

        out = moved.file_path.read_bytes()
        assert out.startswith(BOM + b"---")
        text = out[3:].decode("utf-8")
        fm, _ = split_frontmatter(text.replace("\r\n", "\n"))
        assert fm["updated"] != "2000-01-01"
        if nl == "\r\n":
            assert "\n" not in text.replace("\r\n", "")
        assert Task.from_file(moved.file_path).priority == 2

    def test_sync_state_line_rewrites_and_keeps_bom(self, bom_task):
        pf, task = bom_task
        raw = task.file_path.read_bytes().decode("utf-8")
        nl = "\r\n" if "\r\n" in raw else "\n"
        raw = raw.replace(f"---{nl}", f"---{nl}state: progress{nl}", 1)
        task.file_path.write_bytes(raw.encode("utf-8"))

        assert _sync_state_line(task.file_path, "open") is True

        out = task.file_path.read_bytes()
        assert out.startswith(BOM + b"---")
        text = out[3:].decode("utf-8")
        assert split_frontmatter(text.replace("\r\n", "\n"))[0]["state"] == "open"
        if nl == "\r\n":
            assert "\n" not in text.replace("\r\n", "")
