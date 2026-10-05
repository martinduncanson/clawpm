"""Tests for the ignored-task-state warning (CLAWP-134).

``clawpm doctor`` and ``clawpm project init`` must warn when git would ignore
a project's ``.project/tasks`` files, naming the matching ``.gitignore``
file:line and the fix. Ignoring only the lock file is correct and silent;
``unversioned_ok = true`` in settings.toml silences the warning.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not available")

LOCK_ONLY = ".project/tasks/.clawpm-tasks.lock\n"


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)


def _make_project(project_dir: Path, *, unversioned_ok: bool = False) -> None:
    meta = project_dir / ".project"
    tasks = meta / "tasks"
    for d in (tasks, tasks / "done", tasks / "blocked"):
        d.mkdir(parents=True, exist_ok=True)
    extra = "unversioned_ok = true\n" if unversioned_ok else ""
    (meta / "settings.toml").write_text(
        'id = "gi"\nname = "GI"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{project_dir.as_posix()}"\n{extra}',
        encoding="utf-8",
    )
    (tasks / "GI-001.md").write_text(
        "---\nid: GI-001\nstate: open\n---\n# A task\n", encoding="utf-8"
    )


def _portfolio(tmp_path: Path, monkeypatch, roots: Path) -> None:
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{roots.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))


def _doctor_warnings(tmp_path: Path, monkeypatch, roots: Path) -> list[str]:
    _portfolio(tmp_path, monkeypatch, roots)
    result = CliRunner().invoke(main, ["--format", "json", "doctor"])
    payload = json.loads(result.stdout)
    return [i["message"] for i in payload["issues"] if "gitignore" in i["message"].lower()]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git_init(r)
    return r


def test_ignored_project_dir_warns_with_file_line(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text("node_modules/\n.project/\n", encoding="utf-8")
    _make_project(repo)
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1
    assert ".gitignore:2" in msgs[0]
    assert ".project/tasks/.clawpm-tasks.lock" in msgs[0]


def test_text_mode_prints_warning(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    _portfolio(tmp_path, monkeypatch, tmp_path)
    result = CliRunner().invoke(main, ["doctor"])
    assert ".gitignore:1" in result.output


def test_lock_only_ignored_is_silent(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(LOCK_ONLY, encoding="utf-8")
    _make_project(repo)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_unversioned_ok_silences(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo, unversioned_ok=True)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_project_in_repo_subdirectory_warns(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    sub = repo / "pkg"
    sub.mkdir()
    _make_project(sub)
    msgs = _doctor_warnings(tmp_path, monkeypatch, repo)
    assert len(msgs) == 1
    assert ".gitignore:1" in msgs[0]


def test_not_a_git_repo_is_silent(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    _make_project(plain)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_no_task_files_is_silent(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    (repo / ".project" / "tasks" / "GI-001.md").unlink()
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_project_init_warns_on_stderr_and_json_stays_valid(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _portfolio(tmp_path, monkeypatch, tmp_path)
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    result = runner.invoke(
        main, ["--format", "json", "project", "init", "--in-repo", str(repo), "--id", "gi"]
    )
    assert result.exit_code == 0, result.output
    json.loads(result.stdout)  # stdout must remain parseable
    assert ".gitignore:1" in result.stderr


def test_project_init_clean_repo_no_warning(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(LOCK_ONLY, encoding="utf-8")
    _portfolio(tmp_path, monkeypatch, tmp_path)
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    result = runner.invoke(
        main, ["--format", "json", "project", "init", "--in-repo", str(repo), "--id", "gi"]
    )
    assert result.exit_code == 0, result.output
    assert "gitignore" not in result.stderr.lower()
