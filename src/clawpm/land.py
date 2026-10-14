"""Post-merge landing transaction (CLAWP-147-002, gap G2).

``land_task`` books a merged PR from the MAIN checkout in one fail-closed
transaction: verify the PR is MERGED and its merge commit is an ancestor of
``<remote>/<base>``, fast-forward the base branch, mark the task done, and
commit ONLY the task's own paths (no attribution trailer).

Everything that can refuse runs before the first mutation, so a refusal
(:class:`LandError` with ``changed=False``) leaves the checkout exactly as it
was. The one exception to "nothing changed" is ``git fetch``, which only moves
remote-tracking refs. After the fast-forward starts, a failure raises a
:class:`LandError` with ``changed=True`` and says what was and was not done.
There is no recovery step of any kind: no reset, no force, no branch delete.
``--remove-worktree`` uses plain ``git worktree remove`` (git itself refuses a
dirty one).

Every ref read after the fetch uses the verified tip SHA, never the
``<remote>/<base>`` name (a tag or local branch of that name would win the
lookup). The merge itself stays with the operator; this module never merges.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from clawpm.codex_check import _extract_github_owner_repo
from clawpm.models import TaskState
from clawpm.pr_verify import PrVerifyError, _FULL_SHA, _gh_json, _remote_head, _resolve_repo, _run

_TIMEOUT = 60
_NO_ATTRIBUTION = re.compile(r"co-authored-by|generated with|claude", re.I)
_LAND_BRANCH = "main"


class LandError(Exception):
    """A precondition failed or a step failed. ``changed`` says if anything was mutated."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None, *, changed: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.changed = changed


def _label(args: list[str]) -> str:
    """The subcommand and its first args, without git's leading global options."""
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
        elif args[i].startswith("--no-optional-locks"):
            i += 1
        else:
            break
    return " ".join(args[i:i + 3])


def _git(args: list[str], cwd: Path, *, ok_codes: tuple[int, ...] = (0,), changed: bool = False) -> subprocess.CompletedProcess:
    """Run git in ``cwd``; raise LandError unless the exit code is in ``ok_codes``.

    ``changed`` is True for every call made after the first mutation, so a
    failure there can never be reported as "nothing changed".
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
        )
    except FileNotFoundError:
        raise LandError("git_missing", "`git` is not installed or not on PATH", changed=changed)
    except subprocess.TimeoutExpired:
        raise LandError("git_timeout", f"`git {_label(args)}` timed out after {_TIMEOUT}s", changed=changed)
    except OSError as exc:
        raise LandError("git_failed", f"could not run git: {exc}", changed=changed)
    if proc.returncode not in ok_codes:
        raise LandError(
            "git_failed",
            f"`git {_label(args)}` exited {proc.returncode}",
            {"stderr": (proc.stderr or "").strip()},
            changed=changed,
        )
    return proc


def _out(args: list[str], cwd: Path, *, changed: bool = False) -> str:
    return (_git(args, cwd, changed=changed).stdout or "").strip()


def _dirty_tracked_paths(cwd: Path, *, changed: bool = False) -> list[str]:
    """Tracked paths with staged or unstaged changes (untracked files are ignored).

    ``--no-optional-locks`` keeps status from refreshing (rewriting) the index,
    so a refusal leaves its bytes alone. Submodule ignore settings are
    overridden so a hidden submodule edit cannot pass the clean-checkout gate.
    """
    raw = _git(
        [
            "--no-optional-locks", "-c", "diff.ignoreSubmodules=none",
            "status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames",
            "--ignore-submodules=none",
        ],
        cwd,
        changed=changed,
    ).stdout or ""
    paths = []
    for entry in raw.split("\0"):
        if len(entry) > 3:  # "XY path"
            paths.append(entry[3:])
    return paths


def _is_own(path: str, own: list[str]) -> bool:
    return any(path == o or path.startswith(o + "/") for o in own)


def _task_entry(file_path: Path) -> Path:
    """The file (or, for a decomposed task, the directory) that moves with the task."""
    return file_path.parent if file_path.name == "_task.md" else file_path


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


def _linked_worktrees(cwd: Path) -> list[dict[str, str]]:
    blocks: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in (_out(["worktree", "list", "--porcelain"], cwd)).splitlines():
        if not line.strip():
            if cur:
                blocks.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val
    if cur:
        blocks.append(cur)
    return blocks


def _find_task_worktree(cwd: Path, toplevel: Path, task_id: str) -> dict[str, str] | None:
    """The linked worktree checked out on ``clawpm/<task_id>`` (matched by branch, never by directory name)."""
    found: list[dict[str, str]] = []
    for blk in _linked_worktrees(cwd):
        wt = blk.get("worktree")
        if not wt or Path(wt).resolve() == toplevel:
            continue
        if blk.get("branch") == f"refs/heads/clawpm/{task_id}":
            found.append(blk)
    if len(found) > 1:
        raise LandError(
            "worktree_ambiguous",
            f"more than one worktree matches {task_id}; remove it by hand",
            {"worktrees": [b["worktree"] for b in found]},
        )
    return found[0] if found else None


def _remote_slug(remote: str) -> str | None:
    """owner/name of the remote's effective URL, or None when it is not a github.com URL."""
    parsed = _extract_github_owner_repo(_run(["git", "remote", "get-url", remote]).strip())
    return f"{parsed[0]}/{parsed[1]}" if parsed else None


