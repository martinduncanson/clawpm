"""Phase 1.8 regression tests:

- ``clawpm project announce`` writes/replaces the clawpm-requirement marker
  block in CLAUDE.md > AGENTS.md > README.md (first-found wins).
- ``clawpm project init`` auto-runs announce.
- ``clawpm doctor`` Check d warns when project HEAD has progressed
  >threshold commits since the last work_log entry for that project.
- ``clawpm doctor`` Check e warns when no clawpm-requirement marker is
  present in any of the repo's agent docs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.announce import (
    MARKER_START,
    MARKER_END,
    find_existing_marker_file,
    generate_stanza,
    select_target_file,
    write_or_replace_stanza,
)
from clawpm.cli import main


# ---------------------------------------------------------------------------
# announce module — unit tests
# ---------------------------------------------------------------------------


class TestAnnounceModule:
    def test_generate_stanza_includes_project_id_and_markers(self):
        s = generate_stanza("foo", "Foo Project")
        assert MARKER_START in s
        assert MARKER_END in s
        assert "foo" in s
        assert "Foo Project" in s
        assert "pipx install" in s

    def test_select_target_prefers_claude_md(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text("# foo", encoding="utf-8")
        (tmp_path / "AGENTS.md").write_text("# bar", encoding="utf-8")
        (tmp_path / "README.md").write_text("# baz", encoding="utf-8")
        assert select_target_file(tmp_path).name == "CLAUDE.md"

    def test_select_target_falls_back_to_agents(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# foo", encoding="utf-8")
        (tmp_path / "README.md").write_text("# bar", encoding="utf-8")
        assert select_target_file(tmp_path).name == "AGENTS.md"

    def test_select_target_falls_back_to_readme(self, tmp_path):
        (tmp_path / "README.md").write_text("# foo", encoding="utf-8")
        assert select_target_file(tmp_path).name == "README.md"

    def test_select_target_defaults_to_claude_when_none_exist(self, tmp_path):
        # Returns the path that *would* be created — must not actually exist yet.
        target = select_target_file(tmp_path)
        assert target.name == "CLAUDE.md"
        assert not target.exists()

    def test_write_creates_new_file_when_target_absent(self, tmp_path):
        target, action = write_or_replace_stanza(tmp_path, "foo", "Foo")
        assert action == "created"
        assert target.name == "CLAUDE.md"
        content = target.read_text(encoding="utf-8")
        assert MARKER_START in content
        assert MARKER_END in content

    def test_write_appends_when_file_exists_without_marker(self, tmp_path):
        (tmp_path / "README.md").write_text("# existing readme\n\nbody\n", encoding="utf-8")
        target, action = write_or_replace_stanza(tmp_path, "foo", "Foo")
        assert action == "appended"
        assert target.name == "README.md"
        content = target.read_text(encoding="utf-8")
        assert content.startswith("# existing readme")  # original preserved
        assert MARKER_START in content

    def test_write_replaces_existing_marker_block_in_place(self, tmp_path):
        # Seed an old-format marker block; new content must replace it.
        old = (
            "# header\n\n"
            f"{MARKER_START}\nold stanza content\n{MARKER_END}\n\n"
            "after\n"
        )
        target = tmp_path / "CLAUDE.md"
        target.write_text(old, encoding="utf-8")

        result_target, action = write_or_replace_stanza(tmp_path, "foo", "Foo")
        assert action == "replaced"
        assert result_target == target
        content = target.read_text(encoding="utf-8")
        assert "old stanza content" not in content
        assert "foo" in content
        assert content.startswith("# header")
        assert content.rstrip().endswith("after")

    def test_idempotent_double_write(self, tmp_path):
        write_or_replace_stanza(tmp_path, "foo", "Foo")
        target, action = write_or_replace_stanza(tmp_path, "foo", "Foo")
        assert action == "replaced"  # second time finds marker, replaces
        content = target.read_text(encoding="utf-8")
        # Marker pair appears exactly once
        assert content.count(MARKER_START) == 1
        assert content.count(MARKER_END) == 1

    def test_find_existing_marker_file_returns_none_when_absent(self, tmp_path):
        (tmp_path / "README.md").write_text("# readme without marker", encoding="utf-8")
        assert find_existing_marker_file(tmp_path) is None

    def test_find_existing_marker_file_returns_first_match(self, tmp_path):
        (tmp_path / "CLAUDE.md").write_text(f"x\n{MARKER_START}\ny\n{MARKER_END}\n", encoding="utf-8")
        (tmp_path / "README.md").write_text(f"a\n{MARKER_START}\nb\n{MARKER_END}\n", encoding="utf-8")
        found = find_existing_marker_file(tmp_path)
        assert found is not None
        assert found.name == "CLAUDE.md"  # precedence order


# ---------------------------------------------------------------------------
# project init auto-announce
# ---------------------------------------------------------------------------


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real git repo with one initial commit, ready for `clawpm project init`."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hello')\n", encoding="utf-8")

    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test"}
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "main.py"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, env=env, check=True)

    # Portfolio with this repo as the project_roots root
    portfolio = tmp_path / "portfolio"
    portfolio.mkdir()
    (portfolio / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio.as_posix()}"\n'
        f'project_roots = ["{tmp_path.as_posix()}"]\n',
        encoding="utf-8",
    )
    (portfolio / "work_log.jsonl").touch()

    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(portfolio))
    return {"repo": repo, "portfolio": portfolio, "env": env}


class TestProjectInitAutoAnnounce:
    def test_init_writes_announce_stanza(self, git_repo):
        runner = CliRunner()
        result = runner.invoke(main, ["project", "init", "--in-repo", str(git_repo["repo"])])
        assert result.exit_code == 0, result.output
        # A CLAUDE.md should now exist with the marker
        marker_file = find_existing_marker_file(git_repo["repo"])
        assert marker_file is not None
        assert marker_file.name == "CLAUDE.md"

    def test_init_announce_uses_existing_readme(self, git_repo):
        # Seed a README before init — announce should pick it over creating CLAUDE.md
        (git_repo["repo"] / "README.md").write_text("# myrepo\n", encoding="utf-8")
        runner = CliRunner()
        result = runner.invoke(main, ["project", "init", "--in-repo", str(git_repo["repo"])])
        assert result.exit_code == 0, result.output
        marker_file = find_existing_marker_file(git_repo["repo"])
        assert marker_file is not None
        assert marker_file.name == "README.md"
        # No CLAUDE.md should be created
        assert not (git_repo["repo"] / "CLAUDE.md").exists()


# ---------------------------------------------------------------------------
# Doctor Check e — missing marker
# ---------------------------------------------------------------------------


class TestDoctorMissingMarkerCheck:
    def test_doctor_warns_when_marker_missing(self, git_repo):
        runner = CliRunner()
        # Init without announce side-effect: manually create .project/ minus the announce
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )

        result = runner.invoke(main, ["--format", "json", "doctor"])
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        missing_ids = {m["project_id"] for m in data["missing_markers"]}
        assert "myrepo" in missing_ids

    def test_doctor_passes_after_announce(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )
        # Run announce manually
        write_or_replace_stanza(repo, "myrepo", "myrepo")

        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "doctor"])
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        missing_ids = {m["project_id"] for m in data["missing_markers"]}
        assert "myrepo" not in missing_ids


# ---------------------------------------------------------------------------
# Doctor Check d — commit drift
# ---------------------------------------------------------------------------


class TestDoctorCommitDriftCheck:
    def test_drift_warns_when_no_work_log_and_commits_present(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )

        # Add 6 more commits (total 7 with the initial one) — above default 5 threshold
        for i in range(6):
            f = repo / f"f{i}.txt"
            f.write_text(f"content {i}\n", encoding="utf-8")
            subprocess.run(["git", "add", f.name], cwd=repo, env=git_repo["env"], check=True)
            subprocess.run(["git", "commit", "-q", "-m", f"commit {i}"],
                          cwd=repo, env=git_repo["env"], check=True)

        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "doctor"])
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        drift_ids = {d["project_id"]: d for d in data["commit_drift"]}
        assert "myrepo" in drift_ids
        # 7 commits total, threshold 5, log_status never_logged
        assert drift_ids["myrepo"]["commits_since_last_log"] >= 7
        assert drift_ids["myrepo"]["log_status"] == "never_logged"

    def test_drift_silent_when_threshold_not_exceeded(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )

        # Only 1 commit total — well below default threshold of 5
        runner = CliRunner()
        result = runner.invoke(main, ["--format", "json", "doctor"])
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        drift_ids = {d["project_id"] for d in data["commit_drift"]}
        assert "myrepo" not in drift_ids

    def test_custom_threshold_respected(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )

        # Default threshold 5 wouldn't flag with 1 commit; lower it to 0.
        runner = CliRunner()
        result = runner.invoke(
            main, ["--format", "json", "doctor", "--commits-drift-threshold", "0"]
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        drift_ids = {d["project_id"] for d in data["commit_drift"]}
        assert "myrepo" in drift_ids


# ---------------------------------------------------------------------------
# project announce command (CLI)
# ---------------------------------------------------------------------------


class TestProjectAnnounceCommand:
    def test_announce_writes_marker_block(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "MyRepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )

        runner = CliRunner()
        result = runner.invoke(main, ["project", "announce", "--project", "myrepo"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["status"] == "ok"
        assert data["project_id"] == "myrepo"
        assert data["action"] in ("created", "appended", "replaced")
        # Marker is present in the chosen file
        target = Path(data["file"])
        assert MARKER_START in target.read_text(encoding="utf-8")

    def test_announce_replaces_outdated_marker_block(self, git_repo):
        repo = git_repo["repo"]
        (repo / ".project").mkdir()
        (repo / ".project" / "tasks").mkdir()
        (repo / ".project" / "settings.toml").write_text(
            f'id = "myrepo"\nname = "MyRepo"\nstatus = "active"\npriority = 5\n'
            f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
            encoding="utf-8",
        )
        # Pre-seed an old marker block; announce should replace.
        (repo / "CLAUDE.md").write_text(
            f"# myrepo\n\n{MARKER_START}\nOLD STANZA\n{MARKER_END}\n", encoding="utf-8"
        )

        runner = CliRunner()
        result = runner.invoke(main, ["project", "announce", "--project", "myrepo"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["action"] == "replaced"
        content = (repo / "CLAUDE.md").read_text(encoding="utf-8")
        assert "OLD STANZA" not in content
        assert MARKER_START in content
        assert MARKER_END in content
        # Only one pair of markers should remain
        assert content.count(MARKER_START) == 1
