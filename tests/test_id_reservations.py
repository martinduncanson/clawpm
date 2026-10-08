"""Portfolio-level task-id reservation ledger (CLAWP-092).

Two worktrees of one project each have their own, git-untracked tasks dir, so
a scan-only next-id allocator hands both the same id. The ledger lives under
the shared portfolio root, outside any checkout, so every allocator sees every
other's reservation. "Two worktrees" is simulated here as two separate tasks
dirs behind one portfolio root (``get_tasks_dir`` is pointed at whichever one
is "checked out"); ids always come from the real allocators, never hand-picked.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from clawpm import tasks as tasks_mod
from clawpm.emit_tree import (
    _predict_parent_id,
    emit_tree,
    parse_emit_document,
)
from clawpm.tasks import add_subtask, add_task

LEDGER_NAME = "id_reservations.jsonl"


@pytest.fixture
def two_trees(isolated_portfolio, monkeypatch):
    """Two empty tasks dirs sharing one portfolio root + a switch between them."""
    base = isolated_portfolio.tasks_dir.parent
    dirs = {}
    for name in ("wt-a", "wt-b"):
        d = base / name / "tasks"
        for sub in ("", "done", "blocked"):
            (d / sub).mkdir(parents=True, exist_ok=True)
        dirs[name] = d
    current = {"dir": dirs["wt-a"]}
    monkeypatch.setattr(tasks_mod, "get_tasks_dir", lambda cfg, pid: current["dir"])

    def use(name: str) -> Path:
        current["dir"] = dirs[name]
        return dirs[name]

    isolated_portfolio.use = use
    isolated_portfolio.dirs = dirs
    isolated_portfolio.ledger = isolated_portfolio.root / LEDGER_NAME
    return isolated_portfolio


def _ordinal(task_id: str) -> int:
    return int(task_id.rsplit("-", 1)[1])


def _doc(root: dict, *leaf_keys: str):
    return parse_emit_document({
        "schema_version": 1,
        "root": root,
        "leaves": [
            {
                "ref": f"L{i}",
                "parent_ref": None,
                "title": f"Leaf {i}",
                "leaf_key": key,
                "success_criteria": [{
                    "criterion": "Tests pass",
                    "gradeable_signal": "pytest exit 0",
                    "comparator": "eq:0",
                }],
                "delegability": "agent",
            }
            for i, key in enumerate(leaf_keys)
        ],
    })


class TestAddTaskAcrossTrees:
    def test_second_tree_gets_next_id(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        first = add_task(tt.config, tt.project_id, "from a")
        tt.use("wt-b")
        second = add_task(tt.config, tt.project_id, "from b")
        assert first.id != second.id
        assert _ordinal(second.id) == _ordinal(first.id) + 1

    def test_interleaved_trees_never_collide(self, two_trees):
        tt = two_trees
        ids = []
        for i in range(6):
            tt.use("wt-a" if i % 2 == 0 else "wt-b")
            ids.append(add_task(tt.config, tt.project_id, f"t{i}").id)
        assert len(set(ids)) == 6, ids
        assert [_ordinal(i) for i in ids] == sorted(_ordinal(i) for i in ids)


class TestAddSubtaskAcrossTrees:
    def test_same_parent_distinct_children(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        parent = add_task(tt.config, tt.project_id, "parent")
        # The parent is "committed": present in both checkouts.
        shutil.copy(parent.file_path, tt.dirs["wt-b"] / parent.file_path.name)
        child_a = add_subtask(tt.config, tt.project_id, parent.id, "child a")
        tt.use("wt-b")
        child_b = add_subtask(tt.config, tt.project_id, parent.id, "child b")
        assert child_a.id != child_b.id
        assert _ordinal(child_b.id) == _ordinal(child_a.id) + 1


class TestEmitTreeAcrossTrees:
    def test_root_ids_distinct_and_predictor_agrees(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        res_a = emit_tree(tt.config, tt.project_id, _doc({"title": "root a"}, "a-1"))
        tt.use("wt-b")
        doc_b = _doc({"title": "root b"}, "b-1")
        predicted = _predict_parent_id(doc_b, tt.config, tt.project_id)
        # Predictor and add_task's allocator must agree on what comes next.
        minted = add_task(tt.config, tt.project_id, "plain task")
        assert minted.id == predicted
        assert predicted != res_a.root_id
        res_b = emit_tree(tt.config, tt.project_id, doc_b)
        assert res_b.root_id not in {res_a.root_id, minted.id}

    def test_attach_children_distinct_across_trees(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        parent = add_task(tt.config, tt.project_id, "parent")
        shutil.copy(parent.file_path, tt.dirs["wt-b"] / parent.file_path.name)
        attach = {"attach_to": parent.id}
        res_a = emit_tree(tt.config, tt.project_id, _doc(attach, "a-1", "a-2"))
        tt.use("wt-b")
        res_b = emit_tree(tt.config, tt.project_id, _doc(attach, "b-1", "b-2"))
        ids_a = {t["id"] for t in res_a.emitted}
        ids_b = {t["id"] for t in res_b.emitted}
        assert len(ids_a) == len(ids_b) == 2
        assert not ids_a & ids_b, (ids_a, ids_b)


class TestExplicitIds:
    def test_explicit_id_recorded_then_skipped(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        seed = add_task(tt.config, tt.project_id, "seed")
        prefix = seed.id.rsplit("-", 1)[0]
        explicit = f"{prefix}-{_ordinal(seed.id) + 7:03d}"
        add_task(tt.config, tt.project_id, "explicit", task_id=explicit)
        tt.use("wt-b")
        auto = add_task(tt.config, tt.project_id, "auto in the other tree")
        assert _ordinal(auto.id) == _ordinal(explicit) + 1

    def test_ledger_records_task_id_and_project(self, two_trees):
        tt = two_trees
        t = add_task(tt.config, tt.project_id, "x")
        text = tt.ledger.read_text(encoding="utf-8")
        assert t.id in text and tt.project_id in text


class TestFailOpen:
    def test_missing_ledger_is_todays_behaviour(self, two_trees):
        tt = two_trees
        assert not tt.ledger.exists()
        ids = [add_task(tt.config, tt.project_id, f"t{i}").id for i in range(3)]
        assert [_ordinal(i) for i in ids] == [0, 1, 2]

    def test_corrupt_line_skipped_with_warning(self, two_trees, caplog):
        tt = two_trees
        seed = add_task(tt.config, tt.project_id, "seed")
        prefix = seed.id.rsplit("-", 1)[0]
        with tt.ledger.open("a", encoding="utf-8") as fh:
            fh.write("{this is not json\n")
            fh.write('["a", "bare", "list"]\n')
            fh.write(
                f'{{"key": "{prefix}", "ordinal": 41, "task_id": "{prefix}-041", '
                f'"project_id": "test", "ts": "2026-10-06T00:00:00Z"}}\n'
            )
        tt.use("wt-b")
        with caplog.at_level(logging.WARNING, logger="clawpm.id_reservations"):
            nxt = add_task(tt.config, tt.project_id, "after corruption")
        assert _ordinal(nxt.id) == 42  # the valid line still counts
        assert any("id_reservations" in r.name for r in caplog.records)

    def test_unreadable_ledger_falls_back_to_scan_with_warning(
        self, two_trees, caplog
    ):
        tt = two_trees
        tt.ledger.mkdir()  # reading a directory raises OSError
        with caplog.at_level(logging.WARNING, logger="clawpm.id_reservations"):
            try:
                t = add_task(tt.config, tt.project_id, "x")
            except OSError:
                pytest.fail("an unreadable ledger must fail open, not abort add_task")
        assert _ordinal(t.id) == 0
        assert any("id_reservations" in r.name for r in caplog.records)


class TestProjectsSharingAPrefix:
    def test_reservation_is_scoped_to_its_project(self, tmp_path):
        from clawpm.id_reservations import record_reservation, reserved_high_water

        record_reservation(tmp_path, "SAME", 4, "SAME-004", "alpha")
        assert reserved_high_water(tmp_path, "SAME", "alpha") == 4
        assert reserved_high_water(tmp_path, "SAME", "beta") is None


class TestCanonicalProjectId:
    """Codex r1 #1: case-insensitive filesystems resolve ``--project TPROJ`` and
    ``--project tproj`` to one project, so reservations must key on the id its
    settings declare, not the caller's spelling."""

    def test_two_spellings_share_one_reservation_scope(self, two_trees):
        from clawpm.discovery import get_project

        tt = two_trees
        meta = tt.root / "projects" / "tproj" / ".project"
        meta.mkdir(parents=True)
        (meta / "settings.toml").write_text(
            'id = "tproj"\nname = "T"\nstatus = "active"\npriority = 3\n'
            'task_prefix = "TPRJ"\n',
            encoding="utf-8",
        )
        if get_project(tt.config, "TPROJ") is None:
            pytest.skip("filesystem is case-sensitive: TPROJ does not resolve")
        tt.use("wt-a")
        first = add_task(tt.config, "TPROJ", "upper spelling")
        tt.use("wt-b")
        second = add_task(tt.config, "tproj", "lower spelling")
        assert first.id != second.id
        assert _ordinal(second.id) == _ordinal(first.id) + 1


