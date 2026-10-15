"""CLAWP-147-001 -- ``clawpm pr verify`` head attestation (gh/git mocked at subprocess)."""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.pr_verify import PrVerifyError, verify_pr

HEAD = "a" * 40
OTHER = "b" * 40
REPO = "acme/widgets"


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class FakeEnv:
    """Routes the subprocess seam; records every argv so tests can assert read-only."""

    def __init__(self, **over):
        self.pr = {"headRefOid": HEAD, "headRefName": "feat/x", "mergeable": "MERGEABLE", "state": "OPEN", "url": "https://github.com/acme/widgets/pull/7"}
        self.runs = [{"name": "test", "status": "completed", "conclusion": "success", "head_sha": HEAD}]
        self.run_total = None
        self.statuses = []
        self.status_total = None
        self.remote_sha = HEAD
        self.calls = []
        self.override = {}
        self.__dict__.update(over)

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        key = args[0]
        if key in self.override:
            r = self.override[key]
            if isinstance(r, Exception):
                raise r
            return r
        if args[:3] == ["git", "remote", "get-url"]:
            return _cp("git@github.com:acme/widgets.git\n")
        if args[:2] == ["git", "ls-remote"]:
            return _cp(f"{self.remote_sha}\trefs/heads/feat/x\n" if self.remote_sha else "")
        if args[:2] == ["git", "rev-parse"]:
            return _cp("", rc=1)
        if args[:3] == ["gh", "pr", "view"]:
            return _cp(json.dumps(self.pr))
        if args[0] == "gh" and "check-runs" in args[2]:
            total = self.run_total if self.run_total is not None else len(self.runs)
            return _cp(json.dumps({"total_count": total, "check_runs": self.runs}))
        if args[0] == "gh" and "/status" in args[2]:
            total = self.status_total if self.status_total is not None else len(self.statuses)
            return _cp(json.dumps({"total_count": total, "statuses": self.statuses}))
        raise AssertionError(f"unexpected subprocess call: {args}")


def _verify(env, **kw):
    with patch("clawpm.pr_verify.subprocess.run", env):
        return verify_pr(7, **kw)


def test_all_facts_hold_prints_full_sha_merge_line_and_never_merges():
    env = FakeEnv()
    res = _verify(env, expect_head=HEAD)
    assert res["ok"] is True and res["failed"] == []
    assert res["merge_command"] == f"gh pr merge 7 --repo {REPO} --squash --match-head-commit {HEAD}"
    assert len(HEAD) == 40
    assert not any(c[:3] == ["gh", "pr", "merge"] for c in env.calls)
    assert all(c[:2] != ["git", "push"] for c in env.calls)


def test_expect_head_mismatch_names_both_shas():
    res = _verify(FakeEnv(), expect_head=OTHER)
    assert res["ok"] is False and res["failed"] == ["head"]
    text = " ".join(res["head"]["reasons"])
    assert OTHER in text and HEAD in text
    assert res["merge_command"] is None


def test_remote_branch_head_differs_from_pr_head():
    res = _verify(FakeEnv(remote_sha=OTHER))
    assert res["ok"] is False and res["failed"] == ["head"]
    assert OTHER in " ".join(res["head"]["reasons"]) and HEAD in " ".join(res["head"]["reasons"])


def test_branch_missing_on_remote_is_a_loud_error_not_a_failed_fact():
    # Deleted branch / PR head living in another repo: the facts cannot be established.
    with pytest.raises(PrVerifyError) as ei:
        _verify(FakeEnv(remote_sha=None))
    assert ei.value.code == "remote_branch_missing"
    assert "fork" in ei.value.message and "feat/x" in ei.value.message


def test_pending_ci_is_not_ok_and_named():
    env = FakeEnv(runs=[{"name": "test", "status": "in_progress", "conclusion": None, "head_sha": HEAD}])
    res = _verify(env)
    assert res["ok"] is False and res["failed"] == ["ci"]
    assert "pending: test" in res["ci"]["reasons"]


