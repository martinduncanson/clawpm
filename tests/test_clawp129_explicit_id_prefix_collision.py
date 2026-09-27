"""CLAWP-129 — explicit-ID task creates were never validated against the
portfolio's real prefix claims.

Before this fix, ``add_task(..., task_id="FOO-001")`` only checked whether
``FOO-001`` already existed WITHIN the calling project (the CLAWP-051
clobber guard). It never checked whether ``FOO`` was already another
project's real prefix, so an explicit-ID create could silently establish
(or collide with) a second project's id-space, breaking the "task id is a
portfolio-unique handle" invariant.

Operator policy (2026-09-27): refuse outright on collision, matching the
auto-numbering path's existing behaviour, rather than warn-and-proceed or
auto-suffix.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import add_task


def _make_portfolio(tmp_dir: Path, project_id: str) -> None:
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


def _add_sibling_project(
    tmp_dir: Path, project_id: str, task_prefix: str | None = None
) -> None:
    """Add another project to an ALREADY-created portfolio (call
    ``_make_portfolio`` first — portfolio.toml must exist before this)."""
    meta = tmp_dir / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    prefix_line = f'task_prefix = "{task_prefix}"\n' if task_prefix else ""
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n'
        f"{prefix_line}",
        encoding="utf-8",
    )


class TestExplicitIdPrefixCollision:
    def test_explicit_task_prefix_sibling_refuses_collision(self, tmp_path):
        """Sibling B set an explicit `task_prefix = "SIB"`. Project A's
        explicit-ID create using that same prefix must be refused."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="SIB")
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        with pytest.raises(ValueError, match="SIB.*proj-b"):
            add_task(config, "proj-a", "Colliding task", task_id="SIB-001")

        # Nothing was written for the refused create.
        tasks_dir = tmp_path / "projects" / "proj-a" / ".project" / "tasks"
        assert not (tasks_dir / "SIB-001.md").exists()

    def test_inferred_sibling_prefix_refuses_collision(self, tmp_path):
        """Sibling B has no explicit task_prefix but has already minted a
        real task establishing an INFERRED prefix. Project A's explicit-ID
        create colliding with that inferred prefix must also be refused."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b")
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        # Establish proj-b's inferred prefix via a real (auto-ID) mint.
        seed = add_task(config, "proj-b", "Sibling's own first task", task_id="SIBB-000")
        assert seed is not None

        with pytest.raises(ValueError, match="SIBB.*proj-b"):
            add_task(config, "proj-a", "Colliding task", task_id="SIBB-001")

    def test_explicit_id_matching_own_prefix_is_allowed(self, tmp_path):
        """No false positive: an explicit id whose prefix matches THIS
        project's own already-established prefix must succeed."""
        _make_portfolio(tmp_path, "proj-a")
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        first = add_task(config, "proj-a", "First", task_id="OWN-000")
        assert first is not None

        # Same project, same already-inferred prefix — must not raise.
        second = add_task(config, "proj-a", "Second", task_id="OWN-001")
        assert second is not None
        assert second.id == "OWN-001"

    def test_explicit_id_for_own_first_mint_is_allowed_even_if_unclaimed(self, tmp_path):
        """A project's OWN first explicit-ID task establishes its future
        inferred prefix — it must not be refused just because no OTHER
        project has claimed that prefix yet (there is nothing to collide
        with until a second project actually claims it)."""
        _make_portfolio(tmp_path, "proj-a")
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        task = add_task(config, "proj-a", "First ever task", task_id="FRESH-000")
        assert task is not None
        assert task.id == "FRESH-000"

    def test_malformed_explicit_id_skips_validation(self, tmp_path):
        """An explicit id that doesn't match the PREFIX-NNN shape can't be
        validated against the portfolio at all — it must be let through
        unchanged (not silently rejected as a false positive)."""
        _make_portfolio(tmp_path, "proj-a")
        os.environ["CLAWPM_PORTFOLIO"] = str(tmp_path)
        config = load_portfolio_config(tmp_path)

        task = add_task(config, "proj-a", "Odd id", task_id="not-a-prefix-shape")
        assert task is not None
        assert task.id == "not-a-prefix-shape"
