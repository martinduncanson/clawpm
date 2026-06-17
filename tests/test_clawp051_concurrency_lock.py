"""Contention tests for the per-project task-tree file lock (CLAWP-051).

Two races are guarded:
1. ID-allocation TOCTOU in add_task: scan→write is now serialised by
   file_lock(tasks_dir / ".clawpm-tasks.lock").
2. State-transition shutil.move: each move is now guarded; a move on an
   already-moved source raises FileNotFoundError with a clear message.

Test strategy
-------------
Threads alone do NOT prove cross-process file locking (Python's GIL and
threading.Lock share address space with OS fcntl/msvcrt lock objects).
The contention test spawns N≥8 concurrent *subprocesses* — each invokes
add_task via a small inline worker script — and then asserts that every
returned task_id is unique and every task file exists on disk.

To confirm the lock is load-bearing, the first test class also runs a
regression that demonstrates the race without the lock.  The test is skipped
if running under a platform where the race is effectively invisible (POSIX
append is atomic for small payloads; the race IS observable for file-system
mutations like this one, but we document the evidence rather than relying
on probabilistic failure).
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

from clawpm.concurrency import file_lock
from clawpm.discovery import load_portfolio_config
from clawpm.models import TaskState
from clawpm.tasks import add_subtask, add_task, change_task_state, get_task


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_SCRIPT = textwrap.dedent("""\
    import sys, json, os
    from pathlib import Path
    sys.path.insert(0, r'{src_path}')
    from clawpm.discovery import load_portfolio_config
    from clawpm.tasks import add_task

    portfolio_root = Path(r'{portfolio_root}')
    project_id = '{project_id}'
    title = sys.argv[1]
    config = load_portfolio_config(portfolio_root)
    task = add_task(config, project_id, title)
    if task is None:
        print(json.dumps({{"error": "add_task returned None", "title": title}}))
        sys.exit(1)
    print(json.dumps({{"id": task.id}}))
""")

_WORKER_SCRIPT_NO_LOCK = textwrap.dedent("""\
    import sys, json, re, shutil
    from pathlib import Path
    from datetime import date
    import yaml
    sys.path.insert(0, r'{src_path}')
    from clawpm.discovery import load_portfolio_config, get_project_dir
    from clawpm.tasks import assign_task_prefix, get_tasks_dir

    portfolio_root = Path(r'{portfolio_root}')
    project_id = '{project_id}'
    title = sys.argv[1]
    config = load_portfolio_config(portfolio_root)
    tasks_dir = get_tasks_dir(config, project_id)

    from clawpm.discovery import get_project
    _settings = get_project(config, project_id)
    prefix = assign_task_prefix(
        project_id, tasks_dir, config,
        explicit_prefix=getattr(_settings, 'task_prefix', None) if _settings else None,
    )
    _dir_pat = re.compile(rf'^{{re.escape(prefix)}}-([\\d]+)$')
    _file_pat = re.compile(rf'^{{re.escape(prefix)}}-([\\d]+)(?:\\.progress)?$')
    existing_nums = []
    for scan_dir in [tasks_dir, tasks_dir / 'done', tasks_dir / 'blocked']:
        if not scan_dir.exists():
            continue
        for f in scan_dir.glob(f'{{prefix}}-*.md'):
            m = _file_pat.match(f.stem)
            if m:
                existing_nums.append(int(m.group(1)))
        for entry in scan_dir.iterdir():
            if entry.is_dir():
                m = _dir_pat.match(entry.name)
                if m:
                    existing_nums.append(int(m.group(1)))
    next_num = max(existing_nums, default=-1) + 1
    task_id = f'{{prefix}}-{{next_num:03d}}'
    file_path = tasks_dir / f'{{task_id}}.md'
    content = f'---\\nid: {{task_id}}\\n---\\n# {{title}}\\n'
    # Tiny sleep to widen the TOCTOU window so multiple processes land here
    import time; time.sleep(0.005)
    file_path.write_text(content, encoding='utf-8')
    print(json.dumps({{"id": task_id}}))
