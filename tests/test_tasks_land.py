"""CLAWP-147-002 -- ``clawpm tasks land`` post-merge landing transaction.

Two layers: refusal paths run against a fake git/gh at the subprocess seam so
"nothing changed" is asserted from the recorded argv; the success path and the
push verification run against a real temporary repo + bare remote (only ``gh``
is faked there).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.tasks import add_task

_REAL_RUN = subprocess.run
MERGE = "c" * 40
HEAD = "a" * 40
TIP = "d" * 40
REPO = "acme/widgets"
MUTATING = (("merge",), ("add",), ("commit",), ("push",), ("worktree", "remove"))


def _cp(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


def _norm(args):
    """Drop git's global options (``-c k=v``, ``--no-optional-locks``) so the fakes match on the subcommand."""
    a, i = list(args), 1
    while a[0] == "git" and i < len(a):
        if a[i] == "-c":
            i += 2
        elif a[i] == "--no-optional-locks":
            i += 1
        else:
            break
    return [a[0], *a[i:]]


def _invoke(*args):
    res = CliRunner().invoke(main, ["--format", "json", "tasks", "land", *args], catch_exceptions=False)
    return res


def _payload(res):
    text = res.output.strip()
    return json.loads(text[text.index("{"):])


# --------------------------------------------------------------------------
# Fake git/gh for the refusal paths
# --------------------------------------------------------------------------


class FakeEnv:
    def __init__(self, toplevel: Path, **over):
        self.toplevel = toplevel
        self.git_dir = str(toplevel / ".git")
        self.common_dir = str(toplevel / ".git")
        self.branch = "main"
        self.dirty: list[str] = []
        self.pr = {"state": "MERGED", "mergeCommit": {"oid": MERGE}, "baseRefName": "main", "headRefName": "clawpm/x", "url": "https://github.com/acme/widgets/pull/7"}
        self.head = HEAD
        self.tip = TIP
        self.remote_tip = TIP
        self.merge_in_base = True
        self.head_in_base = True
        self.calls: list[list[str]] = []
        self.gh_error: Exception | None = None
        self.remote_url = "git@github.com:acme/widgets.git"
        self.__dict__.update(over)

    def mutating(self):
        out = []
        for raw in self.calls:
            c = _norm(raw)
            sub = tuple(c[1:3]) if c[:2] == ["git", "worktree"] else tuple(c[1:2])
            if c[0] == "git" and any(sub[: len(m)] == m for m in MUTATING):
                out.append(raw)
        return out

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        a = _norm(args)
        if a[0] == "gh":
            if self.gh_error:
                raise self.gh_error
            return _cp(json.dumps(self.pr))
        sub = a[1]
        if a[1:3] == ["remote", "get-url"]:
            return _cp(self.remote_url + "\n")
        if a[1:3] == ["rev-parse", "--show-toplevel"]:
            return _cp(str(self.toplevel) + "\n")
        if "--git-common-dir" in a:
            return _cp(f"{self.git_dir}\n{self.common_dir}\n")
        if sub == "symbolic-ref":
            return _cp(f"{self.branch}\n", rc=0 if self.branch else 1)
        if sub == "status":
            return _cp("".join(f" M {p}\0" for p in self.dirty))
        if sub == "fetch":
            return _cp()
        if a[1:3] == ["rev-parse", "--verify"]:
            return _cp(self.tip + "\n")
        if sub == "ls-remote":
            return _cp(f"{self.remote_tip}\trefs/heads/main\n")
        if sub == "cat-file":
            return _cp()
        if a[1:3] == ["merge-base", "--is-ancestor"]:
            if a[3] == MERGE:
                return _cp(rc=0 if self.merge_in_base else 1)
            return _cp(rc=0 if self.head_in_base else 1)
        if a[1:3] == ["rev-parse", "HEAD"]:
            return _cp(self.head + "\n")
        if sub == "worktree" and a[2] == "list":
            return _cp(f"worktree {self.toplevel}\nHEAD {self.head}\nbranch refs/heads/main\n")
        if sub == "diff":  # transition()'s files_changed enrichment
            return _cp()
        raise AssertionError(f"unexpected subprocess call: {a}")


@pytest.fixture
def land_env(isolated_portfolio, monkeypatch):
    task = add_task(isolated_portfolio.config, "test", "Land me")
    monkeypatch.chdir(isolated_portfolio.project_dir)
    task_file = isolated_portfolio.tasks_dir / f"{task.id}.md"
    assert task_file.exists()

    def make(**over):
        return FakeEnv(isolated_portfolio.project_dir, **over)

    return isolated_portfolio, task, task_file, make


def _run(env, task, *extra):
    with patch("subprocess.run", env):
        return _invoke(task.id, "--pr", "7", "-p", "test", "--repo", REPO, *extra)


def _assert_refused(res, env, task_file, code):
    assert res.exit_code == 1, res.output
    assert code in res.output
    assert env.mutating() == []
    assert not any(_norm(c)[1:2] == ["fetch"] for c in env.calls)  # land never fetches
    assert task_file.exists()


def test_pr_not_merged_refuses_with_nothing_changed(land_env):
    _, task, task_file, make = land_env
    env = make(pr={"state": "OPEN", "mergeCommit": None, "baseRefName": "main", "headRefName": "x", "url": "u"})
    _assert_refused(_run(env, task), env, task_file, "pr_not_merged")


def test_merge_commit_not_ancestor_of_remote_base_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(merge_in_base=False)
    _assert_refused(_run(env, task), env, task_file, "merge_commit_not_in_base")


def test_stale_tracking_ref_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(tip=HEAD, remote_tip=TIP)
    res = _run(env, task)
    _assert_refused(res, env, task_file, "remote_not_fetched")
    assert "git fetch fork" in res.output


