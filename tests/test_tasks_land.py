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
        self.tip_has_dest = ""
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
        if sub == "ls-tree":
            return _cp(self.tip_has_dest)
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
    _assert_refused(_run(env, task), env, task_file, "tracking_ref_stale")


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

    def __init__(self, pr, lie_after_push: str | None = None, timeout_on: str | None = None):
        self.pr = pr
        self.timeout_on = timeout_on
        self.calls: list[list[str]] = []
        self.lie_after_push = lie_after_push
        self.pushed = False

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if args[0] == "gh":
            return _cp(json.dumps(self.pr))
        if self.timeout_on and _norm(args)[:2] == ["git", self.timeout_on]:
            raise subprocess.TimeoutExpired(args, 1)
        if args[:2] == ["git", "push"]:
            self.pushed = True
        if self.lie_after_push and self.pushed and args[:2] == ["git", "ls-remote"]:
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


def _land(task, pr, *extra, lie_after_push=None, timeout_on=None):
    fake = RealGit(pr, lie_after_push, timeout_on)
    with patch("subprocess.run", fake):
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
    assert not any("--force" in c or "-f" in c for c in fake.calls if c[:2] == ["git", "push"])


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


def test_edit_to_the_tasks_own_file_does_not_block(real_repo):
    port, repo, bare, task, merge, pr = real_repo
    tf = port.tasks_dir / f"{task.id}.md"
    tf.write_text(tf.read_text(encoding="utf-8") + "\nA note.\n", encoding="utf-8", newline="")
    res, _ = _land(task, pr)
    assert res.exit_code == 0, res.output


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


def test_done_destination_present_in_the_target_tip_refuses(land_env):
    _, task, task_file, make = land_env
    env = make(tip_has_dest="present\n")
    _assert_refused(_run(env, task), env, task_file, "done_destination_exists")


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
    before = _g(repo, "rev-parse", "HEAD")
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 1, res.output
    assert "ff_failed" in res.output
    assert _g(repo, "rev-parse", "HEAD") == before
    assert "local edit" in tf.read_text(encoding="utf-8")
    assert _g(repo, "stash", "list") == ""


def test_pr_that_renames_the_task_file_leaves_no_duplicate_in_a_fresh_checkout(real_repo, tmp_path):
    port, repo, bare, task, merge, pr = real_repo
    new = _advance(tmp_path, edit=lambda other: _g(other, "mv", f".project/tasks/{task.id}.md", f".project/tasks/{task.id}.progress.md"))
    res, _ = _land(task, _with_merge(pr, new))
    assert res.exit_code == 0, res.output
    tracked = _g(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert f".project/tasks/done/{task.id}.md" in tracked
    assert f".project/tasks/{task.id}.progress.md" not in tracked
    assert f".project/tasks/{task.id}.md" not in tracked
    assert _g(repo, "status", "--porcelain", "--untracked-files=no") == ""
    assert _g(repo, "rev-list", "--count", f"{new}..HEAD") == "1"


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


@pytest.mark.parametrize("scenario", [_s_open_pr, _s_stray, _s_local_commit, _s_wrong_branch, _s_done_dest, _s_dirty_worktree, _s_locked_worktree, _s_stat_touch_then_open])
def test_every_refusal_leaves_head_index_and_tree_byte_identical(real_repo, tmp_path, scenario):
    port, repo, bare, task, merge, pr = real_repo
    _g(repo, "update-index", "--refresh")
    pr2, extra, code = scenario({"port": port, "repo": repo, "task": task, "pr": pr, "tmp": tmp_path})
    snap = _snapshot(repo)
    res, _ = _land(task, pr2, *extra)
    assert res.exit_code == 1 and code in res.output, res.output
    assert _snapshot(repo) == snap
