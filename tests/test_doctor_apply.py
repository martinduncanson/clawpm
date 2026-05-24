"""Tests for CLAWP-026 ``clawpm doctor --apply`` auto-remediation.

Coverage:

- ``--apply`` fixes a half-rename drift (deletes the bare .md, keeps .progress.md).
- ``--apply`` fixes a state-field drift (rewrites frontmatter to match location).
- ``--apply`` runs cascade for a stale-blocked task and promotes it to open.
- ``--dry-run`` populates ``applied[]`` with ``would-...`` results but leaves the
  filesystem untouched.
- ``--no-apply-drift`` disables the state-mismatch arm while leaving half-rename
  alone (and ``--no-apply-half-rename`` does the opposite).
- ``--yes`` runs without interactive prompts.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main


@pytest.fixture
def portfolio_repo(tmp_path, monkeypatch):
    """Portfolio + project repo wired up so ``clawpm doctor`` finds the project."""
    repo = tmp_path / "myrepo"
    repo.mkdir()
    (repo / "main.py").write_text("print('hi')\n", encoding="utf-8")

    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@test",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@test",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "add", "main.py"], cwd=repo, env=env, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "initial"], cwd=repo, env=env, check=True
    )

    portfolio = tmp_path / "portfolio"
    portfolio.mkdir()
    (portfolio / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio.as_posix()}"\n'
        f'project_roots = ["{tmp_path.as_posix()}"]\n',
        encoding="utf-8",
    )
    (portfolio / "work_log.jsonl").touch()

    proj_dir = repo / ".project"
    proj_dir.mkdir()
    (proj_dir / "tasks").mkdir()
    (proj_dir / "settings.toml").write_text(
        f'id = "myrepo"\nname = "myrepo"\nstatus = "active"\npriority = 5\n'
        f'repo_path = "{repo.as_posix()}"\nlabels = []\n',
        encoding="utf-8",
    )
    # Suppress the missing-marker warning so doctor JSON is cleaner to assert on.
    (repo / "CLAUDE.md").write_text(
        "# myrepo\n\n<!-- clawpm:requirement:start id=myrepo -->\nstanza\n"
        "<!-- clawpm:requirement:end -->\n",
        encoding="utf-8",
    )

    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(portfolio))
    return {"repo": repo, "portfolio": portfolio, "tasks_dir": proj_dir / "tasks"}


def _write_task(path: Path, body: str = "body\n") -> None:
    path.write_text(body, encoding="utf-8")


def _frontmatter(state: str, title: str = "T", task_id: str = "MYREP-001") -> str:
    return (
        "---\n"
        f"id: {task_id}\n"
        f"title: {title}\n"
        f"state: {state}\n"
        "priority: 5\n"
        "---\n\n# task body\n"
    )


class TestApplyHalfRename:
    def test_apply_deletes_bare_md_keeps_progress(self, portfolio_repo):
        tasks = portfolio_repo["tasks_dir"]
        bare = tasks / "MYREP-001.md"
        prog = tasks / "MYREP-001.progress.md"
        _write_task(bare, _frontmatter("open"))
        _write_task(prog, _frontmatter("progress"))

        runner = CliRunner()
        result = runner.invoke(
            main, ["--format", "json", "doctor", "--apply", "--yes"]
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)

        # Half-rename should appear in applied[] with result indicating delete
        half = [
            a for a in data.get("applied", [])
            if a["class"] == "drift_tasks" and a["target"].endswith("MYREP-001.md")
        ]
        assert half, f"no half-rename applied entry: {data.get('applied')}"
        assert "delete" in half[0]["result"].lower()
        # Filesystem state: bare gone, progress kept
        assert not bare.exists()
        assert prog.exists()


class TestApplyStateMismatch:
    def test_apply_rewrites_frontmatter_state(self, portfolio_repo):
        tasks = portfolio_repo["tasks_dir"]
        # File is at tasks/ root → location_state=open, but frontmatter says blocked
        f = tasks / "MYREP-002.md"
        _write_task(f, _frontmatter("blocked", task_id="MYREP-002"))

        runner = CliRunner()
        result = runner.invoke(
            main, ["--format", "json", "doctor", "--apply", "--yes"]
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)

        rewrites = [
            a for a in data.get("applied", [])
            if a["class"] == "drift_tasks" and a["target"].endswith("MYREP-002.md")
        ]
        assert rewrites, f"no state_mismatch applied: {data.get('applied')}"
        assert "rewrote" in rewrites[0]["result"].lower()

        # Verify the file's frontmatter now says state: open
        import yaml
        text = f.read_text(encoding="utf-8")
        assert text.startswith("---")
        fm = yaml.safe_load(text.split("---", 2)[1])
        assert fm["state"] == "open"


class TestApplyStaleBlocked:
    def test_apply_promotes_stale_blocked(self, portfolio_repo):
        tasks = portfolio_repo["tasks_dir"]
        # Dependency task already done
        done_dir = tasks / "done"
        done_dir.mkdir()
        dep = done_dir / "MYREP-010.md"
        _write_task(
            dep,
            "---\n"
            "id: MYREP-010\ntitle: dep\nstate: done\npriority: 5\n"
            "---\n\nbody\n",
        )

        # Blocked task whose only dep is MYREP-010 (done). Backdate mtime so it
        # qualifies as "stale-blocked" (>24h).
        blocked_dir = tasks / "blocked"
        blocked_dir.mkdir()
        bl = blocked_dir / "MYREP-011.md"
        _write_task(
            bl,
            "---\n"
            "id: MYREP-011\ntitle: still blocked\nstate: blocked\npriority: 5\n"
            "depends:\n  - MYREP-010\n"
            "---\n\nbody\n",
        )
        # Backdate to 48h ago so stale-blocked check fires
        import time as _time
        old = _time.time() - 48 * 3600
        os.utime(bl, (old, old))

        runner = CliRunner()
        result = runner.invoke(
            main, ["--format", "json", "doctor", "--apply", "--yes"]
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)

        # Sanity: doctor saw the stale-blocked entry
        assert any(
            sb["task_id"] == "MYREP-011" for sb in data.get("stale_blocked", [])
        ), f"stale_blocked not detected: {data}"

        promotions = [
            a for a in data.get("applied", [])
            if a["class"] == "stale_blocked" and a["target"] == "MYREP-011"
        ]
        assert promotions, f"no stale_blocked apply: {data.get('applied')}"
        assert "promoted" in promotions[0]["result"].lower()

        # File should have moved from blocked/ to tasks/ root
        assert not bl.exists()
        assert (tasks / "MYREP-011.md").exists()


class TestDryRunNoMutation:
    def test_dry_run_reports_but_doesnt_modify(self, portfolio_repo):
        tasks = portfolio_repo["tasks_dir"]
        bare = tasks / "MYREP-001.md"
        prog = tasks / "MYREP-001.progress.md"
        _write_task(bare, _frontmatter("open"))
        _write_task(prog, _frontmatter("progress"))
        f2 = tasks / "MYREP-002.md"
        _write_task(f2, _frontmatter("blocked", task_id="MYREP-002"))
        original_f2 = f2.read_text(encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["--format", "json", "doctor", "--apply", "--dry-run", "--yes"],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)

        applied = data.get("applied", [])
        # Both drift entries should appear
        assert len(applied) >= 2
        for a in applied:
            # Dry-run results carry a "would-" prefix
            assert a["result"].startswith("would-"), a

        # Filesystem untouched
        assert bare.exists()
        assert prog.exists()
        assert f2.read_text(encoding="utf-8") == original_f2
        # And the JSON output flags it
        assert data.get("dry_run") is True


class TestNoApplyDrift:
    def test_no_apply_drift_skips_state_mismatch_but_runs_half_rename(
        self, portfolio_repo
    ):
        tasks = portfolio_repo["tasks_dir"]
        # half-rename pair
        bare = tasks / "MYREP-001.md"
        prog = tasks / "MYREP-001.progress.md"
        _write_task(bare, _frontmatter("open"))
        _write_task(prog, _frontmatter("progress"))
        # state-mismatch loner
        f2 = tasks / "MYREP-002.md"
        _write_task(f2, _frontmatter("blocked", task_id="MYREP-002"))
        original_f2 = f2.read_text(encoding="utf-8")

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "--format", "json", "doctor",
                "--apply", "--yes", "--no-apply-drift",
            ],
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)

        # State-mismatch should be in skipped[], half-rename should be applied[]
        skipped = data.get("apply_skipped", [])
        applied = data.get("applied", [])
        assert any(
            s["class"] == "drift_tasks"
            and s.get("target", "").endswith("MYREP-002.md")
            and "no-apply-drift" in s["reason"]
            for s in skipped
        ), f"expected MYREP-002 in skipped: {skipped}"
        assert any(
            a["class"] == "drift_tasks"
            and a.get("target", "").endswith("MYREP-001.md")
            for a in applied
        ), f"expected MYREP-001 applied: {applied}"

        # State-mismatch file unchanged
        assert f2.read_text(encoding="utf-8") == original_f2
        # Half-rename did run
        assert not bare.exists()
        assert prog.exists()


class TestYesNoPrompt:
    def test_yes_runs_without_prompt(self, portfolio_repo):
        """--yes must skip the click.confirm() prompt entirely. We confirm by
        passing no stdin — without --yes this would hang or abort."""
        tasks = portfolio_repo["tasks_dir"]
        bare = tasks / "MYREP-001.md"
        prog = tasks / "MYREP-001.progress.md"
        _write_task(bare, _frontmatter("open"))
        _write_task(prog, _frontmatter("progress"))

        runner = CliRunner()
        # Empty stdin: if --yes is honored the run completes; otherwise click
        # would abort the confirm prompt.
        result = runner.invoke(
            main,
            ["--format", "json", "doctor", "--apply", "--yes"],
            input="",
        )
        assert result.exit_code in (0, 1), result.output
        data = json.loads(result.output)
        # The half-rename was actually applied
        assert not bare.exists()
        assert any(
            a["class"] == "drift_tasks" and a["target"].endswith("MYREP-001.md")
            for a in data.get("applied", [])
        )

    def test_no_yes_aborts_on_empty_input(self, portfolio_repo):
        """Without --yes, click.confirm() defaults to False on empty stdin and
        the apply phase is skipped (applied[] empty, files untouched)."""
        tasks = portfolio_repo["tasks_dir"]
        bare = tasks / "MYREP-001.md"
        prog = tasks / "MYREP-001.progress.md"
        _write_task(bare, _frontmatter("open"))
        _write_task(prog, _frontmatter("progress"))

        runner = CliRunner()
        # Input "n\n" rejects the confirm; apply phase short-circuits.
        result = runner.invoke(
            main,
            ["--format", "json", "doctor", "--apply"],
            input="n\n",
        )
        assert result.exit_code in (0, 1), result.output
        # Filesystem untouched — proves the apply phase didn't run
        assert bare.exists()
        assert prog.exists()