def test_dirty_unrelated_tracked_path_refuses_and_names_it(land_env):
    _, task, task_file, make = land_env
    env = make(dirty=["src/clawpm/cli/tasks.py"])
    res = _run(env, task)
    _assert_refused(res, env, task_file, "dirty_checkout")
    assert "src/clawpm/cli/tasks.py" in res.output


def test_linked_worktree_is_refused(land_env, tmp_path):
    _, task, task_file, make = land_env
    env = make(git_dir=str(tmp_path / "main" / ".git" / "worktrees" / "wt"), common_dir=str(tmp_path / "main" / ".git"))
    _assert_refused(_run(env, task), env, task_file, "inside_worktree")


def test_wrong_branch_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(branch="feature")
    _assert_refused(_run(env, task), env, task_file, "wrong_branch")


def test_non_fast_forward_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(head_in_base=False)
    _assert_refused(_run(env, task), env, task_file, "not_fast_forward")


def test_gh_missing_is_a_loud_refusal(land_env):
    _, task, task_file, make = land_env
    env = make(gh_error=FileNotFoundError("gh"))
    _assert_refused(_run(env, task), env, task_file, "gh_missing")


def test_already_done_task_is_refused(isolated_portfolio, monkeypatch):
    task = add_task(isolated_portfolio.config, "test", "Done already")
    (isolated_portfolio.tasks_dir / f"{task.id}.md").rename(isolated_portfolio.tasks_dir / "done" / f"{task.id}.md")
    monkeypatch.chdir(isolated_portfolio.project_dir)
    env = FakeEnv(isolated_portfolio.project_dir)
    res = _run(env, task)
    assert res.exit_code == 1 and "task_already_done" in res.output
    assert env.mutating() == []


# --------------------------------------------------------------------------
# Real git: temp repo + bare remote, only gh is faked
# --------------------------------------------------------------------------