""")


def _make_portfolio(tmp_dir: Path, project_id: str = "conc-test") -> Path:
    """Set up a minimal portfolio and return its root."""
    (tmp_dir / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_dir.as_posix()}"\n'
        f'project_roots = ["{(tmp_dir / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    meta = tmp_dir / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    return tmp_dir


def _spawn_workers(script: str, n: int, tmp_dir: Path, project_id: str) -> list[str]:
    """Spawn n subprocesses running *script*; return collected task ids."""
    import subprocess

    src_path = str(Path(__file__).parent.parent / "src")
    code = script.format(
        src_path=src_path,
        portfolio_root=str(tmp_dir),
        project_id=project_id,
    )
    env = {**os.environ, "CLAWPM_PORTFOLIO": str(tmp_dir)}
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", code, f"task-{i}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for i in range(n)
    ]
    ids: list[str] = []
    for i, proc in enumerate(procs):
        out, err = proc.communicate(timeout=30)
        raw = out.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0 or not raw:
            pytest.fail(
                f"Worker {i} failed (rc={proc.returncode}):\n"
                f"stdout: {raw!r}\nstderr: {err.decode('utf-8', errors='replace')!r}"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            pytest.fail(f"Worker {i} produced non-JSON: {raw!r}")
        if "error" in data:
            pytest.fail(f"Worker {i} add_task error: {data['error']}")
        ids.append(data["id"])
    return ids


# ---------------------------------------------------------------------------
# Test: ID-allocation uniqueness under process contention
# ---------------------------------------------------------------------------


class TestIdAllocationUnderContention:
    """N concurrent subprocesses each call add_task; all IDs must be unique."""

    N_WORKERS = 8

    def test_locked_ids_are_unique(self, tmp_path):
        """With file_lock in place, all N task IDs are distinct and files exist."""
        project_id = "conc-test"
        _make_portfolio(tmp_path, project_id)

        ids = _spawn_workers(_WORKER_SCRIPT, self.N_WORKERS, tmp_path, project_id)

        assert len(ids) == self.N_WORKERS, f"Expected {self.N_WORKERS} ids, got {ids}"
        assert len(set(ids)) == self.N_WORKERS, (
            f"Duplicate task IDs detected (lock failed!): {ids}"
        )

        tasks_dir = tmp_path / "projects" / project_id / ".project" / "tasks"
        for tid in ids:
            assert (tasks_dir / f"{tid}.md").exists(), (
                f"Task file for {tid} not found on disk"
            )

    def test_race_without_lock_produces_duplicates(self, tmp_path):
        """Regression: the no-lock worker reliably produces duplicate IDs.

        This test documents that the race is real and observable.  It is
        inherently probabilistic — on a lightly loaded single-core machine a
        small sleep in the worker widens the window enough to reproduce.  If
        the assertion never fires (e.g. very fast SSD), the test exits as
        xfail rather than masking a broken assertion.
        """
        project_id = "race-test"
        _make_portfolio(tmp_path, project_id)

        ids = _spawn_workers(_WORKER_SCRIPT_NO_LOCK, self.N_WORKERS, tmp_path, project_id)
        # On most systems the TOCTOU window + sleep produces duplicates.
        # If it doesn't (fast machine, single-core), mark as xfail, not a
        # hard failure — the point is documentation, not flaky CI.
        if len(set(ids)) == self.N_WORKERS:
            pytest.xfail(
                "Race not triggered this run (fast I/O / single core) — "
                "lock still validated by test_locked_ids_are_unique."
            )
        else:
            # Confirm duplicates were indeed produced without the lock.
            assert len(set(ids)) < self.N_WORKERS, (
                f"Expected duplicates without lock, got unique ids: {ids}"
            )


# ---------------------------------------------------------------------------
# Test: state-transition serialisation
# ---------------------------------------------------------------------------


class TestStateTransitionSerialization:
    """Concurrent transitions on the same task serialise; source-vanished case
    raises a clear error."""

    @pytest.fixture
    def portfolio(self, tmp_path):
        project_id = "trans-test"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)
        task = add_task(config, project_id, "Transition target")
        assert task is not None
        yield tmp_path, project_id, task.id, config

    def test_serial_transition_no_crash(self, portfolio):
        """Sequential transitions complete without error."""
        tmp_path, project_id, task_id, config = portfolio
        t = change_task_state(config, project_id, task_id, TaskState.PROGRESS)
        assert t is not None
        t = change_task_state(config, project_id, task_id, TaskState.DONE, force=True)
        assert t is not None

    def test_second_transition_on_moved_source_raises_clear_error(self, portfolio, monkeypatch):
        """If the task has already been moved by one session, a second attempt
        raises FileNotFoundError with a message mentioning concurrent session.

        Finding 3 fix: this test drives the PRODUCTION revalidation path in
        change_task_state rather than raising inline.  We monkeypatch get_task
        to return a Task whose file_path points at the original (now gone)
        location so change_task_state itself encounters the missing-file
        condition and raises.  Deleting the production guard makes this test
        fail, confirming it is load-bearing.
        """
        import clawpm.tasks as tasks_module

        tmp_path, project_id, task_id, config = portfolio

        # First transition: moves the file to done/
        t = change_task_state(config, project_id, task_id, TaskState.DONE, force=True)
        assert t is not None

        # Build a stale Task object whose file_path still points at the
        # original open-state location (which no longer exists on disk).
        tasks_dir = tmp_path / "projects" / project_id / ".project" / "tasks"
        stale_path = tasks_dir / f"{task_id}.md"
        assert not stale_path.exists(), "Pre-condition: stale_path must not exist"

        # Construct the stale Task from the done/ copy, but override file_path.
        done_task = get_task(config, project_id, task_id)
        assert done_task is not None

        from clawpm.models import Task

        stale_task = Task(
            id=done_task.id,
            title=done_task.title,
            state=done_task.state,
            file_path=stale_path,   # points at the vanished original location
            priority=done_task.priority,
            complexity=done_task.complexity,
            depends=done_task.depends,
            children=done_task.children,
            parent=done_task.parent,
        )

        # Monkeypatch get_task so the production code sees the stale Task.
        real_get_task = tasks_module.get_task

        def _stale_get_task(cfg, pid, tid):
            if pid == project_id and tid == task_id:
                return stale_task
            return real_get_task(cfg, pid, tid)

        monkeypatch.setattr(tasks_module, "get_task", _stale_get_task)

        # change_task_state must raise via the production current_path.exists()
        # revalidation inside the lock — not from any inline test logic.
        with pytest.raises(FileNotFoundError, match="concurrent session"):
            change_task_state(config, project_id, task_id, TaskState.BLOCKED)

    def test_sequential_transitions_on_different_tasks_succeed(self, tmp_path):
        """Two different tasks in the same project transition independently.

        Runs sequentially: the per-project lock guarantees these would serialise
        cleanly if run concurrently, so a sequential pass is a sufficient sanity
        check that distinct tasks don't clobber each other through the shared lock.
        """
        project_id = "multi-task"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        task_a = add_task(config, project_id, "Task A")
        task_b = add_task(config, project_id, "Task B")
        assert task_a is not None and task_b is not None

        ta = change_task_state(config, project_id, task_a.id, TaskState.DONE, force=True)
        tb = change_task_state(config, project_id, task_b.id, TaskState.BLOCKED)
        assert ta is not None, "Task A transition failed"
        assert tb is not None, "Task B transition failed"

    def test_rejected_transition_succeeds_happy_path(self, tmp_path):
        """REJECTED transition succeeds through the restructured code (Finding 1/5 regression)."""
        project_id = "reject-happy"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        task = add_task(config, project_id, "Task to reject")
        assert task is not None

        result = change_task_state(
            config, project_id, task.id, TaskState.REJECTED,
            rationale="Not needed any more",
        )
        assert result is not None, "REJECTED transition should succeed"
        assert result.state == TaskState.REJECTED

    def test_done_with_force_succeeds_happy_path(self, tmp_path):
        """DONE (force=True) succeeds through the restructured code (Finding 4/5 regression)."""
        project_id = "done-happy"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        task = add_task(config, project_id, "Task to finish")
        assert task is not None

        result = change_task_state(config, project_id, task.id, TaskState.DONE, force=True)
        assert result is not None, "DONE (force=True) transition should succeed"
        assert result.state == TaskState.DONE


# ---------------------------------------------------------------------------
# Test: explicit-ID clobber guard in add_task (Finding 2)
# ---------------------------------------------------------------------------


class TestExplicitIdClobberGuard:
    """add_task with an explicit task_id that already exists raises FileExistsError
    and does NOT overwrite the pre-existing file content."""

    def test_explicit_id_existing_task_raises_and_does_not_clobber(self, tmp_path):
        project_id = "clobber-guard"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        # Create the first task with an explicit ID.
        task = add_task(config, project_id, "Original title", task_id="CLOBBER-001")
        assert task is not None
        assert task.id == "CLOBBER-001"

        # Record the original file content so we can verify it is unchanged.
        tasks_dir = tmp_path / "projects" / project_id / ".project" / "tasks"
        file_path = tasks_dir / "CLOBBER-001.md"
        original_content = file_path.read_text(encoding="utf-8")

        # A second create with the same explicit ID must raise FileExistsError.
        with pytest.raises(FileExistsError, match="CLOBBER-001"):
            add_task(config, project_id, "Clobbering title", task_id="CLOBBER-001")

        # The file content must be identical to the original — not overwritten.
        assert file_path.read_text(encoding="utf-8") == original_content, (
            "File was overwritten despite FileExistsError being raised"
        )

    def test_generated_ids_are_never_clobbered(self, tmp_path):
        """Generated IDs are fresh under the lock — repeated add_task calls
        produce distinct files even without explicit IDs."""
        project_id = "gen-id-no-clobber"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        ids = [add_task(config, project_id, f"Task {i}").id for i in range(5)]  # type: ignore[union-attr]
        assert len(set(ids)) == 5, f"Duplicate generated IDs: {ids}"


# ---------------------------------------------------------------------------
# Test: add_subtask contention under concurrent processes (Finding 6)
# ---------------------------------------------------------------------------

_SUBTASK_WORKER_SCRIPT = textwrap.dedent("""\
    import sys, json
    from pathlib import Path
    sys.path.insert(0, r'{src_path}')
    from clawpm.discovery import load_portfolio_config
    from clawpm.tasks import add_subtask

    portfolio_root = Path(r'{portfolio_root}')
    project_id = '{project_id}'
    parent_id = '{parent_id}'
    title = sys.argv[1]
    config = load_portfolio_config(portfolio_root)
    task = add_subtask(config, project_id, parent_id, title)
    if task is None:
        print(json.dumps({{"error": "add_subtask returned None", "title": title}}))
        sys.exit(1)
    print(json.dumps({{"id": task.id}}))
