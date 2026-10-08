"""CLAWP-133: a digit-leading project id must get a task prefix that
``_infer_prefix_from_tasks`` can read back from the project's own files.

Before the fix ``_PREFIX_NUM_RE`` required a leading letter, so the prefix the
allocator derived for ``2-b`` / ``2024`` / ``9-9`` (itself digit-leading) was
never re-inferable. The project looked taskless on every mint and was
re-resolved from scratch each time.

These tests drive the system's own auto-minted ``PREFIX-NNN`` id shape through
``add_task`` (not hand-picked ids): first mint, inference, second mint.

Fix: derived candidates always start with a letter (``_ensure_leading_letter``),
and ``_PREFIX_NUM_RE`` accepts a leading digit so projects that ALREADY minted
digit-leading ids keep their prefix (compat) instead of being re-derived.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawpm.discovery import load_portfolio_config
from clawpm.tasks import (
    _infer_prefix_from_tasks,
    _naive_prefix_candidates,
    _naive_prefix_placeholder,
    add_task,
    assign_all_prefixes,
)


def _make_portfolio(tmp_dir: Path, project_id: str) -> None:
    (tmp_dir / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_dir.as_posix()}"\n'
        f'project_roots = ["{(tmp_dir / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    _add_project(tmp_dir, project_id)


def _add_project(tmp_dir: Path, project_id: str) -> None:
    meta = tmp_dir / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )


def _tasks_dir(tmp_path: Path, project_id: str) -> Path:
    return tmp_path / "projects" / project_id / ".project" / "tasks"


def _load_isolated_config(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return load_portfolio_config(tmp_path)


DIGIT_LEADING_IDS = ["2-b", "2024", "9-9", "3d-print", "7"]


class TestDigitLeadingProjectPrefixIsStable:
    @pytest.mark.parametrize("project_id", DIGIT_LEADING_IDS)
    def test_mint_infer_mint_roundtrip(self, tmp_path, monkeypatch, project_id):
        _make_portfolio(tmp_path, project_id)
        config = _load_isolated_config(tmp_path, monkeypatch)

        first = add_task(config, project_id, "first")
        prefix, num = first.id.rsplit("-", 1)
        assert num == "000", first.id
        assert prefix[0].isalpha(), f"derived prefix must lead with a letter: {first.id}"

        inferred = _infer_prefix_from_tasks(_tasks_dir(tmp_path, project_id))
        assert inferred == prefix, (
            f"prefix {prefix!r} must be re-inferable from its own file, got {inferred!r}"
        )

        second = add_task(config, project_id, "second")
        assert second.id == f"{prefix}-001", (first.id, second.id)

    def test_two_digit_leading_siblings_get_distinct_stable_prefixes(
        self, tmp_path, monkeypatch
    ):
        _make_portfolio(tmp_path, "2024")
        _add_project(tmp_path, "2024-b")
        config = _load_isolated_config(tmp_path, monkeypatch)

        a1 = add_task(config, "2024", "a1")
        b1 = add_task(config, "2024-b", "b1")
        a2 = add_task(config, "2024", "a2")
        b2 = add_task(config, "2024-b", "b2")

        assert a1.id.rsplit("-", 1)[0] != b1.id.rsplit("-", 1)[0]
        assert a2.id.rsplit("-", 1)[0] == a1.id.rsplit("-", 1)[0]
        assert b2.id.rsplit("-", 1)[0] == b1.id.rsplit("-", 1)[0]
        assignments, errors = assign_all_prefixes(config)
        assert errors == {}
        assert len(set(assignments.values())) == len(assignments)

    @pytest.mark.parametrize("project_id", DIGIT_LEADING_IDS)
    def test_every_candidate_leads_with_a_letter_and_placeholder_matches(
        self, project_id
    ):
        candidates = list(_naive_prefix_candidates(project_id))
        assert all(c[0].isalpha() for c in candidates), candidates
        assert _naive_prefix_placeholder(project_id) == candidates[0]

    def test_letter_leading_ids_are_unchanged(self):
        assert _naive_prefix_placeholder("clawpm") == "CLAWP"
        assert _naive_prefix_placeholder("code-quorum") == "CODE"
        assert _naive_prefix_placeholder("web-2") == "WEB2"


class TestExistingDigitLeadingProjectsKeepTheirPrefix:
    """Compat: a project that already minted digit-leading ids (the pre-fix
    behaviour) must keep minting under that prefix, not jump to a new one."""

    @pytest.mark.parametrize(
        "project_id, existing_stem, expected_prefix",
        [("2024", "2024-000", "2024"), ("2-b", "2-B-004", "2-B"), ("9-9", "99-001", "99")],
    )
    def test_existing_ids_are_inferred_and_continued(
        self, tmp_path, monkeypatch, project_id, existing_stem, expected_prefix
    ):
        _make_portfolio(tmp_path, project_id)
        tasks_dir = _tasks_dir(tmp_path, project_id)
        (tasks_dir / f"{existing_stem}.md").write_text(
            f"---\nid: {existing_stem}\ncreated: '2026-01-01'\nupdated: '2026-01-01'\n---\n# old\n",
            encoding="utf-8",
        )
        assert _infer_prefix_from_tasks(tasks_dir) == expected_prefix

        config = _load_isolated_config(tmp_path, monkeypatch)
        nxt = add_task(config, project_id, "next")
        ordinal = int(existing_stem.rsplit("-", 1)[1]) + 1
        assert nxt.id == f"{expected_prefix}-{ordinal:03d}", nxt.id

    def test_subtask_shaped_digit_leading_name_is_still_not_a_prefix(self, tmp_path):
        """The CLAWP-048 stray-subtask filter still applies: ``2024-01-15`` is
        subtask-shaped (prefix ``2024-01``), not evidence of a real prefix."""
        tasks_dir = tmp_path / "tasks"
        tasks_dir.mkdir()
        (tasks_dir / "2024-01-15.md").write_text("x", encoding="utf-8")
        assert _infer_prefix_from_tasks(tasks_dir) is None
