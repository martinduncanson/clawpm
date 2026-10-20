"""Shared pytest fixtures for clawpm tests."""

from __future__ import annotations

import os
import subprocess
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

    # Pin discovery to this sandbox only: an inherited CLAWPM_PROJECT_ROOTS /
    # CLAWPM_WORKSPACE from the runner would otherwise be appended by
    # load_portfolio_config, letting tests discover (and --apply could mutate)
    # real projects outside tmp_path.
    monkeypatch.delenv("CLAWPM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("CLAWPM_WORKSPACE", raising=False)
    monkeypatch.setenv("CLAWPM_PORTFOLIO", root.as_posix())
    config = load_portfolio_config(root)

    return SimpleNamespace(
        root=root,
        config=config,
        project_id="test",
        project_dir=project_dir,
        tasks_dir=tasks_dir,
    )


# ---------------------------------------------------------------------------
# CLAUD-094: no test may spawn a real `claude` process.
#
# Tests patched `_default_judge_invoker` after `evaluate_stop_condition` had
# moved to `make_judge_invoker()`, so the patch did nothing and the suite
# launched real `claude --print` judges (1,189 sessions in two weeks). The
# guard makes any such spawn fail the test loudly. It wraps `Popen.__init__`
# (which `subprocess.run` goes through), raises `pytest.fail` (a
# BaseException, so `except Exception` handlers cannot swallow it) and also
# records the attempt, so a swallowed failure still fails the test at
# teardown. A test that provokes the guard on purpose drains
# `claude_spawn_attempts`.
# ---------------------------------------------------------------------------

_CLAUDE_SPAWN_ATTEMPTS: list = []


def _is_claude_spawn(args) -> bool:
    if isinstance(args, (str, bytes)):
        first = os.fsdecode(args).strip().split(" ")[0]
    elif isinstance(args, os.PathLike):
        first = os.fsdecode(args)
    else:
        try:
            first = os.fsdecode(next(iter(args)))
        except (StopIteration, TypeError):
            return False
    return Path(first.strip("\"'")).stem.lower() == "claude"


@pytest.fixture
def claude_spawn_attempts() -> list:
    """The recorded spawn attempts; clear it when a test provokes the guard."""
    return _CLAUDE_SPAWN_ATTEMPTS


@pytest.fixture(autouse=True)
def _no_real_claude_spawn(monkeypatch):
    real_init = subprocess.Popen.__init__

    def guarded_init(self, args, *a, **k):
        if _is_claude_spawn(args):
            _CLAUDE_SPAWN_ATTEMPTS.append(args)
            pytest.fail(
                f"test tried to spawn a real `claude` process: {args!r}. "
                "Patch stop_condition.make_judge_invoker, or set "
                "CLAWPM_JUDGE_CMD / CLAWPM_RESUME_CMD to a stub.",
                pytrace=False,
            )
        return real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_init)
    _CLAUDE_SPAWN_ATTEMPTS.clear()
    yield
    if _CLAUDE_SPAWN_ATTEMPTS:
        attempts = list(_CLAUDE_SPAWN_ATTEMPTS)
        _CLAUDE_SPAWN_ATTEMPTS.clear()
        pytest.fail(f"real `claude` spawn attempted during test: {attempts!r}")
