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


# --- gate r1: task-reference path escape (P1) ---------------------------------

@pytest.mark.parametrize(
    "ref",
    [
        "../../other/.project/tasks/CLAWP-900",
        "..",
        "/abs/path/CLAWP-900",
        "..\\..\\other\\CLAWP-900",
        "C:/other/CLAWP-900",
        "C:CLAWP-900",
        "-CLAWP-900",
        "",
        "CLAWP-900/../CLAWP-900",
        "CLAWP-900.md",
        "CLAWP-900\n",
    ],
)
def test_bad_task_ref_refused_before_any_io(store, ref):
    config, scope, tasks_dir = store
    # A decoy where a naive join of the reference would land.
    other = tasks_dir.parent.parent / "other" / ".project" / "tasks"
    other.mkdir(parents=True)
    decoy = other / "CLAWP-900.md"
    decoy.write_bytes(BODY_LF.encode())
    mine = _write(tasks_dir, BODY_LF)
    before = (_sha(decoy), _sha(mine))
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", ref, RECORD, scope=scope)
    assert ei.value.code == "bad_task_ref"
    assert (_sha(decoy), _sha(mine)) == before
    assert not list(tasks_dir.glob("*.tmp"))
    assert not (tasks_dir / ".clawpm-tasks.lock").exists()


def _symlink(link: Path, target: Path, *, is_dir: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this host")


def test_symlinked_task_file_pointing_outside_is_refused(store):
    config, scope, tasks_dir = store
    outside = tasks_dir.parent.parent / "outside"
    outside.mkdir()
    target = outside / "CLAWP-901.md"
    target.write_bytes(BODY_LF.encode())
    _symlink(tasks_dir / "CLAWP-901.md", target, is_dir=False)
    before = _sha(target)
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", "CLAWP-901", RECORD, scope=scope)
    assert ei.value.code == "bad_task_ref"
    assert _sha(target) == before
    assert sorted(x.name for x in outside.iterdir()) == ["CLAWP-901.md"]


def test_symlinked_state_dir_pointing_outside_is_refused(store):
    config, scope, tasks_dir = store
    outside = tasks_dir.parent.parent / "outside_done"
    outside.mkdir()
    target = outside / "CLAWP-902.md"
    target.write_bytes(BODY_LF.encode())
    _symlink(tasks_dir / "done", outside, is_dir=True)
    before = _sha(target)
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", "CLAWP-902", RECORD, scope=scope)
    assert ei.value.code == "bad_task_ref"
    assert _sha(target) == before


def test_subtask_refs_still_accepted(store):
    config, scope, tasks_dir = store
    p = tasks_dir / "CLAWP-900-001.md"
    p.write_bytes(BODY_LF.encode())
    assert record_pr_verification(config, "clawp", "CLAWP-900-001", RECORD, scope=scope) == p


# --- gate r1: quoted existing key must be replaced, not duplicated (P2) -------

class _DupLoader(yaml.SafeLoader):
    pass


def _no_dup_mapping(loader, node, deep=False):
    seen = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.YAMLError(f"duplicate key {key!r}")
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep)


_DupLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_dup_mapping)


@pytest.mark.parametrize(
    "existing",
    [
        "pr_verification: {pr: 1}\n",
        '"pr_verification": {pr: 1}\n',
        "'pr_verification': {pr: 1}\n",
        '"pr_verification" : {pr: 1}\n',
        "pr_verification:\n  pr: 1\n  head: x\n",
        '"pr_verification":\n  pr: 1\n',
    ],
)
def test_existing_key_in_any_spelling_ends_with_one_entry(store, existing):
    text = BODY_LF.replace("priority: 3\n", "priority: 3\n" + existing)
    p = _write(store[2], text)
    _record(store)
    body = p.read_bytes().decode("utf-8")
    fm = yaml.load(body.split("---\n", 2)[1], Loader=_DupLoader)  # raises on a duplicate key
    assert fm["pr_verification"] == RECORD
    assert fm["priority"] == 3


def test_unrecognised_spelling_of_the_key_refuses_without_writing(store):
    # Valid YAML for the key (escaped underscore) that no line pattern recognises.
    text = BODY_LF.replace("priority: 3\n", 'priority: 3\n"pr\\x5fverification": {pr: 1}\n')
    p = _write(store[2], text)
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == "bad_frontmatter"
    assert _sha(p) == before


# --- gate r2 class A: containment of the write target -------------------------

