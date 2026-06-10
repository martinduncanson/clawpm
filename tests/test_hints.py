"""Runtime next-action hints (CLAWP-050)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.hints import (
    hints_enabled,
    hints_for_added_task,
    hints_for_next_task,
)
from clawpm.models import (
    Predictions,
    SuccessCriterion,
    Task,
    TaskComplexity,
    TaskState,
)


def _task(**kw) -> Task:
    return Task(
        id=kw.get("id", "X-001"),
        title="t",
        state=TaskState.OPEN,
        complexity=kw.get("complexity"),
        parallel_group=kw.get("parallel_group"),
        predictions=kw.get("predictions") or Predictions(),
    )


# ---------------------------------------------------------------------------
# Unit: the heuristics fire on the right state and stay quiet otherwise.
# ---------------------------------------------------------------------------


class TestHeuristics:
    def test_large_complexity_suggests_decompose(self):
        h = hints_for_added_task(_task(complexity=TaskComplexity.L))
        assert any("decompose" in x for x in h)

    def test_success_criteria_suggests_dispatch_and_judge(self):
        h = hints_for_added_task(
            _task(predictions=Predictions(success_criteria=[SuccessCriterion("tests pass")]))
        )
        assert any("subagent-judge" in x for x in h)

    def test_parallel_group_suggests_batch(self):
        h = hints_for_added_task(_task(parallel_group=1))
        assert any("--batch" in x for x in h)

    def test_plain_task_has_no_hints(self):
        assert hints_for_added_task(_task(complexity=TaskComplexity.S)) == []

    def test_next_hints_on_parallel_group(self):
        assert any("--batch" in x for x in hints_for_next_task(_task(parallel_group=2)))

    def test_next_hints_empty_for_plain(self):
        assert hints_for_next_task(_task()) == []


class TestEnablement:
    def test_enabled_by_default(self):
        assert hints_enabled(None) is True

    def test_env_suppresses(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_NO_HINTS", "1")
        assert hints_enabled(None) is False

    def test_non_truthy_env_does_not_suppress(self, monkeypatch):
        monkeypatch.setenv("CLAWPM_NO_HINTS", "0")
        assert hints_enabled(None) is True


# ---------------------------------------------------------------------------
# CLI integration: hints ride in the JSON `hints` field and respect suppression.
# ---------------------------------------------------------------------------


def _portfolio(tmp_path, monkeypatch):
    (tmp_path / "portfolio.toml").write_text(
        f'portfolio_root = "{tmp_path.as_posix()}"\n'
        f'project_roots = ["{(tmp_path / "projects").as_posix()}"]\n',
        encoding="utf-8",
    )
    meta = tmp_path / "projects" / "proj" / ".project"
    (meta / "tasks" / "done").mkdir(parents=True)
    (meta / "tasks" / "blocked").mkdir(parents=True)
    (meta / "settings.toml").write_text(
        'id = "proj"\nname = "proj"\nstatus = "active"\npriority = 3\n', encoding="utf-8"
    )
    monkeypatch.setenv("CLAWPM_PORTFOLIO", str(tmp_path))


def _add(args):
    return CliRunner().invoke(main, args)


class TestCli:
    def test_add_emits_hints_in_json(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch)
        res = _add(["--format", "json", "tasks", "add", "--project", "proj",
                    "--title", "big", "--complexity", "l"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)["data"]
        assert "hints" in data and any("decompose" in h for h in data["hints"]), data

    def test_no_hints_flag_suppresses(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch)
        res = _add(["--no-hints", "--format", "json", "tasks", "add", "--project", "proj",
                    "--title", "big", "--complexity", "l"])
        assert res.exit_code == 0, res.output
        assert "hints" not in json.loads(res.output)["data"]

    def test_env_suppresses_cli(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch)
        monkeypatch.setenv("CLAWPM_NO_HINTS", "1")
        res = _add(["--format", "json", "tasks", "add", "--project", "proj",
                    "--title", "big", "--complexity", "l"])
        assert "hints" not in json.loads(res.output)["data"]

    def test_plain_add_no_hints_key(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch)
        res = _add(["--format", "json", "tasks", "add", "--project", "proj",
                    "--title", "small", "--complexity", "s"])
        assert "hints" not in json.loads(res.output)["data"]

    def test_show_emits_hints(self, tmp_path, monkeypatch):
        _portfolio(tmp_path, monkeypatch)
        _add(["tasks", "add", "--project", "proj", "--title", "big",
              "--id", "PROJ-001", "--complexity", "xl"])
        res = _add(["--format", "json", "tasks", "show", "--project", "proj", "PROJ-001"])
        assert res.exit_code == 0, res.output
        data = json.loads(res.output)
        assert any("decompose" in h for h in data.get("hints", [])), data