def land_task(
    config,
    project_id: str,
    task_id: str,
    pr_number: int,
    *,
    push: bool = False,
    remove_worktree: bool = False,
    repo: str | None = None,
    remote: str = "fork",
    cwd: Path | None = None,
) -> dict[str, Any]:
    """Run the landing transaction; return a result dict or raise :class:`LandError`."""
    from clawpm.tasks import get_task, get_tasks_dir, parent_rollup_status

    cwd = Path(cwd) if cwd else Path.cwd()

    # 1. Where are we? Must be the main checkout, on `main`.
    toplevel = Path(_out(["rev-parse", "--show-toplevel"], cwd)).resolve()
    git_dir, common_dir = (_out(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd).splitlines() + ["", ""])[:2]
    if Path(git_dir).resolve() != Path(common_dir).resolve():
        raise LandError(
            "inside_worktree",
            f"{toplevel} is a linked worktree (a dispatched worker's checkout, CLAWP-098); "
            "run `tasks land` from the main checkout",
            {"toplevel": str(toplevel)},
        )
    branch = _git(["symbolic-ref", "--short", "-q", "HEAD"], toplevel, ok_codes=(0, 1)).stdout.strip()
    if not branch:
        raise LandError("detached_head", "the main checkout is on a detached HEAD; check out the base branch first")
    if branch != _LAND_BRANCH:
        raise LandError(
            "wrong_branch",
            f"the checkout is on '{branch}'; `tasks land` only lands onto '{_LAND_BRANCH}'",
            {"branch": branch, "required": _LAND_BRANCH},
        )
    before = _out(["rev-parse", "HEAD"], toplevel)

    # 2. The task: must exist, be open work, and live inside this checkout.
    task = get_task(config, project_id, task_id)
    if task is None or task.file_path is None:
        raise LandError("task_not_found", f"No task with id '{task_id}' in project '{project_id}'")
    if task.state == TaskState.DONE:
        raise LandError("task_already_done", f"{task_id} is already done; nothing to land")
    if task.kind == "decision":
        raise LandError(
            "decision_needs_resolution",
            f"{task_id} is a decision task; close it with `tasks state {task_id} done --resolution ...`",
        )
    status = parent_rollup_status(config, project_id, task)
    if status["incomplete"] or status["missing"]:
        raise LandError(
            "subtasks_incomplete",
            f"{task_id} has incomplete subtasks",
            {"incomplete": [c["id"] for c in status["incomplete"]], "missing": list(status["missing"])},
        )
    old_entry = _task_entry(task.file_path)
    try:
        old_rel = _rel(old_entry, toplevel)
    except ValueError:
        raise LandError(
            "task_outside_checkout",
            f"task files at {old_entry} are not inside the checkout {toplevel}",
        )

    # 2b. Where the task will land (done/). Anything already there, tracked or
    #     not, would make the post-ff move refuse, so refuse now instead.
    tasks_dir = get_tasks_dir(config, project_id)
    done_rel: str | None = None
    if tasks_dir is not None:
        done_name = task_id if task.file_path.name == "_task.md" else f"{task_id}.md"
        try:
            done_rel = _rel(tasks_dir / "done" / done_name, toplevel)
        except ValueError:
            done_rel = None
    if done_rel and (toplevel / done_rel).exists():
        raise LandError(
            "done_destination_exists",
            f"{done_rel} already exists in the checkout; move it aside before landing {task_id}",
            {"path": done_rel},
        )

    # 3. Dirty tracked paths outside the task's own files (the stray-edit case).
    stray = [p for p in _dirty_tracked_paths(toplevel) if not _is_own(p, [old_rel])]
    if stray:
        raise LandError(
            "dirty_checkout",
            "tracked paths outside the task's own files are modified: " + ", ".join(stray),
            {"paths": stray},
        )

    # 4. The PR: MERGED, onto this branch, with a full merge commit sha, in the remote's own repo.
    try:
        if repo:
            actual = _remote_slug(remote)
            if actual is None or actual.lower() != repo.lower():
                raise LandError(
                    "repo_remote_mismatch",
                    f"--repo {repo} does not match remote '{remote}' ({actual or 'not a github.com URL'}); "
                    "refusing to attest a PR from a different repository",
                    {"repo": repo, "remote_repo": actual},
                )
        repo_slug = _resolve_repo(repo, remote)
        pr = _gh_json([
            "pr", "view", str(pr_number), "--repo", repo_slug,
            "--json", "state,mergeCommit,baseRefName,headRefName,url",
        ])
    except PrVerifyError as exc:
        raise LandError(exc.code, exc.message, exc.details)
    if pr.get("state") != "MERGED":
        raise LandError("pr_not_merged", f"PR #{pr_number} is {pr.get('state')}, not MERGED", {"state": pr.get("state")})
    mc = pr.get("mergeCommit")
    merge_sha = str(mc.get("oid") if isinstance(mc, dict) else "").lower()
    if not _FULL_SHA.match(merge_sha):
        raise LandError("merge_commit_unknown", f"PR #{pr_number} reports no full merge commit sha", {"mergeCommit": mc})
    base = pr.get("baseRefName")
    if not isinstance(base, str) or not base:
        raise LandError("gh_bad_json", "gh pr view lacks baseRefName")
    if branch != base:
        raise LandError("wrong_branch", f"PR #{pr_number} merged into '{base}' but the checkout is on '{branch}'", {"base": base, "branch": branch})

    # 5. Refs: fetch (moves remote-tracking refs only), then prove the tracking
    #    ref is current, the merge commit is in it and our branch can fast-forward.
    #    From here on only the verified `tip` sha is used, never the ref name.
    _git(["fetch", remote], toplevel)
    target = f"{remote}/{base}"
    tip = _out(["rev-parse", "--verify", f"refs/remotes/{target}^{{commit}}"], toplevel)
    try:
        remote_tip = _remote_head(remote, base)
    except PrVerifyError as exc:
        raise LandError(exc.code, exc.message, exc.details)
    if tip != remote_tip:
        raise LandError(
            "tracking_ref_stale",
            f"{target} is {tip} locally but {remote_tip} on the remote after fetch (frozen fetch refspec?)",
            {"local": tip, "remote": remote_tip},
        )
    if _git(["cat-file", "-e", f"{merge_sha}^{{commit}}"], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError("merge_commit_not_fetched", f"merge commit {merge_sha} is not in the local repository after fetch")
    if _git(["merge-base", "--is-ancestor", merge_sha, tip], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError(
            "merge_commit_not_in_base",
            f"merge commit {merge_sha} is not an ancestor of {target} ({tip})",
            {"merge_commit": merge_sha, "target": target},
        )
    if _git(["merge-base", "--is-ancestor", before, tip], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError(
            "not_fast_forward",
            f"{branch} has commits that are not in {target}; refusing a non-fast-forward",
            {"head": before, "target": target},
        )
    if done_rel and (_git(["ls-tree", "--name-only", tip, "--", done_rel], toplevel).stdout or "").strip():
        raise LandError(
            "done_destination_exists",
            f"{done_rel} already exists in {target}; resolve it before landing {task_id}",
            {"path": done_rel},
        )

    # 6. Optional worktree removal: refuse up front when it cannot succeed.
    wt_path: Path | None = None
    if remove_worktree:
        blk = _find_task_worktree(toplevel, toplevel, task_id)
        if blk is not None:
            wt_path = Path(blk["worktree"])
            if "locked" in blk:
                raise LandError("worktree_locked", f"worktree {wt_path} is locked; unlock it first or drop --remove-worktree", {"worktree": str(wt_path)})
            wt_status = _git(["--no-optional-locks", "status", "--porcelain"], wt_path).stdout or ""
            if wt_status.strip():
                raise LandError("worktree_dirty", f"worktree {wt_path} has uncommitted or untracked files; not removing it", {"worktree": str(wt_path)})

    # ---- first mutation: every failure from here on is changed=True ----
    try:
        # No autostash: a configured merge.autoStash must not turn a refusal into a stash + pop.
        _git(["-c", "merge.autoStash=false", "merge", "--ff-only", "--no-autostash", tip], toplevel)
    except LandError as exc:
        # git refuses a ff that would overwrite local changes before touching anything.
        after = _out(["rev-parse", "HEAD"], toplevel, changed=True)
        raise LandError(
            "ff_failed", exc.message, {**exc.details, "head": after}, changed=(after != before)
        )
    ff_head = _out(["rev-parse", "HEAD"], toplevel, changed=True)

    from clawpm.services.tasks import transition

    done_note = f"Landed via PR #{pr_number}"
    try:
        # The PR may have renamed the task file, so re-resolve it after the ff.
        landed = get_task(config, project_id, task_id)
        ff_rel = _rel(_task_entry(landed.file_path), toplevel) if landed and landed.file_path else None
        res = transition(config, project_id=project_id, task_id=task_id, new_state="done", note=done_note)
        moved = get_task(config, project_id, task_id) if res.get("ok") else None
        new_rel = _rel(_task_entry(moved.file_path), toplevel) if moved and moved.file_path else None
    except LandError:
        raise
    except Exception as exc:  # noqa: BLE001 - past the ff, anything unexpected must still say changed
        raise LandError(
            "state_change_failed",
            f"{task_id} was not marked done after the fast-forward: {exc}",
            {"fast_forwarded_to": ff_head},
            changed=True,
        )
    if not res.get("ok"):
        raise LandError(
            "state_change_failed",
            f"{task_id} was not marked done: {res.get('error')}: {res.get('message')}",
            {"fast_forwarded_to": ff_head, "state_error": res.get("error")},
            changed=True,
        )
    own = list(dict.fromkeys(p for p in (old_rel, ff_rel, new_rel) if p))

    # Stage and commit ONLY the task's own paths. A pathspec is kept when it
    # exists or is still tracked (a pending deletion); one the ff already
    # removed from the index and tree is skipped.
    try:
        specs = [
            p for p in own
            if (toplevel / p).exists() or (_git(["ls-files", "--", p], toplevel, changed=True).stdout or "").strip()
        ]
        msg = f"chore: mark {task_id} done (PR #{pr_number} merged)"
        _git(["add", "-A", "--", *specs], toplevel, changed=True)
        _git(["commit", "-m", msg, "--", *specs], toplevel, changed=True)
    except LandError as exc:
        raise LandError(
            "commit_failed", exc.message,
            {**exc.details, "fast_forwarded_to": ff_head}, changed=True,
        )
    head = _out(["rev-parse", "HEAD"], toplevel, changed=True)
    count = int(_out(["rev-list", "--count", f"{ff_head}..HEAD"], toplevel, changed=True) or 0)
    files = [f for f in (_git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", "HEAD"], toplevel, changed=True).stdout or "").split("\0") if f]
    body = _out(["log", "-1", "--format=%B"], toplevel, changed=True)
    if count != 1 or not all(_is_own(f, own) for f in files) or _NO_ATTRIBUTION.search(body):
        raise LandError(
            "commit_unexpected",
            "the landing commit is not exactly one task-only commit without attribution; inspect it, nothing was undone",
            {"commits": count, "files": files, "head": head},
            changed=True,
        )

    result: dict[str, Any] = {
        "ok": True,
        "task_id": task_id,
        "pr": pr_number,
        "repo": repo_slug,
        "url": pr.get("url"),
        "branch": base,
        "merge_commit": merge_sha,
        "fast_forward": {"from": before, "to": ff_head},
        "commit": {"sha": head, "message": msg, "files": files},
        "pushed": None,
        "worktree_removed": None,
        "uncommitted_other": [p for p in _dirty_tracked_paths(toplevel, changed=True) if not _is_own(p, own)],
        "warnings": list((res.get("data") or {}).get("warnings") or []),
    }

    if push:
        try:
            _git(["push", remote, f"HEAD:refs/heads/{base}"], toplevel)
        except LandError as exc:
            raise LandError("push_failed", exc.message, {**exc.details, "local_head": head, "result": result}, changed=True)
        try:
            remote_after = _remote_head(remote, base)
        except PrVerifyError as exc:
            raise LandError(exc.code, exc.message, {**exc.details, "result": result}, changed=True)
        if remote_after != head:
            raise LandError(
                "push_verify_mismatch",
                f"after push, {remote}/{base} is {remote_after} but local HEAD is {head}",
                {"local_head": head, "remote_head": remote_after, "result": result},
                changed=True,
            )
        result["pushed"] = {"remote": remote, "head": remote_after}

    if remove_worktree and wt_path is not None:
        try:
            _git(["worktree", "remove", str(wt_path)], toplevel)
        except LandError as exc:
            raise LandError("worktree_remove_failed", exc.message, {**exc.details, "result": result}, changed=True)
        result["worktree_removed"] = str(wt_path)
    return result