def test_failing_ci_is_not_ok_and_named():
    env = FakeEnv(runs=[{"name": "lint", "status": "completed", "conclusion": "failure", "head_sha": HEAD}])
    res = _verify(env)
    assert res["ok"] is False and "failing: lint=failure" in res["ci"]["reasons"]


def test_failing_legacy_status_is_not_ok():
    env = FakeEnv(statuses=[{"context": "ci/old", "state": "failure"}])
    res = _verify(env)
    assert res["ok"] is False and "ci/old=failure" in " ".join(res["ci"]["reasons"])


def test_no_checks_at_all_is_not_a_pass():
    res = _verify(FakeEnv(runs=[]))
    assert res["ok"] is False and "no CI checks" in res["ci"]["reasons"][0]


@pytest.mark.parametrize("mergeable", ["CONFLICTING", "UNKNOWN"])
def test_conflicting_or_unknown_mergeable_is_not_ok(mergeable):
    env = FakeEnv()
    env.pr["mergeable"] = mergeable
    res = _verify(env)
    assert res["ok"] is False and res["failed"] == ["mergeable"]
    assert mergeable in res["mergeable"]["reasons"][0]


def test_closed_pr_is_not_ok():
    env = FakeEnv()
    env.pr["state"] = "MERGED"
    res = _verify(env)
    assert res["ok"] is False and res["failed"] == ["mergeable"]


def test_check_run_for_a_different_sha_is_a_loud_error_not_a_pass():
    env = FakeEnv(runs=[{"name": "test", "status": "completed", "conclusion": "success", "head_sha": OTHER}])
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "ci_sha_mismatch"


def test_truncated_check_listing_is_a_loud_error():
    env = FakeEnv(run_total=150)
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "ci_incomplete_listing"


def test_truncated_status_listing_is_a_loud_error():
    sts = [{"context": f"ci/{i}", "state": "success"} for i in range(30)]
    with pytest.raises(PrVerifyError) as ei:
        _verify(FakeEnv(statuses=sts, status_total=31))
    assert ei.value.code == "ci_incomplete_listing"


def test_status_listing_requests_a_full_page():
    env = FakeEnv()
    _verify(env)
    status_calls = [c for c in env.calls if c[0] == "gh" and "/status" in c[2]]
    assert status_calls and all("per_page=100" in c[2] for c in status_calls)


@pytest.mark.parametrize("total", [None, "3", True])
def test_status_response_without_int_total_count_is_gh_bad_json(total):
    env = FakeEnv()

    def call(args, **kw):
        if args[0] == "gh" and "/status" in args[2]:
            doc = {"statuses": []}
            if total is not None:
                doc["total_count"] = total
            return _cp(json.dumps(doc))
        return env(args, **kw)

    with pytest.raises(PrVerifyError) as ei:
        _verify(call)
    assert ei.value.code == "gh_bad_json"


def test_neutral_and_skipped_stay_green_but_are_listed_non_gating():
    env = FakeEnv(runs=[
        {"name": "test", "status": "completed", "conclusion": "success", "head_sha": HEAD},
        {"name": "lint", "status": "completed", "conclusion": "skipped", "head_sha": HEAD},
        {"name": "docs", "status": "completed", "conclusion": "neutral", "head_sha": HEAD},
    ])
    res = _verify(env)
    assert res["ok"] is True
    assert res["ci"]["skipped_or_neutral"] == ["lint=skipped", "docs=neutral"]


def test_gh_missing_is_a_loud_error():
    env = FakeEnv(override={"gh": FileNotFoundError("gh")})
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "gh_missing"


def test_gh_unauthenticated_is_a_loud_error():
    env = FakeEnv(override={"gh": _cp("", rc=4, stderr="To get started with GitHub CLI, please run:  gh auth login")})
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "gh_unauthenticated"


def test_unparseable_gh_json_is_a_loud_error():
    env = FakeEnv(override={"gh": _cp("<html>not json</html>")})
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "gh_bad_json"


def test_pr_view_without_full_head_sha_is_a_loud_error():
    env = FakeEnv()
    env.pr["headRefOid"] = "abc123"
    with pytest.raises(PrVerifyError) as ei:
        _verify(env)
    assert ei.value.code == "gh_bad_json"


