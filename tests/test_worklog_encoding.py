"""CLAWP-046: `clawpm log commit` must decode git output as UTF-8, not the
Windows locale default (cp1252).

Bug: the ingestion `subprocess.run(["git","log",...], text=True)` had no
`encoding=`, so on Windows git's UTF-8 commit subject (e.g. an em-dash,
0xE2 0x80 0x94) was decoded as cp1252 -> "â€"" and stored as mojibake
(0xC3 0xA2 0xE2 0x82 0xAC 0xE2 0x80 0x9D) in the work_log. Confirmed by byte
comparison of git's stored subject vs the work_log entry.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main

EM = "—"  # em-dash; git stores it UTF-8, clawpm must read it back UTF-8
MOJIBAKE = "â€"  # "â€" — the cp1252-misread prefix that must NOT appear


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=a", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def portfolio_with_em_dash_commit(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "f.txt")
    # Commit message via -F from a UTF-8 file, so git receives clean UTF-8 bytes
    # regardless of the test host's argv encoding.
    msg = repo / "_msg.txt"
    msg.write_text(f"fix(x): root cause {EM} reconfigure stdio", encoding="utf-8")
    _git(repo, "commit", "-F", str(msg))

    portfolio = tmp_path / "pf"
    (portfolio / "projects" / "proj" / ".project" / "tasks" / "done").mkdir(parents=True)
    (portfolio / "projects" / "proj" / ".project" / "tasks" / "blocked").mkdir(parents=True)
    (portfolio / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio.as_posix()}"\n'
        f'project_roots = ["{(portfolio / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    (portfolio / "projects" / "proj" / ".project" / "settings.toml").write_text(
        'id = "proj"\nname = "proj"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{repo.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(portfolio))
    return portfolio


def test_log_commit_stores_utf8_subject_not_mojibake(portfolio_with_em_dash_commit):
    res = CliRunner().invoke(
        main, ["--format", "json", "log", "commit", "--project", "proj"]
    )
    assert res.exit_code == 0, res.output

    wl = portfolio_with_em_dash_commit / "work_log.jsonl"
    assert wl.exists(), "work_log.jsonl was not written"
    summaries = [
        json.loads(line).get("summary", "")
        for line in wl.read_text(encoding="utf-8", errors="replace").splitlines()
        if line.strip()
    ]
    # The clean em-dash subject must be stored verbatim...
    assert any(EM in s for s in summaries), summaries
    # ...and the cp1252-mojibake form must NOT appear anywhere.
    assert not any(MOJIBAKE in s for s in summaries), summaries
