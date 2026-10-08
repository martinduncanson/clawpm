"""Tests for the ignored-task-state warning (CLAWP-134).

``clawpm doctor`` and ``clawpm project init`` must warn when git would ignore
a project's ``.project/tasks`` files, naming the matching ``.gitignore``
file:line and the fix. Ignoring only the lock file is correct and silent;
``unversioned_ok = true`` in settings.toml silences the warning.
"""

from __future__ import annotations

import json
import logging
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

def test_negated_rule_is_not_a_warning(tmp_path, monkeypatch, repo):
    # check-ignore -v exits 0 when the LAST matching rule is a negation.
    (repo / ".gitignore").write_text(
        ".project/*\n!.project/tasks/\n!.project/tasks/*.md\n", encoding="utf-8"
    )
    _make_project(repo)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_force_added_tracked_task_does_not_mask_blanket_ignore(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    subprocess.run(
        ["git", "-C", str(repo), "add", "-f", ".project/tasks/GI-001.md"], check=True
    )
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1
    assert ".gitignore:1" in msgs[0]


def test_rejected_subdir_ignore_warns(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/tasks/rejected/\n", encoding="utf-8")
    _make_project(repo)
    rejected = repo / ".project" / "tasks" / "rejected"
    rejected.mkdir()
    (rejected / "GI-002.md").write_text("---\nid: GI-002\n---\n# r\n", encoding="utf-8")
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1


def test_degraded_git_failure_is_logged_not_silent(tmp_path, caplog):
    import logging

    from clawpm import taskstate_ignore as ti

    plain = tmp_path / "plain"
    plain.mkdir()
    _make_project(plain)
    with caplog.at_level(logging.DEBUG, logger="clawpm.taskstate_ignore"):
        assert ti.find_ignored_task_state(plain) is None
    assert any("check-ignore" in r.getMessage() for r in caplog.records)


def test_malformed_settings_still_warns_and_is_logged(tmp_path, caplog, repo):
    import logging

    from clawpm import taskstate_ignore as ti

    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    (repo / ".project" / "settings.toml").write_text("not = [valid toml\n", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="clawpm.taskstate_ignore"):
        assert ti.is_unversioned_ok(repo) is False
        assert ti.ignored_task_state_warning(repo) is not None
    assert any("unversioned_ok" in r.getMessage() for r in caplog.records)


def test_directory_layout_only_project_warns_under_blanket_ignore(tmp_path, monkeypatch, repo):
    # Split tasks live at tasks/<id>/_task.md -- no direct *.md in the state dir.
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    tasks = repo / ".project" / "tasks"
    (tasks / "GI-001.md").unlink()
    split = tasks / "GI-001"
    split.mkdir()
    (split / "_task.md").write_text("---\nid: GI-001\n---\n# split\n", encoding="utf-8")
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1
    assert ".gitignore:1" in msgs[0]


ALLOWLIST = ".project/tasks/*\n!.project/tasks/GI-*.md\n"


def test_filename_allowlist_does_not_false_warn_in_doctor(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(ALLOWLIST, encoding="utf-8")
    _make_project(repo)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_filename_allowlist_does_not_false_warn_on_init(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(ALLOWLIST, encoding="utf-8")
    _portfolio(tmp_path, monkeypatch, tmp_path)
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    result = runner.invoke(
        main, ["--format", "json", "project", "init", "--in-repo", str(repo), "--id", "gi"]
    )
    assert result.exit_code == 0, result.output
    assert "gitignore" not in result.stderr.lower()


def test_archive_only_project_warns_under_blanket_ignore(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    tasks = repo / ".project" / "tasks"
    (tasks / "GI-001.md").unlink()
    archive = tasks / "done" / "archive"
    archive.mkdir(parents=True)
    (archive / "GI-001.md").write_text("---\nid: GI-001\n---\n# a\n", encoding="utf-8")
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1
    assert ".gitignore:1" in msgs[0]


def test_width_specific_allowlist_does_not_false_warn(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(
        ".project/tasks/*\n!.project/tasks/GI-???.md\n", encoding="utf-8"
    )
    _make_project(repo)
    assert _doctor_warnings(tmp_path, monkeypatch, tmp_path) == []


def test_width_specific_allowlist_does_not_false_warn_on_init(tmp_path, monkeypatch, repo):
    (repo / ".gitignore").write_text(
        ".project/tasks/*\n!.project/tasks/GI-???.md\n", encoding="utf-8"
    )
    _portfolio(tmp_path, monkeypatch, tmp_path)
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    result = runner.invoke(
        main, ["--format", "json", "project", "init", "--in-repo", str(repo), "--id", "gi"]
    )
    assert result.exit_code == 0, result.output
    assert "gitignore" not in result.stderr.lower()


def test_probe_skips_existing_numbers(tmp_path):
    from clawpm import taskstate_ignore as ti

    proj = tmp_path / "p"
    _make_project(proj)
    (proj / ".project" / "tasks" / "GI-999.md").write_text("x", encoding="utf-8")
    rel = ti._probe_path(proj, "", proj / ".project" / "tasks")
    assert rel == ".project/tasks/GI-998.md"


# --- CLAWP-135: probe selection ---------------------------------------------


def _init_stderr(tmp_path, repo, project_id):
    runner = CliRunner(mix_stderr=False) if "mix_stderr" in CliRunner.__init__.__code__.co_varnames else CliRunner()
    result = runner.invoke(
        main, ["--format", "json", "project", "init", "--in-repo", str(repo), "--id", project_id]
    )
    assert result.exit_code == 0, result.output
    return result.stderr


def test_init_probes_allocator_resolved_prefix_not_naive(tmp_path, monkeypatch):
    # A sibling already owns CODE, so code-beta mints CODE-B-NNN, not CODE-NNN.
    # Under a CODE-??? allowlist the naive probe CODE-999 is allowed while the
    # real CODE-B-000 is ignored -- init must warn.
    sib = tmp_path / "code-alpha"
    (sib / ".project").mkdir(parents=True)
    (sib / ".project" / "settings.toml").write_text(
        'id = "code-alpha"\nname = "A"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{sib.as_posix()}"\ntask_prefix = "CODE"\n',
        encoding="utf-8",
    )
    repo = tmp_path / "code-beta"
    repo.mkdir()
    _git_init(repo)
    (repo / ".gitignore").write_text(
        ".project/tasks/*\n!.project/tasks/CODE-???.md\n", encoding="utf-8"
    )
    _portfolio(tmp_path, monkeypatch, tmp_path)
    assert ".gitignore:1" in _init_stderr(tmp_path, repo, "code-beta")


def test_probe_excludes_deleted_but_tracked_file(tmp_path, monkeypatch, repo):
    # GI-999 is in the index but gone from disk: check-ignore never reports a
    # tracked path, so probing it would mask the blanket ignore.
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    ghost = repo / ".project" / "tasks" / "GI-999.md"
    ghost.write_text("---\nid: GI-999\n---\n# g\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".project/tasks/GI-999.md"], check=True)
    ghost.unlink()
    msgs = _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert len(msgs) == 1
    assert ".gitignore:1" in msgs[0]


def test_probe_uses_next_width_when_every_number_is_taken(tmp_path):
    from clawpm import taskstate_ignore as ti

    proj = tmp_path / "p"
    _make_project(proj)
    tasks = proj / ".project" / "tasks"
    for n in range(1000):
        (tasks / f"GI-{n:03d}.md").write_text("x", encoding="utf-8")
    rel = ti._probe_path(proj, "", tasks)
    assert rel == ".project/tasks/GI-9999.md"
    assert not (proj / rel).exists()


# --- CLAWP-135 r1: a failed git read is a loud degraded path, not silence ----

_REAL_RUN = subprocess.run


def _fail_git(monkeypatch, verb, *, exc=None, rc=128, stderr="fatal: boom"):
    """Make ``git <verb>`` fail inside taskstate_ignore; everything else is real."""
    from clawpm import taskstate_ignore as ti

    def fake(cmd, *a, **kw):
        if verb in cmd:
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(cmd, rc, stdout="", stderr=stderr)
        return _REAL_RUN(cmd, *a, **kw)

    monkeypatch.setattr(ti.subprocess, "run", fake)


def _warnings(caplog, needle):
    return [
        r for r in caplog.records
        if r.levelname == "WARNING" and r.name == "clawpm.taskstate_ignore"
        and needle in r.getMessage()
    ]


_FAILURES = [
    pytest.param(dict(exc=OSError("no git")), id="oserror"),
    pytest.param(dict(exc=subprocess.TimeoutExpired("git", 5)), id="timeout"),
    pytest.param(dict(rc=128, stderr="fatal: index file corrupt"), id="nonzero-exit"),
]


@pytest.mark.parametrize("failure", _FAILURES)
def test_failed_ls_files_warns(tmp_path, monkeypatch, caplog, repo, failure):
    from clawpm import taskstate_ignore as ti

    _make_project(repo)
    _fail_git(monkeypatch, "ls-files", **failure)
    with caplog.at_level("DEBUG", logger="clawpm.taskstate_ignore"):
        assert ti._index_names(repo, "") == set()
    msgs = _warnings(caplog, "ls-files")
    assert len(msgs) == 1
    assert "clawpm:" in msgs[0].getMessage()


@pytest.mark.parametrize("failure", _FAILURES)
def test_failed_check_ignore_warns(tmp_path, monkeypatch, caplog, repo, failure):
    from clawpm import taskstate_ignore as ti

    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    _fail_git(monkeypatch, "check-ignore", **failure)
    with caplog.at_level("DEBUG", logger="clawpm.taskstate_ignore"):
        assert ti.find_ignored_task_state(repo) is None
    assert len(_warnings(caplog, "check-ignore")) == 1


def test_failed_ls_files_in_doctor_is_visible_not_masked_silently(
    tmp_path, monkeypatch, caplog, repo
):
    # The reviewer's repro: GI-999 is tracked-but-deleted, the index read
    # fails, the probe lands on GI-999 and the blanket-ignore warning vanishes.
    # The loss of the exclusion must at least be announced.
    (repo / ".gitignore").write_text(".project/\n", encoding="utf-8")
    _make_project(repo)
    ghost = repo / ".project" / "tasks" / "GI-999.md"
    ghost.write_text("---\nid: GI-999\n---\n# g\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-f", ".project/tasks/GI-999.md"], check=True)
    ghost.unlink()
    _fail_git(monkeypatch, "ls-files", rc=128, stderr="fatal: index file corrupt")
    with caplog.at_level("DEBUG", logger="clawpm.taskstate_ignore"):
        _doctor_warnings(tmp_path, monkeypatch, tmp_path)
    assert _warnings(caplog, "ls-files")


def test_not_a_git_repo_stays_quiet_at_warning_level(tmp_path, caplog):
    # "Not a repo" is the normal answer for an unversioned project, not a
    # degraded check: it must not start warning on every doctor run.
    from clawpm import taskstate_ignore as ti

    plain = tmp_path / "plain"
    plain.mkdir()
    _make_project(plain)
    with caplog.at_level("DEBUG", logger="clawpm.taskstate_ignore"):
        assert ti.find_ignored_task_state(plain) is None
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("ls-files" in r.getMessage() for r in caplog.records)  # still debug-logged
