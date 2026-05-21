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
from urllib.parse import urlparse


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

    Authoritative host check: parses the URL and requires hostname == "github.com"
    (not a substring match — defeats look-alikes like evilgithub.com.example.com).

    Examples:
      git@github.com:martinduncanson/clawpm.git → (martinduncanson, clawpm)
      https://github.com/martinduncanson/clawpm.git → (martinduncanson, clawpm)
      ssh://git@github.com:22/foo/bar.git → (foo, bar)  (port stripped)
      https://user:token@github.com/foo/bar.git → (foo, bar)
      https://evilgithub.com/foo/bar.git → None  (hostname mismatch)
      https://gitlab.com/foo/bar.git → None
    """
    if not url:
        return None

    # SSH-shorthand form `git@github.com:owner/repo.git` is not URL-parseable;
    # handle it explicitly via prefix match (strict — won't match look-alikes).
    if url.startswith("git@github.com:"):
        path_part = url[len("git@github.com:"):]
    else:
        # All other forms (https://, http://, ssh://, git://, git+https://, etc.)
        # parse through urlparse. Strip a leading `git+` scheme prefix to
        # accommodate pip-style URLs.
        normalized = url.removeprefix("git+")
        try:
            parsed = urlparse(normalized)
        except ValueError:
            return None
        if (parsed.hostname or "").lower() != "github.com":
            return None
        path_part = parsed.path.lstrip("/")

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


def _scan_pr_for_codex(owner: str, repo: str, pr_number: int) -> bool | None:
    """Return True if Codex authored ≥1 comment/review on the PR, False if confirmed absent,
    or None if we couldn't query (transient API failure — no signal).

    Distinguishes three states deliberately:
      - True  → Codex confirmed present on this PR
      - False → both endpoints returned successfully AND no codex login appeared
      - None  → both endpoints failed; the call gave no usable signal

    Probes two endpoints with `per_page=100` to avoid pagination-related false negatives
    on busy PRs (default per_page is 30):
      - issue comments  → repos/{o}/{r}/issues/{n}/comments  (the PR conversation)
      - pull reviews    → repos/{o}/{r}/pulls/{n}/reviews    (where Codex's review verdicts land)

    Deliberately does NOT scan inline review-thread reply comments
    (`pulls/{n}/comments`) — Codex always posts as a review or issue comment,
    never as an inline reply, per the wait-for-codex script's source-of-truth
    investigation. See `reference-codex-review-wait-script.md` for the rationale.
    """
    endpoints = [
        f"repos/{owner}/{repo}/issues/{pr_number}/comments?per_page=100",
        f"repos/{owner}/{repo}/pulls/{pr_number}/reviews?per_page=100",
    ]
    any_success = False
    for endpoint in endpoints:
        payload = _gh_api_json(endpoint)
        if not isinstance(payload, list):
            continue  # this endpoint failed; the other may still succeed
        any_success = True
        for entry in payload:
            user = entry.get("user") if isinstance(entry, dict) else None
            login = (user or {}).get("login", "") if isinstance(user, dict) else ""
            if "codex" in login.lower():
                return True
    if not any_success:
        return None  # both endpoints failed — no usable signal
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

    # Track successful vs. failed per-PR scans separately. A warning requires both:
    # (a) enough PRs in the sample (already checked above), AND
    # (b) enough SUCCESSFUL scans confirming Codex's absence — not transient failures.
    successful_scans = 0
    for pr_number in pr_numbers:
        result = _scan_pr_for_codex(owner, repo, pr_number)
        if result is True:
            return None  # confirmed presence — no warning
        if result is False:
            successful_scans += 1
        # result is None → couldn't query; skip without counting against absence

    # If we couldn't reliably query enough PRs to confirm absence, there's no signal.
    # Use the same MIN_SAMPLE_SIZE floor that gated the PR-count check above so the
    # two "insufficient signal" thresholds are consistent.
    if successful_scans < MIN_SAMPLE_SIZE:
        return None

    return {
        "repo": f"{owner}/{repo}",
        "repo_path": repo_path.as_posix(),
        "sample_size": len(pr_numbers),
        "successful_scans": successful_scans,
        "sampled_prs": pr_numbers,
        "suggested_action": (
            f"Codex bot not found on {successful_scans} of {len(pr_numbers)} sampled "
            f"closed PRs of {owner}/{repo}. Codex may not be installed on this repo, "
            f"or may not be configured to auto-review. Install / configure at "
            f"{INSTALL_URL}."
        ),
    }