def _other_project(tmp_path: Path) -> tuple[Path, Path]:
    other = tmp_path / "other" / ".project" / "tasks"
    other.mkdir(parents=True)
    victim = other / "CLAWP-900.md"
    victim.write_bytes(BODY_LF.encode())
    return other, victim


def test_tasks_dir_resolving_to_another_project_is_refused_by_construction(store, tmp_path):
    # Platform-independent form of the symlinked-tasks-dir finding: the tasks dir the
    # resolver hands back is not <verified project dir>/tasks.
    config, scope, tasks_dir = store
    other, victim = _other_project(tmp_path)
    before = _sha(victim)
    with patch.object(pr_record, "get_tasks_dir", lambda *a, **kw: other):
        with pytest.raises(PrRecordError) as ei:
            record_pr_verification(config, "clawp", "CLAWP-900", RECORD, scope=scope)
    assert ei.value.code == "unsafe_store"
    assert _sha(victim) == before
    assert not (other / ".clawpm-tasks.lock").exists()
    assert not list(other.glob("*.tmp"))


def test_symlinked_tasks_dir_is_refused(store, tmp_path):
    config, scope, tasks_dir = store
    other, victim = _other_project(tmp_path)
    tasks_dir.rmdir()
    _symlink(tasks_dir, other, is_dir=True)
    before = _sha(victim)
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", "CLAWP-900", RECORD, scope=scope)
    assert ei.value.code == "unsafe_store"
    assert _sha(victim) == before
    assert not (other / ".clawpm-tasks.lock").exists()


def test_junctioned_tasks_dir_is_refused(store, tmp_path):
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("junctions are a Windows feature")
    config, scope, tasks_dir = store
    other, victim = _other_project(tmp_path)
    tasks_dir.rmdir()
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tasks_dir), str(other)], capture_output=True
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction on this host")
    before = _sha(victim)
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", "CLAWP-900", RECORD, scope=scope)
    assert ei.value.code == "unsafe_store"
    assert _sha(victim) == before


def test_predictable_tmp_name_is_never_written_through(store):
    # A pre-existing <id>.md.tmp (the old predictable temp name) is left alone.
    config, scope, tasks_dir = store
    p = _write(tasks_dir, BODY_LF)
    squatter = tasks_dir / "CLAWP-900.md.tmp"
    squatter.write_bytes(b"squatter\n")
    _record(store)
    assert squatter.read_bytes() == b"squatter\n"
    assert _fm(p)["pr_verification"] == RECORD


def test_tmp_symlink_to_outside_file_is_not_followed(store, tmp_path):
    config, scope, tasks_dir = store
    p = _write(tasks_dir, BODY_LF)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"precious\n")
    _symlink(tasks_dir / "CLAWP-900.md.tmp", outside, is_dir=False)
    _record(store)
    assert outside.read_bytes() == b"precious\n"
    assert not p.is_symlink()
    assert _fm(p)["pr_verification"] == RECORD


def test_task_file_symlinked_inside_the_store_is_refused(store):
    config, scope, tasks_dir = store
    real = _write(tasks_dir, BODY_LF)
    _symlink(tasks_dir / "CLAWP-903.md", real, is_dir=False)
    before = _sha(real)
    with pytest.raises(PrRecordError) as ei:
        record_pr_verification(config, "clawp", "CLAWP-903", RECORD, scope=scope)
    assert ei.value.code == "bad_task_ref"
    assert _sha(real) == before
    assert (tasks_dir / "CLAWP-903.md").is_symlink()


def test_temp_file_removed_when_replace_fails(store):
    config, scope, tasks_dir = store
    p = _write(tasks_dir, BODY_LF)
    before = _sha(p)

    def failing(fn, *args, **kwargs):
        if getattr(fn, "__name__", "") == "replace":
            raise OSError("disk says no")
        return fn(*args, **kwargs)

    def no_replace(*args, **kwargs):
        raise OSError("disk says no")

    # The path writer replaces through retry_transient(os.replace, ...); the POSIX
    # dir_fd writer calls os.replace(..., src_dir_fd=, dst_dir_fd=) directly, so
    # patch both seams and the failure is exercised on either branch.
    with patch.object(pr_record, "retry_transient", failing), \
            patch.object(pr_record.os, "replace", no_replace):
        with pytest.raises(OSError):
            _record(store)
    assert _sha(p) == before
    assert not list(tasks_dir.glob("*.tmp"))


# --- gate r2 class B: frontmatter structure, not line heuristics --------------

def _load_unique(p: Path) -> dict:
    body = p.read_bytes().decode("utf-8").lstrip("﻿").replace("\r\n", "\n")
    return yaml.load(body.split("---\n", 2)[1], Loader=_DupLoader)  # raises on a duplicate key


