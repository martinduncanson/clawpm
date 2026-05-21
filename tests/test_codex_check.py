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
from click.testing import CliRunner

from clawpm.cli import main
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

    def test_lookalike_hostnames_rejected(self):
        # Substring-style guards would admit these; strict hostname check rejects.
        assert _extract_github_owner_repo("https://evilgithub.com/foo/bar.git") is None
        assert _extract_github_owner_repo("https://github.com.example.com/foo/bar.git") is None
        assert _extract_github_owner_repo("https://notgithub.com/foo/bar.git") is None

    def test_ssh_scheme_url(self):
        # ssh://git@github.com/owner/repo.git
        assert _extract_github_owner_repo("ssh://git@github.com/owner/repo.git") == (
            "owner", "repo"
        )

    def test_ssh_scheme_with_port(self):
        # The port-in-URL parse bug PRE-REVIEW caught — must not consume "22" as owner.
        assert _extract_github_owner_repo("ssh://git@github.com:22/owner/repo.git") == (
            "owner", "repo"
        )

    def test_https_with_credentials(self):
        # user:token@ prefix in URL — common for CI/PAT-based clones
        assert _extract_github_owner_repo(
            "https://user:token@github.com/foo/bar.git"
        ) == ("foo", "bar")

    def test_git_plus_https_scheme(self):
        # pip-style git+https URLs
        assert _extract_github_owner_repo(
            "git+https://github.com/foo/bar.git"
        ) == ("foo", "bar")


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

    def test_origin_non_github_but_upstream_github(self, tmp_path):
        # Multi-remote: origin is gitlab, upstream is github. Should find the github one.
        subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True, check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", "https://gitlab.com/foo/bar.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        subprocess.run(
            ["git", "remote", "add", "upstream", "https://github.com/realowner/realrepo.git"],
            cwd=tmp_path, capture_output=True, check=True,
        )
        assert _parse_github_remote(tmp_path) == ("realowner", "realrepo")


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

    def test_warning_includes_repo_path_alongside_repo(self, tmp_path):
        """Sibling-check consistency: warning carries both repo (owner/repo) AND repo_path (posix)."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        comments = {n: [{"user": {"login": "human"}}] for n in range(1, 6)}
        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock(prs, comments)):
            result = check_codex_availability(repo)
        assert result is not None
        assert result["repo"] == "o/r"
        # repo_path must match the input (posix-normalized) for consumer parity with
        # commit_drift and missing_markers warnings.
        assert result["repo_path"] == repo.as_posix()


class TestTransientFailureHandling:
    """Codex's P1 finding: transient gh api failures must not produce false-positive warnings.

    _scan_pr_for_codex now returns None for "couldn't query" vs False for "queried, no codex".
    check_codex_availability requires successful_scans ≥ MIN_SAMPLE_SIZE before warning.
    """

    def test_all_per_pr_endpoints_fail_returns_no_warning(self, tmp_path):
        """When every per-PR scan fails (None), we have no signal — no warning."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]

        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            # All per-PR endpoints fail
            return None

        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=fake_gh):
            assert check_codex_availability(repo) is None

    def test_majority_per_pr_failures_returns_no_warning(self, tmp_path):
        """Even if some PRs scan successfully, if successful_scans < MIN_SAMPLE_SIZE, no warning."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        # 2 successful (PRs 1, 2 — empty results = no codex but valid query)
        # 3 transient failures (PRs 3, 4, 5 — None)
        empty = []

        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            for n in (1, 2):
                if f"/{n}/comments" in endpoint or f"/{n}/reviews" in endpoint:
                    return empty
            return None  # PRs 3-5 fail

        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=fake_gh):
            # 2 successful scans < MIN_SAMPLE_SIZE (3) → no signal → no warning
            assert check_codex_availability(repo) is None

    def test_one_endpoint_per_pr_failing_still_counts_as_successful_scan(self, tmp_path):
        """If issue-comments succeeds but reviews fails (or vice versa), the PR was queried —
        absence is real signal, not transient failure."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        empty = [{"user": {"login": "human"}}]

        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            if "/comments" in endpoint:
                return empty  # comments endpoint succeeds with no codex
            if "/reviews" in endpoint:
                return None  # reviews endpoint fails — but comments was enough signal
            return None

        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=fake_gh):
            # All 5 PRs scanned successfully (via comments endpoint), no codex found → warning
            result = check_codex_availability(repo)
        assert result is not None
        assert result["successful_scans"] == 5

    def test_warning_reports_successful_scans_count(self, tmp_path):
        """The warning dict surfaces how many PRs were actually scanned successfully."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        comments = {n: [{"user": {"login": "human"}}] for n in range(1, 6)}
        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=self._gh_api_mock(prs, comments) if False else None):
            pass

        # Use the existing helper directly
        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            return [{"user": {"login": "human"}}]

        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=fake_gh):
            result = check_codex_availability(repo)
        assert result is not None
        assert "successful_scans" in result
        assert result["successful_scans"] == 5
        # Suggested action mentions both successful_scans AND sample_size for clarity
        assert "5 of 5" in result["suggested_action"]

    # Helper for the test above and below
    @staticmethod
    def _gh_api_mock(prs: list[dict], comments_by_pr: dict | None = None):
        comments_by_pr = comments_by_pr or {}

        def fake_gh(endpoint, timeout=10):
            if "pulls?" in endpoint:
                return prs
            for n, payload in comments_by_pr.items():
                if f"/{n}/comments" in endpoint or f"/{n}/reviews" in endpoint:
                    return payload
            return []

        return fake_gh


class TestPagination:
    """Codex's P2 finding: endpoints must use per_page=100 to avoid pagination-related
    false negatives on busy PRs (default per_page is 30)."""

    def test_per_pr_endpoints_request_per_page_100(self, tmp_path):
        """_scan_pr_for_codex must call endpoints with per_page=100."""
        repo = _make_github_repo(tmp_path)
        prs = [{"number": n} for n in range(1, 6)]
        seen_endpoints: list[str] = []

        def fake_gh(endpoint, timeout=10):
            seen_endpoints.append(endpoint)
            if "pulls?" in endpoint:
                return prs
            return []

        with patch("clawpm.codex_check._parse_github_remote", return_value=("o", "r")), \
             patch("clawpm.codex_check._gh_api_json", side_effect=fake_gh):
            check_codex_availability(repo)

        per_pr_endpoints = [e for e in seen_endpoints if "/comments" in e or "/reviews" in e]
        assert len(per_pr_endpoints) > 0
        for endpoint in per_pr_endpoints:
            assert "per_page=100" in endpoint, f"Endpoint missing pagination: {endpoint}"


# ---------------------------------------------------------------------------
# CLI flag wiring — `clawpm doctor --check-codex` end-to-end
# ---------------------------------------------------------------------------


class TestDoctorCheckCodexFlag:
    def test_flag_default_off_skips_codex_check(self, tmp_path, monkeypatch):
        """Without --check-codex, no network calls are made and no codex_availability key
        appears in the JSON output (or if it does, it's an empty list)."""
        # Set up a minimal portfolio so doctor has something to walk
        (tmp_path / "portfolio.toml").write_text(
            f'portfolio_root = "{tmp_path.as_posix()}"\n'
            f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n",
            encoding="utf-8",
        )
        (tmp_path / "projects").mkdir()
        (tmp_path / "work_log.jsonl").touch()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))

        called = {"check_codex_availability": 0}

        def fake_check(*args, **kwargs):
            called["check_codex_availability"] += 1
            return None

        runner = CliRunner()
        with patch("clawpm.codex_check.check_codex_availability", side_effect=fake_check):
            result = runner.invoke(main, ["doctor"])

        assert result.exit_code == 0, result.output
        # Without flag, check shouldn't fire
        assert called["check_codex_availability"] == 0
        data = json.loads(result.output)
        # codex_availability list present in output but empty (additive shape)
        assert data.get("codex_availability") == []

    def test_flag_on_fires_codex_check_per_project(self, tmp_path, monkeypatch):
        """With --check-codex, the check fires once per project (here: zero projects, so zero calls)."""
        (tmp_path / "portfolio.toml").write_text(
            f'portfolio_root = "{tmp_path.as_posix()}"\n'
            f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n'
            "[defaults]\nstatus = \"active\"\n",
            encoding="utf-8",
        )
        (tmp_path / "projects").mkdir()
        (tmp_path / "work_log.jsonl").touch()
        monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))

        runner = CliRunner()
        # With no projects in portfolio, the check loop doesn't iterate — but the flag
        # is recognized and the JSON shape is right. Smoke that the wiring exists.
        result = runner.invoke(main, ["doctor", "--check-codex"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert "codex_availability" in data
