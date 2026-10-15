"""Head attestation for a pull request (CLAWP-147-001, gap G1).

``verify_pr`` answers one question before an operator merges: do the pushed
branch, the PR head, the CI results and the mergeability verdict all describe
the SAME commit?  Four facts, each reported separately; ``ok`` is true only if
every one holds.

* ``head``      remote branch head == PR ``headRefOid`` (== ``expect_head`` if given)
* ``ci``        every check run / commit status on that exact SHA is complete and green
* ``mergeable`` the PR is OPEN and GitHub reports MERGEABLE (UNKNOWN is not a pass)
* ``merge_command``  built from live data with the FULL 40-char SHA, only when ok

``require_merged=True`` (CLAWP-151) attests a PR that is ALREADY merged: the
``mergeable`` fact is replaced by a ``merged`` fact (state MERGED and a full
40-char ``mergeCommit.oid``), the result gains ``merge_commit``, and
``merge_command`` stays None.  The head and CI facts are unchanged.

This module is read-only: it never merges, comments or writes to the remote.
Anything that stops it proving a fact (gh missing, unauthenticated, unparseable
JSON, a check run reported for a different SHA, a truncated check or status
listing, a branch missing from the remote) raises
:class:`PrVerifyError` -- a loud failure, never a partial pass.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import Any

from clawpm.codex_check import _extract_github_owner_repo

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_GREEN_CONCLUSIONS = {"success", "neutral", "skipped"}
_TIMEOUT = 30
_CHECKS_PER_PAGE = 100


class PrVerifyError(Exception):
    """The attestation could not be computed (as opposed to computed and failed)."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _run(args: list[str]) -> str:
    """Run a read-only subprocess; return stdout or raise PrVerifyError."""
    tool = args[0]
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        raise PrVerifyError(f"{tool}_missing", f"`{tool}` is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise PrVerifyError(f"{tool}_timeout", f"`{' '.join(args[:3])}` timed out after {_TIMEOUT}s")
    except OSError as exc:
        raise PrVerifyError(f"{tool}_failed", f"could not run `{tool}`: {exc}")
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if tool == "gh" and re.search(r"auth login|not logged in|authentication|GH_TOKEN|HTTP 401", stderr, re.I):
            raise PrVerifyError("gh_unauthenticated", "gh is not authenticated (run `gh auth login`)", {"stderr": stderr})
        raise PrVerifyError(
            f"{tool}_failed",
            f"`{' '.join(args[:4])}` exited {proc.returncode}",
            {"stderr": stderr},
        )
    return proc.stdout or ""


def _gh_json(args: list[str]) -> dict[str, Any]:
    out = _run(["gh", *args])
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, ValueError):
        raise PrVerifyError("gh_bad_json", f"gh returned unparseable JSON for `gh {' '.join(args[:3])}`", {"stdout": out[:200]})
    if not isinstance(data, dict):
        raise PrVerifyError("gh_bad_json", f"gh returned a non-object for `gh {' '.join(args[:3])}`")
    return data


def _resolve_repo(repo: str | None, remote: str) -> str:
    if repo:
        return repo
    url = _run(["git", "remote", "get-url", remote]).strip()
    parsed = _extract_github_owner_repo(url)
    if not parsed:
        raise PrVerifyError("repo_unresolved", f"cannot derive owner/repo from remote '{remote}' ({url!r}); pass --repo")
    return f"{parsed[0]}/{parsed[1]}"


def _remote_head(remote: str, branch: str) -> str:
    out = _run(["git", "ls-remote", remote, f"refs/heads/{branch}"])
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and _FULL_SHA.match(sha.strip()):
            return sha.strip()
    # A deleted branch, or a PR whose head lives in another repo: nothing to attest against.
    raise PrVerifyError(
        "remote_branch_missing",
        f"branch '{branch}' not found on remote '{remote}' (deleted, or the PR head is in another repo)",
    )


def _local_head(branch: str) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    sha = (proc.stdout or "").strip()
    return sha if proc.returncode == 0 and _FULL_SHA.match(sha) else None


def _check_head(pr_head: str, remote_head: str, expect: str | None) -> dict[str, Any]:
    reasons = []
    if remote_head != pr_head:
        reasons.append(f"remote branch head {remote_head} != PR head {pr_head}")
    if expect is not None and expect != pr_head:
        reasons.append(f"expected head {expect} != PR head {pr_head}")
    return {
        "ok": not reasons,
        "pr_head": pr_head,
        "remote_head": remote_head,
        "expect_head": expect,
        "reasons": reasons,
    }


def _complete_listing(doc: dict[str, Any], key: str, what: str) -> list[Any]:
    """``doc[key]`` only if the listing is provably whole (``total_count`` == len)."""
    items, total = doc.get(key), doc.get("total_count")
    if not isinstance(items, list) or not isinstance(total, int) or isinstance(total, bool):
        raise PrVerifyError("gh_bad_json", f"{what} response lacks {key}/total_count")
    if total > len(items):
        raise PrVerifyError(
            "ci_incomplete_listing",
            f"{what} listing truncated ({len(items)} of {total}); refusing a partial verdict",
        )
    return items


