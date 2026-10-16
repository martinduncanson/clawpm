"""CLAWP-158 — one unique-temp ``atomic_write`` for every file rewrite.

The old writers staged through a SHARED temp name (``<id>.tmp``,
``<id>.md.tmp``, ``<id>.rollback.tmp``). Two writers of files that map to the
same shared name replaced or deleted each other's temp. Every site test plants
a sentinel at each legacy temp name next to the target: after the write the
sentinels must be untouched (a unique temp never touches them), no new temp may
remain, and an injected ``os.replace`` failure must leave the original bytes
alone and remove only the writer's own temp.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

import pytest

import clawpm.concurrency as concurrency
from clawpm.concurrency import atomic_write, commit_staged_pair
from clawpm.discovery import load_portfolio_config
from clawpm.doctor_apply import _rewrite_frontmatter_state
from clawpm.mission import (
    _rewrite_mission,
    add_mission,
    add_mission_mini_goal,
    get_mission,
)
from clawpm.tasks import (
    _append_child_to_parent_frontmatter,
    _append_decision_to_parent,
    _restore_file_bytes,
    _stamp_updated_file,
    _sync_state_line,
    _write_rejection_frontmatter,
    _write_resolution_frontmatter,
    add_subtask,
    add_task,
    edit_fog,
    edit_task,
)

SENTINEL = b"SENTINEL-do-not-touch"


def _legacy_names(p: Path) -> list[Path]:
    """Every shared temp name the pre-CLAWP-158 writers could use for ``p``."""
    return [
        p.with_suffix(".tmp"),
        p.with_suffix(p.suffix + ".tmp"),
        p.with_suffix(".rollback.tmp"),
        p.with_suffix(p.suffix + ".rollback.tmp"),
    ]


def _plant(*targets: Path) -> list[Path]:
    planted = []
    for t in targets:
        for name in _legacy_names(t):
            name.write_bytes(SENTINEL)
            planted.append(name)
    return planted


def _assert_clean(directory: Path, planted: list[Path]) -> None:
    for s in planted:
        assert s.read_bytes() == SENTINEL, f"{s.name} was clobbered or deleted"
    leftovers = [
        p.name for p in directory.rglob("*.tmp") if p not in set(planted)
    ]
    assert leftovers == [], f"temp files left behind: {leftovers}"


def _boom(*_a, **_k):
    raise OSError(5, "injected replace failure")


@pytest.fixture
def portfolio():
    root = Path(tempfile.mkdtemp(prefix="clawpm_c158_"))
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{(root / "projects").as_posix()}"]\n\n'
        '[defaults]\nstatus = "active"\n',
        encoding="utf-8",
    )
    meta = root / "projects" / "test-project" / ".project"
    (meta / "tasks").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    old = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(root)
    try:
        yield {
            "config": load_portfolio_config(root),
            "tasks_dir": meta / "tasks",
            "root": root,
        }
    finally:
        if old is None:
            os.environ.pop("CLAWPM_PORTFOLIO", None)
        else:
            os.environ["CLAWPM_PORTFOLIO"] = old
        shutil.rmtree(root, ignore_errors=True)


TASK_TEXT = (
    "---\nid: T-001\ntitle: A task\nstate: open\npriority: 3\n"
    "updated: '2026-01-01'\n---\n# A task\n\nbody\n"
)


def _task(tmp_path: Path, name: str = "T-001.md", text: str = TASK_TEXT) -> Path:
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return p


# --- the helper itself -----------------------------------------------------


class TestAtomicWriteHelper:
    def test_writes_bytes_and_str(self, tmp_path):
        p = tmp_path / "f.md"
        atomic_write(p, b"a\r\nb\n")
        assert p.read_bytes() == b"a\r\nb\n"
        atomic_write(p, "x\ny\n", newline="")
        assert p.read_bytes() == b"x\ny\n"
        atomic_write(p, "é\n", encoding="utf-8", newline="\n")
        assert p.read_bytes() == "é\n".encode("utf-8")

    def test_str_default_newline_matches_write_text(self, tmp_path):
        a, b = tmp_path / "a.md", tmp_path / "b.md"
        atomic_write(a, "l1\nl2\n")
        b.write_text("l1\nl2\n", encoding="utf-8")
        assert a.read_bytes() == b.read_bytes()

    def test_creates_new_file_and_leaves_no_temp(self, tmp_path):
        p = tmp_path / "new.md"
        atomic_write(p, "hi")
        assert p.exists()
        assert list(tmp_path.glob("*.tmp")) == []

    def test_temp_names_are_unique_per_call(self, tmp_path, monkeypatch):
        seen = []
        real = os.replace

        def spy(src, dst, *a, **k):
            seen.append(Path(src).name)
            return real(src, dst, *a, **k)

        monkeypatch.setattr(os, "replace", spy)
        atomic_write(tmp_path / "a.md", "1")
        atomic_write(tmp_path / "a.md", "2")
        atomic_write(tmp_path / "b.md", "3")
        assert len(set(seen)) == 3

    def test_replace_failure_removes_temp_and_keeps_original(
        self, tmp_path, monkeypatch
    ):
        p = tmp_path / "f.md"
        p.write_bytes(b"orig")
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            atomic_write(p, b"new")
        assert p.read_bytes() == b"orig"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_write_failure_removes_temp(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_bytes(b"orig")
        with pytest.raises(UnicodeEncodeError):
            atomic_write(p, "\ud800", encoding="utf-8")  # lone surrogate
        assert p.read_bytes() == b"orig"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_keyboard_interrupt_still_cleans_up(self, tmp_path, monkeypatch):
        p = tmp_path / "f.md"
        p.write_bytes(b"orig")

        def interrupt(*_a, **_k):
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "replace", interrupt)
        with pytest.raises(KeyboardInterrupt):
            atomic_write(p, b"new")
        assert list(tmp_path.glob("*.tmp")) == []

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_preserves_existing_mode(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_bytes(b"x")
        p.chmod(0o640)
        atomic_write(p, b"y")
        assert (p.stat().st_mode & 0o777) == 0o640

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_new_file_honours_current_umask_not_import_time_umask(self, tmp_path):
        old = os.umask(0o077)
        try:
            tight = tmp_path / "tight.md"
            atomic_write(tight, b"x")
            os.umask(0o022)
            loose = tmp_path / "loose.md"
            atomic_write(loose, b"x")
        finally:
            os.umask(old)
        assert (tight.stat().st_mode & 0o777) == 0o600
        assert (loose.stat().st_mode & 0o777) == 0o644

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
    def test_existing_mode_carried_over_regardless_of_umask(self, tmp_path):
        p = tmp_path / "f.md"
        p.write_bytes(b"x")
        p.chmod(0o640)
        old = os.umask(0o077)
        try:
            atomic_write(p, b"y")
        finally:
            os.umask(old)
        assert (p.stat().st_mode & 0o777) == 0o640

    def test_chmod_failure_aborts_before_replace(self, tmp_path, monkeypatch):
        p = tmp_path / "f.md"
        p.write_bytes(b"orig")

        def bad_chmod(*_a, **_k):
            raise OSError(1, "injected chmod failure")

        monkeypatch.setattr(os, "chmod", bad_chmod)
        with pytest.raises(OSError):
            atomic_write(p, b"new")
        assert p.read_bytes() == b"orig"
        assert list(tmp_path.glob("*.tmp")) == []

    def test_retries_when_temp_name_collides(self, tmp_path, monkeypatch):
        p = tmp_path / "f.md"
        fixed = iter(["a" * 32, "a" * 32, "b" * 32])

        class _U:
            def __init__(self, h):
                self.hex = h

        monkeypatch.setattr(concurrency.uuid, "uuid4", lambda: _U(next(fixed)))
        (tmp_path / f".f.md.{'a' * 32}.tmp").write_bytes(SENTINEL)
        atomic_write(p, b"new")
        assert p.read_bytes() == b"new"
        assert (tmp_path / f".f.md.{'a' * 32}.tmp").read_bytes() == SENTINEL

    def test_concurrent_writers_same_dir_never_collide(self, tmp_path):
        barrier = threading.Barrier(8)
        errors: list[BaseException] = []

        def work(i: int) -> None:
            try:
                barrier.wait()
                for n in range(20):
                    atomic_write(tmp_path / f"f{i}.md", f"{i}:{n}\n")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        assert list(tmp_path.glob("*.tmp")) == []
        for i in range(8):
            assert (tmp_path / f"f{i}.md").read_text().startswith(f"{i}:19")


# --- per-site: direct writers ---------------------------------------------


def _direct_sites():
    """(id, target-builder, callable(path)) for the file-level writers."""

    def stamp(p):
        _stamp_updated_file(p, "2026-02-02")

    def sync(p):
        assert _sync_state_line(p, "done") is True

    def reject(p):
        _write_rejection_frontmatter(p, "because", None)

    def resolve(p):
        _write_resolution_frontmatter(p, "decided")

    def restore(p):
        _restore_file_bytes(p, TASK_TEXT.encode("utf-8"))

    def decision_parent(p):
        _append_decision_to_parent(p, "T-002", "child", "res")

    def child_parent(p):
        _append_child_to_parent_frontmatter(p, "T-001-001")

    def doctor(p):
        _rewrite_frontmatter_state(p, "done")

    return {
        "stamp_updated": stamp,
        "sync_state": sync,
        "rejection": reject,
        "resolution": resolve,
        "restore_bytes": restore,
        "decision_parent": decision_parent,
        "child_parent": child_parent,
        "doctor_apply": doctor,
    }


_PARENT_TEXT = (
    "---\nid: T-001\ntitle: P\nstate: open\npriority: 3\n"
    "updated: '2026-01-01'\n---\n# P\n\n## Decisions so far\n\n"
)


@pytest.mark.parametrize("site", sorted(_direct_sites()))
class TestDirectWriterSites:
    def _target(self, tmp_path, site):
        if site in ("decision_parent", "child_parent"):
            return _task(tmp_path, "_task.md", _PARENT_TEXT)
        return _task(tmp_path)

    def test_does_not_touch_shared_legacy_names(self, tmp_path, site):
        p = self._target(tmp_path, site)
        planted = _plant(p)
        _direct_sites()[site](p)
        _assert_clean(tmp_path, planted)

    def test_replace_failure_cleans_only_own_temp(self, tmp_path, site, monkeypatch):
        p = self._target(tmp_path, site)
        before = p.read_bytes()
        planted = _plant(p)
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            _direct_sites()[site](p)
        assert p.read_bytes() == before
        _assert_clean(tmp_path, planted)


class TestCrlfPreserved:
    def test_stamp_updated_keeps_crlf_bytes(self, tmp_path):
        p = _task(tmp_path, text=TASK_TEXT.replace("\n", "\r\n"))
        _stamp_updated_file(p, "2026-02-02")
        data = p.read_bytes()
        assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


# --- per-site: public mutators --------------------------------------------


class TestPublicMutatorSites:
    def test_add_task(self, portfolio):
        tasks_dir = portfolio["tasks_dir"]
        target = tasks_dir / "TEST-050.md"
        planted = _plant(target)
        add_task(portfolio["config"], "test", "New", task_id="TEST-050")
        assert target.exists()
        _assert_clean(tasks_dir, planted)

    def test_add_task_replace_failure(self, portfolio, monkeypatch):
        tasks_dir = portfolio["tasks_dir"]
        planted = _plant(tasks_dir / "TEST-050.md")
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            add_task(portfolio["config"], "test", "New", task_id="TEST-050")
        assert not (tasks_dir / "TEST-050.md").exists()
        _assert_clean(tasks_dir, planted)

    def test_edit_task_and_edit_fog(self, portfolio):
        cfg, tasks_dir = portfolio["config"], portfolio["tasks_dir"]
        add_task(cfg, "test", "T", task_id="TEST-051")
        target = tasks_dir / "TEST-051.md"
        planted = _plant(target)
        edit_task(cfg, "test", "TEST-051", title="T2")
        edit_fog(cfg, "test", "TEST-051", add="unknown thing")
        _assert_clean(tasks_dir, planted)

    def test_edit_task_replace_failure(self, portfolio, monkeypatch):
        cfg, tasks_dir = portfolio["config"], portfolio["tasks_dir"]
        add_task(cfg, "test", "T", task_id="TEST-051")
        target = tasks_dir / "TEST-051.md"
        before = target.read_bytes()
        planted = _plant(target)
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            edit_task(cfg, "test", "TEST-051", title="T2")
        assert target.read_bytes() == before
        _assert_clean(tasks_dir, planted)

    def test_add_subtask_pair(self, portfolio):
        cfg, tasks_dir = portfolio["config"], portfolio["tasks_dir"]
        parent = add_task(cfg, "test", "P", task_id="TEST-052")
        files = list(tasks_dir.rglob("*TEST-052*"))
        planted = _plant(*[f for f in files if f.suffix == ".md"])
        # The first subtask splits the flat parent into a directory task; plant
        # the legacy names for the parent's _task.md and the NEXT child too.
        first = add_subtask(cfg, "test", parent.id, "child")
        parent_dir = first.file_path.parent
        planted += _plant(parent_dir / "_task.md", parent_dir / "TEST-052-002.md")
        add_subtask(cfg, "test", parent.id, "second child")
        assert (parent_dir / "TEST-052-002.md").exists()
        _assert_clean(tasks_dir, planted)

    def test_add_subtask_noop_parent_branch(self, portfolio, monkeypatch):
        # _child_append_text returns None for a fresh child id only when the
        # parent already lists it, which the ordinal allocator never produces,
        # so the single-write branch is unreachable through public state alone.
        # Force the pure helper to report a no-op and drive the real add_subtask.
        import clawpm.tasks as tasks_mod

        cfg, tasks_dir = portfolio["config"], portfolio["tasks_dir"]
        parent = add_task(cfg, "test", "P", task_id="TEST-054")
        first = add_subtask(cfg, "test", parent.id, "child")
        parent_dir = first.file_path.parent
        parent_before = (parent_dir / "_task.md").read_bytes()
        planted = _plant(parent_dir / "TEST-054-002.md")
        monkeypatch.setattr(tasks_mod, "_child_append_text", lambda *a, **k: None)
        add_subtask(cfg, "test", parent.id, "second child")
        assert (parent_dir / "TEST-054-002.md").exists()
        assert (parent_dir / "_task.md").read_bytes() == parent_before
        _assert_clean(tasks_dir, planted)


class TestMissionSites:
    def test_add_mission_and_rewrite(self, portfolio):
        cfg, root = portfolio["config"], portfolio["root"]
        mission_dir_parent = root / "projects" / "test-project" / ".project"
        # Plant after creation so the target path is known; creation itself is
        # checked via the post-condition of no temp files.
        m = add_mission(cfg, "test", "M", "outcome", mission_id="TEST-MISSION-001")
        assert m.file_path is not None
        planted = _plant(m.file_path)
        _rewrite_mission(m)
        _assert_clean(mission_dir_parent, planted)

    def test_add_mission_does_not_touch_legacy_names(self, portfolio):
        cfg, root = portfolio["config"], portfolio["root"]
        meta = root / "projects" / "test-project" / ".project"
        mdir = meta / "missions"
        mdir.mkdir(exist_ok=True)
        planted = _plant(mdir / "TEST-MISSION-002.md")
        add_mission(cfg, "test", "M", "outcome", mission_id="TEST-MISSION-002")
        _assert_clean(meta, planted)

    def test_rewrite_mission_replace_failure(self, portfolio, monkeypatch):
        cfg, root = portfolio["config"], portfolio["root"]
        meta = root / "projects" / "test-project" / ".project"
        m = add_mission(cfg, "test", "M", "outcome", mission_id="TEST-MISSION-001")
        before = m.file_path.read_bytes()
        planted = _plant(m.file_path)
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            _rewrite_mission(m)
        assert m.file_path.read_bytes() == before
        _assert_clean(meta, planted)

    def test_mini_goal_rollback_branch(self, portfolio, monkeypatch):
        import clawpm.mission as mission_mod

        cfg, root, tasks_dir = portfolio["config"], portfolio["root"], portfolio["tasks_dir"]
        meta = root / "projects" / "test-project" / ".project"
        add_mission(cfg, "test", "M", "outcome", mission_id="TEST-MISSION-001")
        add_task(cfg, "test", "T", task_id="TEST-055")
        task_file = tasks_dir / "TEST-055.md"
        before = task_file.read_bytes()
        planted = _plant(task_file)

        def fail_mission_write(_mission):
            raise OSError(5, "injected mission write failure")

        monkeypatch.setattr(mission_mod, "_rewrite_mission", fail_mission_write)
        with pytest.raises(OSError):
            add_mission_mini_goal(cfg, "test", "TEST-MISSION-001", "TEST-055")
        # The tagged task was written, then rolled back through atomic_write.
        assert task_file.read_bytes() == before
        _assert_clean(meta, planted)

    def test_mini_goal_writes(self, portfolio):
        cfg, root, tasks_dir = portfolio["config"], portfolio["root"], portfolio["tasks_dir"]
        meta = root / "projects" / "test-project" / ".project"
        m = add_mission(cfg, "test", "M", "outcome", mission_id="TEST-MISSION-001")
        add_task(cfg, "test", "T", task_id="TEST-053")
        planted = _plant(m.file_path, tasks_dir / "TEST-053.md")
        add_mission_mini_goal(cfg, "test", "TEST-MISSION-001", "TEST-053")
        assert get_mission(cfg, "test", "TEST-MISSION-001") is not None
        _assert_clean(meta, planted)


# --- two-phase pair + remaining sites -------------------------------------


class TestCommitStagedPair:
    def test_legacy_names_untouched_and_no_leftover(self, tmp_path):
        a, b = tmp_path / "a.md", tmp_path / "b.md"
        a.write_text("old1")
        b.write_text("old2")
        planted = _plant(a, b)
        commit_staged_pair((a, "new1"), (b, "new2"))
        assert a.read_text() == "new1" and b.read_text() == "new2"
        _assert_clean(tmp_path, planted)

    def test_second_staging_failure_removes_first_temp(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a.md", tmp_path / "b.md"
        a.write_text("old1")
        b.write_text("old2")
        planted = _plant(a, b)
        real = concurrency._stage_temp
        calls = {"n": 0}

        def flaky(path, data, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full staging second file")
            return real(path, data, **kw)

        monkeypatch.setattr(concurrency, "_stage_temp", flaky)
        with pytest.raises(OSError):
            commit_staged_pair((a, "new1"), (b, "new2"))
        assert a.read_text() == "old1" and b.read_text() == "old2"
        _assert_clean(tmp_path, planted)


class TestReadOnlyTargetCleanup:
    """A read-only target carries its mode onto the temp; cleanup must still remove it.

    On Windows ``os.unlink`` fails on a read-only file, so a temp that inherited
    ``0o444`` was left behind when the write failed (Codex r2 P2).
    """

    @staticmethod
    def _readonly(p: Path, data: bytes = b"orig") -> Path:
        p.write_bytes(data)
        p.chmod(0o444)
        return p

    @staticmethod
    def _release(*paths: Path) -> None:
        for p in paths:
            try:
                p.chmod(stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass

    def test_atomic_write_readonly_target_real_replace(self, tmp_path):
        p = self._readonly(tmp_path / "f.md")
        try:
            try:
                atomic_write(p, b"new")
            except OSError:
                # Windows: replace onto a read-only target fails.
                assert p.read_bytes() == b"orig"
            assert list(tmp_path.glob("*.tmp")) == []
        finally:
            self._release(p)

    def test_atomic_write_replace_failure_removes_readonly_temp(
        self, tmp_path, monkeypatch
    ):
        p = self._readonly(tmp_path / "f.md")
        try:
            monkeypatch.setattr(os, "replace", _boom)
            with pytest.raises(OSError):
                atomic_write(p, b"new")
            assert p.read_bytes() == b"orig"
            assert list(tmp_path.glob("*.tmp")) == []
        finally:
            self._release(p)

    def test_pair_first_replace_failure_removes_readonly_temps(
        self, tmp_path, monkeypatch
    ):
        a = self._readonly(tmp_path / "a.md", b"old1")
        b = self._readonly(tmp_path / "b.md", b"old2")
        try:
            monkeypatch.setattr(os, "replace", _boom)
            with pytest.raises(OSError):
                commit_staged_pair((a, "new1"), (b, "new2"))
            assert a.read_bytes() == b"old1" and b.read_bytes() == b"old2"
            assert list(tmp_path.glob("*.tmp")) == []
        finally:
            self._release(a, b)

    def test_pair_second_replace_failure_removes_readonly_temp(
        self, tmp_path, monkeypatch
    ):
        a = tmp_path / "a.md"
        a.write_bytes(b"old1")
        b = self._readonly(tmp_path / "b.md", b"old2")
        real = os.replace

        def replace_a_only(src, dst, *args, **kw):
            if Path(dst) == b:
                raise OSError("injected failure committing second file")
            return real(src, dst, *args, **kw)

        try:
            monkeypatch.setattr(os, "replace", replace_a_only)
            with pytest.raises(OSError):
                commit_staged_pair((a, "new1"), (b, "new2"))
            assert b.read_bytes() == b"old2"
            assert list(tmp_path.glob("*.tmp")) == []
        finally:
            self._release(a, b)

    def test_pair_second_staging_failure_removes_readonly_first_temp(
        self, tmp_path, monkeypatch
    ):
        a = self._readonly(tmp_path / "a.md", b"old1")
        b = tmp_path / "b.md"
        b.write_bytes(b"old2")
        real = concurrency._stage_temp
        calls = {"n": 0}

        def flaky(path, data, **kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full staging second file")
            return real(path, data, **kw)

        try:
            monkeypatch.setattr(concurrency, "_stage_temp", flaky)
            with pytest.raises(OSError):
                commit_staged_pair((a, "new1"), (b, "new2"))
            assert list(tmp_path.glob("*.tmp")) == []
        finally:
            self._release(a, b)


class TestDispatchSites:
    @staticmethod
    def _pid_name(p: Path) -> Path:
        return p.with_name(f"{p.name}.{os.getpid()}.tmp")

    def test_dispatch_writes_leave_pid_named_temp_alone(self, tmp_path):
        from clawpm.dispatch import (
            session_start_payload_path,
            settings_path,
            write_dispatch_settings,
        )

        settings, sidecar = settings_path(tmp_path), session_start_payload_path(tmp_path)
        settings.parent.mkdir(parents=True, exist_ok=True)
        planted = []
        for t in (settings, sidecar):
            name = self._pid_name(t)
            name.write_bytes(SENTINEL)
            planted.append(name)
        written = write_dispatch_settings(tmp_path, "T-001", "test", rubric_markdown="r")
        assert settings.read_bytes() == written.settings_bytes
        assert sidecar.exists()
        _assert_clean(tmp_path, planted)

    def test_dispatch_write_failure_cleans_only_own_temp(self, tmp_path, monkeypatch):
        from clawpm.dispatch import _write_exact

        p = tmp_path / "settings.local.json"
        p.write_bytes(b"orig")
        planted = [self._pid_name(p)]
        planted[0].write_bytes(SENTINEL)
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            _write_exact(p, "new")
        assert p.read_bytes() == b"orig"
        _assert_clean(tmp_path, planted)


class TestReflectVoidSite:
    def _ref_file(self, portfolio) -> Path:
        ref_dir = portfolio["root"] / "reflections"
        ref_dir.mkdir(exist_ok=True)
        ref = ref_dir / "TEST-060.jsonl"
        ref.write_bytes(b'{"event": "complete", "task_id": "TEST-060"}\n')
        return ref

    def test_void_does_not_touch_legacy_names(self, portfolio):
        from click.testing import CliRunner

        from clawpm.cli import main

        ref = self._ref_file(portfolio)
        planted = _plant(ref)
        result = CliRunner().invoke(
            main, ["reflect", "void", "TEST-060", "--reason", "x"]
        )
        assert result.exit_code == 0, result.output
        assert b'"event": "void"' in ref.read_bytes()
        _assert_clean(ref.parent, planted)

    def test_void_replace_failure_keeps_original(self, portfolio, monkeypatch):
        from click.testing import CliRunner

        from clawpm.cli import main

        ref = self._ref_file(portfolio)
        before = ref.read_bytes()
        planted = _plant(ref)
        monkeypatch.setattr(os, "replace", _boom)
        CliRunner().invoke(main, ["reflect", "void", "TEST-060", "--reason", "x"])
        assert ref.read_bytes() == before
        _assert_clean(ref.parent, planted)


class TestEmitTreeAtomicWrite:
    def test_unique_temp_and_cleanup(self, tmp_path, monkeypatch):
        from clawpm.emit_tree import _atomic_write

        p = tmp_path / "x.md"
        p.write_text("old")
        planted = _plant(p)
        _atomic_write(p, "new")
        assert p.read_text() == "new"
        _assert_clean(tmp_path, planted)
        monkeypatch.setattr(os, "replace", _boom)
        with pytest.raises(OSError):
            _atomic_write(p, "newer")
        assert p.read_text() == "new"
        _assert_clean(tmp_path, planted)