def test_short_expect_head_rejected():
    with pytest.raises(PrVerifyError) as ei:
        _verify(FakeEnv(), expect_head="abc1234")
    assert ei.value.code == "bad_expect_head"


# --- CLI: exit codes and structured output --------------------------------


def _invoke(env, *args):
    with patch("clawpm.pr_verify.subprocess.run", env):
        return CliRunner().invoke(main, ["--format", "json", "pr", "verify", "7", *args])


def test_cli_ok_exit_0_and_json_has_merge_line():
    r = _invoke(FakeEnv(), "--expect-head", HEAD)
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["merge_command"].endswith(f"--match-head-commit {HEAD}")


def test_cli_failed_fact_exit_1_with_ok_false():
    r = _invoke(FakeEnv(), "--expect-head", OTHER)
    assert r.exit_code == 1
    assert json.loads(r.stdout)["ok"] is False


def test_cli_missing_remote_branch_exit_2_structured_error():
    r = _invoke(FakeEnv(remote_sha=None))
    assert r.exit_code == 2
    assert json.loads(r.stderr)["error"] == "remote_branch_missing"
    assert '"ok": true' not in r.stdout


def test_cli_gh_missing_exit_2_structured_error_no_partial_pass():
    r = _invoke(FakeEnv(override={"gh": FileNotFoundError("gh")}))
    assert r.exit_code == 2
    err = json.loads(r.stderr)
    assert err["error"] == "gh_missing"
    assert '"ok": true' not in r.stdout


# --- CLAWP-151: merged mode (require_merged=True) ---

MERGE = "c" * 40


def _merged_env(**pr_over):
    env = FakeEnv()
    env.pr["state"] = "MERGED"
    env.pr["mergeable"] = "UNKNOWN"
    env.pr["mergeCommit"] = {"oid": MERGE}
    env.pr.update(pr_over)
    return env


def test_merged_mode_ok_with_merge_commit_and_no_merge_command():
    env = _merged_env()
    res = _verify(env, expect_head=HEAD, require_merged=True)
    assert res["ok"] is True and res["failed"] == []
    assert res["merge_commit"] == MERGE
    assert res["merge_command"] is None
    assert res["merged"]["ok"] is True and "mergeable" not in res
    pr_view = next(c for c in env.calls if c[:3] == ["gh", "pr", "view"])
    assert "mergeCommit" in pr_view[pr_view.index("--json") + 1]


def test_merged_mode_open_pr_fails_the_merged_fact():
    env = FakeEnv()  # OPEN + MERGEABLE: fine for default mode, not for merged mode
    res = _verify(env, require_merged=True)
    assert res["ok"] is False and res["failed"] == ["merged"]
    assert res["merge_commit"] is None


@pytest.mark.parametrize("merge_commit", [None, {}, {"oid": ""}, {"oid": "abc123"}, {"oid": "C" * 40 + "0"}])
def test_merged_mode_without_full_merge_commit_is_not_ok(merge_commit):
    env = _merged_env(mergeCommit=merge_commit)
    res = _verify(env, require_merged=True)
    assert res["ok"] is False and res["failed"] == ["merged"]
    assert res["merge_commit"] is None


def test_merged_mode_still_checks_head_and_ci():
    env = _merged_env()
    env.runs = [{"name": "test", "status": "completed", "conclusion": "failure", "head_sha": HEAD}]
    res = _verify(env, expect_head=OTHER, require_merged=True)
    assert res["ok"] is False and res["failed"] == ["head", "ci"]


def test_merged_mode_deleted_remote_branch_is_still_a_loud_error():
    with pytest.raises(PrVerifyError) as ei:
        env = _merged_env()
        env.remote_sha = None
        _verify(env, require_merged=True)
    assert ei.value.code == "remote_branch_missing"


def test_default_mode_result_has_no_merged_keys():
    res = _verify(FakeEnv())
    assert "merged" not in res and "merge_commit" not in res
    env = FakeEnv()
    _verify(env)
    pr_view = next(c for c in env.calls if c[:3] == ["gh", "pr", "view"])
    assert pr_view[pr_view.index("--json") + 1] == "headRefOid,headRefName,mergeable,state,url"
