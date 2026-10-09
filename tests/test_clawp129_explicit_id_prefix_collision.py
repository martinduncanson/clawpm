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

Review-round-2 additions (PR #66 — grok-4.6, grok-4.5, PRE-REVIEW subagent,
all independently caught): a subtask-shaped explicit id (``SIB-001-002``)
and a lowercase explicit id (``sib-001``) each bypassed the guard entirely
in round 1, and an unreadable sibling directory was silently treated as
"not a collision" instead of failing closed like ``assign_all_prefixes``
does for the identical failure. Tests below cover all three.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import PortfolioPrefixScanError, add_task


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


def _load_isolated_config(tmp_path: Path, monkeypatch):
    """Point discovery at ``tmp_path`` only — auto-restored on teardown.

    Mirrors ``conftest.py``'s ``isolated_portfolio`` fixture's env handling
    (``monkeypatch.setenv`` + the two ``delenv`` calls), which that fixture's
    own docstring explains exists specifically because a bare
    ``os.environ["CLAWPM_PORTFOLIO"] = ...`` with no restore leaks into
    later tests, and an inherited ``CLAWPM_PROJECT_ROOTS``/``CLAWPM_WORKSPACE``
    would fold the developer's REAL portfolio into these multi-project
    collision checks. Not reusing the fixture directly because it seeds
    exactly one project; these tests need to seed several themselves.
    """
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return load_portfolio_config(tmp_path)