def _refuses(store, extra: str, code="bad_frontmatter"):
    text = BODY_LF.replace("priority: 3\n", "priority: 3\n" + extra)
    p = _write(store[2], text)
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == code
    assert _sha(p) == before
    assert not list(store[2].glob("*.tmp"))


def test_multiline_flow_value_is_replaced_whole(store):
    text = BODY_LF.replace("priority: 3\n", "priority: 3\npr_verification: {pr: 1,\npriority: 1}\n")
    p = _write(store[2], text)
    _record(store)
    fm = _load_unique(p)
    assert fm["priority"] == 3
    assert fm["pr_verification"] == RECORD
    assert "priority: 1" not in p.read_bytes().decode("utf-8")


def test_multiline_flow_with_closing_brace_on_own_line(store):
    text = BODY_LF.replace("priority: 3\n", "pr_verification: {\n  pr: 1,\n  head: x\n}\npriority: 3\n")
    p = _write(store[2], text)
    _record(store)
    fm = _load_unique(p)
    assert fm["priority"] == 3 and fm["pr_verification"] == RECORD


def test_multiline_flow_crlf_preserved(store):
    text = BODY_LF.replace("priority: 3\n", "priority: 3\npr_verification: {pr: 1,\npriority: 1}\n")
    p = _write(store[2], text, crlf=True)
    _record(store)
    raw = p.read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n")
    assert _load_unique(p)["priority"] == 3


def test_block_sequence_at_key_indent_is_replaced_whole(store):
    text = BODY_LF.replace("priority: 3\n", "pr_verification:\n- a\n- b\npriority: 3\n")
    p = _write(store[2], text)
    _record(store)
    fm = _load_unique(p)
    assert fm["pr_verification"] == RECORD and fm["priority"] == 3


def test_empty_value_is_replaced(store):
    text = BODY_LF.replace("priority: 3\n", "pr_verification:\npriority: 3\n")
    p = _write(store[2], text)
    _record(store)
    fm = _load_unique(p)
    assert fm["pr_verification"] == RECORD and fm["priority"] == 3


def test_trailing_comment_on_value_line_is_kept(store):
    text = BODY_LF.replace("priority: 3\n", "pr_verification: {pr: 1} # keep me\npriority: 3\n")
    p = _write(store[2], text)
    _record(store)
    assert "# keep me" in p.read_bytes().decode("utf-8")
    assert _load_unique(p)["pr_verification"] == RECORD


def test_escaped_spelling_plus_plain_spelling_refuses(store):
    _refuses(store, '"pr\\x5fverification": {pr: 1}\npr_verification: {pr: 2}\n')


def test_alias_used_as_key_refuses(store):
    _refuses(store, "anchor: &k pr_verification\n*k : {pr: 1}\npr_verification: {pr: 2}\n")


def test_lone_alias_key_refuses(store):
    _refuses(store, "anchor: &k pr_verification\n*k : {pr: 1}\n")


def test_duplicate_plain_keys_refuse(store):
    _refuses(store, "pr_verification: {pr: 1}\npr_verification: {pr: 2}\n")


def test_duplicate_unrelated_key_refuses(store):
    _refuses(store, "priority: 4\n")


def test_anchored_value_refuses(store):
    _refuses(store, "pr_verification: &a {pr: 1}\nalias_user: *a\n")


def test_tagged_value_refuses(store):
    _refuses(store, "pr_verification: !!map {pr: 1}\n")


def test_block_scalar_value_refuses(store):
    _refuses(store, "pr_verification: |\n  text\n  more\n")


def test_nested_block_scalar_in_value_refuses(store):
    _refuses(store, "pr_verification:\n  note: |\n    text\n")


def test_flow_style_root_refuses(store):
    p = _write(store[2], "---\n{id: CLAWP-900, title: x}\n---\nbody\n")
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == "bad_frontmatter"
    assert _sha(p) == before


def test_indented_key_refuses(store):
    p = _write(store[2], "---\n  id: CLAWP-900\n  pr_verification: {pr: 1}\n---\nbody\n")
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == "bad_frontmatter"
    assert _sha(p) == before


def test_unicode_line_separator_in_frontmatter_refuses(store):
    p = _write(store[2], "---\nid: CLAWP-900\ntitle: a b\n---\nbody\n")
    before = _sha(p)
    with pytest.raises(PrRecordError) as ei:
        _record(store)
    assert ei.value.code == "bad_frontmatter"
    assert _sha(p) == before


