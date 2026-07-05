"""Transaction-integrity tests for concurrency v4 (CLAWP-071).

Covers three deferred-from-CLAWP-067 restructures:

1. GENERAL 2-PHASE ATOMICITY of task-write + parent/mission-write pairs
   (``add_subtask`` child-create then parent-append; ``add_mission_mini_goal``
   task-tag then mission-rewrite) — a failure between the two writes must not
   leave divergent state.
2. STRUCTURAL TOCTOU — ``change_task_state`` resolves the task AND classifies
   directory-vs-file INSIDE the per-project lock (one consistent snapshot).
3. EXTERNAL-TAMPERING FS WRAPS — an external delete/move mid-critical-section
   surfaces a friendly ``ConcurrentModificationError`` (a ``ValueError``) with an
   actionable message, not a raw ``FileNotFoundError``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

import clawpm.mission as mission_mod
import clawpm.tasks as tasks_mod
from clawpm.concurrency import (
    ConcurrentModificationError,
    commit_staged_pair,
    guard_fs_tamper,
)
from clawpm.discovery import load_portfolio_config
from clawpm.mission import add_mission, add_mission_mini_goal, get_mission
from clawpm.models import TaskState
from clawpm.tasks import (
    add_subtask,
    add_task,
    change_task_state,
    edit_task,
    get_task,
    parent_rollup_status,
)


@pytest.fixture
def temp_portfolio():
    """A temporary portfolio with one active test project (id ``test``)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_c71_")
    portfolio_root = Path(temp_dir)
    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n\n'
        "[defaults]\nstatus = \"active\"\n",
        encoding="utf-8",
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()
    (tasks_dir / "rejected").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    try:
        yield {
            "root": portfolio_root,
            "project_dir": project_dir,
            "tasks_dir": tasks_dir,
            "config": load_portfolio_config(portfolio_root),
        }
    finally:
        if old_env is not None:
            os.environ["CLAWPM_PORTFOLIO"] = old_env
        else:
            os.environ.pop("CLAWPM_PORTFOLIO", None)
        shutil.rmtree(temp_dir, ignore_errors=True)


def _fail_read_after_first(monkeypatch, target: Path) -> dict:
    """Make ``target``'s ``read_text`` succeed once (resolution) then raise
    ``FileNotFoundError`` — simulating an external delete in the exists()→read
    window while the file is still physically present for the exists() check."""
    real_read = Path.read_text
    state = {"n": 0}

    def fake(self, *args, **kwargs):
        if self == target:
            state["n"] += 1
            if state["n"] >= 2:
                raise FileNotFoundError(
                    2, "No such file (simulated external delete)", str(self)
                )
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fake)
    return state


# ---------------------------------------------------------------------------
# guard_fs_tamper / ConcurrentModificationError — mechanism
# ---------------------------------------------------------------------------


class TestGuardFsTamper:
    def test_concurrent_modification_is_valueerror(self):
        assert issubclass(ConcurrentModificationError, ValueError)

    def test_maps_filenotfound(self):
        with pytest.raises(ConcurrentModificationError) as ei:
            with guard_fs_tamper("Widget X"):
                raise FileNotFoundError(2, "gone")
        assert "Widget X" in str(ei.value)
        assert "Retry" in str(ei.value)

    def test_maps_notadirectory(self):
        with pytest.raises(ConcurrentModificationError):
            with guard_fs_tamper("Widget X"):
                raise NotADirectoryError(20, "parent swapped")

    def test_passes_through_plain_valueerror(self):
        with pytest.raises(ValueError) as ei:
            with guard_fs_tamper("Widget X"):
                raise ValueError("domain validation error")
        assert not isinstance(ei.value, ConcurrentModificationError)

    def test_passes_through_permissionerror(self):
        # A permanent ACL denial (POSIX) / ambiguous winerror must NOT be masked.
        with pytest.raises(PermissionError):
            with guard_fs_tamper("Widget X"):
                raise PermissionError("access denied")


# ---------------------------------------------------------------------------
# commit_staged_pair — two-phase primitive
# ---------------------------------------------------------------------------


