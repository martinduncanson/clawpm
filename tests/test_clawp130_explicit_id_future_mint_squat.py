"""CLAWP-130 — an explicit-ID create could squat a prefix that a still-
taskless sibling would later deterministically auto-mint into.

Follow-up from CLAWP-129's review (PR #66, PRE-REVIEW subagent finding #3).
``check_explicit_id_prefix_collision`` originally compared an explicit id
only against each sibling's CURRENT real claim (``resolve_existing_prefix``:
an explicit ``task_prefix`` or the dominant inferred prefix). A still-
taskless sibling has no real claim yet, so its FUTURE first-mint candidate
(what ``assign_all_prefixes`` would deterministically assign it) was
invisible to the check.

Scenario: project A explicitly creates ``--id BRAVO-900`` while nothing
currently claims ``BRAVO``. A still-taskless ``bravo-project`` later runs
its first auto-mint, ``assign_all_prefixes`` deterministically assigns it
``BRAVO`` (nothing has claimed it), and it mints ``BRAVO-000`` — colliding
with A's pre-existing ``BRAVO-900``.

Operator decision (2026-10-01, recorded in ``.project/tasks/CLAWP-130.md``):
option 2 — widen the check to compare against ``assign_all_prefixes``' full
assignment map, which already covers every currently-taskless sibling's
deterministic candidate, not just real claims. Accepted consequence: an
explicit id nobody currently holds can now be refused solely because the
allocator would someday assign its prefix to a different still-taskless
project.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import add_task


def _make_portfolio(tmp_dir: Path, project_id: str, task_prefix: str | None = None) -> None:
    (tmp_dir / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_dir.as_posix()}"\n'
        f'project_roots = ["{(tmp_dir / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    _add_project(tmp_dir, project_id, task_prefix=task_prefix)


def _add_project(tmp_dir: Path, project_id: str, task_prefix: str | None = None) -> None:
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


def _load_isolated_config(tmp_path: Path, monkeypatch):
    """Mirrors test_clawp129's isolation helper — see that file's docstring
    for why a bare env mutation without restore is unsafe here."""
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return load_portfolio_config(tmp_path)


class TestExplicitIdFutureMintSquat:
    def test_explicit_id_refused_when_taskless_sibling_would_later_mint_it(
        self, tmp_path, monkeypatch
    ):
        """The BRAVO-900 / bravo-project scenario from CLAWP-130's writeup:
        a still-taskless sibling's deterministic future candidate must now
        be treated as a collision, not just a sibling's current real claim.
        """
        _make_portfolio(tmp_path, "proj-a", task_prefix="ALPHA")
        # bravo-project is task-less: no task_prefix, no minted tasks.
        # Its naive first-mint candidate (id.upper()[:5], CLAWP-096 strip)
        # is "BRAVO" — nothing else claims it, so assign_all_prefixes would
        # deterministically hand it exactly that.
        _add_project(tmp_path, "bravo-project")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="BRAVO.*bravo-project"):
            add_task(config, "proj-a", "Squats bravo-project's future mint", task_id="BRAVO-900")

        # Nothing was written for the refused create.
        tasks_dir = tmp_path / "projects" / "proj-a" / ".project" / "tasks"
        assert not (tasks_dir / "BRAVO-900.md").exists()

    def test_explicit_id_allowed_when_no_sibling_would_ever_mint_it(self, tmp_path, monkeypatch):
        """Sanity check in the other direction: an explicit id whose prefix
        no CURRENT or FUTURE sibling candidate touches must still succeed —
        the widened check must not over-refuse every unclaimed prefix."""
        _make_portfolio(tmp_path, "proj-a", task_prefix="ALPHA")
        # A sibling whose own naive candidate is unrelated to "ZULU".
        _add_project(tmp_path, "charlie-project")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "proj-a", "Unrelated prefix", task_id="ZULU-001")
        assert task is not None
        assert task.id == "ZULU-001"

    def test_own_first_mint_still_allowed_even_if_unclaimed(self, tmp_path, monkeypatch):
        """CLAWP-116 precedent, re-verified against the widened check per
        CLAWP-130's acceptance criteria: a project's OWN first explicit-ID
        task must still establish its future inferred prefix without being
        refused by its own (self-)entry in `assign_all_prefixes`' map."""
        _make_portfolio(tmp_path, "proj-a")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "proj-a", "First ever task", task_id="FRESH-000")
        assert task is not None
        assert task.id == "FRESH-000"
