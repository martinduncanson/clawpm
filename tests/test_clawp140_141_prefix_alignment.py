"""CLAWP-140 / CLAWP-141: ``context.expand_task_id`` and
``context.get_project_prefix`` must agree with the task-id allocator.

Follow-ups from the Codex review of PR #89 (CLAWP-133). Ids here are built from
the allocator's own output (``_naive_prefix_placeholder`` / ``add_task``), not
hand-picked, so the grammar tested is the grammar actually minted:
``PREFIX-NNN`` roots, ``PREFIX-NNN-NNN`` subtasks, multi-hyphen prefixes
(``MY-PR``), letter-suffixed digit-leading derivations (``P2-B``), and
pre-CLAWP-133 digit-leading prefixes that already exist on disk.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clawpm import context
from clawpm.discovery import load_portfolio_config
from clawpm.tasks import _naive_prefix_placeholder, add_task

PROJECT_IDS = [
    "clawpm",
    "my-project",
    "my_project",
    "code-quorum",
    "2-b",
    "2024",
    "team-2-b",
    "arb-prd",
    "ab",
    "a-b-c",
    "web-2",
]


class TestGetProjectPrefixMatchesAllocator:
    @pytest.mark.parametrize("project_id", PROJECT_IDS)
    def test_equals_allocator_base_candidate(self, project_id):
        assert context.get_project_prefix(project_id) == _naive_prefix_placeholder(project_id)

    def test_known_shapes(self):
        # Allocator, not a naive [:5] of the separator-stripped id.
        assert context.get_project_prefix("code-quorum") == "CODE"
        assert context.get_project_prefix("my-project") == "MY-PR"
        assert context.get_project_prefix("2-b") == "P2-B"
        assert context.get_project_prefix("web-2") == "WEB2"

    def test_taskless_digit_leading_project_gets_letter_leading_prefix(self):
        assert context.get_project_prefix("2024")[0].isalpha()


class TestExpandFullIdsAreNotReread:
    @pytest.mark.parametrize("project_id", PROJECT_IDS)
    def test_minted_root_and_subtask_ids_are_stable(self, project_id):
        prefix = context.get_project_prefix(project_id)
        root = f"{prefix}-007"
        sub = f"{prefix}-007-002"
        assert context.expand_task_id(root, project_id) == root
        assert context.expand_task_id(sub, project_id) == sub
        assert context.expand_task_id(root.lower(), project_id) == root

    @pytest.mark.parametrize("project_id", PROJECT_IDS)
    def test_short_refs_expand_under_allocator_prefix(self, project_id):
        prefix = context.get_project_prefix(project_id)
        assert context.expand_task_id("7", project_id) == f"{prefix}-007"
        assert context.expand_task_id("7-2", project_id) == f"{prefix}-007-002"

    def test_multi_hyphen_prefix_full_id_with_other_project(self):
        # Full id of a DIFFERENT prefix passes through, even with a hyphen in it.
        assert context.expand_task_id("P2-B-001", "clawpm") == "P2-B-001"
        assert context.expand_task_id("ARB-P-000-001", "clawpm") == "ARB-P-000-001"

    def test_explicit_digit_leading_prefix_full_id_wins_over_subtask_grammar(self):
        # "2024-001" is a full id under prefix "2024", not subtask 001 of task 2024.
        assert context.expand_task_id("2024-001", "2024", "2024") == "2024-001"
        assert context.expand_task_id("2024-001-002", "2024", "2024") == "2024-001-002"

    def test_short_subtask_ref_still_expands_when_prefix_differs(self):
        assert context.expand_task_id("4-001", "clawpm", "SAME") == "SAME-004-001"

    def test_bare_digit_leading_ref_without_prefix_is_still_a_short_subtask(self):
        assert context.expand_task_id("4-001", "clawpm") == "CLAWP-004-001"


def _portfolio(tmp_path: Path, monkeypatch, project_id: str):
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    meta = tmp_path / "projects" / project_id / ".project"
    tasks_dir = meta / "tasks"
    (tasks_dir / "done").mkdir(parents=True)
    (tasks_dir / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        f'id = "{project_id}"\nname = "{project_id}"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))
    return load_portfolio_config(tmp_path), tasks_dir


class TestLegacyDigitLeadingProjectsKeepWorking:
    def test_prefix_minted_before_clawp133_is_preserved(self, tmp_path, monkeypatch):
        _config, tasks_dir = _portfolio(tmp_path, monkeypatch, "2024")
        (tasks_dir / "2024-000.md").write_text("---\nid: 2024-000\n---\n# t\n", encoding="utf-8")
        (tasks_dir / "2024-001.md").write_text("---\nid: 2024-001\n---\n# t\n", encoding="utf-8")
        assert context.get_project_prefix("2024") == "2024"
        assert context.expand_task_id("1", "2024") == "2024-001"
        assert context.expand_task_id("2024-001", "2024") == "2024-001"

    def test_new_taskless_project_still_gets_letter_leading_prefix(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch, "2024")
        assert context.get_project_prefix("2024") == "P2024"

    def test_prefix_from_a_real_mint_round_trips_through_expand(self, tmp_path, monkeypatch):
        config, _tasks_dir = _portfolio(tmp_path, monkeypatch, "code-quorum")
        task = add_task(config, "code-quorum", "first")
        assert context.expand_task_id(task.id, "code-quorum") == task.id
        assert context.expand_task_id("0", "code-quorum") == task.id


class TestAllocatorResolvedPrefixForCollidingProjects:
    def test_project_that_minted_under_a_collision_prefix_expands_under_it(
        self, tmp_path, monkeypatch
    ):
        # A project whose ids were minted as CODE-B-000 (CODE was taken) must
        # expand a short ref under CODE-B, not the base candidate CODE.
        _config, tasks_dir = _portfolio(tmp_path, monkeypatch, "code-base")
        (tasks_dir / "CODE-B-000.md").write_text("---\nid: CODE-B-000\n---\n# t\n", encoding="utf-8")
        (tasks_dir / "CODE-B-001.md").write_text("---\nid: CODE-B-001\n---\n# t\n", encoding="utf-8")
        assert context.get_project_prefix("code-base") == "CODE-B"
        assert context.expand_task_id("1", "code-base") == "CODE-B-001"
