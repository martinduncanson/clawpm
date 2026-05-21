"""Tests for the doctor codex-availability check (CLAWP-008).

Covers:
- Remote URL parsing (HTTPS, SSH, non-GitHub → None, malformed)
- check_codex_availability behavior under various conditions, with gh API mocked
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from clawpm.codex_check import (
    _extract_github_owner_repo,
    _parse_github_remote,
    check_codex_availability,
    MIN_SAMPLE_SIZE,
)


# ---------------------------------------------------------------------------
# Remote URL parsing — _extract_github_owner_repo
# ---------------------------------------------------------------------------


class TestExtractGithubOwnerRepo:
    def test_ssh_form(self):
        assert _extract_github_owner_repo("git@github.com:martinduncanson/clawpm.git") == (
            "martinduncanson", "clawpm"
        )

    def test_https_form(self):
        assert _extract_github_owner_repo("https://github.com/martinduncanson/clawpm.git") == (
            "martinduncanson", "clawpm"
        )

    def test_https_no_dotgit(self):
        assert _extract_github_owner_repo("https://github.com/martinduncanson/clawpm") == (
            "martinduncanson", "clawpm"
        )

    def test_non_github_returns_none(self):
        assert _extract_github_owner_repo("https://gitlab.com/foo/bar.git") is None
        assert _extract_github_owner_repo("https://bitbucket.org/foo/bar.git") is None

    def test_malformed_returns_none(self):
        assert _extract_github_owner_repo("") is None
        assert _extract_github_owner_repo("not-a-url") is None
        assert _extract_github_owner_repo("https://github.com/") is None
        assert _extract_github_owner_repo("https://github.com/only-owner") is None

    def test_nested_path_only_takes_first_two_segments(self):
        # Some GH Enterprise / monorepo URLs have deeper paths; we only want owner/repo
        assert _extract_github_owner_repo(
            "https://github.com/org/repo/tree/main/subdir"
        ) == ("org", "repo")


# ---------------------------------------------------------------------------
# Remote parsing from a real git repo — _parse_github_remote
# ---------------------------------------------------------------------------


class TestParseGithubRemote:
    def test_returns_none_for_missing_repo(self, tmp_path):
        # tmp_path is not a git repo at all
        assert _parse_github_remote(tmp_path) is None

    def test_returns_none_for_git_repo_without_remotes(self, tmp_path):
        # Initialize bare git repo with no remotes
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        assert _parse_github_remote(tmp_path) is None

    def test_prefers_origin_over_other_remotes(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/owner1/repo1.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "upstream", "https://github.com/owner2/repo2.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        assert _parse_github_remote(tmp_path) == ("owner1", "repo1")

    def test_falls_back_to_non_github_skip(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://gitlab.com/owner/repo.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        assert _parse_github_remote(tmp_path) is None


# ---------------------------------------------------------------------------
# check_codex_availability — integration with mocked gh
# ---------------------------------------------------------------------------


def _mock_gh_response(stdout: str, returncode: int = 0):
    """Build a MagicMock that mimics subprocess.run's CompletedProcess."""
    mock_result = MagicMock()
    mock_result.returncode = returncode
    mock_result.stdout = stdout
    return mock_result


def _make_github_repo(tmp_path: Path, owner: str = "owner", repo: str = "repo") -> Path:
    """Set up a tmp dir as a git repo with a github.com origin."""
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"],
        cwd=tmp_path, capture_output=True, check=True,
    )
    return tmp_path


class TestCheckCodexAvailability:
    def test_returns_none_for_missing_repo_path(self):
        assert check_codex_availability(None) is None
        assert check_codex_availability(Path("/nonexistent/path")) is None

    def test_returns_none_for_non_github_repo(self, tmp_path):
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://gitlab.com/foo/bar.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        assert check_codex_availability(tmp_path) is None

    def _gh_api_mock(self, prs: list[dict], comments_by_pr: dict | None = None):
        """Build a fake _gh_api_json that returns PR list + per-PR comments.

        comments_by_pr: maps pr_number → list of comment dicts (same for reviews).
        Default empty list (no Codex).
        """
        comments_by_pr = comments_by_pr or {}

        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            # Match pr_number from endpoint path
            for n, payload in comments_by_pr.items():
                if f"/{n}/comments" in endpoint or f"/{n}/reviews" in endpoint:
                    return payload
            return []

        return fake_gh

    def test_returns_none_when_pr_sample_below_threshold(self, tmp_path):
        repo = _make_github_repo(tmp_path)
        with patch("clawpm.codex_check._parse_github_remote", return_value=("owner", "repo")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock([{"number": 1}, {"number": 2}])):
            assert check_codex_availability(repo) is None

    def test_returns_none_when_codex_present_in_sample(self, tmp_path):
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        # All 5 PRs have Codex in comments
        comments = {n: [{"user": {"login": "chatgpt-codex-connector[bot]"}}] for n in range(1, 6)}
        with patch("clawpm.codex_check._parse_github_remote", return_value=("owner", "repo")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock(prs, comments)):
            assert check_codex_availability(repo) is None

    def test_returns_warning_when_codex_absent_across_sample(self, tmp_path):
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        # No Codex in any PR
        comments = {n: [{"user": {"login": "human-reviewer"}}] for n in range(1, 6)}
        with patch("clawpm.codex_check._parse_github_remote", return_value=("myorg", "myrepo")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock(prs, comments)):
            result = check_codex_availability(repo)
        assert result is not None
        assert result["repo"] == "myorg/myrepo"
        assert result["sample_size"] == 5
        assert "Codex bot not found" in result["suggested_action"]

    def test_gh_api_failure_returns_none(self, tmp_path):
        repo = _make_github_repo(tmp_path)
        with patch("clawpm.codex_check._parse_github_remote", return_value=("owner", "repo")), \
             patch("clawpm.codex_check._gh_api_json", return_value=None):
            assert check_codex_availability(repo) is None

    def test_handles_codex_in_one_of_many_prs(self, tmp_path):
        """Codex appears in one PR's reviews — should NOT warn (heuristic exits early)."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        # Only PR #3 has Codex
        comments = {n: [{"user": {"login": "human"}}] for n in range(1, 6)}
        comments[3] = [{"user": {"login": "codex-bot"}}]
        with patch("clawpm.codex_check._parse_github_remote", return_value=("owner", "repo")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock(prs, comments)):
            assert check_codex_availability(repo) is None
