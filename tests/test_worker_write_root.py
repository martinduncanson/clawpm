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


# Fail-closed safety net: the CLI contract is exit 1 = "nothing to fence" and
# exit 3 = "marker unusable". An unhandled exception exits 1 with a traceback,
# which a hook cannot tell apart from "no fence", so ANY unexpected error while
# resolving must surface as marker_unusable / exit 3.


def _assert_unusable_cli(res, needle: str) -> None:
    assert res.exit_code == 3, (res.exit_code, res.output, res.exception)
    out = json.loads(res.stdout)
    assert out["ok"] is False
    assert out["reason"] == "marker_unusable"
    assert needle in out["detail"]
    assert "root" not in out


def test_resolve_runtime_error_symlink_loop_is_unusable(tmp_path, monkeypatch):
    # Python 3.11 raises RuntimeError (not OSError) from Path.resolve() on a
    # symlink loop.
    wt = tmp_path / "wt"
    _write_marker(wt)

    def boom(self, *a, **kw):
        raise RuntimeError("Symlink loop from 'x'")

    monkeypatch.setattr(Path, "resolve", boom)
    direct = resolve_write_root(wt)
    assert direct["ok"] is False
    assert direct["reason"] == "marker_unusable"
    assert "RuntimeError" in direct["detail"]
    _assert_unusable_cli(_run(wt), "RuntimeError")


def test_deeply_nested_json_marker_is_unusable(tmp_path):
    wt = tmp_path / "wt"
    depth = 200000
    body = (
        b'{"' + CLAWPM_MARKER_KEY.encode() + b'": '
        + b"[" * depth + b"]" * depth + b"}"
    )
    _write_marker(wt, body=body)
    direct = resolve_write_root(wt)
    assert direct["ok"] is False
    assert direct["reason"] == "marker_unusable"
    assert "root" not in direct
    res = _run(wt)
    assert res.exit_code == 3, (res.exit_code, res.output, res.exception)
    assert json.loads(res.stdout)["reason"] == "marker_unusable"


