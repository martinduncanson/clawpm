"""Post-merge landing transaction (CLAWP-147-002, gap G2).

``land_task`` books a merged PR from the MAIN checkout in one fail-closed
transaction: verify the PR is MERGED and its merge commit is an ancestor of
``<remote>/<base>``, fast-forward the base branch, mark the task done, and
commit ONLY the task's own paths (no attribution trailer).

Everything that can refuse runs before the first mutation, so a refusal
(:class:`LandError` with ``changed=False``) leaves the checkout exactly as it
was. Landing never fetches: the operator runs ``git fetch <remote>`` first, and
the local ``<remote>/<base>`` ref must already equal the remote's tip. The task
file must also be clean and byte-identical at HEAD and at that tip, so
eligibility is judged on one agreed version. After the fast-forward starts, a
failure raises a :class:`LandError` with ``changed=True`` and says what was and
was not done. There is no recovery step of any kind (no reset, no force, no
branch delete) with ONE exception: a failed fast-forward restores ``ORIG_HEAD``
(git writes it before it can refuse) to its exact prior bytes, or deletes it if
it was absent, so a refused landing leaves no trace in the git directory. ``--remove-worktree`` uses plain ``git worktree remove`` (git
itself refuses a dirty one).

Every ref read uses the verified tip SHA, never the ``<remote>/<base>`` name (a
tag or local branch of that name would win the lookup). The merge itself stays
with the operator; this module never merges.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from clawpm.codex_check import _extract_github_owner_repo
from clawpm.models import TaskState
from clawpm.pr_verify import PrVerifyError, _FULL_SHA, _gh_json

_TIMEOUT = 60
_NO_ATTRIBUTION = re.compile(r"co-authored-by|generated with|claude", re.I)
_LAND_BRANCH = "main"

# Every git call runs with these pinned, so user/repo config cannot change what
# a command does (CLAWP-147-002 rounds 2-4: autostash, squash mergeoptions,
# hidden untracked files, ignored submodules). Command-line
# `-c` outranks every config file.
_GIT_PINS = (
    "merge.autoStash=false",
    "merge.ff=only",
    f"branch.{_LAND_BRANCH}.mergeoptions=",
    "status.showUntrackedFiles=all",
    "diff.ignoreSubmodules=none",
    "core.fsmonitor=false",
    "submodule.recurse=false",
    "push.recurseSubmodules=no",
    "push.followTags=false",
    "gc.auto=0",
    "maintenance.auto=false",
)
# Location and config overrides inherited from a parent git process or the shell.
_GIT_ENV_STRIPPED = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
)
# transition() result keys ending in `_errors` that only degrade the audit trail.
_SOFT_TRANSITION_ERRORS = ("reflection_errors", "files_changed_errors", "parent_ready_errors")


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
    return " ".join(args[:3])


def _git_env(*, mutates: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_ENV_STRIPPED}
    env["GIT_TERMINAL_PROMPT"] = "0"  # a credential prompt must fail, not hang
    env["GIT_ADVICE"] = "0"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    if not mutates:
        env["GIT_OPTIONAL_LOCKS"] = "0"  # a read must not rewrite the index
    return env


def _git(
    args: list[str], cwd: Path, *, ok_codes: tuple[int, ...] = (0,), changed: bool = False,
    mutates: bool = False, binary: bool = False, stdin: bytes | None = None,
) -> subprocess.CompletedProcess:
    """The ONE place land.py runs git: config pinned, location env stripped.

    Raise LandError unless the exit code is in ``ok_codes``. ``changed`` is
    True for every call made after the first mutation, so a failure there can
    never be reported as "nothing changed". ``mutates`` marks the calls that
    write (merge, add, commit, push, worktree remove); every other call runs
    with optional locks off. ``binary`` returns bytes on stdout; ``stdin``
    (bytes) implies it.
    """
    pins = [x for pin in _GIT_PINS for x in ("-c", pin)]
    binary = binary or stdin is not None
    text_kw: dict[str, Any] = {} if binary else {"text": True, "encoding": "utf-8", "errors": "replace"}
    try:
        proc = subprocess.run(
            ["git", *pins, *args],
            cwd=str(cwd),
            capture_output=True,
            timeout=_TIMEOUT,
            env=_git_env(mutates=mutates),
            **({"input": stdin} if stdin is not None else {}),
            **text_kw,
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
            {"stderr": (proc.stderr.decode("utf-8", "replace") if isinstance(proc.stderr, bytes) else (proc.stderr or "")).strip()},
            changed=changed,
        )
    return proc


def _out(args: list[str], cwd: Path, *, changed: bool = False) -> str:
    return (_git(args, cwd, changed=changed).stdout or "").strip()


def _dirty_tracked_paths(cwd: Path, *, changed: bool = False) -> list[str]:
    """Tracked paths with staged or unstaged changes (untracked files are ignored).

    A read-only call (optional locks off), so a refusal leaves the index bytes
    alone; submodule ignore settings are pinned off so a hidden submodule edit
    cannot pass the clean-checkout gate.
    """
    return [p for _, p in _status_entries(cwd, untracked="no", changed=changed)]


def _status_entries(cwd: Path, *, untracked: str, changed: bool = False) -> list[tuple[str, str]]:
    """``(XY, path)`` per ``git status`` entry; ``untracked`` is ``no`` or ``all``."""
    raw = _git(
        ["status", "--porcelain=v1", "-z", f"--untracked-files={untracked}", "--no-renames", "--ignore-submodules=none"],
        cwd,
        changed=changed,
    ).stdout or ""
    return [(e[:2], e[3:]) for e in raw.split("\0") if len(e) > 3]  # "XY path"


_GIT_DIR_STATE_FILES = ("ORIG_HEAD", "MERGE_HEAD", "MERGE_MODE", "MERGE_MSG", "SQUASH_MSG")


def _git_dir_state(cwd: Path, *, changed: bool = False) -> tuple[tuple[str, bytes | None], ...]:
    """Bytes of the merge bookkeeping files in the --git-dir (``None`` when absent) plus every ref."""
    git_dir = Path(_out(["rev-parse", "--absolute-git-dir"], cwd, changed=changed))
    files: list[tuple[str, bytes | None]] = []
    for name in _GIT_DIR_STATE_FILES:
        try:
            files.append((name, (git_dir / name).read_bytes()))
        except FileNotFoundError:
            files.append((name, None))
        except OSError as exc:
            raise LandError("git_failed", f"could not read {git_dir / name}: {exc}", changed=changed)
    refs = _git(["for-each-ref", "--format=%(refname) %(objectname)"], cwd, changed=changed).stdout or ""
    return (*files, ("refs", refs.encode("utf-8", "replace")))


def _restore_orig_head(cwd: Path, before: tuple[tuple[str, bytes | None], ...]) -> None:
    """Put ORIG_HEAD back exactly as it was (failed ff only); best effort, the caller re-compares."""
    wanted = dict(before).get("ORIG_HEAD")
    try:
        path = Path(_out(["rev-parse", "--absolute-git-dir"], cwd, changed=True)) / "ORIG_HEAD"
        if wanted is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(wanted)
    except (OSError, LandError):
        pass  # the comparison that follows reports changed=True


def _checkout_state(cwd: Path, *, changed: bool = False) -> tuple:
    """HEAD, the logical index, the status including untracked files, and the git-dir bookkeeping.

    A failed merge can leave the index or working files changed while HEAD has
    not moved (git updates them before the ref), so "nothing changed" is
    decided by comparing this tuple before and after, never HEAD alone. The
    last element covers ORIG_HEAD / MERGE_* / SQUASH_MSG and every ref.
    """
    return (
        _out(["rev-parse", "HEAD"], cwd, changed=changed),
        _git(["ls-files", "--stage", "-z"], cwd, changed=changed).stdout or "",
        _git(["status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames", "--ignore-submodules=none"], cwd, changed=changed).stdout or "",
        _git_dir_state(cwd, changed=changed),
    )


def _worktree_blocker(wt: Path) -> tuple[str, str] | None:
    """``(code, message)`` when a plain ``git worktree remove`` of ``wt`` would refuse or lose work."""
    if _status_entries(wt, untracked="all"):
        return "worktree_dirty", f"worktree {wt} has uncommitted or untracked files; not removing it"
    for entry in (_git(["ls-files", "--stage", "-z"], wt).stdout or "").split("\0"):
        meta, _, sub = entry.partition("\t")
        if meta.startswith("160000"):  # a gitlink; git refuses to remove a worktree whose submodule is populated
            if (wt / sub / ".git").exists():
                return "worktree_has_submodules", f"worktree {wt} has a populated submodule ({sub}); git will not remove it"
    return None


def _task_at_tip(config, project_id: str, task_id: str, toplevel: Path, tasks_dir: Path, tip: str, target: str) -> None:
    """Refuse now if the task cannot be marked done on the post-fast-forward tree.

    The fast-forward replaces the task files, so eligibility read from the
    current checkout can be stale in either direction. The post-ff task tree is
    simulated in a temp dir: the tip's blobs plus the local task files the ff
    leaves in place (untracked or ignored ones, which can hold an open child).
    The task and its rollup are read from that tree through a pinned Scope, then
    judged by the SAME validator ``transition()`` uses, together with the
    ``done/`` destination as it will be after the ff. Nothing in the checkout
    is mutated.
    """
    from clawpm.sessions import Scope
    from clawpm.tasks import (
        DestinationFacts, get_task, parent_rollup_status, validate_done_transition,
    )

    try:
        tasks_rel = _rel(tasks_dir, toplevel)
    except ValueError:
        raise LandError("task_outside_checkout", f"tasks dir {tasks_dir} is not inside the checkout {toplevel}")
    # Blobs are read straight from the tree object: `git archive` would apply
    # export-ignore / export-subst attributes (from the tip or info/attributes).
    listing = _git(["ls-tree", "-r", "-z", "--full-tree", tip, "--", tasks_rel], toplevel, binary=True).stdout
    entries = []
    tip_paths: set[str] = set()
    for rec in listing.split(b"\0"):
        meta, _, name = rec.partition(b"\t")
        parts = meta.split()
        if len(parts) != 3:
            continue
        name_s = name.decode("utf-8", "replace")
        tip_paths.add(name_s)
        if parts[1] == b"blob" and parts[0] != b"120000":  # symlinks are not task files
            entries.append((parts[2], name_s))
    if not entries:
        raise LandError("task_missing_at_tip", f"{tasks_rel} does not exist in {target}; cannot validate {task_id} there")
    tracked = {
        p for p in (_git(["ls-files", "-z", "--", tasks_rel], toplevel).stdout or "").split("\0") if p
    }

    def tracked_at_head(rel: str) -> bool:
        return rel in tracked or any(p.startswith(rel + "/") for p in tracked)

    def unverifiable(why: str) -> LandError:
        return LandError(
            "task_tree_unverifiable",
            f"cannot simulate the task tree after the fast-forward ({why}); landing {task_id} refused",
            {"reason": why},
        )

    def kind_at_tip(rel: str) -> str:
        """absent | dir | other for ``rel`` once the ff has been applied."""
        out = _git(["ls-tree", "-z", "--full-tree", tip, "--", rel], toplevel, binary=True).stdout
        for rec in out.split(b"\0"):
            meta, _, name = rec.partition(b"\t")
            parts = meta.split()
            if len(parts) == 3 and name.decode("utf-8", "replace") == rel:
                return "dir" if parts[1] == b"tree" else "other"
        if tracked_at_head(rel):  # removed by the ff
            return "absent"
        try:
            st = os.lstat(toplevel / rel)
        except (FileNotFoundError, NotADirectoryError):
            return "absent"
        except OSError as exc:
            raise unverifiable(f"{rel}: {exc}")
        return "dir" if stat.S_ISDIR(st.st_mode) else "other"

    blobs = _git(["cat-file", "--batch"], toplevel, stdin=b"".join(oid + b"\n" for oid, _ in entries)).stdout
    with tempfile.TemporaryDirectory(prefix="clawpm-land-") as tmp:
        root = Path(tmp).resolve()
        pos = 0
        for oid, name in entries:
            nl = blobs.index(b"\n", pos)
            header = blobs[pos:nl].split()
            if len(header) != 3 or header[1] != b"blob":
                raise LandError("git_failed", f"git cat-file --batch gave {blobs[pos:nl]!r} for {oid.decode()}")
            size = int(header[2])
            data, pos = blobs[nl + 1:nl + 1 + size], nl + 1 + size + 1
            dest = (root / name).resolve()
            if root not in dest.parents:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        # Local task files that survive the ff: not in the tip (which wins) and
        # not tracked at HEAD (those vanish with it). The task's own done/
        # destination is judged below as a destination fact, not as a task file.
        own_leaf = f"done/{task_id}"

        def is_destination(rel_in_tasks: str) -> bool:
            return rel_in_tasks in (own_leaf, f"{own_leaf}.md") or rel_in_tasks.startswith(own_leaf + "/")

        def fail_walk(exc: OSError) -> None:
            raise exc

        try:
            for dirpath, dirnames, filenames in os.walk(tasks_dir, followlinks=False, onerror=fail_walk):
                here = Path(dirpath)
                for d in dirnames:
                    rel_d = (here / d).relative_to(tasks_dir).as_posix()
                    if os.path.islink(here / d) and not is_destination(rel_d):
                        raise unverifiable(f"{(Path(tasks_rel) / rel_d).as_posix()} is a symlinked directory")
                for fn in filenames:
                    if not fn.endswith(".md"):
                        continue
                    rel_f = (here / fn).relative_to(tasks_dir).as_posix()
                    full_rel = (Path(tasks_rel) / rel_f).as_posix()
                    if full_rel in tip_paths or tracked_at_head(full_rel) or is_destination(rel_f):
                        continue
                    out_file = (root / full_rel).resolve()
                    if root not in out_file.parents:
                        continue
                    out_file.parent.mkdir(parents=True, exist_ok=True)
                    out_file.write_bytes((here / fn).read_bytes())
        except OSError as exc:
            raise unverifiable(str(exc))

        scope = Scope(pinned_project_dir=(root / tasks_rel).parent)
        tip_task = get_task(config, project_id, task_id, scope=scope)
        if tip_task is None:
            raise LandError("task_missing_at_tip", f"{task_id} does not exist in {target}; the PR removed or renamed it")
        if tip_task.state == TaskState.DONE:
            raise LandError("task_already_done", f"{task_id} is already done; nothing to land")
        decomposed = tip_task.file_path is not None and tip_task.file_path.name == "_task.md"
        done_rel = f"{tasks_rel}/done"
        leaf_rel = f"{done_rel}/{task_id if decomposed else task_id + '.md'}"
        done_kind = kind_at_tip(done_rel)
        facts = DestinationFacts(
            done_dir=done_kind,
            leaf=kind_at_tip(leaf_rel) if done_kind != "other" else "absent",
            leaf_is_current=False,
            done_path=done_rel,
            leaf_path=leaf_rel,
        )
        refusal = validate_done_transition(
            task_id, tip_task.kind, None, False,
            parent_rollup_status(config, project_id, tip_task, scope=scope), facts,
        )
        if refusal is not None:
            raise LandError(refusal.code, refusal.message, refusal.details)


def _check_task_file_agreed(toplevel: Path, rel: str, head: str, tip: str, target: str, task_id: str) -> None:
    """Refuse unless ``rel`` has no local modification and its blob is the same at HEAD and the tip."""
    if rel in {p for _, p in _status_entries(toplevel, untracked="no")}:
        raise LandError(
            "task_file_dirty",
            f"{rel} has local modifications (staged or unstaged); commit or discard them before landing {task_id}",
            {"path": rel},
        )
    head_blob = _git(["rev-parse", "--verify", "-q", f"{head}:{rel}"], toplevel, ok_codes=(0, 1)).stdout.strip()
    if not head_blob:
        raise LandError("task_file_not_committed", f"{rel} is not tracked at HEAD; commit it before landing {task_id}", {"path": rel})
    tip_blob = _git(["rev-parse", "--verify", "-q", f"{tip}:{rel}"], toplevel, ok_codes=(0, 1)).stdout.strip()
    if not tip_blob:
        raise LandError("task_missing_at_tip", f"{rel} does not exist in {target}; the PR removed or renamed {task_id}", {"path": rel})
    if tip_blob != head_blob:
        raise LandError(
            "task_file_differs_at_tip",
            f"{rel} differs between HEAD and {target}; fast-forward or reconcile it first, then land {task_id}",
            {"path": rel, "head_blob": head_blob, "tip_blob": tip_blob},
        )


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
    parsed = _extract_github_owner_repo(_out(["remote", "get-url", remote], Path.cwd()))
    return f"{parsed[0]}/{parsed[1]}" if parsed else None


def _remote_branch_tip(remote: str, branch: str, cwd: Path, *, changed: bool = False) -> str:
    """The sha of ``refs/heads/<branch>`` on ``remote``, through the pinned runner (pr_verify's own is unpinned)."""
    out = _out(["ls-remote", remote, f"refs/heads/{branch}"], cwd, changed=changed)
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.strip() == f"refs/heads/{branch}" and _FULL_SHA.match(sha.strip()):
            return sha.strip()
    raise LandError(
        "remote_branch_missing",
        f"branch '{branch}' not found on remote '{remote}' (deleted, or the PR head is in another repo)",
        changed=changed,
    )


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
    from clawpm.tasks import get_task, get_tasks_dir

    cwd = Path(cwd) if cwd else Path.cwd()

    # 1. Where are we? Must be the main checkout, on `main`.
    toplevel = Path(_out(["rev-parse", "--show-toplevel"], cwd)).resolve()
    git_dir, common_dir = (_out(["rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir"], cwd).splitlines() + ["", ""])[:2]
    # CLAWP-162: this comparison gates the landing; an unresolvable side
    # refuses instead of comparing unresolved spellings.
    from clawpm.sessions import canonical_path

    try:
        _in_linked_worktree = canonical_path(Path(git_dir)) != canonical_path(Path(common_dir))
    except OSError as exc:
        raise LandError(
            "path_unresolvable",
            f"Could not resolve {git_dir} or {common_dir} ({type(exc).__name__}: {exc}); "
            "refusing to land without knowing whether this is the main checkout",
            {"toplevel": str(toplevel)},
        ) from exc
    if _in_linked_worktree:
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
    # Eligibility (open children, decision kind) is judged at the verified tip in
    # step 5b: the fast-forward replaces these files, so the local view can be stale.
    if task.state == TaskState.DONE:
        raise LandError("task_already_done", f"{task_id} is already done; nothing to land")
    old_entry = _task_entry(task.file_path)
    try:
        old_rel = _rel(old_entry, toplevel)
    except ValueError:
        raise LandError(
            "task_outside_checkout",
            f"task files at {old_entry} are not inside the checkout {toplevel}",
        )

    tasks_dir = get_tasks_dir(config, project_id)

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
        repo_slug = repo or _remote_slug(remote)
        if not repo_slug:
            raise LandError("repo_unresolved", f"cannot derive owner/repo from remote '{remote}'; pass --repo")
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

    # 5. Refs: no fetch (the operator ran `git fetch <remote>`). Prove the tracking
    #    ref is current, the merge commit is in it and our branch can fast-forward.
    #    From here on only the verified `tip` sha is used, never the ref name.
    target = f"{remote}/{base}"
    tip = _out(["rev-parse", "--verify", f"refs/remotes/{target}^{{commit}}"], toplevel)
    remote_tip = _remote_branch_tip(remote, base, toplevel)
    if tip != remote_tip:
        raise LandError(
            "remote_not_fetched",
            f"{target} is {tip} locally but {remote_tip} on the remote; run `git fetch {remote}` first (landing never fetches)",
            {"local": tip, "remote": remote_tip},
        )
    if _git(["cat-file", "-e", f"{merge_sha}^{{commit}}"], toplevel, ok_codes=(0, 1)).returncode != 0:
        raise LandError("merge_commit_not_fetched", f"merge commit {merge_sha} is not in the local repository; run `git fetch {remote}` first")
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

    # 5a. One agreed task file: clean against HEAD and byte-identical at the tip.
    #     Local edits that the ff would keep (or conflict with) are refused here,
    #     so transition() can never judge a different file than the one checked.
    _check_task_file_agreed(toplevel, _rel(task.file_path, toplevel), before, tip, target, task_id)

    # 5b. The task and its done/ destination as they will exist after the ff,
    #     judged by the validator transition() itself uses. Anything that would
    #     make the post-ff move refuse is refused here, before any mutation.
    if tasks_dir is not None:
        _task_at_tip(config, project_id, task_id, toplevel, tasks_dir, tip, target)

    # 6. Optional worktree removal: refuse up front when it cannot succeed.
    wt_path: Path | None = None
    if remove_worktree:
        blk = _find_task_worktree(toplevel, toplevel, task_id)
        if blk is not None:
            wt_path = Path(blk["worktree"])
            if "locked" in blk:
                raise LandError("worktree_locked", f"worktree {wt_path} is locked; unlock it first or drop --remove-worktree", {"worktree": str(wt_path)})
            blocker = _worktree_blocker(wt_path)
            if blocker:
                raise LandError(blocker[0], blocker[1], {"worktree": str(wt_path)})

    # 7. Snapshot the checkout; HEAD must still be what every check above ran against.
    state_before = _checkout_state(toplevel)
    if state_before[0] != before:
        raise LandError("head_moved", f"HEAD moved from {before} to {state_before[0]} during the checks; run again", {"head": state_before[0]})

    # ---- first mutation: every failure from here on is changed=True ----
    try:
        # Pinned off (see _GIT_PINS) and repeated on the command line: no autostash, no squash, ff only.
        _git(["merge", "--ff-only", "--no-squash", "--no-autostash", tip], toplevel, mutates=True)
    except LandError as exc:
        # A failed merge can have updated the index or files before the ref, so
        # compare the whole checkout state, not HEAD alone; if it cannot be read, assume changed.
        try:
            _restore_orig_head(toplevel, state_before[3])  # git writes it before refusing
            changed = _checkout_state(toplevel) != state_before
            after = _out(["rev-parse", "HEAD"], toplevel)
        except LandError:
            changed, after = True, None
        raise LandError("ff_failed", exc.message, {**exc.details, "head": after}, changed=changed)
    ff_head = _out(["rev-parse", "HEAD"], toplevel, changed=True)
    if ff_head != tip:
        raise LandError(
            "ff_not_applied",
            f"git merge --ff-only exited 0 but HEAD is {ff_head}, not the verified tip {tip}; nothing was marked done",
            {"head": ff_head, "tip": tip},
            changed=_checkout_state(toplevel, changed=True) != state_before,
        )

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
        _git(["add", "-A", "--", *specs], toplevel, changed=True, mutates=True)
        _git(["commit", "-m", msg, "--", *specs], toplevel, changed=True, mutates=True)
    except LandError as exc:
        raise LandError(
            "commit_failed", exc.message,
            {**exc.details, "fast_forwarded_to": ff_head}, changed=True,
        )
    head = _out(["rev-parse", "HEAD"], toplevel, changed=True)
    count = int(_out(["rev-list", "--count", f"{ff_head}..HEAD"], toplevel, changed=True) or 0)
    parent = _git(["rev-parse", "--verify", "-q", "HEAD^"], toplevel, ok_codes=(0, 1), changed=True).stdout.strip()
    files = [f for f in (_git(["diff-tree", "--no-commit-id", "--name-only", "-r", "--no-renames", "-z", "HEAD"], toplevel, changed=True).stdout or "").split("\0") if f]
    body = _out(["log", "-1", "--format=%B"], toplevel, changed=True)
    if count != 1 or parent != ff_head or not all(_is_own(f, own) for f in files) or _NO_ATTRIBUTION.search(body):
        raise LandError(
            "commit_unexpected",
            "the landing commit is not exactly one task-only commit without attribution; inspect it, nothing was undone",
            {"commits": count, "parent": parent, "files": files, "head": head},
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
    # transition() reports secondary failures (a dependent left blocked, a log
    # not written) in `*_errors` keys while still returning ok=True. Surface
    # them: audit-only ones as warnings, the rest make the landing not ok.
    tdata = res.get("data") or {}
    errors: list[dict[str, Any]] = []
    for key in sorted(k for k in tdata if k.endswith("_errors") and tdata[k]):
        for item in tdata[key] if isinstance(tdata[key], list) else [tdata[key]]:
            if key in _SOFT_TRANSITION_ERRORS:
                result["warnings"].append(f"{key}: {item}")
            else:
                errors.append({"source": key, "detail": item})
    if errors:
        result["ok"] = False
        result["landed"] = True
        result["errors"] = errors

    if push:
        try:
            _git(["push", "--no-follow-tags", "--no-recurse-submodules", remote, f"HEAD:refs/heads/{base}"], toplevel, mutates=True)
        except LandError as exc:
            raise LandError("push_failed", exc.message, {**exc.details, "local_head": head, "result": result}, changed=True)
        try:
            remote_after = _remote_branch_tip(remote, base, toplevel, changed=True)
        except LandError as exc:
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
            blocker = _worktree_blocker(wt_path)  # re-check: work may have appeared since the precheck
            if blocker:
                raise LandError(blocker[0], blocker[1], {"worktree": str(wt_path)}, changed=True)
            _git(["worktree", "remove", str(wt_path)], toplevel, mutates=True)
        except LandError as exc:
            raise LandError("worktree_remove_failed", exc.message, {**exc.details, "result": result}, changed=True)
        result["worktree_removed"] = str(wt_path)
    return result
