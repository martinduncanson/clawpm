"""Service-layer proof for the MCP server (CLAWP-077 → CLAWP-068).

These tests exercise ``clawpm.services.tasks.transition`` directly — with a
portfolio config and plain kwargs, NO click, NO ``CliRunner``, NO
``clawpm.cli`` import — proving the MCP server (built next, CLAWP-068, whose
spec mandates direct core calls and forbids subprocessing the CLI) can drive
the exact same state-change orchestration without the command layer.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys

import pytest

from clawpm.services.tasks import transition, transition_isolated
from clawpm.tasks import add_subtask, add_task, get_task
from clawpm.models import TaskState


def test_transition_marks_done_without_click(isolated_portfolio):
    # No CliRunner, no clawpm.cli import — just the service + domain layer,
    # exactly as the MCP server will call it.
    config = isolated_portfolio.config
    task = add_task(config, "test", title="Service-layer task")

    result = transition(config, project_id="test", task_id=task.id, new_state="done")

    assert result["ok"] is True
    assert result["task_id"] == task.id
    assert result["data"]["state"] == "done"
    # The state change is durable and observable through the domain layer.
    reloaded = get_task(config, "test", task.id)
    assert reloaded.state == TaskState.DONE


def test_transition_blocked_returns_structured_result(isolated_portfolio):
    config = isolated_portfolio.config
    task = add_task(config, "test", title="Blockable")

    result = transition(
        config, project_id="test", task_id=task.id,
        new_state="blocked", note="waiting on upstream",
    )

    assert result["ok"] is True
    assert result["data"]["state"] == "blocked"


def test_transition_rollup_gate_blocks_incomplete_parent(isolated_portfolio):
    # The orchestration (not the raw mutator) owns the parent-rollup gate: a
    # parent with an incomplete child cannot be completed without force.
    config = isolated_portfolio.config
    parent = add_task(config, "test", title="Parent")
    add_subtask(config, "test", parent.id, "Child")

    result = transition(config, project_id="test", task_id=parent.id, new_state="done")

    assert result["ok"] is False
    assert result["error"] == "subtasks_incomplete"


def test_transition_isolated_wraps_unexpected_error_in_batch(isolated_portfolio, monkeypatch):
    config = isolated_portfolio.config
    task = add_task(config, "test", title="Boom")

    import clawpm.services.tasks as svc

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(svc, "change_task_state", _boom)

    # batch=True converts an unexpected exception into a visible failure result
    # instead of unwinding the whole batch (mirrors the CLI bulk path).
    result = transition_isolated(True, config, project_id="test", task_id=task.id, new_state="done")

    assert result["ok"] is False
    assert result["error"] == "unexpected_error"
    assert result["error_class"] == "RuntimeError"


def test_service_layer_has_no_click_or_cli_imports():
    # The whole point of the seam (CLAWP-068): the orchestration must not depend
    # on click or the clawpm.cli package — including function-local imports.
    # AST-walk every import so a future edit reintroducing one fails loudly.
    import clawpm.services.tasks as svc

    tree = ast.parse(inspect.getsource(svc))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    assert not any(m == "click" or m.startswith("click.") for m in imported), imported
    assert not any(m == "clawpm.cli" or m.startswith("clawpm.cli") for m in imported), imported


def test_service_import_chain_does_not_load_click():
    # Stronger than the AST check: importing the service in a FRESH interpreter
    # must not pull click into sys.modules through ANY transitive dependency
    # (CLAWP-077 Codex review — clawpm.reflect used to import click at module
    # top, so transition's reflection path dragged click into the MCP import
    # chain; parse_duration's click import is now lazy). Proves the seam is
    # genuinely click-free at import, not just in its own source.
    code = (
        "import sys\n"
        "import clawpm.services.tasks\n"
        "leaked = sorted(m for m in sys.modules if m == 'click' or m.startswith('click.'))\n"
        "assert not leaked, leaked\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, env=env,
    )
    assert proc.returncode == 0, f"click leaked into the service import chain:\n{proc.stderr}"


def test_transition_rejects_out_of_taxonomy_surprise_tag(isolated_portfolio):
    # The service boundary validates surprise tags so a non-CLI caller (MCP)
    # cannot write an out-of-vocabulary tag into the fixed calibration taxonomy.
    config = isolated_portfolio.config
    task = add_task(config, "test", title="Bad tag")

    with pytest.raises(ValueError, match="Unknown surprise tag"):
        transition(
            config, project_id="test", task_id=task.id,
            new_state="done", surprise_tags=("not_a_real_tag",),
        )
