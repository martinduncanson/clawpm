"""CLAWP-147-003 (G4): `clawpm sessions write-root` exposes the dispatched
worker's write root, derived from the dispatch marker, for a harness fence hook.

The root is the nearest ancestor of cwd (inclusive) that carries a dispatch
marker in `.claude/settings.local.json`. Exit codes: 0 root found, 1 no
dispatched worktree (no fence), 3 marker present but unusable (no unambiguous
root).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.dispatch import CLAWPM_MARKER_KEY
from clawpm.sessions import resolve_write_root


def _write_marker(wt: Path, body: bytes | None = None, **marker) -> None:
    settings = wt / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    if body is None:
        marker.setdefault("task_id", "CLAWP-1")
        marker.setdefault("project_id", "test")
        body = json.dumps({CLAWPM_MARKER_KEY: marker}, indent=2).encode("utf-8")
    settings.write_bytes(body)


def _run(cwd: Path, *extra: str):
    return CliRunner().invoke(main, ["sessions", "write-root", "--cwd", str(cwd), *extra])


def test_cli_prints_root_json_from_worktree_root(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt)
    res = _run(wt)
    assert res.exit_code == 0, res.output
    out = json.loads(res.stdout)
    assert out["ok"] is True
    assert Path(out["root"]) == wt.resolve()
    assert Path(out["root"]).is_absolute()
    assert out["task_id"] == "CLAWP-1"
    assert out["project_id"] == "test"


def test_cli_from_nested_subdir_reports_worktree_root(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt)
    deep = wt / "src" / "pkg"
    deep.mkdir(parents=True)
    out = json.loads(_run(deep).stdout)
    assert out["ok"] is True
    assert Path(out["root"]) == wt.resolve()


def test_cli_outside_any_dispatched_worktree_is_no_fence(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = _run(plain)
    assert res.exit_code == 1
    out = json.loads(res.stdout)
    assert out["ok"] is False
    assert out["reason"] == "no_dispatch_marker"
    assert "root" not in out


def test_settings_without_clawpm_block_is_no_fence(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt, body=b'{"permissions": {}}')
    res = _run(wt)
    assert res.exit_code == 1
    assert json.loads(res.stdout)["reason"] == "no_dispatch_marker"


def test_nearest_marker_wins(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    _write_marker(outer, task_id="CLAWP-OUTER")
    _write_marker(inner, task_id="CLAWP-INNER")
    out = json.loads(_run(inner).stdout)
    assert Path(out["root"]) == inner.resolve()
    assert out["task_id"] == "CLAWP-INNER"


def test_crlf_marker_file_is_read(tmp_path):
    wt = tmp_path / "wt"
    body = json.dumps(
        {CLAWPM_MARKER_KEY: {"task_id": "CLAWP-9", "project_id": "test"}}, indent=2
    ).replace("\n", "\r\n").encode("utf-8")
    assert b"\r\n" in body
    _write_marker(wt, body=body)
    out = json.loads(_run(wt).stdout)
    assert out["ok"] is True
    assert out["task_id"] == "CLAWP-9"


@pytest.mark.parametrize(
    "body",
    [b"{not json", b"[1, 2]", json.dumps({CLAWPM_MARKER_KEY: "x"}).encode()],
    ids=["malformed", "non-object", "marker-not-object"],
)
def test_damaged_marker_is_not_an_unambiguous_root(tmp_path, body):
    wt = tmp_path / "wt"
    _write_marker(wt, body=body)
    res = _run(wt)
    assert res.exit_code == 3
    out = json.loads(res.stdout)
    assert out["ok"] is False
    assert out["reason"] == "marker_unusable"
    assert out["detail"]


def test_damaged_marker_does_not_fall_through_to_outer_root(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    _write_marker(outer)
    _write_marker(inner, body=b"{not json")
    res = _run(inner)
    assert res.exit_code == 3
    assert "root" not in json.loads(res.stdout)


@pytest.mark.parametrize("task_id", [None, "", 7])
def test_marker_without_usable_task_id_is_unusable(tmp_path, task_id):
    wt = tmp_path / "wt"
    _write_marker(wt, task_id=task_id)
    res = _run(wt)
    assert res.exit_code == 3
    assert json.loads(res.stdout)["reason"] == "marker_unusable"


@pytest.mark.parametrize(
    "marker",
    [
        {"task_id": "CLAWP-1", "version": 1},
        {"task_id": "CLAWP-1", "project_id": None},
        {"task_id": "CLAWP-1", "project_id": 7},
        {"task_id": "CLAWP-1", "project_id": ""},
    ],
    ids=["missing", "null", "non-string", "empty"],
)
def test_marker_without_usable_project_id_is_unusable(tmp_path, marker):
    # The dispatch writer always writes both ids, so a marker lacking a
    # non-empty string project_id is damaged, not a root a fence can trust.
    outer = tmp_path / "outer"
    inner = outer / "inner"
    _write_marker(outer)
    _write_marker(inner, body=json.dumps({CLAWPM_MARKER_KEY: marker}).encode("utf-8"))
    direct = resolve_write_root(inner)
    assert direct["ok"] is False
    assert direct["reason"] == "marker_unusable"
    assert "project_id" in direct["detail"]
    assert "root" not in direct  # no fall-through to the outer root
    res = _run(inner)
    assert res.exit_code == 3
    out = json.loads(res.stdout)
    assert out["reason"] == "marker_unusable"
    assert "project_id" in out["detail"]
    assert "root" not in out


def _marker_file(marker) -> bytes:
    return json.dumps({CLAWPM_MARKER_KEY: marker}).encode("utf-8")


_GOOD = {"task_id": "CLAWP-1", "project_id": "test"}

# Every shape an inner settings file can take, with a VALID outer marker above
# it. "outer" shapes are ordinary "no marker here, keep walking"; "unusable"
# shapes are damaged and must stop the walk (exit 3, never the outer root).
_INNER_SHAPES = [
    # (id, body, expected) ; body None = no settings file at all
    ("file-missing", None, "outer"),
    ("key-absent", b'{"permissions": {}}', "outer"),
    ("empty-file", b"", "unusable"),
    ("whitespace-file", b"  \r\n", "unusable"),
    ("bom-prefixed", b"\xef\xbb\xbf" + _marker_file(_GOOD), "unusable"),
    ("non-utf8", b"\xff\xfe{\x00", "unusable"),
    ("malformed-json", b"{not json", "unusable"),
    ("top-list", b"[1, 2]", "unusable"),
    ("top-string", b'"x"', "unusable"),
    ("top-null", b"null", "unusable"),
    ("top-int", b"7", "unusable"),
    ("key-null", _marker_file(None), "unusable"),
    ("key-list", _marker_file([_GOOD]), "unusable"),
    ("key-string", _marker_file("x"), "unusable"),
    ("key-int", _marker_file(0), "unusable"),
    ("key-false", _marker_file(False), "unusable"),
    ("key-true", _marker_file(True), "unusable"),
    ("key-empty-object", _marker_file({}), "unusable"),
    ("task-missing", _marker_file({"project_id": "test"}), "unusable"),
    ("task-null", _marker_file({"task_id": None, "project_id": "test"}), "unusable"),
    ("task-int", _marker_file({"task_id": 7, "project_id": "test"}), "unusable"),
    ("task-empty", _marker_file({"task_id": "", "project_id": "test"}), "unusable"),
    ("task-blank", _marker_file({"task_id": "  ", "project_id": "test"}), "unusable"),
    ("project-missing", _marker_file({"task_id": "CLAWP-1"}), "unusable"),
    ("project-null", _marker_file({"task_id": "CLAWP-1", "project_id": None}), "unusable"),
    ("project-int", _marker_file({"task_id": "CLAWP-1", "project_id": 7}), "unusable"),
    ("project-empty", _marker_file({"task_id": "CLAWP-1", "project_id": ""}), "unusable"),
    ("project-blank", _marker_file({"task_id": "CLAWP-1", "project_id": "\t"}), "unusable"),
]


@pytest.mark.parametrize(
    "body,expected",
    [pytest.param(b, e, id=i) for i, b, e in _INNER_SHAPES],
)
def test_inner_settings_shape_never_yields_a_wrong_root(tmp_path, body, expected):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    inner.mkdir(parents=True)
    _write_marker(outer, task_id="CLAWP-OUTER")
    if body is not None:
        _write_marker(inner, body=body)
    direct = resolve_write_root(inner)
    res = _run(inner)
    out = json.loads(res.stdout)
    if expected == "outer":
        assert direct["ok"] is True
        assert Path(direct["root"]) == outer.resolve()
        assert res.exit_code == 0
        assert Path(out["root"]) == outer.resolve()
    else:
        assert direct["ok"] is False
        assert direct["reason"] == "marker_unusable"
        assert "root" not in direct
        assert res.exit_code == 3
        assert out["reason"] == "marker_unusable"
        assert "root" not in out


def test_settings_path_that_is_a_directory_is_unusable(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    _write_marker(outer)
    (inner / ".claude" / "settings.local.json").mkdir(parents=True)
    res = _run(inner)
    assert res.exit_code == 3
    assert json.loads(res.stdout)["reason"] == "marker_unusable"


def test_null_marker_key_alone_is_unusable_not_no_fence(tmp_path):
    # No outer marker either: a present-but-null key is still damaged (3),
    # not "no marker anywhere" (1).
    wt = tmp_path / "wt"
    _write_marker(wt, body=_marker_file(None))
    res = _run(wt)
    assert res.exit_code == 3
    assert "null" in json.loads(res.stdout)["detail"]


def test_dotdot_segments_resolve_to_same_root(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt)
    (wt / "a").mkdir()
    weird = wt / "a" / ".." / "a"
    assert Path(resolve_write_root(weird)["root"]) == wt.resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows path spelling")
def test_windows_path_spellings_agree(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt)
    sub = wt / "sub"
    sub.mkdir()
    expected = resolve_write_root(sub)["root"]
    swapped = str(sub).replace("\\", "/")
    upper = str(sub).upper()
    for spelling in (swapped, upper):
        got = resolve_write_root(Path(spelling))
        assert got["ok"] is True
        assert os.path.normcase(got["root"]) == os.path.normcase(expected)
    # Backslash form in the JSON output, absolute with a drive.
    out = json.loads(_run(Path(swapped)).stdout)
    assert Path(out["root"]).drive


def test_text_format_prints_just_the_root(tmp_path):
    wt = tmp_path / "wt"
    _write_marker(wt)
    res = CliRunner().invoke(
        main, ["--format", "text", "sessions", "write-root", "--cwd", str(wt)]
    )
    assert res.exit_code == 0
    assert Path(res.stdout.strip()) == wt.resolve()


def test_defaults_to_process_cwd(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    _write_marker(wt)
    monkeypatch.chdir(wt)
    res = CliRunner().invoke(main, ["sessions", "write-root"])
    assert res.exit_code == 0
    assert Path(json.loads(res.stdout)["root"]) == wt.resolve()