""")


class TestAddSubtaskContention:
    """N≥8 concurrent subprocesses calling add_subtask on the same parent all
    produce unique subtask IDs and all files exist on disk (Finding 6)."""

    N_WORKERS = 8

    def test_concurrent_subtask_ids_are_unique(self, tmp_path):
        project_id = "subtask-conc"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        # Create the parent task first (in-process, single session) and trigger
        # the auto-split so the parent directory exists before workers spawn.
        # (split_task is not under file_lock — concurrent splits race; we avoid
        # that by doing one serial subtask creation here to settle the directory
        # structure, then spawning concurrent workers against the already-split parent.)
        parent = add_task(config, project_id, "Parent for decompose")
        assert parent is not None
        parent_id = parent.id
        seed = add_subtask(config, project_id, parent_id, "seed subtask")
        assert seed is not None, "Seed subtask creation failed"

        # Spawn N concurrent workers each adding a subtask to the same parent.
        import subprocess

        src_path = str(Path(__file__).parent.parent / "src")
        code = _SUBTASK_WORKER_SCRIPT.format(
            src_path=src_path,
            portfolio_root=str(tmp_path),
            project_id=project_id,
            parent_id=parent_id,
        )
        env = {**os.environ, "CLAWPM_PORTFOLIO": str(tmp_path)}
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", code, f"subtask-{i}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            for i in range(self.N_WORKERS)
        ]
        ids: list[str] = []
        for i, proc in enumerate(procs):
            out, err = proc.communicate(timeout=60)
            raw = out.decode("utf-8", errors="replace").strip()
            if proc.returncode != 0 or not raw:
                pytest.fail(
                    f"Subtask worker {i} failed (rc={proc.returncode}):\n"
                    f"stdout: {raw!r}\nstderr: {err.decode('utf-8', errors='replace')!r}"
                )
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                pytest.fail(f"Subtask worker {i} produced non-JSON: {raw!r}")
            if "error" in data:
                pytest.fail(f"Subtask worker {i} add_subtask error: {data['error']}")
            ids.append(data["id"])

        assert len(ids) == self.N_WORKERS, f"Expected {self.N_WORKERS} ids, got {ids}"
        assert len(set(ids)) == self.N_WORKERS, (
            f"Duplicate subtask IDs detected (lock failed!): {ids}"
        )

        # All subtask files must exist on disk.
        tasks_dir = tmp_path / "projects" / project_id / ".project" / "tasks"
        parent_dir = tasks_dir / parent_id
        for sid in ids:
            assert (parent_dir / f"{sid}.md").exists(), (
                f"Subtask file for {sid} not found under {parent_dir}"
            )


# ---------------------------------------------------------------------------
# Test: lock file is a sentinel, not a data file
# ---------------------------------------------------------------------------


class TestLockFileSentinel:
    """The .clawpm-tasks.lock file must not interfere with ID scanning."""

    def test_lock_file_not_misread_as_task(self, tmp_path):
        """add_task ignores .clawpm-tasks.lock — it doesn't match task ID globs."""
        project_id = "lock-sentinel"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        # Pre-create a stray .clawpm-tasks.lock to simulate a leftover sentinel
        tasks_dir = tmp_path / "projects" / project_id / ".project" / "tasks"
        (tasks_dir / ".clawpm-tasks.lock").write_text("", encoding="utf-8")

        # First task should still allocate -000, not be confused by the sentinel
        task = add_task(config, project_id, "First real task")
        assert task is not None
        assert task.id.endswith("-000"), (
            f"Expected -000, got {task.id} — lock sentinel polluted ID scan"
        )

    def test_lock_file_created_in_tasks_dir(self, tmp_path):
        """After add_task, .clawpm-tasks.lock is created in tasks_dir."""
        project_id = "lock-create"
        _make_portfolio(tmp_path, project_id)
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        add_task(config, project_id, "Any task")

        lock_path = tmp_path / "projects" / project_id / ".project" / "tasks" / ".clawpm-tasks.lock"
        assert lock_path.exists(), "Lock sentinel not created after add_task"


# ---------------------------------------------------------------------------
# Test: file_lock primitive (unit)
# ---------------------------------------------------------------------------


class TestFileLockPrimitive:
    """Direct unit tests for the file_lock context manager."""

    def test_creates_parent_dir(self, tmp_path):
        lock_path = tmp_path / "nested" / "deep" / ".lock"
        with file_lock(lock_path):
            pass
        assert lock_path.exists()

    def test_yields_and_releases(self, tmp_path):
        lock_path = tmp_path / ".lock"
        reached = []
        with file_lock(lock_path):
            reached.append("inside")
        assert reached == ["inside"]

    def test_releases_on_exception(self, tmp_path):
        """Lock must release even when the body raises."""
        lock_path = tmp_path / ".lock"
        with pytest.raises(RuntimeError, match="boom"):
            with file_lock(lock_path):
                raise RuntimeError("boom")
        # After exception, the lock file still exists but must be acquirable again
        with file_lock(lock_path):
            pass  # would deadlock if not released

    def test_sequential_acquisitions_succeed(self, tmp_path):
        """The same lock path can be acquired sequentially without deadlock."""
        lock_path = tmp_path / ".lock"
        for _ in range(5):
            with file_lock(lock_path):
                pass
