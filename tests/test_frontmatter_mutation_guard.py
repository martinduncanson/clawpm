"""Regression tests for CLAWP-091 — non-dict frontmatter at mutation sites.

A hand-edited task/mission file can end up with YAML frontmatter that parses
to something other than a mapping (a bare scalar or a list). Before this fix,
mutation sites like ``edit_task`` assumed a ``dict`` and raised a raw,
unfriendly ``TypeError`` (e.g. ``list indices must be integers or slices, not
str``) the first time they did ``frontmatter[key] = value``. These tests pin
that every genuine mutation site now raises a clear ``FrontmatterError``
(a ``ValueError`` subclass) instead, naming the file/task, and never a bare
``TypeError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clawpm.doctor_apply import _rewrite_frontmatter_state
from clawpm.frontmatter import FrontmatterError
from clawpm.models import TaskComplexity, TaskState
from clawpm.tasks import _write_rejection_frontmatter, add_task, change_task_state, edit_task


def _write_list_frontmatter(path: Path, task_id: str = "TEST-001") -> None:
    """A task file whose frontmatter block is a bare YAML list, not a mapping."""
    path.write_text(
        f"---\n- {task_id}\n- not a mapping\n---\n# Some title\n\nbody\n",
        encoding="utf-8",
    )


def _write_scalar_frontmatter(path: Path) -> None:
    """A task file whose frontmatter block is a bare YAML scalar string."""
    path.write_text(
        "---\njust a string, not a mapping\n---\n# Some title\n\nbody\n",
        encoding="utf-8",
    )


class TestEditTaskRejectsNonMapping:
    """edit_task unwraps FrontmatterError into a task-specific ValueError,
    matching its existing "unterminated"/"unparseable" branches — so the
    friendly error here is a plain ValueError, not FrontmatterError itself.
    Either way, the key regression this pins is what it is NOT: a raw
    TypeError from `frontmatter[key] = value` on a list/str.
    """

    def test_list_frontmatter_raises_friendly_error_not_typeerror(self, isolated_portfolio):
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Editable")
        _write_list_frontmatter(task.file_path, task.id)

        with pytest.raises(ValueError) as exc:
            edit_task(cfg, isolated_portfolio.project_id, task.id, priority=9)

        assert not isinstance(exc.value, TypeError)
        assert task.id in str(exc.value)
        assert "mapping" in str(exc.value)

    def test_scalar_frontmatter_raises_friendly_error_not_typeerror(self, isolated_portfolio):
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Editable2")
        _write_scalar_frontmatter(task.file_path)

        with pytest.raises(ValueError) as exc:
            edit_task(cfg, isolated_portfolio.project_id, task.id, complexity=TaskComplexity.M)

        assert not isinstance(exc.value, TypeError)
        assert "mapping" in str(exc.value)

    def test_file_is_untouched_after_refused_edit(self, isolated_portfolio):
        """Refusing the edit must not clobber or partially rewrite the file."""
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Untouched")
        _write_list_frontmatter(task.file_path, task.id)
        original = task.file_path.read_text(encoding="utf-8")

        with pytest.raises(ValueError):
            edit_task(cfg, isolated_portfolio.project_id, task.id, priority=1)

        assert task.file_path.read_text(encoding="utf-8") == original


class TestRejectionRejectsNonMapping:
    def test_reject_with_list_frontmatter_raises_friendly_error(self, isolated_portfolio):
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Rejectable")
        _write_list_frontmatter(task.file_path, task.id)

        with pytest.raises(FrontmatterError) as exc:
            change_task_state(
                cfg,
                isolated_portfolio.project_id,
                task.id,
                TaskState.REJECTED,
                rationale="not worth it",
            )

        assert exc.value.reason == "not_a_mapping"
        assert str(task.file_path) in str(exc.value)

    def test_write_rejection_frontmatter_unit_level(self, tmp_path):
        """Direct unit test of the helper (bypasses the task-tree lookup)."""
        f = tmp_path / "TEST-002.md"
        _write_list_frontmatter(f, "TEST-002")

        with pytest.raises(FrontmatterError) as exc:
            _write_rejection_frontmatter(f, "not worth it", None)

        assert exc.value.reason == "not_a_mapping"
        assert str(f) in str(exc.value)


class TestDoctorApplyRejectsNonMapping:
    def test_rewrite_frontmatter_state_raises_friendly_error(self, tmp_path):
        """_rewrite_frontmatter_state unwraps FrontmatterError into a
        file-naming ValueError, matching its sibling "unterminated" branch
        (added after PRE-REVIEW flagged the original bare `else: raise`
        as losing file identity for doctor, which walks many files)."""
        f = tmp_path / "TEST-003.md"
        _write_list_frontmatter(f, "TEST-003")

        with pytest.raises(ValueError) as exc:
            _rewrite_frontmatter_state(f, "done")

        assert not isinstance(exc.value, TypeError)
        assert str(f) in str(exc.value)


class TestStampUpdatedRejectsNonMapping:
    """PRE-REVIEW finding: _set_updated_line/_stamp_updated_file bypass the
    frontmatter.py helpers entirely (a surgical text-splice, not a YAML
    round-trip) and were never guarded — splicing an `updated:` line into a
    list/scalar frontmatter block downgrades it from cleanly-recoverable
    (not_a_mapping) into genuinely UNPARSEABLE YAML. change_task_state(...,
    DONE/PROGRESS/BLOCKED) reaches this path and must refuse to corrupt the
    file, the same way it already refuses for an unterminated fence."""

    def test_state_change_to_done_does_not_corrupt_list_frontmatter(self, isolated_portfolio):
        """change_task_state still moves the file (Task.from_file stays
        lenient — see TestReadPathsStayLenientByDesign — so get_task()
        resolves it fine), but the `updated:` stamp step must refuse to
        splice into non-mapping frontmatter rather than corrupt it into
        unparseable YAML. The list frontmatter survives, byte-for-byte,
        at the new (moved) location."""
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Stampable")
        _write_list_frontmatter(task.file_path, task.id)
        original = task.file_path.read_text(encoding="utf-8")

        change_task_state(cfg, isolated_portfolio.project_id, task.id, TaskState.DONE)

        moved = isolated_portfolio.tasks_dir / "done" / f"{task.id}.md"
        assert moved.exists()
        assert moved.read_text(encoding="utf-8") == original
        # And it must still be exactly the list it started as — not corrupted
        # into unparseable YAML by a blind `updated:` splice.
        assert yaml.safe_load(moved.read_text(encoding="utf-8").split("---", 2)[1]) == [
            task.id,
            "not a mapping",
        ]

    def test_set_updated_line_refuses_list_frontmatter(self):
        """Direct unit test of the text-splice helper in isolation."""
        from clawpm.tasks import _set_updated_line

        text = "---\n- a\n- b\n---\n# Title\n"
        result = _set_updated_line(text, "2026-09-02")
        assert result is None  # refused, matching the unterminated-fence contract
        # And the refusal must not have mutated valid YAML into invalid YAML.
        assert yaml.safe_load(text.split("---", 2)[1]) == ["a", "b"]

    def test_set_updated_line_still_stamps_a_real_mapping(self):
        from clawpm.tasks import _set_updated_line

        text = "---\nid: X\n---\n# Title\n"
        result = _set_updated_line(text, "2026-09-02")
        assert result is not None
        assert "updated: '2026-09-02'" in result


class TestEmitTreeStampPrdRefRejectsNonMapping:
    """PRE-REVIEW finding: emit_tree._stamp_prd_ref has NO surrounding
    try/except at its call site — before CLAWP-091, non-mapping frontmatter
    hit `fm.get("prd_ref")` and raised an uncaught AttributeError there (a
    loud crash during tree emission). Swallowing "not_a_mapping" the same
    way as the other three (benign) reasons would turn that into a silent,
    unnoticed skip — so it must still fail loudly, just with a friendly
    message instead of a raw AttributeError."""

    def test_stamp_prd_ref_raises_on_list_frontmatter(self, tmp_path):
        from clawpm.emit_tree import _stamp_prd_ref

        f = tmp_path / "PARENT.md"
        _write_list_frontmatter(f, "PARENT")

        with pytest.raises(ValueError) as exc:
            _stamp_prd_ref(f, "RESEARCH-001")

        assert not isinstance(exc.value, TypeError)
        assert str(f) in str(exc.value)


class TestReadPathsStayLenientByDesign:
    """Task.from_file / Research.from_file / Mission.from_file deliberately
    keep the pre-existing lenient fallback (any malformation -> {}, matching
    pre-CLAWP-079 behaviour) for CLAWP-091's "not_a_mapping" reason too, even
    though it's a stronger corruption signal than the other three reasons.

    An earlier version of this fix made them re-raise instead. That broke
    edit_task/change_task_state: both resolve the file via get_task(), which
    wraps Task.from_file in a broad `except Exception: continue` and reports
    "not found" on ANY exception — so the re-raise never reached the caller,
    it just changed get_task()'s result from "a sparse Task" to "None",
    and edit_task never got as far as its OWN re-parse (the thing that
    actually raises the friendly, file-naming error this task exists to
    add). These tests pin the chosen behaviour so it isn't "fixed" back into
    that regression: the read path stays lenient, and the MUTATION site
    (edit_task, _write_rejection_frontmatter, etc.) is where the guard is
    enforced with a friendly error — see the classes above."""

    def test_task_from_file_defaults_rather_than_raises(self, tmp_path):
        from clawpm.models import Task

        f = tmp_path / "TEST-004.md"
        _write_list_frontmatter(f, "TEST-004")

        task = Task.from_file(f)  # must not raise
        assert task.id == "TEST-004"  # falls back to the filename stem

    def test_get_task_still_finds_the_file_not_a_mapping_or_not(self, isolated_portfolio):
        """get_task() on a non-mapping-frontmatter file returns a (sparse)
        Task, not None — which is exactly what lets edit_task reach its own
        re-parse and raise the friendly error, instead of reporting a file
        that plainly exists on disk as "task not found"."""
        cfg = isolated_portfolio.config
        task = add_task(cfg, isolated_portfolio.project_id, "Ghost")
        _write_list_frontmatter(task.file_path, task.id)

        from clawpm.tasks import get_task
        found = get_task(cfg, isolated_portfolio.project_id, task.id)
        assert found is not None
        assert found.file_path == task.file_path

    def test_research_from_file_defaults_rather_than_raises(self, tmp_path):
        from clawpm.models import Research

        f = tmp_path / "research-001.md"
        _write_list_frontmatter(f, "research-001")

        item = Research.from_file(f)  # must not raise
        assert item.id == "research-001"

    def test_mission_from_file_defaults_rather_than_raises(self, tmp_path):
        from clawpm.mission import Mission

        f = tmp_path / "MISSION-001.md"
        _write_list_frontmatter(f, "MISSION-001")

        mission = Mission.from_file(f)  # must not raise
        assert mission.id == "MISSION-001"
