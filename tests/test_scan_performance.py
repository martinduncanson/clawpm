"""CLAWP-080: scans derive their directory set from the state filter.

`list_tasks(state=open)` and `get_next_task` must not read or YAML-parse files
under the done/ or blocked/ silos — those grow unboundedly with no archival, so
touching them per call is the performance bug this guards against.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.models import Task, TaskState
from clawpm.tasks import list_tasks, get_next_task


@pytest.fixture
def portfolio():
    tmp = Path(tempfile.mkdtemp(prefix="clawpm_scan_"))
    (tmp / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp.as_posix()}"\n'
        f'project_roots = ["{tmp.as_posix()}"]\n'
        "[defaults]\nstatus = \"active\"\n"
    )
    repo = tmp / "repo"
    repo.mkdir()
    meta = repo / ".project"
    meta.mkdir()
    (meta / "settings.toml").write_text(
        'id = "scan"\nname = "Scan"\nstatus = "active"\npriority = 3\n'
        f'repo_path = "{tmp.as_posix()}"\n'
    )
    tasks_dir = meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    def write(loc: Path, tid: str, **fm):
        lines = [f"id: {tid}"]
        for k, v in fm.items():
            lines.append(f"{k}: {v}")
        (loc / f"{tid}.md").write_text(
            "---\n" + "\n".join(lines) + f"\n---\n# {tid}\n", encoding="utf-8"
        )

    write(tasks_dir, "OPEN-1", priority=1)
    write(tasks_dir, "OPEN-2", priority=2, depends="[DONE-1]")
    write(tasks_dir, "OPEN-3", priority=3, depends="[OPEN-1]")
    write(tasks_dir / "done", "DONE-1", priority=5)
    write(tasks_dir / "done", "DONE-2", priority=5)
    write(tasks_dir / "blocked", "BLOCKED-1", priority=5)

    old = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(tmp)
    config = load_portfolio_config(tmp)
    yield {"config": config, "tasks_dir": tasks_dir}
    if old:
        os.environ["CLAWPM_PORTFOLIO"] = old
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(tmp, ignore_errors=True)


def _record_from_file(monkeypatch):
    """Patch Task.from_file to record every path it parses."""
    seen: list[Path] = []
    original = Task.from_file.__func__

    def spy(cls, path):
        seen.append(Path(path))
        return original(cls, path)

    monkeypatch.setattr(Task, "from_file", classmethod(spy))
    return seen


def _touched_silo(seen, silo: str) -> bool:
    return any(silo in p.parts for p in seen)


class TestScanLocationSkip:
    def test_list_open_skips_done_and_blocked(self, portfolio, monkeypatch):
        seen = _record_from_file(monkeypatch)
        tasks = list_tasks(portfolio["config"], "scan", state_filter=TaskState.OPEN)
        assert {t.id for t in tasks} == {"OPEN-1", "OPEN-2", "OPEN-3"}
        assert not _touched_silo(seen, "done")
        assert not _touched_silo(seen, "blocked")

    def test_get_next_task_skips_done_and_blocked_parse(self, portfolio, monkeypatch):
        seen = _record_from_file(monkeypatch)
        nxt = get_next_task(portfolio["config"], "scan")
        assert nxt is not None and nxt.id == "OPEN-1"
        assert not _touched_silo(seen, "done")
        assert not _touched_silo(seen, "blocked")

    def test_list_done_filter_scans_only_done(self, portfolio, monkeypatch):
        seen = _record_from_file(monkeypatch)
        tasks = list_tasks(portfolio["config"], "scan", state_filter=TaskState.DONE)
        assert {t.id for t in tasks} == {"DONE-1", "DONE-2"}
        assert not _touched_silo(seen, "blocked")

    def test_list_blocked_filter_scans_only_blocked(self, portfolio, monkeypatch):
        seen = _record_from_file(monkeypatch)
        tasks = list_tasks(portfolio["config"], "scan", state_filter=TaskState.BLOCKED)
        assert {t.id for t in tasks} == {"BLOCKED-1"}
        assert not _touched_silo(seen, "done")

    def test_no_filter_still_sees_all_states(self, portfolio):
        tasks = list_tasks(portfolio["config"], "scan")
        by_state = {t.state for t in tasks}
        assert TaskState.OPEN in by_state
        assert TaskState.DONE in by_state
        assert TaskState.BLOCKED in by_state


class TestGetNextTaskDependencies:
    def test_dependency_on_done_task_is_satisfied(self, portfolio):
        # OPEN-2 depends on DONE-1; get_next skips OPEN-1 only if it were
        # unavailable — OPEN-1 has no deps so it wins by priority. Remove it to
        # prove OPEN-2's done-dependency resolves via the cheap done-id set.
        (portfolio["tasks_dir"] / "OPEN-1.md").unlink()
        (portfolio["tasks_dir"] / "OPEN-3.md").unlink()
        nxt = get_next_task(portfolio["config"], "scan")
        assert nxt is not None and nxt.id == "OPEN-2"

    def test_next_dir_task_has_children_linked(self, portfolio):
        # A directory-task parent with an open child, returned as next, must
        # carry .children like list_tasks would (parent-linking parity).
        tasks_dir = portfolio["tasks_dir"]
        for f in ("OPEN-1.md", "OPEN-2.md", "OPEN-3.md"):
            (tasks_dir / f).unlink()
        parent = tasks_dir / "PARENT-1"
        parent.mkdir()
        (parent / "_task.md").write_text(
            "---\nid: PARENT-1\npriority: 1\n---\n# parent\n", encoding="utf-8"
        )
        (parent / "PARENT-1-001.md").write_text(
            "---\nid: PARENT-1-001\nparent: PARENT-1\npriority: 2\n---\n# child\n",
            encoding="utf-8",
        )
        nxt = get_next_task(portfolio["config"], "scan")
        assert nxt is not None and nxt.id == "PARENT-1"
        assert "PARENT-1-001" in nxt.children

    def test_dependency_on_open_task_blocks(self, portfolio):
        # OPEN-3 depends on OPEN-1 (still open) → must be skipped. Remove the
        # higher-priority frees so OPEN-3 would be next iff its dep resolved.
        (portfolio["tasks_dir"] / "OPEN-1.md").unlink()
        (portfolio["tasks_dir"] / "OPEN-2.md").unlink()
        nxt = get_next_task(portfolio["config"], "scan")
        # OPEN-1 is gone (its dep) so OPEN-3 stays blocked → no next task.
        assert nxt is None