class TestNestedEmitConsultsLedger:
    """Codex r1 #2: every parent at every depth applies its own ledger
    high-water mark, not just the emit root."""

    def test_inner_parent_skips_reserved_nested_ordinal(self, two_trees):
        from clawpm.id_reservations import record_task_id

        tt = two_trees
        tt.use("wt-a")
        parent = add_task(tt.config, tt.project_id, "shared parent")
        # Another worktree already minted the first grandchild under the
        # not-yet-existing child <parent>-001.
        record_task_id(tt.root, f"{parent.id}-001-001", tt.project_id)
        crit = [{
            "criterion": "Tests pass",
            "gradeable_signal": "pytest exit 0",
            "comparator": "eq:0",
        }]
        doc = parse_emit_document({
            "schema_version": 1,
            "root": {"attach_to": parent.id},
            "leaves": [
                {"ref": "L0", "parent_ref": None, "title": "inner",
                 "leaf_key": "n-0", "success_criteria": crit,
                 "delegability": "agent"},
                {"ref": "L1", "parent_ref": "L0", "title": "grandchild",
                 "leaf_key": "n-1", "success_criteria": crit,
                 "delegability": "agent"},
            ],
        })
        res = emit_tree(tt.config, tt.project_id, doc)
        ids = {t["id"] for t in res.emitted}
        assert f"{parent.id}-001-001" not in ids, ids
        assert f"{parent.id}-001-002" in ids, ids


