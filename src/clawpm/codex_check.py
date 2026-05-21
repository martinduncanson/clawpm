"""Heuristic check for whether the Codex GitHub bot is configured on a tracked project's repo.

Walks the last N closed/merged PRs and scans authors of comments + reviews for any
login matching `codex` (case-insensitive). If zero matches across the sample and the
sample is large enough to be meaningful, flag the repo as `codex-availability` warning.

This is a heuristic. It can produce false negatives if:
- The repo is Codex-configured but only triggers `@codex` on tag (not auto-review) and
  no PR in the sample tagged Codex.
- The PRs in the sample were merged before Codex was installed.

The warning text reflects both cases ("Codex may not be installed OR may not be
configured to auto-review") and points to the install URL.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


MIN_SAMPLE_SIZE = 3
"""If the repo has fewer closed PRs than this, the heuristic has too little signal.
Skip rather than false-positive on new repos."""

DEFAULT_SAMPLE_SIZE = 5
"""How many recent closed PRs to scan when checking for Codex presence."""

INSTALL_URL = "https://github.com/settings/installations"
"""Where the operator manages GitHub App installations (Codex is one of them)."""


def _parse_github_remote(repo_path: Path) -> tuple[str, str] | None:
    """Return (owner, repo) if the repo has a github.com remote, else None.

    Handles both SSH (git@github.com:owner/repo.git) and HTTPS
    (https://github.com/owner/repo.git) remote URLs. Prefers `origin`,
    falls back to `fork`, then any other remote.
    """
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None

    remotes: dict[str, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name, url = parts[0], parts[1]
        remotes.setdefault(name, url)

    preferred_order = ["origin", "fork"]
    candidate_urls: list[str] = []
    for name in preferred_order:
        if name in remotes:
            candidate_urls.append(remotes[name])
    for name, url in remotes.items():
        if name not in preferred_order:
            candidate_urls.append(url)

    for url in candidate_urls:
        owner_repo = _extract_github_owner_repo(url)
        if owner_repo is not None:
            return owner_repo
    return None


def _extract_github_owner_repo(url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a github.com remote URL, or None if not GitHub.

    Examples:
      git@github.com:martinduncanson/clawpm.git → (martinduncanson, clawpm)
      https://github.com/martinduncanson/clawpm.git → (martinduncanson, clawpm)
      https://gitlab.com/foo/bar.git → None
    """
    if "github.com" not in url:
        return None

    path_part: str | None = None
    if url.startswith("git@github.com:"):
        path_part = url[len("git@github.com:"):]
    elif "github.com/" in url:
        path_part = url.split("github.com/", 1)[1]
    if not path_part:
        return None

    path_part = path_part.removesuffix(".git").strip("/")
    segments = path_part.split("/")
    if len(segments) < 2:
        return None
    owner, repo = segments[0], segments[1]
    if not owner or not repo:
        return None
    return owner, repo


def _gh_api_json(endpoint: str, timeout: int = 10) -> list | dict | None:
    """Run `gh api <endpoint>` and parse JSON. Returns None on any failure.

    Gracefully degrades when gh is unauthenticated, network is down, rate-limited,
    or the endpoint 404s — caller should treat None as "no signal" not "no Codex".
    """
    try:
        result = subprocess.run(
            ["gh", "api", endpoint],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _scan_pr_for_codex(owner: str, repo: str, pr_number: int) -> bool:
    """Return True if any comment or review on the PR was authored by a codex-* login.

    Probes both the comments endpoint (issue comments + PR conversation) and the
    reviews endpoint (where Codex's actual review verdicts land per
    `reference-codex-review-wait-script.md`).
    """
    for kind in ("comments", "reviews"):
        endpoint = f"repos/{owner}/{repo}/issues/{pr_number}/{kind}" if kind == "comments" else f"repos/{owner}/{repo}/pulls/{pr_number}/{kind}"
        payload = _gh_api_json(endpoint)
        if not isinstance(payload, list):
            continue
        for entry in payload:
            user = entry.get("user") if isinstance(entry, dict) else None
            login = (user or {}).get("login", "") if isinstance(user, dict) else ""
            if "codex" in login.lower():
                return True
    return False


def check_codex_availability(
    repo_path: Path | None,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
) -> dict | None:
    """Return a warning dict if Codex appears absent from recent PRs, else None.

    Returns None (no warning) when:
    - repo_path is missing or doesn't exist
    - repo has no github.com remote
    - gh CLI is unavailable or unauthenticated
    - PR sample is below MIN_SAMPLE_SIZE (insufficient signal)
    - At least one codex-* author appears in the sample

    Returns a warning dict when:
    - github remote present, sample meets MIN_SAMPLE_SIZE, and zero codex-* authors
      appear across all sampled PRs' comments + reviews.
    """
    if repo_path is None or not repo_path.exists():
        return None

    parsed = _parse_github_remote(repo_path)
    if parsed is None:
        return None
    owner, repo = parsed

    prs_payload = _gh_api_json(
        f"repos/{owner}/{repo}/pulls?state=closed&per_page={sample_size}"
    )
    if not isinstance(prs_payload, list):
        return None

    pr_numbers = [pr["number"] for pr in prs_payload if isinstance(pr, dict) and "number" in pr]
    if len(pr_numbers) < MIN_SAMPLE_SIZE:
        return None

    for pr_number in pr_numbers:
        if _scan_pr_for_codex(owner, repo, pr_number):
            return None

    return {
        "repo": f"{owner}/{repo}",
        "sample_size": len(pr_numbers),
        "sampled_prs": pr_numbers,
        "suggested_action": (
            f"Codex bot not found on the last {len(pr_numbers)} closed PRs of "
            f"{owner}/{repo}. Codex may not be installed on this repo, or may "
            f"not be configured to auto-review. Install / configure at {INSTALL_URL}."
        ),
    }