class TestCommitStagedPair:
    def test_both_committed_no_leftover_tmps(self, tmp_path):
        p1 = tmp_path / "a.md"
        p2 = tmp_path / "b.md"
        p1.write_text("old1")
        p2.write_text("old2")
        commit_staged_pair((p1, "new1"), (p2, "new2"))
        assert p1.read_text() == "new1"
        assert p2.read_text() == "new2"
        assert not (tmp_path / "a.md.tmp").exists()
        assert not (tmp_path / "b.md.tmp").exists()

    def test_staging_failure_leaves_both_targets_untouched(self, tmp_path, monkeypatch):
        p1 = tmp_path / "a.md"
        p2 = tmp_path / "b.md"
        p1.write_text("old1")
        p2.write_text("old2")
        real_wt = Path.write_text

        def failing_wt(self, data, *args, **kwargs):
            if self.name == "b.md.tmp":
                raise OSError("disk full staging second file")
            return real_wt(self, data, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_wt)
        with pytest.raises(OSError):
            commit_staged_pair((p1, "new1"), (p2, "new2"))
        # Neither target changed; both tmps cleaned up.
        assert p1.read_text() == "old1"
        assert p2.read_text() == "old2"
        assert not (tmp_path / "a.md.tmp").exists()
        assert not (tmp_path / "b.md.tmp").exists()

    def test_second_commit_failure_commits_first_only(self, tmp_path, monkeypatch):
        p1 = tmp_path / "a.md"
        p2 = tmp_path / "b.md"
        p1.write_text("old1")
        p2.write_text("old2")
        real_replace = Path.replace

        def failing_replace(self, target):
            if Path(target).name == "b.md":
                raise OSError("cannot commit second file")
            return real_replace(self, target)

        monkeypatch.setattr(Path, "replace", failing_replace)
        with pytest.raises(OSError):
            commit_staged_pair((p1, "new1"), (p2, "new2"))
        # `first` committed (the caller's self-healing direction); `second` not.
        assert p1.read_text() == "new1"
        assert p2.read_text() == "old2"
        assert not (tmp_path / "b.md.tmp").exists()


# ---------------------------------------------------------------------------
# _child_append_text contract (Codex P2: no-op vs build-failure)
# ---------------------------------------------------------------------------


class TestChildAppendTextContract:
    def _parent(self, tmp_path, children=None):
        p = tmp_path / "_task.md"
        fm = "id: P\npriority: 5\n"
        if children is not None:
            fm += "children:\n" + "".join(f"- {c}\n" for c in children)
        p.write_text(f"---\n{fm}---\n# Parent\n", encoding="utf-8")
        return p

    def test_returns_text_for_fresh_child(self, tmp_path):
        p = self._parent(tmp_path)
        out = tasks_mod._child_append_text(p, "P-001")
        assert out is not None and "P-001" in out

    def test_returns_none_when_already_listed(self, tmp_path):
        p = self._parent(tmp_path, children=["P-001"])
        assert tasks_mod._child_append_text(p, "P-001") is None

    def test_returns_none_for_non_task_md(self, tmp_path):
        p = tmp_path / "P-001.md"
        p.write_text("---\nid: P-001\n---\n# x\n", encoding="utf-8")
        assert tasks_mod._child_append_text(p, "P-001-001") is None

    def test_raises_when_parent_missing(self, tmp_path):
        # Build-failure, NOT a no-op: must raise so the two-phase caller writes
        # neither file rather than orphaning the child.
        missing = tmp_path / "_task.md"
        with pytest.raises(ConcurrentModificationError):
            tasks_mod._child_append_text(missing, "P-001")

    def test_raises_when_frontmatter_unparseable(self, tmp_path):
        from clawpm.frontmatter import FrontmatterError

        p = tmp_path / "_task.md"
        p.write_text("---\nid: [unterminated\n---\n# x\n", encoding="utf-8")
        with pytest.raises((FrontmatterError, ValueError)):
            tasks_mod._child_append_text(p, "P-001")


# ---------------------------------------------------------------------------
# Item 1a — add_subtask 2-phase atomicity
# ---------------------------------------------------------------------------