def test_other_keys_survive_a_splice_unchanged(store):
    text = BODY_LF.replace(
        "priority: 3\n", 'tags: [a, b]\npr_verification: {pr: 1}\nowner: "me"\npriority: 3\n'
    )
    p = _write(store[2], text)
    before = _fm(p)
    _record(store)
    after = _fm(p)
    assert {k: v for k, v in after.items() if k != "pr_verification"} == {
        k: v for k, v in before.items() if k != "pr_verification"
    }


# --- gate r3: directory-swap TOCTOU (verified identity must survive the write) --

def _link_dir(link: Path, target: Path) -> None:
    import subprocess
    import sys

    if sys.platform == "win32":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if made.returncode != 0:
            pytest.skip("cannot create a junction on this host")
    else:
        _symlink(link, target, is_dir=True)


def _swap_tasks_dir(tasks_dir: Path, other: Path) -> Path:
    import os

    moved = tasks_dir.with_name("tasks.orig")
    os.rename(tasks_dir, moved)
    _link_dir(tasks_dir, other)
    return moved


def test_tasks_dir_swapped_before_the_lock_never_writes_the_other_store(store, tmp_path):
    config, scope, tasks_dir = store
    other, victim = _other_project(tmp_path)
    mine = _write(tasks_dir, BODY_LF)
    before = (_sha(victim), _sha(mine))
    moved = []

    @contextmanager
    def swap_then_lock(path, *a, **kw):
        moved.append(_swap_tasks_dir(tasks_dir, other))
        with file_lock(path, *a, **kw):
            yield

    with patch.object(pr_record, "file_lock", swap_then_lock):
        with pytest.raises(PrRecordError) as ei:
            record_pr_verification(config, "clawp", "CLAWP-900", RECORD, scope=scope)
    assert ei.value.code == "unsafe_store"
    assert _sha(victim) == before[0]
    assert _sha(moved[0] / "CLAWP-900.md") == before[1]
    assert not list(other.glob("*.tmp")) and not list(moved[0].glob("*.tmp"))


def test_swap_during_the_write_cannot_redirect_it(store, tmp_path):
    # Swap at the splice step (after the store is pinned / its fd is open).  Windows:
    # the OS refuses the rename while the handle is open.  POSIX: the write still
    # lands in the original directory through its fd.
    config, scope, tasks_dir = store
    other, victim = _other_project(tmp_path)
    mine = _write(tasks_dir, BODY_LF)
    before = _sha(victim)
    real_splice = pr_record._splice
    seen = {}

    def swapping_splice(text, record):
        try:
            seen["moved"] = _swap_tasks_dir(tasks_dir, other)
        except PermissionError:
            seen["blocked"] = True
        return real_splice(text, record)

    with patch.object(pr_record, "_splice", swapping_splice):
        record_pr_verification(config, "clawp", "CLAWP-900", RECORD, scope=scope)
    assert _sha(victim) == before
    landed = mine if seen.get("blocked") else seen["moved"] / "CLAWP-900.md"
    assert _fm(landed)["pr_verification"] == RECORD


def test_identity_mismatch_after_verification_is_refused(store):
    import os
    import types

    config, scope, tasks_dir = store
    _write(tasks_dir, BODY_LF)
    pin = pr_record._PinnedStore(tasks_dir.resolve())
    pin.recheck()
    real = os.lstat

    def fake(path, *a, **kw):
        st = real(path, *a, **kw)
        if os.path.normcase(str(path)) == os.path.normcase(str(pin.path)):
            return types.SimpleNamespace(st_mode=st.st_mode, st_dev=st.st_dev, st_ino=st.st_ino + 1)
        return st

    with patch.object(os, "lstat", fake):
        with pytest.raises(PrRecordError) as ei:
            pin.recheck()
    assert ei.value.code == "unsafe_store"


def test_reparse_point_tasks_dir_is_refused_even_if_identity_matches(store):
    import os
    import types

    config, scope, tasks_dir = store
    real = os.lstat
    # Resolved BEFORE patching: on POSIX Path.resolve() itself calls os.lstat, so
    # resolving inside ``fake`` would re-enter the patch and recurse forever.
    target = os.path.normcase(str(tasks_dir.resolve()))

    def fake(path, *a, **kw):
        st = real(path, *a, **kw)
        if os.path.normcase(str(path)) == target:
            return types.SimpleNamespace(
                st_mode=st.st_mode, st_dev=st.st_dev, st_ino=st.st_ino, st_reparse_tag=0xA0000003
            )
        return st

    with patch.object(os, "lstat", fake):
        with pytest.raises(PrRecordError) as ei:
            pr_record._PinnedStore(tasks_dir.resolve())
    assert ei.value.code == "unsafe_store"


