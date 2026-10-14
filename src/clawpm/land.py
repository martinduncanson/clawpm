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

The merge itself stays with the operator; this module never merges.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from clawpm.models import TaskState
from clawpm.pr_verify import PrVerifyError, _FULL_SHA, _gh_json, _remote_head, _resolve_repo

_TIMEOUT = 60
_NO_ATTRIBUTION = re.compile(r"co-authored-by|generated with|claude", re.I)


class LandError(Exception):
    """A precondition failed or a step failed. ``changed`` says if anything was mutated."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None, *, changed: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.changed = changed


def _git(args: list[str], cwd: Path, *, ok_codes: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    """Run git in ``cwd``; raise LandError unless the exit code is in ``ok_codes``."""
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
        raise LandError("git_missing", "`git` is not installed or not on PATH")
    except subprocess.TimeoutExpired:
        raise LandError("git_timeout", f"`git {' '.join(args[:3])}` timed out after {_TIMEOUT}s")
    except OSError as exc:
        raise LandError("git_failed", f"could not run git: {exc}")
    if proc.returncode not in ok_codes:
        raise LandError(
            "git_failed",
            f"`git {' '.join(args[:3])}` exited {proc.returncode}",
            {"stderr": (proc.stderr or "").strip()},
        )
    return proc


def _out(args: list[str], cwd: Path) -> str:
    return (_git(args, cwd).stdout or "").strip()


def _dirty_tracked_paths(cwd: Path) -> list[str]:
    """Tracked paths with staged or unstaged changes (untracked files are ignored)."""
    raw = _git(["status", "--porcelain=v1", "-z", "--untracked-files=no", "--no-renames"], cwd).stdout or ""
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


def _find_task_worktree(cwd: Path, toplevel: Path, task_id: str) -> Path | None:
    found = []
    for blk in _linked_worktrees(cwd):
        wt = blk.get("worktree")
        if not wt or Path(wt).resolve() == toplevel:
            continue
        if blk.get("branch") == f"refs/heads/clawpm/{task_id}" or Path(wt).name == task_id:
            found.append(Path(wt))
    if len(found) > 1:
        raise LandError(
            "worktree_ambiguous",
            f"more than one worktree matches {task_id}; remove it by hand",
            {"worktrees": [str(p) for p in found]},
        )
    return found[0] if found else None


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
    from clawpm.tasks import get_task, parent_rollup_status

    cwd = Path(cwd) if cwd else Path.cwd()

    # 1. Where are we? Must be the main checkout, on the PR's base branch.
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

    # 3. Dirty tracked paths outside the task's own files (the stray-edit case).
    stray = [p for p in _dirty_tracked_paths(toplevel) if not _is_own(p, [old_rel])]
    if stray:
        raise LandError(
            "dirty_checkout",
            "tracked paths outside the task's own files are modified: " + ", ".join(stray),
            {"paths": stray},
        )

    # 4. The PR: MERGED, onto this branch, with a full merge commit sha.
    try:
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
    _git(["fetch", remote], toplevel)
    target = f"{remote}/{base}"
    tip = _out(["rev-parse", "--verify", f"refs/remotes/{target}"], toplevel)
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
    if _git(["merge-base", "--is-ancestor", merge_sha, target], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError(
            "merge_commit_not_in_base",
            f"merge commit {merge_sha} is not an ancestor of {target}",
            {"merge_commit": merge_sha, "target": target},
        )
    if _git(["merge-base", "--is-ancestor", "HEAD", target], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError(
            "not_fast_forward",
            f"{branch} has commits that are not in {target}; refusing a non-fast-forward",
            {"head": _out(["rev-parse", "HEAD"], toplevel), "target": target},
        )

    # 6. Optional worktree removal: refuse up front when it cannot succeed.
    wt_path: Path | None = None
    if remove_worktree:
        wt_path = _find_task_worktree(toplevel, toplevel, task_id)
        if wt_path is not None and (_git(["status", "--porcelain"], wt_path).stdout or "").strip():
            raise LandError("worktree_dirty", f"worktree {wt_path} has uncommitted or untracked files; not removing it", {"worktree": str(wt_path)})

    # ---- first mutation ----
    before = _out(["rev-parse", "HEAD"], toplevel)
    try:
        _git(["merge", "--ff-only", target], toplevel)
    except LandError as exc:
        # git refuses a ff that would overwrite local changes before touching anything.
        after = _out(["rev-parse", "HEAD"], toplevel)
        raise LandError(
            "ff_failed", exc.message, {**exc.details, "head": after}, changed=(after != before)
        )
    ff_head = _out(["rev-parse", "HEAD"], toplevel)

    from clawpm.services.tasks import transition

    done_note = f"Landed via PR #{pr_number}"
    res = transition(config, project_id=project_id, task_id=task_id, new_state="done", note=done_note)
    if not res.get("ok"):
        raise LandError(
            "state_change_failed",
            f"{task_id} was not marked done: {res.get('error')}: {res.get('message')}",
            {"fast_forwarded_to": ff_head, "state_error": res.get("error")},
            changed=ff_head != before,
        )
    moved = get_task(config, project_id, task_id)
    new_rel = _rel(_task_entry(moved.file_path), toplevel) if moved and moved.file_path else None
    own = [p for p in (old_rel, new_rel) if p]

    # Stage and commit ONLY the task's own paths.
    specs = [
        p for p in own
        if (toplevel / p).exists() or (_git(["ls-files", "--", p], toplevel).stdout or "").strip()
    ]
    msg = f"chore: mark {task_id} done (PR #{pr_number} merged)"
    try:
        _git(["add", "-A", "--", *specs], toplevel)
        _git(["commit", "-m", msg, "--", *specs], toplevel)
    except LandError as exc:
        raise LandError(
            "commit_failed", exc.message,
            {**exc.details, "fast_forwarded_to": ff_head, "task_paths": specs}, changed=True,
        )
    head = _out(["rev-parse", "HEAD"], toplevel)
    count = int(_out(["rev-list", "--count", f"{ff_head}..HEAD"], toplevel) or 0)
    files = [f for f in (_git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", "HEAD"], toplevel).stdout or "").split("\0") if f]
    body = _out(["log", "-1", "--format=%B"], toplevel)
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
        "uncommitted_other": [p for p in _dirty_tracked_paths(toplevel) if not _is_own(p, own)],
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