def _check_ci(repo: str, sha: str) -> dict[str, Any]:
    """CI state tied to ``sha`` by querying the commit's own check-runs/status.

    ``gh pr view --json statusCheckRollup`` carries no per-check SHA, so it
    cannot prove which commit was checked; the commit-scoped REST endpoints
    can, and each check run echoes its ``head_sha`` which we cross-check.
    """
    runs = _complete_listing(
        _gh_json(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page={_CHECKS_PER_PAGE}"]),
        "check_runs", "check-runs",
    )
    statuses = _complete_listing(
        _gh_json(["api", f"repos/{repo}/commits/{sha}/status?per_page={_CHECKS_PER_PAGE}"]),
        "statuses", "commit status",
    )

    failing: list[str] = []
    pending: list[str] = []
    # Green by GitHub's own merge gate, so non-gating here; surfaced so a human can see them.
    soft: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            raise PrVerifyError("gh_bad_json", "check run entry is not an object")
        if run.get("head_sha") != sha:
            raise PrVerifyError(
                "ci_sha_mismatch",
                f"check run {run.get('name')!r} reports head_sha {run.get('head_sha')!r}, not {sha}",
            )
        name = str(run.get("name"))
        if run.get("status") != "completed":
            pending.append(name)
        elif run.get("conclusion") not in _GREEN_CONCLUSIONS:
            failing.append(f"{name}={run.get('conclusion')}")
        elif run.get("conclusion") != "success":
            soft.append(f"{name}={run.get('conclusion')}")
    for st in statuses:
        if not isinstance(st, dict):
            raise PrVerifyError("gh_bad_json", "commit status entry is not an object")
        state, name = st.get("state"), str(st.get("context"))
        if state == "pending":
            pending.append(name)
        elif state != "success":
            failing.append(f"{name}={state}")

    n_checks = len(runs) + len(statuses)
    reasons = []
    if n_checks == 0:
        reasons.append("no CI checks reported on this commit")
    if pending:
        reasons.append(f"pending: {', '.join(pending)}")
    if failing:
        reasons.append(f"failing: {', '.join(failing)}")
    return {
        "ok": not reasons, "sha": sha, "checks": n_checks, "pending": pending, "failing": failing,
        "skipped_or_neutral": soft, "reasons": reasons,
    }


def _check_mergeable(state: str | None, mergeable: str | None) -> dict[str, Any]:
    reasons = []
    if state != "OPEN":
        reasons.append(f"PR state is {state}, not OPEN")
    if mergeable != "MERGEABLE":
        reasons.append(f"mergeable is {mergeable} (need MERGEABLE)")
    return {"ok": not reasons, "state": state, "mergeable": mergeable, "reasons": reasons}


def _check_merged(state: str | None, merge_commit: Any) -> dict[str, Any]:
    oid = merge_commit.get("oid") if isinstance(merge_commit, dict) else None
    oid = oid.lower() if isinstance(oid, str) else None
    reasons = []
    if state != "MERGED":
        reasons.append(f"PR state is {state}, not MERGED")
    if oid is None or not _FULL_SHA.match(oid):
        reasons.append("PR has no full 40-char merge commit sha")
        oid = None
    return {"ok": not reasons, "state": state, "merge_commit": oid, "reasons": reasons}


def verify_pr(
    pr_number: int,
    *,
    expect_head: str | None = None,
    repo: str | None = None,
    remote: str = "fork",
    require_merged: bool = False,
) -> dict[str, Any]:
    """Attest the four facts for ``pr_number``; raise PrVerifyError if unprovable."""
    if expect_head is not None:
        expect_head = expect_head.strip().lower()
        if not _FULL_SHA.match(expect_head):
            raise PrVerifyError("bad_expect_head", "--expect-head must be a full 40-char hex SHA", {"got": expect_head})

    repo_slug = _resolve_repo(repo, remote)
    pr = _gh_json([
        "pr", "view", str(pr_number), "--repo", repo_slug,
        "--json",
        "headRefOid,headRefName,mergeable,state,url" + (",mergeCommit" if require_merged else ""),
    ])
    pr_head = str(pr.get("headRefOid") or "").lower()
    branch = pr.get("headRefName")
    if not _FULL_SHA.match(pr_head) or not isinstance(branch, str) or not branch:
        raise PrVerifyError("gh_bad_json", "gh pr view lacks a full headRefOid / headRefName", {"got": {k: pr.get(k) for k in ("headRefOid", "headRefName")}})

    head = _check_head(pr_head, _remote_head(remote, branch), expect_head)
    ci = _check_ci(repo_slug, pr_head)
    if require_merged:
        merge = _check_merged(pr.get("state"), pr.get("mergeCommit"))
        merge_key = "merged"
    else:
        merge = _check_mergeable(pr.get("state"), pr.get("mergeable"))
        merge_key = "mergeable"
    ok = head["ok"] and ci["ok"] and merge["ok"]

    result: dict[str, Any] = {
        "ok": ok,
        "pr": pr_number,
        "repo": repo_slug,
        "branch": branch,
        "url": pr.get("url"),
        "local_head": _local_head(branch),  # informational: a stale checkout is not a PR defect
        "head": head,
        "ci": ci,
        merge_key: merge,
        "failed": [name for name, fact in (("head", head), ("ci", ci), (merge_key, merge)) if not fact["ok"]],
        "merge_command": (
            f"gh pr merge {pr_number} --repo {repo_slug} --squash --match-head-commit {pr_head}"
            if ok and not require_merged
            else None
        ),
    }
    if require_merged:
        result["merge_commit"] = merge["merge_commit"]
    return result