class TestAddSubtaskTwoPhase:
    def test_normal_links_child_on_parent(self, temp_portfolio):
        config = temp_portfolio["config"]
        parent = add_task(config, "test", "Parent")
        child = add_subtask(config, "test", parent.id, "Child")
        assert child is not None
        parent_dir = temp_portfolio["tasks_dir"] / parent.id
        pfm = yaml.safe_load(
            (parent_dir / "_task.md").read_text(encoding="utf-8").split("---", 2)[1]
        )
        assert child.id in (pfm.get("children") or [])

    def test_parent_build_failure_writes_no_child(self, temp_portfolio, monkeypatch):
        """A failure BUILDING the parent children-list text must leave NEITHER
        file written (the common failure is fully atomic — no orphan child)."""
        config = temp_portfolio["config"]
        parent = add_task(config, "test", "Parent")

        def boom(_parent_path, _child_id):
            raise OSError("simulated failure building parent frontmatter")

        monkeypatch.setattr(tasks_mod, "_child_append_text", boom)
        with pytest.raises(OSError):
            add_subtask(config, "test", parent.id, "Child")

        parent_dir = temp_portfolio["tasks_dir"] / parent.id
        # split_task ran (parent is now a dir), but NO child file was committed.
        children = (
            list(parent_dir.glob(f"{parent.id}-*.md")) if parent_dir.exists() else []
        )
        assert children == [], "no orphan child should exist after a build failure"

    def test_crash_after_child_before_parent_is_self_healing(
        self, temp_portfolio, monkeypatch
    ):
        """A hard kill between the child commit and the parent children-list
        commit leaves only an orphan child — which parent_rollup_status still
        counts (dir-scan backstop) and whose subtask number is NOT reused."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")

        def crash_after_first(first, second, encoding="utf-8"):
            (p1, c1), (_p2, _c2) = first, second
            p1.write_text(c1, encoding=encoding)  # child committed…
            raise OSError("simulated kill between the two commits")  # …parent not

        monkeypatch.setattr(tasks_mod, "commit_staged_pair", crash_after_first)
        with pytest.raises(OSError):
            add_subtask(config, "test", parent.id, "Child one")

        parent_dir = tasks_dir / parent.id
        child_id = f"{parent.id}-001"
        child_file = parent_dir / f"{child_id}.md"
        assert child_file.exists(), "child is committed first (safe direction)"

        # Parent children-list did NOT get the append (the divergence).
        pfm = yaml.safe_load(
            (parent_dir / "_task.md").read_text(encoding="utf-8").split("---", 2)[1]
        )
        assert child_id not in (pfm.get("children") or [])

        # Backstop: parent_rollup_status reconciles the orphan via its dir-scan,
        # so the parent is NOT falsely ready and the orphan is still tracked.
        fresh_parent = get_task(config, "test", parent.id)
        status = parent_rollup_status(config, "test", fresh_parent)
        assert status["ready"] is False
        tracked = {c["id"] for c in status["incomplete"]} | set(status["missing"])
        assert child_id in tracked

        # Numbering does not reuse the orphan's slot on the next add.
        monkeypatch.setattr(tasks_mod, "commit_staged_pair", commit_staged_pair)
        second = add_subtask(config, "test", parent.id, "Child two")
        assert second.id == f"{parent.id}-002"

    def test_numbering_union_counts_directory_child(self, temp_portfolio):
        """Codex P2 r2 (CLAWP-071): add_subtask's ID allocation must count
        DIRECTORY-task children too — otherwise a split orphan absent from the
        persisted children list has its number reused, creating a flat
        ``<parent>/<child>.md`` colliding with the existing directory task."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        child1 = add_subtask(config, "test", parent.id, "Real child")  # -001
        assert child1.id == f"{parent.id}-001"

        # Plant a DIRECTORY-task child at the next number (-002), NOT persisted
        # in the parent's children list (simulating a crash orphan later split).
        parent_dir = tasks_dir / parent.id
        planted_id = f"{parent.id}-002"
        planted_dir = parent_dir / planted_id
        planted_dir.mkdir()
        (planted_dir / "_task.md").write_text(
            f"---\nid: {planted_id}\nparent: {parent.id}\npriority: 5\n---\n# Split orphan\n",
            encoding="utf-8",
        )

        # Next add_subtask must SKIP -002 (the planted dir) and not collide.
        nxt = add_subtask(config, "test", parent.id, "Next child")
        assert nxt.id == f"{parent.id}-003"
        # No flat file was created next to the planted directory task.
        assert not (parent_dir / f"{planted_id}.md").exists()
        assert planted_dir.is_dir()

    def test_rollup_backstop_finds_directory_child(self, temp_portfolio):
        """Codex P2 (CLAWP-071): parent_rollup_status must reconcile a
        DIRECTORY-task child not in the persisted children list — otherwise a
        crash-orphaned child that was later split into a directory would drop out
        of the rollup and let the parent be marked DONE while the child is open."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        child1 = add_subtask(config, "test", parent.id, "Real child")
        # Complete the only persisted child so the parent is otherwise ready.
        change_task_state(config, "test", child1.id, TaskState.DONE)
        fresh = get_task(config, "test", parent.id)
        assert parent_rollup_status(config, "test", fresh)["ready"] is True

        # Manually plant an OPEN directory-task child that is NOT in the parent's
        # persisted children list (simulating a split orphan).
        parent_dir = tasks_dir / parent.id
        orphan_id = f"{parent.id}-777"
        orphan_dir = parent_dir / orphan_id
        orphan_dir.mkdir()
        (orphan_dir / "_task.md").write_text(
            f"---\nid: {orphan_id}\nparent: {parent.id}\npriority: 5\n---\n# Split orphan\n",
            encoding="utf-8",
        )

        fresh = get_task(config, "test", parent.id)
        status = parent_rollup_status(config, "test", fresh)
        assert status["ready"] is False, "directory child must gate the rollup"
        tracked = {c["id"] for c in status["incomplete"]} | set(status["missing"])
        assert orphan_id in tracked

    def test_numbering_union_counts_rejected_directory_child(self, temp_portfolio):
        """Codex P2 r3 (CLAWP-071): a crash-orphaned split child later REJECTED
        lives at ``tasks/rejected/<child>/_task.md``. The allocator must count it
        or the next add reuses its ordinal, and get_task later resolves the
        rejected ledger entry instead of the freshly-minted child."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        child1 = add_subtask(config, "test", parent.id, "Real child")  # -001
        assert child1.id == f"{parent.id}-001"

        # Plant a REJECTED directory-task child at -002, NOT in persisted children.
        rejected_id = f"{parent.id}-002"
        rejected_dir = tasks_dir / "rejected" / rejected_id
        rejected_dir.mkdir(parents=True)
        (rejected_dir / "_task.md").write_text(
            f"---\nid: {rejected_id}\nparent: {parent.id}\npriority: 5\n---\n# Rejected split\n",
            encoding="utf-8",
        )

        nxt = add_subtask(config, "test", parent.id, "Next child")
        assert nxt.id == f"{parent.id}-003", "must skip the rejected -002 ordinal"

    def test_numbering_union_counts_toplevel_reopened_directory_child(
        self, temp_portfolio
    ):
        """Codex P2 r3 (CLAWP-071): a split child completed then REOPENED is moved
        to the TOP-LEVEL ``tasks/<child>/_task.md`` (not back under the parent),
        keeping ``parent: <parent>``. The allocator must scan the top-level tasks
        dir for directory children or it reuses the reopened child's ordinal."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        child1 = add_subtask(config, "test", parent.id, "Real child")  # -001
        assert child1.id == f"{parent.id}-001"

        # Plant a top-level directory-task child at -002 (reopened split orphan),
        # NOT persisted in the parent's children list.
        reopened_id = f"{parent.id}-002"
        reopened_dir = tasks_dir / reopened_id
        reopened_dir.mkdir()
        (reopened_dir / "_task.md").write_text(
            f"---\nid: {reopened_id}\nparent: {parent.id}\npriority: 5\n---\n# Reopened split\n",
            encoding="utf-8",
        )

        nxt = add_subtask(config, "test", parent.id, "Next child")
        assert nxt.id == f"{parent.id}-003", "must skip the reopened -002 ordinal"

    def test_rollup_backstop_finds_rejected_directory_child(self, temp_portfolio):
        """Codex P2 r3 (CLAWP-071): parent_rollup_status must count a rejected
        directory child. A rejected child is unresolved (not DONE), so it must gate
        the parent — otherwise the parent can be completed over it."""
        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        child1 = add_subtask(config, "test", parent.id, "Real child")
        change_task_state(config, "test", child1.id, TaskState.DONE)
        fresh = get_task(config, "test", parent.id)
        assert parent_rollup_status(config, "test", fresh)["ready"] is True

        # Plant a REJECTED directory-task child not in persisted children.
        orphan_id = f"{parent.id}-778"
        orphan_dir = tasks_dir / "rejected" / orphan_id
        orphan_dir.mkdir(parents=True)
        (orphan_dir / "_task.md").write_text(
            f"---\nid: {orphan_id}\nparent: {parent.id}\npriority: 5\n---\n# Rejected split\n",
            encoding="utf-8",
        )

        fresh = get_task(config, "test", parent.id)
        status = parent_rollup_status(config, "test", fresh)
        assert status["ready"] is False, "rejected child must gate the rollup"
        tracked = {c["id"] for c in status["incomplete"]} | set(status["missing"])
        assert orphan_id in tracked

    def test_emit_tree_allocator_delegates_to_shared_union(self, temp_portfolio):
        """Codex P2 r3 (CLAWP-071): emit_tree's ``_existing_child_nums`` must route
        through the shared allocator so its collision pre-check / id-mint sees the
        SAME occupied ordinals (rejected + top-level directory children) that
        ``add_subtask`` does — no drift between the two allocators."""
        from clawpm import emit_tree as emit_mod
        from clawpm.tasks import _existing_child_ordinals

        config = temp_portfolio["config"]
        tasks_dir = temp_portfolio["tasks_dir"]
        parent = add_task(config, "test", "Parent")
        add_subtask(config, "test", parent.id, "Real child")  # -001

        # Plant a rejected directory child at -002 that only the shared (state-dir)
        # scan can see — the old emit_tree mirror scanned neither rejected nor
        # top-level directory children.
        rejected_id = f"{parent.id}-002"
        rejected_dir = tasks_dir / "rejected" / rejected_id
        rejected_dir.mkdir(parents=True)
        (rejected_dir / "_task.md").write_text(
            f"---\nid: {rejected_id}\nparent: {parent.id}\npriority: 5\n---\n# Rejected\n",
            encoding="utf-8",
        )

        emit_nums = emit_mod._existing_child_nums(tasks_dir, parent.id)
        shared_nums = _existing_child_ordinals(
            tasks_dir, tasks_dir / parent.id, parent.id
        )
        assert emit_nums == shared_nums
        assert 1 in emit_nums and 2 in emit_nums, "sees -001 and the rejected -002"


# ---------------------------------------------------------------------------
# Item 1b — add_mission_mini_goal 2-phase atomicity
# ---------------------------------------------------------------------------


class TestMissionMiniGoalTwoPhase:
    def test_unrenderable_mission_leaves_task_untagged(
        self, temp_portfolio, monkeypatch
    ):
        """Pre-flight render validates the mission BEFORE the task frontmatter is
        written, so an unwritable mission leaves the task un-tagged — no
        divergence, and no reliance on compensation rollback."""
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "obj", deadline_days=7)
        t = add_task(config, "test", "Task to link")

        def boom_render(_mission):
            raise ValueError("mission unrenderable (simulated)")

        monkeypatch.setattr(mission_mod, "_render_mission", boom_render)
        with pytest.raises(ValueError, match="unrenderable"):
            add_mission_mini_goal(config, "test", m.id, t.id)

        after = get_task(config, "test", t.id)
        assert not after.parent_mission, "task must NOT be tagged when mission unwritable"
        reloaded = get_mission(config, "test", m.id)
        assert t.id not in {g.id for g in reloaded.mini_goals}

    def test_happy_path_links_both_sides(self, temp_portfolio):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "obj", deadline_days=7)
        t = add_task(config, "test", "Task to link")
        add_mission_mini_goal(config, "test", m.id, t.id)
        after = get_task(config, "test", t.id)
        assert after.parent_mission == m.id
        reloaded = get_mission(config, "test", m.id)
        assert t.id in {g.id for g in reloaded.mini_goals}

    def test_rewrite_failure_rolls_back_task_frontmatter(
        self, temp_portfolio, monkeypatch
    ):
        """CLAWP-071: when the mission REWRITE fails (after pre-render validation
        passed and the task frontmatter was committed), the compensation must roll
        the task frontmatter back so the two files don't diverge (task tagged but
        mission missing the goal). The in-memory ``mission.mini_goals`` pop that
        rides along (Antigravity review) keeps the discarded internal object
        consistent but isn't observable through the public API — the disk-side
        rollback is."""
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "obj", deadline_days=7)
        t = add_task(config, "test", "Task to link")

        # Let the pre-render (validation) pass, then fail the actual rewrite.
        def boom_rewrite(_mission):
            raise OSError("mission write fault (simulated)")

        monkeypatch.setattr(mission_mod, "_rewrite_mission", boom_rewrite)
        with pytest.raises(OSError, match="write fault"):
            add_mission_mini_goal(config, "test", m.id, t.id)

        # Task frontmatter rolled back — not tagged.
        after = get_task(config, "test", t.id)
        assert not after.parent_mission, "task frontmatter must be rolled back"
        # Mission file on disk never gained the goal.
        reloaded = get_mission(config, "test", m.id)
        assert t.id not in {g.id for g in reloaded.mini_goals}


# ---------------------------------------------------------------------------
# Item 2 — structural TOCTOU in change_task_state
# ---------------------------------------------------------------------------


class TestChangeTaskStateSnapshot:
    def test_resolution_and_classification_happen_inside_lock(
        self, temp_portfolio, monkeypatch
    ):
        """The task resolution (get_task) + directory-vs-file classification must
        run INSIDE the per-project lock, so a concurrent split can't split the
        snapshot. Verified by recording lock-enter/exit around the get_task."""
        config = temp_portfolio["config"]
        t = add_task(config, "test", "Task")

        events: list[str] = []
        real_lock = tasks_mod.file_lock
        real_get = tasks_mod.get_task

        @contextmanager
        def recording_lock(path, *args, **kwargs):
            events.append("lock_enter")
            try:
                with real_lock(path, *args, **kwargs):
                    yield
            finally:
                events.append("lock_exit")

        def recording_get(*args, **kwargs):
            events.append("get_task")
            return real_get(*args, **kwargs)

        monkeypatch.setattr(tasks_mod, "file_lock", recording_lock)
        monkeypatch.setattr(tasks_mod, "get_task", recording_get)

        change_task_state(config, "test", t.id, TaskState.PROGRESS)

        assert events[0] == "lock_enter", "lock must be acquired before resolution"
        first_get = events.index("get_task")
        lock_exit = events.index("lock_exit")
        assert first_get < lock_exit, "resolution/classification must be under the lock"


# ---------------------------------------------------------------------------
# Item 3 — external-tampering FS wraps
# ---------------------------------------------------------------------------


class TestExternalTamperWraps:
    def test_edit_task_external_delete_maps_to_friendly_error(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        t = add_task(config, "test", "Editable")
        real_get = tasks_mod.get_task

        def deleting_get(*args, **kwargs):
            task = real_get(*args, **kwargs)
            if task and task.file_path and task.file_path.exists():
                task.file_path.unlink()  # external delete, after in-lock resolution
            return task

        monkeypatch.setattr(tasks_mod, "get_task", deleting_get)
        with pytest.raises(ConcurrentModificationError):
            edit_task(config, "test", t.id, priority=1)

    def test_write_rejection_frontmatter_missing_file_maps(self, temp_portfolio):
        missing = temp_portfolio["tasks_dir"] / "gone.md"
        with pytest.raises(ConcurrentModificationError):
            tasks_mod._write_rejection_frontmatter(missing, "a reason", None)

    def test_mission_task_vanishes_between_check_and_read(
        self, temp_portfolio, monkeypatch
    ):
        config = temp_portfolio["config"]
        m = add_mission(config, "test", "M", "obj", deadline_days=7)
        t = add_task(config, "test", "Task")
        target = get_task(config, "test", t.id).file_path
        _fail_read_after_first(monkeypatch, target)
        with pytest.raises(ConcurrentModificationError):
            add_mission_mini_goal(config, "test", m.id, t.id)

    # NOTE: serve.py's write endpoints were demoted to read-only no-ops by
    # CLAWP-078, so there is no in-lock read_text left to guard there — the
    # serve tamper-wrap and its test were dropped on the CLAWP-079/080/078
    # rebase (superseded), leaving three live wrap sites.