def _fake_os(*, name="posix", supports=(), **attrs):
    import types

    return types.SimpleNamespace(
        name=name,
        supports_dir_fd=set(supports),
        open=object(), stat=object(), unlink=object(), rename=object(), replace=object(),
        **attrs,
    )


def test_dirfd_branch_selection_follows_the_os_capabilities():
    # Real CPython/Linux lists os.rename (renameat) in supports_dir_fd and never
    # os.replace; the fd-relative writer must still be selected there.
    linux = _fake_os(O_DIRECTORY=1, O_NOFOLLOW=1)
    linux.supports_dir_fd = {linux.open, linux.stat, linux.unlink, linux.rename}
    assert pr_record._dirfd_supported(linux) is True
    assert linux.replace not in linux.supports_dir_fd

    missing_rename = _fake_os(O_DIRECTORY=1, O_NOFOLLOW=1)
    missing_rename.supports_dir_fd = {missing_rename.open, missing_rename.stat, missing_rename.unlink}
    assert pr_record._dirfd_supported(missing_rename) is False

    assert pr_record._dirfd_supported(_fake_os(name="nt", O_DIRECTORY=1, O_NOFOLLOW=1)) is False
    no_nofollow = _fake_os(O_DIRECTORY=1)
    no_nofollow.supports_dir_fd = {no_nofollow.open, no_nofollow.stat, no_nofollow.unlink, no_nofollow.rename}
    assert pr_record._dirfd_supported(no_nofollow) is False


@pytest.mark.parametrize("supported", [True, False])
def test_record_selects_writer_by_dirfd_support_at_call_time(store, supported):
    import os

    config, scope, tasks_dir = store
    p = _write(tasks_dir, BODY_LF)
    called = []
    devnull = []

    def fake_open_dirfd(self):
        devnull.append(os.open(os.devnull, os.O_RDONLY))
        return devnull[-1]

    def fake_dirfd_writer(pin, dirfd, task_id, project_id, record):
        called.append("dirfd")
        return p

    def fake_path_writer(pin, task_id, project_id, record):
        called.append("path")
        return p

    with patch.object(pr_record, "_dirfd_supported", lambda *a: supported), \
            patch.object(pr_record._PinnedStore, "open_dirfd", fake_open_dirfd), \
            patch.object(pr_record, "_write_via_dirfd", fake_dirfd_writer), \
            patch.object(pr_record, "_write_via_path", fake_path_writer):
        assert _record(store) == p
    assert called == (["dirfd"] if supported else ["path"])
    assert bool(devnull) is supported


def test_os_replace_onto_a_symlink_replaces_the_link_not_its_target(tmp_path):
    # Regression guard for gate r4 P1 (claimed: os.replace(tmp, link) writes at the
    # link's target, outside the store).  os.replace has rename semantics (POSIX
    # rename(2), Windows MoveFileEx): the destination ENTRY is replaced, a symlink
    # is not followed.  So a task file swapped for a link just before the replace
    # loses the link; the external file is untouched.  Probed on Windows 11 / py3.12.
    import os

    outside = tmp_path / "outside.md"
    outside.write_bytes(b"EXTERNAL SENTINEL\n")
    link = tmp_path / "task.md"
    _symlink(link, outside, is_dir=False)
    tmp = tmp_path / "task.md.tmp"
    tmp.write_bytes(b"NEW CONTENT\n")
    os.replace(tmp, link)
    assert outside.read_bytes() == b"EXTERNAL SENTINEL\n"
    assert not link.is_symlink()
    assert link.read_bytes() == b"NEW CONTENT\n"


def test_task_file_replaced_mid_write_is_not_clobbered(store):
    import os

    config, scope, tasks_dir = store
    p = _write(tasks_dir, BODY_LF)
    real_splice = pr_record._splice
    interloper = b"---" + bytes([10]) + b"id: CLAWP-900" + bytes([10]) + b"title: edited elsewhere" + bytes([10]) + b"---" + bytes([10])

    def swap_file(text, record):
        side = tasks_dir / "side.tmpfile"
        side.write_bytes(interloper)
        os.replace(side, p)
        return real_splice(text, record)

    with patch.object(pr_record, "_splice", swap_file):
        with pytest.raises(PrRecordError) as ei:
            _record(store)
    assert ei.value.code == "bad_task_ref"
    assert p.read_bytes() == interloper
    assert not list(tasks_dir.glob("*.tmp"))
