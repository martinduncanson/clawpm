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
        f = tmp_path / "TEST-003.md"
        _write_list_frontmatter(f, "TEST-003")

        with pytest.raises(FrontmatterError) as exc:
            _rewrite_frontmatter_state(f, "done")

        assert exc.value.reason == "not_a_mapping"
