"""CLAWP-108: ``tasks edit`` merges predictions instead of replacing the block.

Bug: ``clawpm tasks edit <id> --hypothesis X`` (any single prediction flag)
built a Predictions object holding ONLY that field and ``edit_task`` wrote it
as the whole ``predictions:`` block, silently erasing duration, complexity,
confidence, pre_mortem, scope, filled_by and the rest.

Contract under test: an edit overwrites ONLY the prediction fields explicitly
passed; every other existing field (including ones with no edit flag, such as
``filled_by`` and ``thrash_threshold``) survives untouched in the frontmatter.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from clawpm.cli import main
from clawpm.discovery import load_portfolio_config
from clawpm.models import Predictions, SuccessCriterion, TaskComplexity
from clawpm.tasks import add_task, edit_task, get_task


@pytest.fixture
def temp_portfolio():
    """Temporary portfolio with a single test project (mirrors test_scope.py)."""
    temp_dir = tempfile.mkdtemp(prefix="clawpm_clawp108_test_")
    portfolio_root = Path(temp_dir)

    (portfolio_root / "portfolio.toml").write_text(
        f'portfolio_root = "{portfolio_root.as_posix()}"\n'
        f'project_roots = ["{(portfolio_root / "projects").as_posix()}"]\n'
        "[defaults]\n"
        'status = "active"\n',
        encoding="utf-8",
    )
    projects_dir = portfolio_root / "projects"
    projects_dir.mkdir()
    project_dir = projects_dir / "test-project"
    project_dir.mkdir()
    project_meta = project_dir / ".project"
    project_meta.mkdir()
    (project_meta / "settings.toml").write_text(
        'id = "test"\nname = "Test Project"\nstatus = "active"\npriority = 3\n',
        encoding="utf-8",
    )
    tasks_dir = project_meta / "tasks"
    tasks_dir.mkdir()
    (tasks_dir / "done").mkdir()
    (tasks_dir / "blocked").mkdir()

    old_env = os.environ.get("CLAWPM_PORTFOLIO")
    os.environ["CLAWPM_PORTFOLIO"] = str(portfolio_root)
    config = load_portfolio_config(portfolio_root)

    yield {"root": portfolio_root, "config": config}

    if old_env:
        os.environ["CLAWPM_PORTFOLIO"] = old_env
    else:
        os.environ.pop("CLAWPM_PORTFOLIO", None)
    shutil.rmtree(temp_dir, ignore_errors=True)


def _full_predictions() -> Predictions:
    """Every prediction field populated, so any nulling is observable."""
    return Predictions(
        duration_min=90,
        complexity=TaskComplexity.M,
        files_changed=4,
        files_scope=["src/a.py", "src/b.py"],
        frameworks=["click", "pyyaml"],
        pitfalls="windows path quoting",
        hypothesis="baseline hypothesis",
        success_criteria=[SuccessCriterion.from_cli("baseline criterion")],
        approach="baseline approach",
        unknowns="baseline unknowns",
        confidence=3,
        reference_tasks=["CLAWP-001", "CLAWP-002"],
        pre_mortem="baseline pre-mortem",
        predicted_iterations=2,
        filled_by="operator-edited",
        thrash_threshold=7,
    )


def _raw_predictions(config, task_id: str) -> dict:
    """Read the on-disk ``predictions:`` mapping (not the parsed dataclass)."""
    task = get_task(config, "test", task_id)
    assert task is not None and task.file_path is not None
    text = task.file_path.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---", 2)[1])
    return fm["predictions"]


def _seed(config) -> tuple[str, dict]:
    task = add_task(config, "test", "Full predictions", predictions=_full_predictions())
    assert task is not None
    return task.id, _raw_predictions(config, task.id)


# (cli args, {frontmatter key: expected new value})
EDIT_CASES = [
    pytest.param(["--predict-duration", "2h"], {"duration_min": 120}, id="predict-duration"),
    pytest.param(["--predict-complexity", "xl"], {"complexity": "xl"}, id="predict-complexity"),
    pytest.param(["--predict-files-changed", "11"], {"files_changed": 11}, id="predict-files-changed"),
    pytest.param(["--predict-scope", "docs/new"], {"files_scope": ["docs/new"]}, id="predict-scope"),
    pytest.param(["--predict-frameworks", "rich"], {"frameworks": ["rich"]}, id="predict-frameworks"),
    pytest.param(["--predict-pitfalls", "new pitfall"], {"pitfalls": "new pitfall"}, id="predict-pitfalls"),
    pytest.param(["--hypothesis", "new hypothesis"], {"hypothesis": "new hypothesis"}, id="hypothesis"),
    pytest.param(
        ["--success-criteria", "new criterion"],
        {"success_criteria": [SuccessCriterion.from_cli("new criterion").to_yaml()]},
        id="success-criteria",
    ),
    pytest.param(["--predict-approach", "new approach"], {"approach": "new approach"}, id="predict-approach"),
    pytest.param(["--unknowns", "new unknowns"], {"unknowns": "new unknowns"}, id="unknowns"),
    pytest.param(["--confidence", "5"], {"confidence": 5}, id="confidence"),
    pytest.param(["--reference-task", "CLAWP-099"], {"reference_tasks": ["CLAWP-099"]}, id="reference-task"),
    pytest.param(["--pre-mortem", "new pre-mortem"], {"pre_mortem": "new pre-mortem"}, id="pre-mortem"),
    pytest.param(["--predict-iterations", "5"], {"predicted_iterations": 5}, id="predict-iterations"),
]


@pytest.mark.parametrize("cli_args,changed", EDIT_CASES)
def test_edit_single_prediction_flag_preserves_all_other_fields(temp_portfolio, cli_args, changed):
    config = temp_portfolio["config"]
    task_id, before = _seed(config)
    # Sanity: the seed really did persist every field, incl. flag-less ones.
    assert before["filled_by"] == "operator-edited"
    assert before["thrash_threshold"] == 7
    assert len(before) == 16

    result = CliRunner().invoke(
        main, ["tasks", "edit", task_id, "--project", "test", *cli_args],
    )
    assert result.exit_code == 0, result.output

    after = _raw_predictions(config, task_id)
    expected = {**before, **changed}
    assert after == expected


def test_edit_predict_scope_file_replaces_only_scope(temp_portfolio):
    config = temp_portfolio["config"]
    task_id, before = _seed(config)
    scope_file = temp_portfolio["root"] / "pscope.txt"
    scope_file.write_text("docs/x\ndocs/y\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        ["tasks", "edit", task_id, "--project", "test", "--predict-scope-file", str(scope_file)],
    )
    assert result.exit_code == 0, result.output
    assert _raw_predictions(config, task_id) == {**before, "files_scope": ["docs/x", "docs/y"]}


def test_edit_two_prediction_flags_changes_only_those_two(temp_portfolio):
    config = temp_portfolio["config"]
    task_id, before = _seed(config)

    result = CliRunner().invoke(
        main,
        ["tasks", "edit", task_id, "--project", "test",
         "--hypothesis", "h2", "--confidence", "1"],
    )
    assert result.exit_code == 0, result.output
    assert _raw_predictions(config, task_id) == {**before, "hypothesis": "h2", "confidence": 1}


def test_edit_non_prediction_flag_leaves_predictions_untouched(temp_portfolio):
    config = temp_portfolio["config"]
    task_id, before = _seed(config)

    result = CliRunner().invoke(
        main, ["tasks", "edit", task_id, "--project", "test", "--priority", "2"],
    )
    assert result.exit_code == 0, result.output
    assert _raw_predictions(config, task_id) == before


def test_edit_creates_block_with_just_that_field_when_none_exists(temp_portfolio):
    config = temp_portfolio["config"]
    task = add_task(config, "test", "No predictions yet")
    assert task is not None

    result = CliRunner().invoke(
        main, ["tasks", "edit", task.id, "--project", "test", "--hypothesis", "fresh"],
    )
    assert result.exit_code == 0, result.output
    assert _raw_predictions(config, task.id) == {"hypothesis": "fresh"}


def test_edit_task_overlay_preserves_unknown_frontmatter_keys(temp_portfolio):
    """Keys the dataclass doesn't model are not ours to drop either."""
    config = temp_portfolio["config"]
    task_id, _ = _seed(config)
    task = get_task(config, "test", task_id)
    text = task.file_path.read_text(encoding="utf-8")
    task.file_path.write_text(
        text.replace("predictions:\n", "predictions:\n  future_field: keep-me\n", 1),
        encoding="utf-8",
    )

    edit_task(config, "test", task_id, predictions=Predictions(hypothesis="h"))

    after = _raw_predictions(config, task_id)
    assert after["future_field"] == "keep-me"
    assert after["hypothesis"] == "h"
    assert after["confidence"] == 3


def test_mcp_tasks_edit_preserves_other_prediction_fields(temp_portfolio):
    """The MCP tasks_edit tool shares edit_task, so it had the same data loss."""
    from clawpm import mcp_server

    config = temp_portfolio["config"]
    task_id, before = _seed(config)

    result = mcp_server.tasks_edit(task_id=task_id, project="test", confidence=5)
    assert result["ok"] is True, result
    assert _raw_predictions(config, task_id) == {**before, "confidence": 5}


def test_mcp_tasks_edit_still_defaults_filled_by_to_agent_when_unset(temp_portfolio):
    """No existing predictions + no predicted_by: documented 'agent' fallback."""
    from clawpm import mcp_server

    config = temp_portfolio["config"]
    task = add_task(config, "test", "No predictions yet")
    result = mcp_server.tasks_edit(task_id=task.id, project="test", confidence=2)
    assert result["ok"] is True, result
    assert _raw_predictions(config, task.id) == {"confidence": 2, "filled_by": "agent"}