def _g(cwd, *args):
    return _REAL_RUN(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()


class RealGit:
    """Pass git through to the real binary; answer gh from canned data."""

    def __init__(self, pr, lie_after_push: str | None = None, timeout_on: str | None = None, on_merge=None, rewrite_merge=None, after_push=None):
        self.pr = pr
        self.after_push = after_push
        self.timeout_on = timeout_on
        self.on_merge = on_merge  # called just before git merge runs; may mutate the checkout, may raise
        self.rewrite_merge = rewrite_merge  # replaces the merge argv (simulates inherited config changing git's behaviour)
        self.calls: list[list[str]] = []
        self.envs: list[dict | None] = []
        self.lie_after_push = lie_after_push
        self.pushed = False

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        self.envs.append(kw.get("env"))
        if args[0] == "gh":
            return _cp(json.dumps(self.pr))
        if self.timeout_on and _norm(args)[:2] == ["git", self.timeout_on]:
            raise subprocess.TimeoutExpired(args, 1)
        if _norm(args)[:2] == ["git", "merge"]:
            if self.on_merge:
                self.on_merge()
            if self.rewrite_merge:
                args = self.rewrite_merge(args)
        if _norm(args)[:2] == ["git", "push"]:
            self.pushed = True
            proc = _REAL_RUN(args, **kw)
            if self.after_push:
                self.after_push()
            return proc
        if self.lie_after_push and self.pushed and _norm(args)[:2] == ["git", "ls-remote"]:
            return _cp(f"{self.lie_after_push}\trefs/heads/main\n")
        return _REAL_RUN(args, **kw)


@pytest.fixture
def real_repo(isolated_portfolio, tmp_path, monkeypatch):
    repo = isolated_portfolio.project_dir
    nohooks = tmp_path / "nohooks"
    nohooks.mkdir()
    _g(repo, "init", "-b", "main")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"), ("core.autocrlf", "false"), ("core.hooksPath", str(nohooks))):
        _g(repo, "config", k, v)
    (repo / ".gitignore").write_text("*.lock\n", encoding="utf-8")
    task = add_task(isolated_portfolio.config, "test", "Land me")
    _g(repo, "add", "-A")
    _g(repo, "commit", "-m", "base")
    bare = tmp_path / "remote.git"
    _g(tmp_path, "init", "--bare", "-b", "main", str(bare))
    _g(repo, "remote", "add", "fork", str(bare))
    _g(repo, "push", "fork", "main")
    other = tmp_path / "other"
    _g(tmp_path, "clone", str(bare), str(other))
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"), ("core.hooksPath", str(nohooks))):
        _g(other, "config", k, v)
    (other / "feature.txt").write_text("the merged PR\n", encoding="utf-8")
    _g(other, "add", "feature.txt")
    _g(other, "commit", "-m", "feat: the merged PR (#7)")
    _g(other, "push", "origin", "main")
    merge = _g(other, "rev-parse", "HEAD")
    monkeypatch.chdir(repo)
    monkeypatch.setattr("clawpm.land._remote_slug", lambda remote: REPO)  # the temp remote is a bare path, not github
    pr = {"state": "MERGED", "mergeCommit": {"oid": merge}, "baseRefName": "main", "headRefName": "clawpm/x", "url": "https://github.com/acme/widgets/pull/7"}
    return isolated_portfolio, repo, bare, task, merge, pr


def _land(task, pr, *extra, lie_after_push=None, timeout_on=None, on_merge=None, rewrite_merge=None, env=None, after_push=None, prefetch=True):
    if prefetch:  # `tasks land` never fetches: the operator runs `git fetch <remote>` first
        _REAL_RUN(["git", "fetch", "--no-recurse-submodules", "fork"], cwd=str(Path.cwd()), check=True, capture_output=True)
    fake = RealGit(pr, lie_after_push, timeout_on, on_merge, rewrite_merge, after_push)
    with patch("subprocess.run", fake), patch.dict("os.environ", env or {}):
        res = _invoke(task.id, "--pr", "7", "-p", "test", "--repo", REPO, *extra)
    return res, fake


def test_success_one_task_only_commit_no_trailer_and_ff(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr)
    assert res.exit_code == 0, res.output
    out = _payload(res)
    assert out["ok"] and out["fast_forward"]["from"] == before and out["fast_forward"]["to"] == merge
    assert _g(repo, "rev-list", "--count", f"{merge}..HEAD") == "1"
    assert _g(repo, "rev-parse", "HEAD^") == merge
    files = _g(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "HEAD").splitlines()
    assert sorted(files) == sorted([f".project/tasks/{task.id}.md", f".project/tasks/done/{task.id}.md"])
    body = _g(repo, "log", "-1", "--format=%B")
    assert body == f"chore: mark {task.id} done (PR #7 merged)"
    assert "Co-Authored-By" not in body and "Generated with" not in body
    assert (repo / "feature.txt").exists()
    assert (port.tasks_dir / "done" / f"{task.id}.md").exists() and not (port.tasks_dir / f"{task.id}.md").exists()
    assert _g(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert out["pushed"] is None
    assert _g(repo, "rev-parse", "fork/main") == merge  # not pushed without --push


def test_push_verifies_remote_head_equals_local(real_repo):
    _, repo, bare, task, merge, pr = real_repo
    res, fake = _land(task, pr, "--push")
    assert res.exit_code == 0, res.output
    head = _g(repo, "rev-parse", "HEAD")
    assert _g(bare, "rev-parse", "refs/heads/main") == head
    assert _payload(res)["pushed"]["head"] == head
    assert not any("--force" in c or "-f" in c for c in fake.calls if _norm(c)[:2] == ["git", "push"])


def test_push_verification_mismatch_is_loud(real_repo):
    _, repo, bare, task, merge, pr = real_repo
    res, _ = _land(task, pr, "--push", lie_after_push="e" * 40)
    assert res.exit_code == 2, res.output
    assert "push_verify_mismatch" in res.output
    assert _g(repo, "rev-parse", "HEAD") != merge  # the local commit exists, nothing was undone


def test_stray_dirty_file_refuses_in_a_real_repo(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    stray = repo / ".gitignore"
    stray.write_text("*.lock\n# stray edit\n", encoding="utf-8")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr)
    assert res.exit_code == 1 and ".gitignore" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before
    assert (port.tasks_dir / f"{task.id}.md").exists()
    assert not (repo / "feature.txt").exists()
    assert "# stray edit" in stray.read_text(encoding="utf-8")


def test_edit_to_the_tasks_own_file_refuses_before_any_mutation(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    tf = port.tasks_dir / f"{task.id}.md"
    tf.write_text(tf.read_text(encoding="utf-8") + "\nA note.\n", encoding="utf-8", newline="")
    before = _refs(repo)
    res, _ = _land(task, pr)
    assert res.exit_code == 1 and "task_file_dirty" in res.output and '"changed": false' in res.output, res.output
    assert _refs(repo) == before
    assert "A note." in tf.read_text(encoding="utf-8")


def test_local_commits_not_on_remote_refuse_non_fast_forward(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    (repo / "local.txt").write_text("x\n", encoding="utf-8")
    _g(repo, "add", "local.txt")
    _g(repo, "commit", "-m", "local only")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr)
    assert res.exit_code == 1 and "not_fast_forward" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before


def test_remove_worktree_removes_the_clean_task_worktree(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 0, res.output
    assert not wt.exists()
    assert _g(repo, "branch", "--list", f"clawpm/{task.id}")  # branch is never deleted


def test_remove_worktree_refuses_up_front_when_dirty(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    (wt / "scratch.txt").write_text("wip\n", encoding="utf-8")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 1 and "worktree_dirty" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before and wt.exists()


# --------------------------------------------------------------------------
# Round 3 (Codex gate): preconditions checked late, refs not pinned to the verified sha
# --------------------------------------------------------------------------


def test_branch_must_be_main_even_when_the_pr_base_matches(land_env):
    _, task, task_file, make = land_env
    env = make(branch="develop", pr={"state": "MERGED", "mergeCommit": {"oid": MERGE}, "baseRefName": "develop", "headRefName": "x", "url": "u"})
    res = _run(env, task)
    _assert_refused(res, env, task_file, "wrong_branch")
    assert "develop" in res.output and "main" in res.output


def test_repo_that_is_not_the_remotes_repo_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(remote_url="git@github.com:someone-else/widgets.git")
    _assert_refused(_run(env, task), env, task_file, "repo_remote_mismatch")


def test_repo_with_an_unparseable_remote_url_fails_closed(land_env):
    _, task, task_file, make = land_env
    env = make(remote_url="/some/local/path.git")
    _assert_refused(_run(env, task), env, task_file, "repo_remote_mismatch")


def test_done_destination_present_in_the_target_tip_refuses(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo

    def edit(other):
        done = other / ".project" / "tasks" / "done"
        done.mkdir(exist_ok=True)
        (done / f"{task.id}.md").write_text("stale\n", encoding="utf-8")

    new = _advance(tmp_path, edit=edit)
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "done_destination_exists" in res.output and '"changed": false' in res.output, res.output
    assert _g(repo, "rev-parse", "HEAD") == before


def test_tasks_land_never_auto_inits_a_project(land_env):
    import clawpm.cli.tasks as cli_tasks

    _, task, task_file, make = land_env
    seen = {}
    real = cli_tasks.require_project

    def spy(ctx, project_id, *a, **kw):
        seen.update(kw)
        return real(ctx, project_id, *a, **kw)

    env = make(pr={"state": "OPEN", "mergeCommit": None, "baseRefName": "main", "headRefName": "x", "url": "u"})
    with patch.object(cli_tasks, "require_project", spy):
        _assert_refused(_run(env, task), env, task_file, "pr_not_merged")
    assert seen.get("auto_init") is False


def _refs(repo):
    """HEAD plus every ref (branches, tags, remote-tracking): byte-identical across a refusal."""
    return _g(repo, "rev-parse", "HEAD"), _g(repo, "for-each-ref", "--format=%(refname) %(objectname)")


def _advance(tmp_path, name="adv.txt", body="more\n", edit=None):
    """Land one more commit on the bare remote from the `other` clone; return its sha."""
    other = tmp_path / "other"
    if edit:
        edit(other)
    else:
        (other / name).write_text(body, encoding="utf-8")
    _g(other, "add", "-A")
    _g(other, "commit", "-m", "feat: another merged change")
    _g(other, "push", "origin", "main")
    return _g(other, "rev-parse", "HEAD")


def _with_merge(pr, sha):
    return {**pr, "mergeCommit": {"oid": sha}}


def test_a_tag_named_like_the_tracking_ref_cannot_shadow_the_verified_tip(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "tag", "fork/main", _g(repo, "rev-parse", "HEAD"))  # tags win over refs/remotes in rev lookup
    res, fake = _land(task, pr)
    assert res.exit_code == 0, res.output
    assert _g(repo, "rev-parse", "HEAD^") == merge
    assert not any(c[-1] == "fork/main" for c in fake.calls if c[0] == "git")


def test_inherited_autostash_cannot_turn_a_refusal_into_a_mutation(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    rel = f".project/tasks/{task.id}.md"

    def edit(other):
        (other / rel).write_text((other / rel).read_text(encoding="utf-8") + "\nremote edit\n", encoding="utf-8", newline="")

    new = _advance(tmp_path, edit=edit)
    tf = port.tasks_dir / f"{task.id}.md"
    tf.write_text(tf.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8", newline="")
    _g(repo, "config", "merge.autoStash", "true")
    before = _refs(repo)
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "task_file_dirty" in res.output, res.output
    assert _refs(repo) == before
    assert "local edit" in tf.read_text(encoding="utf-8")
    assert _g(repo, "stash", "list") == ""


def test_pr_that_renames_the_task_file_refuses_before_the_ff(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    new = _advance(tmp_path, edit=lambda other: _g(other, "mv", f".project/tasks/{task.id}.md", f".project/tasks/{task.id}.progress.md"))
    before = _refs(repo)
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "task_missing_at_tip" in res.output and '"changed": false' in res.output, res.output
    assert _refs(repo) == before


def test_untracked_done_destination_refuses_before_the_fast_forward(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    done = port.tasks_dir / "done"
    done.mkdir(exist_ok=True)
    (done / f"{task.id}.md").write_text("stale untracked copy\n", encoding="utf-8")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr)
    assert res.exit_code == 1 and "done_destination_exists" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before and not (repo / "feature.txt").exists()


def test_a_git_failure_after_the_commit_reports_changed_true(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    res, _ = _land(task, pr, timeout_on="rev-list")
    assert res.exit_code == 2, res.output
    assert '"changed": true' in res.output
    assert _g(repo, "rev-list", "--count", f"{merge}..HEAD") == "1"  # the commit exists, nothing undone


def test_remove_worktree_matches_by_branch_not_directory_name(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", "someone-elses-branch", "main")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 0, res.output
    assert wt.exists()
    assert _payload(res)["worktree_removed"] is None


def test_remove_worktree_refuses_a_locked_worktree_up_front(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    _g(repo, "worktree", "lock", str(wt))
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 1 and "worktree_locked" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before and wt.exists()


def test_status_probe_does_not_rewrite_the_index_on_a_refusal(real_repo):
    import os

    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "update-index", "--refresh")
    gi = repo / ".gitignore"
    st = gi.stat()
    os.utime(gi, (st.st_atime, st.st_mtime + 10))  # stat-only change: status would refresh and write the index
    index_before = (repo / ".git" / "index").read_bytes()
    res, _ = _land(task, {**pr, "state": "OPEN", "mergeCommit": None})
    assert res.exit_code == 1 and "pr_not_merged" in res.output
    assert (repo / ".git" / "index").read_bytes() == index_before


def test_submodule_ignore_config_cannot_hide_a_dirty_tracked_submodule(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    subsrc = tmp_path / "subsrc"
    subsrc.mkdir()
    nohooks = tmp_path / "nohooks"
    _g(subsrc, "init", "-b", "main")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"), ("core.hooksPath", str(nohooks))):
        _g(subsrc, "config", k, v)
    (subsrc / "f.txt").write_text("1\n", encoding="utf-8")
    _g(subsrc, "add", "-A")
    _g(subsrc, "commit", "-m", "sub")
    _g(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(subsrc), "sub")
    _g(repo, "commit", "-m", "add submodule")
    _g(repo, "push", "--force", "fork", "main")  # test-local bare remote: rebase the fixture history onto the submodule commit
    other = tmp_path / "other"
    _g(other, "fetch", "origin")
    _g(other, "reset", "--hard", "origin/main")
    new = _advance(tmp_path)
    (repo / "sub" / "f.txt").write_text("dirty\n", encoding="utf-8")
    _g(repo, "config", "submodule.sub.ignore", "all")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "dirty_checkout" in res.output and "sub" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before


# -- class audit: every refusal leaves HEAD, the index and the working tree byte-identical --


def _snapshot(repo):
    files = {}
    for p in sorted(repo.rglob("*")):
        rel = p.relative_to(repo)
        if ".git" in rel.parts or not p.is_file() or p.suffix == ".lock":
            continue
        files[rel.as_posix()] = p.read_bytes()
    return _g(repo, "rev-parse", "HEAD"), (repo / ".git" / "index").read_bytes(), files


def _s_open_pr(ctx):
    return {**ctx["pr"], "state": "OPEN", "mergeCommit": None}, [], "pr_not_merged"


def _s_stray(ctx):
    (ctx["repo"] / ".gitignore").write_text("*.lock\n# stray\n", encoding="utf-8")
    return ctx["pr"], [], "dirty_checkout"


def _s_local_commit(ctx):
    (ctx["repo"] / "local.txt").write_text("x\n", encoding="utf-8")
    _g(ctx["repo"], "add", "local.txt")
    _g(ctx["repo"], "commit", "-m", "local only")
    return ctx["pr"], [], "not_fast_forward"


def _s_wrong_branch(ctx):
    _g(ctx["repo"], "checkout", "-b", "develop")
    return ctx["pr"], [], "wrong_branch"


def _s_done_dest(ctx):
    done = ctx["port"].tasks_dir / "done"
    done.mkdir(exist_ok=True)
    (done / f"{ctx['task'].id}.md").write_text("stale\n", encoding="utf-8")
    return ctx["pr"], [], "done_destination_exists"


def _s_dirty_worktree(ctx):
    wt = ctx["tmp"] / "wts" / ctx["task"].id
    _g(ctx["repo"], "worktree", "add", str(wt), "-b", f"clawpm/{ctx['task'].id}", "main")
    (wt / "scratch.txt").write_text("wip\n", encoding="utf-8")
    return ctx["pr"], ["--remove-worktree"], "worktree_dirty"


def _s_locked_worktree(ctx):
    wt = ctx["tmp"] / "wts" / ctx["task"].id
    _g(ctx["repo"], "worktree", "add", str(wt), "-b", f"clawpm/{ctx['task'].id}", "main")
    _g(ctx["repo"], "worktree", "lock", str(wt))
    return ctx["pr"], ["--remove-worktree"], "worktree_locked"


def _s_stat_touch_then_open(ctx):
    import os

    gi = ctx["repo"] / ".gitignore"
    st = gi.stat()
    os.utime(gi, (st.st_atime, st.st_mtime + 10))
    return {**ctx["pr"], "state": "OPEN", "mergeCommit": None}, [], "pr_not_merged"


def _tip_edit_make_decision(task_id):
    def edit(other):
        p = other / ".project" / "tasks" / f"{task_id}.md"
        p.write_bytes(p.read_text(encoding="utf-8").replace("---\n", "---\nkind: decision\n", 1).encode("utf-8"))
    return edit


def _tip_edit_add_child(task_id):
    def edit(other):
        (other / ".project" / "tasks" / f"{task_id}-C1.md").write_bytes(
            f"---\nid: {task_id}-C1\nparent: {task_id}\ncreated: '2026-10-14'\npriority: 5\n---\n# child\n".encode("utf-8")
        )
    return edit


def _s_hidden_untracked_worktree(ctx):
    wt = ctx["tmp"] / "wts" / ctx["task"].id
    _g(ctx["repo"], "worktree", "add", str(wt), "-b", f"clawpm/{ctx['task'].id}", "main")
    (wt / "scratch.txt").write_text("wip\n", encoding="utf-8")
    _g(ctx["repo"], "config", "status.showUntrackedFiles", "no")  # plain `git worktree remove` would not see the file
    return ctx["pr"], ["--remove-worktree"], "worktree_dirty"


def _s_submodule_worktree(ctx):
    wt = ctx["tmp"] / "wts" / ctx["task"].id
    _g(ctx["repo"], "worktree", "add", str(wt), "-b", f"clawpm/{ctx['task'].id}", "main")
    _add_populated_submodule(ctx["tmp"], wt)
    return ctx["pr"], ["--remove-worktree"], "worktree_has_submodules"


def _s_decision_at_tip(ctx):
    new = _advance(ctx["tmp"], edit=_tip_edit_make_decision(ctx["task"].id))
    return _with_merge(ctx["pr"], new), [], "task_file_differs_at_tip"


def _s_child_at_tip(ctx):
    new = _advance(ctx["tmp"], edit=_tip_edit_add_child(ctx["task"].id))
    return _with_merge(ctx["pr"], new), [], "subtasks_incomplete"


def _s_removed_at_tip(ctx):
    tid = ctx["task"].id
    new = _advance(ctx["tmp"], edit=lambda other: _g(other, "rm", f".project/tasks/{tid}.md"))
    return _with_merge(ctx["pr"], new), [], "task_missing_at_tip"


def _add_populated_submodule(tmp_path, wt):
    subsrc = tmp_path / "subsrc"
    subsrc.mkdir()
    nohooks = tmp_path / "nohooks"
    _g(subsrc, "init", "-b", "main")
    for k, v in (("user.name", "t"), ("user.email", "t@example.com"), ("commit.gpgsign", "false"), ("core.hooksPath", str(nohooks))):
        _g(subsrc, "config", k, v)
    (subsrc / "f.txt").write_text("1\n", encoding="utf-8")
    _g(subsrc, "add", "-A")
    _g(subsrc, "commit", "-m", "sub")
    _g(wt, "-c", "protocol.file.allow=always", "submodule", "add", str(subsrc), "sub")
    _g(wt, "-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={nohooks}", "commit", "-m", "add submodule")


_SCENARIOS = [
    _s_open_pr, _s_stray, _s_local_commit, _s_wrong_branch, _s_done_dest, _s_dirty_worktree, _s_locked_worktree,
    _s_stat_touch_then_open, _s_hidden_untracked_worktree, _s_submodule_worktree, _s_decision_at_tip, _s_child_at_tip,
    _s_removed_at_tip,
]


@pytest.mark.parametrize("scenario", _SCENARIOS)
def test_every_refusal_leaves_head_index_and_tree_byte_identical(real_repo, tmp_path, scenario):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "update-index", "--refresh")
    pr2, extra, code = scenario({"port": port, "repo": repo, "task": task, "pr": pr, "tmp": tmp_path})
    snap = _snapshot(repo)
    res, _ = _land(task, pr2, *extra)
    assert res.exit_code == 1 and code in res.output, res.output
    assert _snapshot(repo) == snap


# --------------------------------------------------------------------------
# Round 5 (Codex r2): behaviour inherited from config / hidden state
# --------------------------------------------------------------------------

HOSTILE_CONFIG = (
    ("branch.main.mergeoptions", "--squash"),
    ("status.showUntrackedFiles", "no"),
    ("merge.autoStash", "true"),
    ("merge.ff", "false"),
    ("diff.ignoreSubmodules", "all"),
    ("fetch.prune", "true"),
    ("fetch.pruneTags", "true"),
    ("push.followTags", "true"),
    ("submodule.recurse", "true"),
)
_PINS = [
    "merge.autoStash=false", "merge.ff=only", "branch.main.mergeoptions=", "status.showUntrackedFiles=all",
    "diff.ignoreSubmodules=none", "core.fsmonitor=false", "submodule.recurse=false",
    "push.recurseSubmodules=no", "push.followTags=false",
    "gc.auto=0", "maintenance.auto=false",
]
_PIN_ARGV = [x for p in _PINS for x in ("-c", p)]


def _hostile(repo):
    for k, v in HOSTILE_CONFIG:
        _g(repo, "config", k, v)


def test_inherited_squash_mergeoptions_still_fast_forwards(real_repo):  # (a)
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "config", "branch.main.mergeoptions", "--squash")
    res, _ = _land(task, pr)
    assert res.exit_code == 0, res.output
    assert _payload(res)["fast_forward"]["to"] == merge
    assert _g(repo, "rev-parse", "HEAD^") == merge
    assert (port.tasks_dir / "done" / f"{task.id}.md").exists()


def test_merge_that_exits_0_without_moving_head_marks_nothing_done(real_repo):  # (a) postcondition
    port, repo, bare, task, merge, pr = real_repo
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr, rewrite_merge=lambda a: a[: a.index("merge") + 1] + ["--squash", a[-1]])
    assert res.exit_code == 2 and "ff_not_applied" in res.output, res.output
    assert '"changed": true' in res.output  # the squash staged the PR's files
    assert _g(repo, "rev-parse", "HEAD") == before
    assert (port.tasks_dir / f"{task.id}.md").exists() and not (port.tasks_dir / "done" / f"{task.id}.md").exists()


def test_untracked_worktree_files_hidden_by_config_block_removal(real_repo, tmp_path):  # (b)
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    (wt / "scratch.txt").write_text("wip\n", encoding="utf-8")
    _g(repo, "config", "status.showUntrackedFiles", "no")
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 1 and "worktree_dirty" in res.output, res.output
    assert _g(repo, "rev-parse", "HEAD") == before
    assert (wt / "scratch.txt").read_text(encoding="utf-8") == "wip\n"


def test_work_that_appears_in_the_worktree_after_the_precheck_is_not_deleted(real_repo, tmp_path):  # (b) recheck before removal
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    _g(repo, "config", "status.showUntrackedFiles", "no")
    res, _ = _land(task, pr, "--push", "--remove-worktree", after_push=lambda: (wt / "late.txt").write_text("late\n", encoding="utf-8"))
    assert res.exit_code == 2 and "worktree_remove_failed" in res.output, res.output
    assert (wt / "late.txt").read_text(encoding="utf-8") == "late\n"


def test_failed_merge_that_touched_the_index_but_not_head_is_reported_changed(real_repo):  # (c)
    port, repo, bare, task, merge, pr = real_repo
    before = _g(repo, "rev-parse", "HEAD")

    def half_merge():
        (repo / "half.txt").write_text("half\n", encoding="utf-8")
        _g(repo, "add", "half.txt")
        raise subprocess.TimeoutExpired("git merge", 1)

    res, _ = _land(task, pr, on_merge=half_merge)
    assert res.exit_code == 2 and "ff_failed" in res.output, res.output
    assert '"changed": true' in res.output
    assert _g(repo, "rev-parse", "HEAD") == before  # HEAD alone would have said "nothing changed"


def test_failed_merge_that_changed_nothing_is_still_a_clean_refusal(real_repo):  # (c) no false positive
    port, repo, bare, task, merge, pr = real_repo

    def nothing():
        raise subprocess.TimeoutExpired("git merge", 1)

    res, _ = _land(task, pr, on_merge=nothing)
    assert res.exit_code == 1 and "ff_failed" in res.output and '"changed": false' in res.output, res.output


def test_task_that_becomes_a_decision_at_the_tip_refuses_before_the_ff(real_repo, tmp_path):  # (d)
    port, repo, bare, task, merge, pr = real_repo
    new = _advance(tmp_path, edit=_tip_edit_make_decision(task.id))
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "task_file_differs_at_tip" in res.output, res.output
    assert _g(repo, "rev-parse", "HEAD") == before


def test_tip_validation_is_not_fooled_by_export_ignore_attributes(real_repo, tmp_path):  # (d) git archive would honour these
    port, repo, bare, task, merge, pr = real_repo
    (repo / ".git" / "info" / "attributes").write_text("* export-ignore\n", encoding="utf-8")
    new = _advance(tmp_path, edit=_tip_edit_add_child(task.id))
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1 and "subtasks_incomplete" in res.output, res.output


def test_populated_submodule_in_the_worktree_refuses_before_the_ff(real_repo, tmp_path):  # (e)
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    _add_populated_submodule(tmp_path, wt)
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, pr, "--remove-worktree")
    assert res.exit_code == 1 and "worktree_has_submodules" in res.output, res.output
    assert _g(repo, "rev-parse", "HEAD") == before and wt.exists()


def _with_transition_errors(monkeypatch, **errors):
    import clawpm.services.tasks as svc

    real = svc.transition

    def wrapped(*a, **kw):
        res = real(*a, **kw)
        res.setdefault("data", {}).update(errors)
        return res

    monkeypatch.setattr(svc, "transition", wrapped)


def test_a_failed_dependency_cascade_makes_the_landing_not_ok(real_repo, monkeypatch):  # (f)
    port, repo, bare, task, merge, pr = real_repo
    _with_transition_errors(monkeypatch, cascade_errors=[{"error_class": "OSError", "message": "dependent stayed blocked"}])
    res, _ = _land(task, pr)
    assert res.exit_code == 2, res.output
    out = _payload(res)
    assert out["ok"] is False and out["landed"] is True
    assert out["errors"] == [{"source": "cascade_errors", "detail": {"error_class": "OSError", "message": "dependent stayed blocked"}}]
    assert _g(repo, "rev-parse", "HEAD^") == merge  # the commit exists, nothing undone


def test_audit_only_transition_errors_stay_warnings(real_repo, monkeypatch):  # (f)
    port, repo, bare, task, merge, pr = real_repo
    _with_transition_errors(monkeypatch, reflection_errors=[{"error_class": "X", "message": "no reflection"}])
    res, _ = _land(task, pr)
    assert res.exit_code == 0, res.output
    out = _payload(res)
    assert out["ok"] is True and "errors" not in out
    assert any(w.startswith("reflection_errors:") for w in out["warnings"])


def test_hostile_inherited_config_and_env_cannot_change_a_successful_landing(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "fetch", "fork")  # the operator's own fetch, before the hostile config is in play
    _hostile(repo)
    _g(repo, "update-ref", "refs/remotes/fork/stale", "HEAD")  # fetch.prune=true would delete this
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    env = {
        "GIT_DIR": str(tmp_path / "nowhere" / ".git"),
        "GIT_WORK_TREE": str(tmp_path / "nowhere"),
        "GIT_INDEX_FILE": str(tmp_path / "nowhere.index"),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "branch.main.mergeoptions",
        "GIT_CONFIG_VALUE_0": "--squash",
        "GIT_CONFIG_PARAMETERS": "'merge.ff'='false'",
    }
    res, fake = _land(task, pr, "--push", "--remove-worktree", env=env, prefetch=False)
    assert res.exit_code == 0, res.output
    out = _payload(res)
    head = _g(repo, "rev-parse", "HEAD")
    assert out["ok"] and out["fast_forward"]["to"] == merge and _g(repo, "rev-parse", "HEAD^") == merge
    assert _g(bare, "rev-parse", "refs/heads/main") == head
    assert not wt.exists()
    assert _g(repo, "rev-parse", "--verify", "-q", "refs/remotes/fork/stale")  # not pruned
    assert _g(repo, "stash", "list") == ""


def test_every_git_call_land_makes_is_pinned_and_env_stripped(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    wt = tmp_path / "wts" / task.id
    _g(repo, "worktree", "add", str(wt), "-b", f"clawpm/{task.id}", "main")
    env = {"GIT_DIR": str(repo / ".git"), "GIT_WORK_TREE": str(repo), "GIT_INDEX_FILE": str(repo / ".git" / "index"), "GIT_CONFIG_COUNT": "0"}
    res, fake = _land(task, pr, "--push", "--remove-worktree", env=env)
    assert res.exit_code == 0, res.output
    git_calls = [(a, e) for a, e in zip(fake.calls, fake.envs) if a[0] == "git"]
    pinned = [(a, e) for a, e in git_calls if a[1:1 + len(_PIN_ARGV)] == _PIN_ARGV]
    unpinned = [a for a, e in git_calls if a[1:1 + len(_PIN_ARGV)] != _PIN_ARGV]
    # the only git processes outside land.py's runner are transition()'s own files_changed enrichment
    assert {a[1] for a in unpinned} <= {"diff", "rev-parse", "log"}, unpinned
    subs = {a[1 + len(_PIN_ARGV)] for a, _ in pinned}
    assert {"merge", "commit", "push", "ls-remote", "worktree", "status", "ls-tree", "cat-file"} <= subs, subs
    assert "fetch" not in subs and not any("fetch" in a for a in unpinned)  # land never fetches
    for _, e in pinned:
        assert e is not None
        assert not {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} & set(e)
        assert e["GIT_TERMINAL_PROMPT"] == "0"


def test_land_py_has_exactly_one_process_spawn_site():
    import re

    import clawpm.land as land

    src = Path(land.__file__).read_text(encoding="utf-8")
    assert len(re.findall(r"subprocess\.(run|Popen|call|check_output|check_call)\(", src)) == 1
    assert "os.system" not in src and "_remote_head" not in src and "_resolve_repo" not in src


# --------------------------------------------------------------------------
# Round 7 (Codex r3): remote-level prune config, tip-shaped destination, tip-state eligibility
# --------------------------------------------------------------------------


def _git_calls(fake):
    return [_norm(c)[1:] for c in fake.calls if c[0] == "git"]


def test_land_never_runs_fetch_and_refuses_when_the_tracking_ref_is_stale(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    before = _refs(repo)
    res, fake = _land(task, pr, prefetch=False)  # remote has the merge, fork/main does not yet
    assert res.exit_code == 1 and "remote_not_fetched" in res.output and "git fetch fork" in res.output, res.output
    assert '"changed": false' in res.output
    assert _refs(repo) == before
    assert not any(c[0] == "fetch" for c in _git_calls(fake))


def test_land_with_a_prefetched_tracking_ref_succeeds_without_running_fetch(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "fetch", "fork")
    res, fake = _land(task, pr, prefetch=False)
    assert res.exit_code == 0, res.output
    assert not any(c[0] == "fetch" for c in _git_calls(fake))


def test_extra_remote_tag_refspec_cannot_move_a_local_tag(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "tag", "v1")  # local v1 -> HEAD
    _g(bare, "tag", "v1", merge)  # the remote's v1 differs
    _g(repo, "config", "--add", "remote.fork.fetch", "+refs/tags/*:refs/tags/*")
    before = _refs(repo)
    res, _ = _land(task, pr, prefetch=False)
    assert res.exit_code == 1 and "remote_not_fetched" in res.output, res.output
    assert _refs(repo) == before
    # even with the tracking ref current (operator fetched the branch only), the landing leaves the tag alone
    _g(repo, "update-ref", "refs/remotes/fork/main", merge)
    tag = _g(repo, "rev-parse", "v1")
    res, _ = _land(task, pr, prefetch=False)
    assert res.exit_code == 0, res.output
    assert _g(repo, "rev-parse", "v1") == tag


@pytest.mark.parametrize("staged", [False, True])
def test_local_task_kind_edit_refuses_before_any_mutation(real_repo, staged):
    port, repo, bare, task, merge, pr = real_repo
    tf = port.tasks_dir / f"{task.id}.md"
    tf.write_bytes(tf.read_text(encoding="utf-8").replace("---\n", "---\nkind: decision\n", 1).encode("utf-8"))
    if staged:
        _g(repo, "add", str(tf))
    _g(repo, "fetch", "fork")
    before, content = _refs(repo), tf.read_bytes()
    res, _ = _land(task, pr, prefetch=False)
    assert res.exit_code == 1 and "task_file_dirty" in res.output and '"changed": false' in res.output, res.output
    assert _refs(repo) == before and tf.read_bytes() == content


def test_task_file_that_differs_between_head_and_tip_refuses(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo

    def edit(other):
        p = other / ".project" / "tasks" / f"{task.id}.md"
        p.write_bytes(p.read_bytes() + b"\nremote note\n")

    new = _advance(tmp_path, edit=edit)
    _g(repo, "fetch", "fork")
    before = _refs(repo)
    res, _ = _land(task, _with_merge(pr, new), prefetch=False)
    assert res.exit_code == 1 and "task_file_differs_at_tip" in res.output and '"changed": false' in res.output, res.output
    assert _refs(repo) == before


def _tip_edit_convert_to_dir(task_id):
    def edit(other):
        (other / ".project" / "tasks" / task_id).mkdir()
        _g(other, "mv", f".project/tasks/{task_id}.md", f".project/tasks/{task_id}/_task.md")
    return edit


def _s_shape_change_dest_exists(ctx):
    """The PR turns <ID>.md into <ID>/_task.md: the file at the local path is gone at the tip, so land refuses."""
    done_dir = ctx["port"].tasks_dir / "done" / ctx["task"].id
    done_dir.mkdir(parents=True)
    (done_dir / "stale.md").write_text("stale\n", encoding="utf-8")
    new = _advance(ctx["tmp"], edit=_tip_edit_convert_to_dir(ctx["task"].id))
    return _with_merge(ctx["pr"], new), [], "task_missing_at_tip"


def _s_shape_change_dest_in_tip(ctx):
    tid = ctx["task"].id

    def edit(other):
        d = other / ".project" / "tasks" / "done" / tid
        d.mkdir(parents=True)
        (d / "stale.md").write_text("stale\n", encoding="utf-8")
        _tip_edit_convert_to_dir(tid)(other)

    new = _advance(ctx["tmp"], edit=edit)
    return _with_merge(ctx["pr"], new), [], "task_missing_at_tip"


@pytest.mark.parametrize("scenario", [_s_shape_change_dest_exists, _s_shape_change_dest_in_tip])
def test_shape_changing_pr_refusals_leave_head_index_and_tree_byte_identical(real_repo, tmp_path, scenario):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "update-index", "--refresh")
    pr2, extra, code = scenario({"port": port, "repo": repo, "task": task, "pr": pr, "tmp": tmp_path})
    snap = _snapshot(repo)
    res, _ = _land(task, pr2, *extra)
    assert res.exit_code == 1 and code in res.output and '"changed": false' in res.output, res.output
    assert _snapshot(repo) == snap


def test_parent_whose_children_are_completed_at_the_tip_lands_without_a_manual_pull(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    tid = task.id
    other = tmp_path / "other"
    _g(other, "pull", "--ff-only", "origin", "main")
    tasks = other / ".project" / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / f"{tid}-C1.md").write_bytes(f"---\nid: {tid}-C1\nparent: {tid}\ncreated: '2026-10-14'\npriority: 5\n---\n# child\n".encode("utf-8"))
    _g(other, "add", "-A")
    _g(other, "commit", "-m", "add child")
    _g(other, "push", "origin", "main")
    _g(repo, "pull", "--ff-only", "fork", "main")  # the local checkout has the open child and the PR commit

    def edit(other):
        (other / ".project" / "tasks" / "done").mkdir(exist_ok=True)
        _g(other, "mv", f".project/tasks/{tid}-C1.md", f".project/tasks/done/{tid}-C1.md")

    new = _advance(tmp_path, edit=edit)
    res, _ = _land(task, _with_merge(pr, new))  # the local checkout still shows an open child
    assert res.exit_code == 0, res.output
    assert (port.tasks_dir / "done" / f"{tid}.md").exists()