class TestExplicitIdPrefixCollision:
    def test_explicit_task_prefix_sibling_refuses_collision(self, tmp_path, monkeypatch):
        """Sibling B set an explicit `task_prefix = "SIB"`. Project A's
        explicit-ID create using that same prefix must be refused."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="SIB")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="SIB.*proj-b"):
            add_task(config, "proj-a", "Colliding task", task_id="SIB-001")

        # Nothing was written for the refused create.
        tasks_dir = tmp_path / "projects" / "proj-a" / ".project" / "tasks"
        assert not (tasks_dir / "SIB-001.md").exists()

    def test_inferred_sibling_prefix_refuses_collision(self, tmp_path, monkeypatch):
        """Sibling B has no explicit task_prefix but has already minted a
        real task establishing an INFERRED prefix. Project A's explicit-ID
        create colliding with that inferred prefix must also be refused."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        # Establish proj-b's inferred prefix via a real (auto-ID) mint.
        seed = add_task(config, "proj-b", "Sibling's own first task", task_id="SIBB-000")
        assert seed is not None

        with pytest.raises(ValueError, match="SIBB.*proj-b"):
            add_task(config, "proj-a", "Colliding task", task_id="SIBB-001")

    def test_explicit_id_matching_own_prefix_is_allowed(self, tmp_path, monkeypatch):
        """No false positive: an explicit id whose prefix matches THIS
        project's own already-established prefix must succeed."""
        _make_portfolio(tmp_path, "proj-a")
        config = _load_isolated_config(tmp_path, monkeypatch)

        first = add_task(config, "proj-a", "First", task_id="OWN-000")
        assert first is not None

        # Same project, same already-inferred prefix — must not raise.
        second = add_task(config, "proj-a", "Second", task_id="OWN-001")
        assert second is not None
        assert second.id == "OWN-001"

    def test_explicit_id_for_own_first_mint_is_allowed_even_if_unclaimed(
        self, tmp_path, monkeypatch
    ):
        """A project's OWN first explicit-ID task establishes its future
        inferred prefix — it must not be refused just because no OTHER
        project has claimed that prefix yet (there is nothing to collide
        with until a second project actually claims it)."""
        _make_portfolio(tmp_path, "proj-a")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "proj-a", "First ever task", task_id="FRESH-000")
        assert task is not None
        assert task.id == "FRESH-000"

    def test_malformed_explicit_id_skips_validation(self, tmp_path, monkeypatch):
        """An explicit id that doesn't match the PREFIX-NNN shape can't be
        validated against the portfolio at all — it must be let through
        unchanged (not silently rejected as a false positive)."""
        _make_portfolio(tmp_path, "proj-a")
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "proj-a", "Odd id", task_id="not-a-prefix-shape")
        assert task is not None
        assert task.id == "not-a-prefix-shape"

    def test_subtask_shaped_explicit_id_still_refuses_collision(self, tmp_path, monkeypatch):
        """A subtask-shaped explicit id (PREFIX-NNN-MMM) derives its prefix
        from the TOP-LEVEL segment, not the parent-task-shaped `PREFIX-NNN`
        substring -- round-1 review (grok-4.6, PRE-REVIEW) caught this
        deriving "SIB-001" (a parent id, matching no real project prefix)
        instead of "SIB", silently letting a subtask-shaped id squat a
        sibling's real prefix."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="SIB")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="SIB.*proj-b"):
            add_task(config, "proj-a", "Subtask-shaped squat", task_id="SIB-001-002")

    def test_lowercase_explicit_id_still_refuses_collision(self, tmp_path, monkeypatch):
        """A lowercase explicit id must be matched case-insensitively --
        round-1 review (grok-4.6, PRE-REVIEW) caught `_PREFIX_NUM_RE`'s
        `[A-Z]` anchor silently skipping validation for `--id sib-001` even
        though `expand_task_id` upper-cases every REFERENCE to that same
        id elsewhere, so `sib-001` and a sibling's real `SIB-001` resolve
        to the same handle."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="SIB")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="SIB.*proj-b"):
            add_task(config, "proj-a", "Lowercase squat", task_id="sib-001")

    def test_sibling_prefix_ending_in_digit_still_refuses_collision(self, tmp_path, monkeypatch):
        """A real `task_prefix` can itself end in a digit segment (e.g.
        "TEAM-2") -- round-2 review (grok-4.6, Codex) caught the recursive
        peel over-stripping past this to "TEAM", missing the sibling's
        actual claim. Checking the WHOLE peel chain (not just the fully-
        peeled value) must still catch it."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="TEAM-2")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="TEAM-2.*proj-b"):
            add_task(config, "proj-a", "Digit-suffixed prefix squat", task_id="TEAM-2-001")

    def test_own_digit_suffixed_prefix_not_false_refused(self, tmp_path, monkeypatch):
        """The other direction of the same bug: THIS project's own real
        prefix ending in a digit ("TEAM-2") must not be false-refused just
        because an unrelated sibling happens to own the OVER-stripped
        remainder ("TEAM")."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="TEAM")
        config = _load_isolated_config(tmp_path, monkeypatch)

        # proj-a's own explicit prefix is "TEAM-2" -- distinct from proj-b's "TEAM".
        settings_path = tmp_path / "projects" / "proj-a" / ".project" / "settings.toml"
        settings_path.write_text(
            'id = "proj-a"\nname = "proj-a"\nstatus = "active"\npriority = 3\n'
            'task_prefix = "TEAM-2"\n',
            encoding="utf-8",
        )
        config = _load_isolated_config(tmp_path, monkeypatch)

        task = add_task(config, "proj-a", "Own digit-suffixed prefix", task_id="TEAM-2-001")
        assert task is not None
        assert task.id == "TEAM-2-001"

    def test_own_prefix_as_peel_ancestor_still_refuses_more_specific_sibling(
        self, tmp_path, monkeypatch
    ):
        """round-3 review (grok-4.5) catch: the own-prefix short-circuit
        must only fire on the MOST SPECIFIC candidate. This project's own
        prefix is "TEAM" (a peeled ANCESTOR of the id's chain); sibling B's
        real, MORE SPECIFIC claim is "TEAM-2". Matching "TEAM" anywhere in
        the chain must not skip checking "TEAM-2" against proj-b first."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="TEAM-2")
        config = _load_isolated_config(tmp_path, monkeypatch)

        # Establish proj-a's own prefix as the plain "TEAM" (auto-mint).
        seed = add_task(config, "proj-a", "proj-a's own first task", task_id="TEAM-000")
        assert seed is not None

        with pytest.raises(ValueError, match="TEAM-2.*proj-b"):
            add_task(config, "proj-a", "Squats proj-b's more specific claim", task_id="TEAM-2-001")

    def test_prefix_with_underscore_still_refuses_collision(self, tmp_path, monkeypatch):
        """A real `task_prefix` isn't restricted to the `_PREFIX_NUM_RE`
        character set ([A-Z0-9-]) -- `ProjectSettings`/`assign_task_prefix`
        accept and mint any string verbatim (round-3 Codex catch:
        `task_prefix = "OPS_TEAM"`, an underscore). The check must compare
        directly against the real prefix STRING, not a regex-derived one,
        so a namespace outside that character set still gets caught."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="OPS_TEAM")
        config = _load_isolated_config(tmp_path, monkeypatch)

        with pytest.raises(ValueError, match="OPS_TEAM.*proj-b"):
            add_task(config, "proj-a", "Underscore-prefix squat", task_id="OPS_TEAM-001")

    def test_unreadable_sibling_fails_closed(self, tmp_path, monkeypatch):
        """An unreadable/locked sibling directory must FAIL CLOSED (raise
        PortfolioPrefixScanError), not be silently treated as "no
        collision" -- round-1 review (grok-4.6, grok-4.5, a history-lens
        pass) caught this behaving backwards from `assign_all_prefixes`'
        own contract for the identical failure mode."""
        _make_portfolio(tmp_path, "proj-a")
        _add_sibling_project(tmp_path, "proj-b", task_prefix="SIB")
        config = _load_isolated_config(tmp_path, monkeypatch)

        import clawpm.tasks as tasks_module

        real_resolve = tasks_module.resolve_existing_prefix

        def _flaky_resolve(settings):
            if settings.id == "proj-b":
                raise OSError("simulated: sibling directory unreadable")
            return real_resolve(settings)

        monkeypatch.setattr(tasks_module, "resolve_existing_prefix", _flaky_resolve)

        with pytest.raises(PortfolioPrefixScanError, match="proj-b"):
            add_task(config, "proj-a", "During sibling outage", task_id="XYZ-001")
