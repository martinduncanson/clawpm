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


# Files with a BOM but no frontmatter: the synthesise-frontmatter write path must
# not carry the peeled BOM into the body (CLAWP-144 Codex r2): the invariant is
# zero or one leading BOM after any write and a body that never starts with one.
_SYNTH_BODIES = {
    "heading": b"# Real task\n",
    "heading-crlf": b"# Real task\r\n",
    "bom-only": b"",
}


@pytest.fixture(params=sorted(_SYNTH_BODIES))
def bom_nofm_task(request, isolated_portfolio):
    task = add_task(
        isolated_portfolio.config, isolated_portfolio.project_id, "Bom synth",
    )
    assert task is not None and task.file_path is not None
    task.file_path.write_bytes(BOM + _SYNTH_BODIES[request.param])
    return isolated_portfolio, task, request.param


def _assert_one_clean_bom(path, expect_title):
    raw = path.read_bytes()
    assert raw.count(BOM) <= 1, "BOM duplicated by the write"
    text = raw.decode("utf-8").removeprefix("\ufeff")
    assert "\ufeff" not in text, "body carries a stray BOM"
    if expect_title is not None:
        assert Task.from_file(path).title == expect_title
    return raw


class TestSynthesisedFrontmatterBom:
    def _expected_title(self, kind):
        return "Real task" if kind.startswith("heading") else None

    def test_edit_fog_synthesise_keeps_single_bom(self, bom_nofm_task):
        from clawpm.tasks import edit_fog

        pf, task, kind = bom_nofm_task
        assert edit_fog(pf.config, pf.project_id, task.id, add="x") is not None
        raw = _assert_one_clean_bom(task.file_path, self._expected_title(kind))
        assert raw.startswith(BOM + b"---")
        # Second write is idempotent on the BOM / body shape.
        assert edit_fog(pf.config, pf.project_id, task.id, add="x") is not None
        raw2 = _assert_one_clean_bom(task.file_path, self._expected_title(kind))
        assert raw2.startswith(BOM + b"---")
        assert raw2.split(b"---", 2)[2] == raw.split(b"---", 2)[2]

    def test_edit_task_synthesise_drops_peeled_bom_from_body(self, bom_nofm_task):
        pf, task, kind = bom_nofm_task
        assert edit_task(pf.config, pf.project_id, task.id, priority=3) is not None
        _assert_one_clean_bom(task.file_path, self._expected_title(kind))
        assert edit_task(pf.config, pf.project_id, task.id, priority=3) is not None
        _assert_one_clean_bom(task.file_path, self._expected_title(kind))

    def test_doctor_state_rewrite_synthesise_drops_peeled_bom(self, bom_nofm_task):
        from clawpm.doctor_apply import _rewrite_frontmatter_state

        pf, task, kind = bom_nofm_task
        _rewrite_frontmatter_state(task.file_path, "open")
        _assert_one_clean_bom(task.file_path, self._expected_title(kind))

    def test_read_path_title_ignores_bom_without_frontmatter(self, bom_nofm_task):
        pf, task, kind = bom_nofm_task
        expect = self._expected_title(kind)
        if expect is not None:
            assert Task.from_file(task.file_path).title == expect