def test_json_recursion_error_is_unusable(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    _write_marker(wt)

    def boom(*a, **kw):
        raise RecursionError("maximum recursion depth exceeded")

    # Scoped: the test itself parses the CLI output with json.loads afterwards.
    with monkeypatch.context() as m:
        m.setattr(json, "loads", boom)
        direct = resolve_write_root(wt)
        res = _run(wt)
    assert direct["ok"] is False
    assert direct["reason"] == "marker_unusable"
    assert "RecursionError" in direct["detail"]
    _assert_unusable_cli(res, "RecursionError")


def test_error_in_an_outer_directory_does_not_become_no_fence(tmp_path, monkeypatch):
    # The walk must complete cleanly for exit 1: an exception mid-walk is 3.
    import clawpm.dispatch as dispatch

    plain = tmp_path / "plain"
    plain.mkdir()
    real = dispatch.inspect_dispatch_marker
    calls = []

    def flaky(candidate, **kw):
        calls.append(candidate)
        if len(calls) == 2:
            raise RuntimeError("Symlink loop")
        return real(candidate, **kw)

    monkeypatch.setattr(dispatch, "inspect_dispatch_marker", flaky)
    res = _run(plain)
    _assert_unusable_cli(res, "RuntimeError")


@pytest.mark.parametrize(
    "exc", [ValueError("v"), KeyError("k"), AttributeError("a"), RuntimeError("r")]
)
def test_cli_never_exits_1_when_resolver_itself_raises(tmp_path, monkeypatch, exc):
    import clawpm.sessions as sessions_mod

    def boom(cwd):
        raise exc

    monkeypatch.setattr(sessions_mod, "resolve_write_root", boom)
    res = _run(tmp_path)
    _assert_unusable_cli(res, type(exc).__name__)


def test_cli_text_format_when_resolver_raises(tmp_path, monkeypatch):
    import clawpm.sessions as sessions_mod

    def boom(cwd):
        raise AttributeError("a")

    monkeypatch.setattr(sessions_mod, "resolve_write_root", boom)
    res = CliRunner().invoke(
        main, ["--format", "text", "sessions", "write-root", "--cwd", str(tmp_path)]
    )
    assert res.exit_code == 3
    assert "marker_unusable" in res.output


def test_no_marker_anywhere_still_exits_1(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    res = _run(plain)
    assert res.exit_code == 1
    assert json.loads(res.stdout)["reason"] == "no_dispatch_marker"


# Output failures are part of the fail-closed region: a dead pipe must not
# escape as a traceback (exit 1 = "nothing to fence"). If the resolver said
# ok=true but the answer could not be delivered, the result is exit 3. A genuine
# no-marker state stays exit 1 even when its message cannot be written: the
# state is "nothing to fence" whether or not anyone hears about it.
#
# These call main.main(standalone_mode=False) with sys.stdout/sys.stderr
# replaced directly: CliRunner flushes its own stream after the command, which
# a dead stream would break, and standalone mode would let click's own EPIPE
# handler mask an escaping exception.


class _DeadStream:
    def __init__(self, exc):
        self._exc = exc

    def write(self, *a, **kw):
        raise self._exc

    def flush(self, *a, **kw):
        raise self._exc

    def isatty(self):
        return False


class _FlushOnlyDeadStream(_DeadStream):
    # Writes are buffered happily; the failure only surfaces on flush.
    def write(self, *a, **kw):
        return 0


_DEAD_EXCS = [
    BrokenPipeError(32, "Broken pipe"),
    OSError(22, "Invalid argument"),
    ValueError("I/O operation on closed file"),
]


def _exit_code(monkeypatch, fmt: str, cwd: Path, *, stdout, stderr) -> int:
    import io

    monkeypatch.setattr(sys, "stdout", stdout if stdout is not None else io.StringIO())
    monkeypatch.setattr(sys, "stderr", stderr if stderr is not None else io.StringIO())
    try:
        main.main(
            ["--format", fmt, "sessions", "write-root", "--cwd", str(cwd)],
            standalone_mode=False, prog_name="clawpm",
        )
    except SystemExit as e:
        return e.code
    return 0


@pytest.mark.parametrize("exc", _DEAD_EXCS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_ok_result_with_dead_stdout_exits_3_not_traceback(tmp_path, monkeypatch, exc, fmt):
    wt = tmp_path / "wt"
    _write_marker(wt)
    code = _exit_code(monkeypatch, fmt, wt, stdout=_DeadStream(exc), stderr=_DeadStream(exc))
    assert code == 3


@pytest.mark.parametrize("fmt", ["json", "text"])
def test_ok_result_with_deferred_flush_failure_exits_3(tmp_path, monkeypatch, fmt):
    wt = tmp_path / "wt"
    _write_marker(wt)
    dead = _FlushOnlyDeadStream(BrokenPipeError(32, "Broken pipe"))
    assert _exit_code(monkeypatch, fmt, wt, stdout=dead, stderr=None) == 3


def test_ok_result_with_dead_stdout_leaves_a_stderr_note(tmp_path, monkeypatch):
    import io

    wt = tmp_path / "wt"
    _write_marker(wt)
    err = io.StringIO()
    code = _exit_code(
        monkeypatch, "json", wt, stdout=_DeadStream(BrokenPipeError(32, "x")), stderr=err
    )
    assert code == 3
    assert "marker_unusable" in err.getvalue()


@pytest.mark.parametrize("exc", _DEAD_EXCS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_marker_unusable_with_dead_output_exits_3(tmp_path, monkeypatch, exc, fmt):
    wt = tmp_path / "wt"
    _write_marker(wt, body=b"{not json")
    # text mode writes the reason to stderr, json mode to stdout: kill both.
    code = _exit_code(monkeypatch, fmt, wt, stdout=_DeadStream(exc), stderr=_DeadStream(exc))
    assert code == 3


@pytest.mark.parametrize("exc", _DEAD_EXCS, ids=lambda e: type(e).__name__)
@pytest.mark.parametrize("fmt", ["json", "text"])
def test_no_marker_with_dead_output_still_exits_1(tmp_path, monkeypatch, exc, fmt):
    # Deliberate: the state is genuinely "nothing to fence", so exit 1 survives
    # an undeliverable message. Only a clean SystemExit(1), never a traceback.
    plain = tmp_path / "plain"
    plain.mkdir()
    code = _exit_code(monkeypatch, fmt, plain, stdout=_DeadStream(exc), stderr=_DeadStream(exc))
    assert code == 1
