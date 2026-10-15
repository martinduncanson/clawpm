"""CLAWP-151 part 1 -- locked surgical ``pr_verification`` task-record writer."""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from clawpm import pr_record
from clawpm.concurrency import file_lock
from clawpm.models import PortfolioConfig, ProjectStatus
from clawpm.pr_record import PrRecordError, record_pr_verification
from clawpm.sessions import Scope

SHA = "a" * 40
MERGE = "c" * 40
RECORD = {
    "pr": 7,
    "head": SHA,
    "merge_commit": MERGE,
    "verified_at": "2026-10-15T10:00:00+00:00",
    "remote": "fork",
    "repo": "acme/widgets",
}
BODY_LF = (
    "---\n"
    "id: CLAWP-900\n"
    "title: Sample task\n"
    "# keep this comment\n"
    "priority: 3\n"
    "---\n"
    "\n"
    "Body text.\n"
)


@pytest.fixture
def store(tmp_path):
    project_dir = tmp_path / ".project"
    tasks_dir = project_dir / "tasks"
    tasks_dir.mkdir(parents=True)
    config = PortfolioConfig(
        portfolio_root=tmp_path, project_roots=[tmp_path], default_status=ProjectStatus.ACTIVE
    )
    return config, Scope(pinned_project_dir=project_dir), tasks_dir


def _write(tasks_dir: Path, text: str, *, crlf=False, bom=False) -> Path:
    if crlf:
        text = text.replace("\n", "\r\n")
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    p = tasks_dir / "CLAWP-900.md"
    p.write_bytes(data)
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _record(store, task_id="CLAWP-900", record=None):
    config, scope, _ = store
    return record_pr_verification(config, "clawp", task_id, record or RECORD, scope=scope)


def _fm(p: Path) -> dict:
    text = p.read_bytes().decode("utf-8").lstrip("﻿").replace("\r\n", "\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_success_splices_one_line_before_closing_fence(store):
    p = _write(store[2], BODY_LF)
    assert _record(store) == p
    out = p.read_bytes().decode("utf-8")
    assert out.count("pr_verification:") == 1
    assert _fm(p)["pr_verification"] == RECORD
    # Everything except the single inserted line is byte-identical.
    removed = [ln for ln in out.split("\n") if ln.startswith("pr_verification:")]
    assert len(removed) == 1
    assert out.replace(removed[0] + "\n", "", 1) == BODY_LF


def test_rerun_replaces_not_duplicates(store):
    p = _write(store[2], BODY_LF)
    _record(store)
    newer = dict(RECORD, pr=8, verified_at="2026-10-16T00:00:00+00:00")
    _record(store, record=newer)
    out = p.read_bytes().decode("utf-8")
    assert out.count("pr_verification:") == 1
    assert _fm(p)["pr_verification"] == newer


def test_replaces_existing_key_with_indented_continuation(store):
    text = BODY_LF.replace(
        "priority: 3\n", "pr_verification:\n  pr: 1\n  head: old\npriority: 3\n"
    )
    p = _write(store[2], text)
    _record(store)
    out = p.read_bytes().decode("utf-8")
    assert "head: old" not in out and out.count("pr_verification:") == 1
    assert _fm(p)["pr_verification"] == RECORD
    assert _fm(p)["priority"] == 3


def test_bom_file_preserved(store):
    p = _write(store[2], BODY_LF, bom=True)
    _record(store)
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf---\n")
    assert raw.count(b"\xef\xbb\xbf") == 1
    assert _fm(p)["pr_verification"] == RECORD


def test_crlf_file_preserved(store):
    p = _write(store[2], BODY_LF, crlf=True)
    _record(store)
    raw = p.read_bytes()
    # No bare LF anywhere: every newline is still CRLF.
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert _fm(p)["pr_verification"] == RECORD
    stripped = b"\r\n".join(
        ln for ln in raw.split(b"\r\n") if not ln.startswith(b"pr_verification:")
    )
    assert stripped == BODY_LF.replace("\n", "\r\n").encode("utf-8")


def test_lf_file_has_no_carriage_returns(store):
    p = _write(store[2], BODY_LF)
    _record(store)
    assert b"\r" not in p.read_bytes()


@pytest.mark.parametrize(
    "text,code",
    [
        ("Just a body, no fence.\n", "no_frontmatter"),
        ("---\nid: X\ntitle: unterminated\n", "no_frontmatter"),
        ("---\n- a\n- b\n---\nbody\n", "bad_frontmatter"),
        ("---\njust a scalar\n---\nbody\n", "bad_frontmatter"),
        ("---\nid: [unclosed\n---\nbody\n", "bad_frontmatter"),
    ],
)
def test_refusals_leave_file_bytes_identical(store, text, code):
    p = _write(store[2], text)
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == code
    assert _sha(p) == before
    assert not list(store[2].glob("*.tmp"))


def test_missing_task_refuses_without_writing(store):
    p = _write(store[2], BODY_LF)
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store, task_id="CLAWP-999")
    assert ei.value.code == "task_not_found"
    assert _sha(p) == before


def test_bad_record_keys_refuse(store):
    p = _write(store[2], BODY_LF)
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store, record={"pr": 7})
    assert ei.value.code == "bad_record"
    assert _sha(p) == before


def test_write_holds_the_project_tasks_lock(store):
    _write(store[2], BODY_LF)
    taken = []

    @contextmanager
    def spy(path, *a, **kw):
        taken.append(Path(path))
        with file_lock(path, *a, **kw):
            yield

    with patch.object(pr_record, "file_lock", spy):
        _record(store)
    assert taken == [store[2] / ".clawpm-tasks.lock"]
