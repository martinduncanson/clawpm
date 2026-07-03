"""Shared pytest fixtures for clawpm tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from clawpm.discovery import load_portfolio_config


@pytest.fixture
def isolated_portfolio(tmp_path: Path, monkeypatch) -> SimpleNamespace:
    """A throwaway portfolio + single project, isolated via ``monkeypatch.setenv``.

    Replaces the per-file ``os.environ["CLAWPM_PORTFOLIO"]`` save/set/restore
    pattern. ``monkeypatch.setenv`` auto-restores the prior value even when a
    test (or fixture setup after this point) raises, which closes the env-leak
    class outright — a bare save/restore with no ``try/finally`` leaks the env
    into later tests if anything between the mutation and the restore fails.

    Returns a namespace with:

    - ``root`` — the portfolio root (also the ``CLAWPM_PORTFOLIO`` value).
    - ``config`` — the loaded :class:`PortfolioConfig`.
    - ``project_id`` — ``"test"`` (matches the seeded ``settings.toml``).
    - ``project_dir`` — the project directory under ``projects/``.
    - ``tasks_dir`` — ``project_dir/.project/tasks`` with ``progress``/``done``/
      ``blocked`` state subdirectories already created.
    """
    root = tmp_path / "portfolio"
    root.mkdir()
    projects_dir = root / "projects"
    (root / "portfolio.toml").write_text(
        f'portfolio_root = "{root.as_posix()}"\n'
        f'project_roots = ["{projects_dir.as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_meta = project_dir / ".project"
    project_meta.mkdir(parents=True)
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    for sub in ("progress", "done", "blocked"):
        (tasks_dir / sub).mkdir(parents=True)

    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(root))
    config = load_portfolio_config(root)

    return SimpleNamespace(
        root=root,
        config=config,
        project_id="test",
        project_dir=project_dir,
        tasks_dir=tasks_dir,
    )
