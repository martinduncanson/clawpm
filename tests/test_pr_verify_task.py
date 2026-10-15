"""CLAWP-151: ``clawpm pr verify <N> --task ID`` records a merged-PR attestation.

Every refusal must leave the task file byte-identical (sha256 compared); success
writes exactly one ``pr_verification`` frontmatter line that ``tasks show`` and
``tasks list`` expose.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.pr_record import RECORD_KEYS

HEAD = "a" * 40
OTHER = "b" * 40
MERGE = "c" * 40
TASK = "TEST-001"

_SETTINGS = 'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n'
_FM = "---\nid: TEST-001\ntitle: A task\nstate: open\npriority: 3\n"
_BODY = "# A task\n\nbody text\n"


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class FakeEnv:
    """Routes the subprocess seam used by pr_verify (a MERGED PR by default)."""

    def __init__(self, **over):
        self.pr = {
            "headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "UNKNOWN",
            "state": "MERGED", "mergeCommit": {"oid": MERGE},
            "url": "https://github.com/acme/widgets/pull/7",
        }
        self.runs = [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": HEAD}]
        self.remote_sha = HEAD
        self.override = {}
        self.__dict__.update(over)

    def __call__(self, args, **kw):
        key = args[0]
        if key in self.override:
            r = self.override[key]
            if isinstance(r, Exception):
                raise r
            return r
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp("git@github.com:acme/widgets.git\n")
        if args[:2] == ["git", "ls-remote"]:
            return _cp(f"{self.remote_sha}\trefs/heads/feat/x\n" if self.remote_sha else "")
        if args[:2] == ["git", "rev-parse"]:
            return _cp("", rc=1)
        if args[:3] == ["gh", "pr", "view"]:
            return _cp(json.dumps(self.pr))
        if args[0] == "gh" and "check-runs" in args[2]:
            return _cp(json.dumps({"total_count": len(self.runs), "check_runs": self.runs}))
        if args[0] == "gh" and "/status" in args[2]:
            return _cp(json.dumps({"total_count": 0, "statuses": []}))
        raise AssertionError(f"unexpected subprocess call: {args}")


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / "portfolio"
    repo = root / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{root.as_posix()}"]\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    meta = repo / ".project"
    (meta / "tasks" / "done").mkdir(parents=True)
    (meta / "settings.toml").write_text(_SETTINGS + f'repo_path = "{repo.as_posix()}"\n', encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    monkeypatch.chdir(elsewhere)
    return meta / "tasks"


def _task_path(store: Path) -> Path:
    return store / f"{TASK}.md"


def _write(store: Path, data: bytes) -> Path:
    p = _task_path(store)
    p.write_bytes(data)
    return p


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _run(env, *args, task=True):
    argv = ["--format", "json", "pr", "verify", "7", *args]
    if task:
        argv += ["--task", TASK, "--project", "test"]
    with patch("clawpm.pr_verify.subprocess.run", env):
        return CliRunner().invoke(main, argv)


def _plain(store: Path) -> Path:
    return _write(store, (_FM + "---\n" + _BODY).encode("utf-8"))


# --- refusals leave the file identical ---------------------------------------

@pytest.mark.parametrize(
    "name, env_kw, args, code",
    [
        ("head_mismatch", {}, ["--expect-head", OTHER], 1),
        ("ci_failing", {"runs": [{"name": "t", "status": "completed", "conclusion": "failure", "head_sha": HEAD}]}, [], 1),
        ("not_merged", {"pr": {"headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "MERGEABLE", "state": "OPEN", "url": "u"}}, [], 1),
        ("no_merge_commit", {"pr": {"headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "UNKNOWN", "state": "MERGED", "mergeCommit": None, "url": "u"}}, [], 1),
        ("gh_missing", {"override": {"gh": FileNotFoundError("gh")}}, [], 2),
    ],
)
def test_refusals_leave_task_file_identical(store, name, env_kw, args, code):
    p = _plain(store)
    before = _sha(p)
    r = _run(FakeEnv(**env_kw), *args)
    assert r.exit_code == code, (r.output, r.stderr)
    assert _sha(p) == before
    assert b"pr_verification" not in p.read_bytes()
    assert not list(store.glob("*.tmp"))


def test_task_not_found_exits_1_and_creates_nothing(store):
    def listing():  # the lock file is an expected, empty artifact of taking the lock
        return sorted(x.name for x in store.iterdir() if x.name != ".clawpm-tasks.lock")

    before = listing()
    r = _run(FakeEnv())
    assert r.exit_code == 1, (r.output, r.stderr)
    assert json.loads(r.stderr)["error"] == "task_not_found"
    assert listing() == before


@pytest.mark.parametrize(
    "data, code",
    [
        (("no frontmatter here\n" + _BODY).encode(), "no_frontmatter"),
        (("---\n- a\n- b\n---\n" + _BODY).encode(), "bad_frontmatter"),
        (("---\nid: [unclosed\n---\n" + _BODY).encode(), "bad_frontmatter"),
    ],
)
def test_bad_frontmatter_exits_1_file_identical(store, data, code):
    p = _write(store, data)
    before = _sha(p)
    r = _run(FakeEnv())
    assert r.exit_code == 1, (r.output, r.stderr)
    assert json.loads(r.stderr)["error"] == code
    assert _sha(p) == before


# --- success -----------------------------------------------------------------

def test_success_writes_record_and_show_and_list_expose_it(store):
    p = _plain(store)
    r = _run(FakeEnv(), "--expect-head", HEAD)
    assert r.exit_code == 0, (r.output, r.stderr)
    out = json.loads(r.stdout)
    assert out["ok"] is True

    with patch("clawpm.pr_verify.subprocess.run", FakeEnv()):
        show = CliRunner().invoke(main, ["--format", "json", "tasks", "show", TASK, "-p", "test"])
        lst = CliRunner().invoke(main, ["--format", "json", "tasks", "list", "-p", "test"])
    assert show.exit_code == 0, show.output
    rec = json.loads(show.stdout)["pr_verification"]
    assert set(rec) == set(RECORD_KEYS)
    assert rec["pr"] == 7 and rec["head"] == HEAD and rec["merge_commit"] == MERGE
    assert rec["repo"] == "acme/widgets" and rec["remote"] == "fork"
    assert rec["verified_at"].endswith("Z") or "+00:00" in rec["verified_at"]
    rows = json.loads(lst.stdout)
    rows = rows if isinstance(rows, list) else rows.get("tasks", rows)
    assert [t for t in rows if t["id"] == TASK][0]["pr_verification"] == rec
    # exactly one new line, nothing else touched
    new = p.read_bytes().decode()
    assert new.count("pr_verification:") == 1
    assert new.endswith("---\n" + _BODY)


@pytest.mark.parametrize("bom, eol", [("", "\r\n"), ("﻿", "\n"), ("﻿", "\r\n")])
def test_bom_and_crlf_preserved_outside_the_one_line(store, bom, eol):
    original = bom + (_FM + "---\n" + _BODY).replace("\n", eol)
    p = _write(store, original.encode("utf-8"))
    r = _run(FakeEnv())
    assert r.exit_code == 0, (r.output, r.stderr)
    new = p.read_bytes().decode("utf-8")
    assert new.startswith(bom + "---")
    lines = new.split(eol)
    kept = [ln for ln in lines if not ln.startswith("pr_verification:")]
    assert eol.join(kept) == original
    assert "\n" not in new.replace(eol, "")


def test_rerun_replaces_rather_than_duplicates(store):
    p = _plain(store)
    assert _run(FakeEnv()).exit_code == 0
    env2 = FakeEnv(pr={"headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "UNKNOWN",
                       "state": "MERGED", "mergeCommit": {"oid": "d" * 40}, "url": "u"})
    assert _run(env2).exit_code == 0
    text = p.read_text(encoding="utf-8")
    assert text.count("pr_verification:") == 1
    assert "d" * 40 in text and MERGE not in text


# --- unchanged without --task -------------------------------------------------

def test_without_task_output_unchanged_and_nothing_written(store):
    p = _plain(store)
    before = _sha(p)
    r = _run(FakeEnv(pr={"headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "MERGEABLE",
                         "state": "OPEN", "url": "u"}), "--expect-head", HEAD, task=False)
    assert r.exit_code == 0, (r.output, r.stderr)
    out = json.loads(r.stdout)
    assert out["merge_command"].endswith(f"--match-head-commit {HEAD}")
    assert "merged" not in out and "merge_commit" not in out and "pr_verification" not in out
    assert _sha(p) == before


def test_task_unset_means_to_dict_has_no_pr_verification_key(store):
    _plain(store)
    r = CliRunner().invoke(main, ["--format", "json", "tasks", "show", TASK, "-p", "test"])
    assert r.exit_code == 0, r.output
    assert "pr_verification" not in json.loads(r.stdout)