class TestCaseFoldedKeys:
    """Codex r2: the repo treats ids case-insensitively (``expand_task_id``
    upper-cases, and Windows filenames collide), so keys must fold case."""

    def test_lowercase_explicit_root_id_reserves_the_uppercase_prefix(self, two_trees):
        tt = two_trees
        tt.use("wt-a")
        seed = add_task(tt.config, tt.project_id, "seed")
        prefix = seed.id.rsplit("-", 1)[0]
        explicit = f"{prefix.lower()}-{_ordinal(seed.id) + 1:03d}"
        add_task(tt.config, tt.project_id, "explicit lower", task_id=explicit)
        tt.use("wt-b")
        auto = add_task(tt.config, tt.project_id, "auto")
        assert auto.id == f"{prefix}-{_ordinal(seed.id) + 2:03d}"

    def test_lowercase_explicit_subtask_id_reserves_the_uppercase_parent(
        self, two_trees
    ):
        tt = two_trees
        tt.use("wt-a")
        parent = add_task(tt.config, tt.project_id, "parent")
        shutil.copy(parent.file_path, tt.dirs["wt-b"] / parent.file_path.name)
        first = add_subtask(tt.config, tt.project_id, parent.id, "child")
        explicit = f"{parent.id.lower()}-{_ordinal(first.id) + 4:03d}"
        add_task(tt.config, tt.project_id, "explicit sub", task_id=explicit)
        tt.use("wt-b")
        nxt = add_subtask(tt.config, tt.project_id, parent.id, "next child")
        assert _ordinal(nxt.id) == _ordinal(explicit) + 1
