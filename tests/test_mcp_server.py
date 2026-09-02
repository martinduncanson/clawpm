"""Wire-level tests for the clawpm MCP server (CLAWP-068).

The core scenario drives a real ``ClientSession`` connected to the server over
in-memory streams via ``create_connected_server_and_client_session`` — the full
MCP JSON-RPC handshake (initialize → list_tools → call_tool), with pydantic
message (de)serialization, not direct Python function calls. That proves the
wire contract: tool schemas, argument marshalling, and result shapes all round-
trip through the protocol exactly as a host (Cursor, Claude Code, …) would see.
"""

from __future__ import annotations

import json

import anyio
import pytest

# The MCP server is behind the optional `mcp` extra; skip cleanly when absent.
pytest.importorskip("mcp")

from clawpm import mcp_server as M


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _payload(result) -> dict:
    """Extract the tool's returned dict from a CallToolResult.

    Prefers ``structuredContent`` (what a structured-output-aware host reads);
    falls back to JSON-parsing the text content block. FastMCP may wrap a bare
    mapping under a ``result`` key for structured output — unwrap that so tests
    assert against the tool's own dict.
    """
    sc = getattr(result, "structuredContent", None)
    if sc is not None:
        if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
            return sc["result"]
        return sc
    content = result.content[0]
    return json.loads(content.text)


async def _call(client, name: str, args: dict):
    return _payload(await client.call_tool(name, args))


def _run(scenario, *args):
    """Run an async scenario coroutine from a sync test without pytest-asyncio."""
    return anyio.run(scenario, *args)


# ---------------------------------------------------------------------------
# tool surface + tier gating (success criterion 2)
# ---------------------------------------------------------------------------

def test_core_tool_surface():
    async def scenario():
        server = M.build_server("core")
        return await server.list_tools()

    tools = _run(scenario)
    names = sorted(t.name for t in tools)
    assert names == [
        "context", "mission_list", "next", "research_add", "research_list",
        "tasks_add", "tasks_edit", "tasks_get", "tasks_list", "tasks_state",
    ]
    # Core-mode tool count must stay lean (<= 12) to avoid host context pollution.
    assert len(tools) <= 12


def test_every_tool_has_input_schema():
    async def scenario():
        return await M.build_server("all").list_tools()

    for tool in _run(scenario):
        assert tool.inputSchema is not None
        assert tool.inputSchema.get("type") == "object"


def test_resolve_tier_fallback_to_core():
    assert M.resolve_tier(None) == M.TIERS["core"]
    assert M.resolve_tier("") == M.TIERS["core"]
    assert M.resolve_tier("nonsense") == M.TIERS["core"]
    assert M.resolve_tier("STANDARD") == M.TIERS["standard"]
    assert M.resolve_tier(" all ") == M.TIERS["all"]


def test_tier_gate_excludes_higher_tiers(monkeypatch):
    """A higher-tier tool is hidden in core, visible in standard/all."""
    specs = [
        M.ToolSpec("core_tool", "core", lambda: {}),
        M.ToolSpec("standard_tool", "standard", lambda: {}),
        M.ToolSpec("all_tool", "all", lambda: {}),
    ]
    monkeypatch.setattr(M, "TOOL_SPECS", specs)
    assert [s.name for s in M.specs_for_tier("core")] == ["core_tool"]
    assert [s.name for s in M.specs_for_tier("standard")] == ["core_tool", "standard_tool"]
    assert [s.name for s in M.specs_for_tier("all")] == ["core_tool", "standard_tool", "all_tool"]


def test_tools_env_var_default(monkeypatch):
    """build_server with no explicit tier reads CLAWPM_MCP_TOOLS, default core."""
    monkeypatch.delenv(M.TOOLS_ENV_VAR, raising=False)

    async def scenario():
        return await M.build_server().list_tools()

    assert len(_run(scenario)) == 10


def test_all_current_tools_are_core():
    assert all(s.min_tier == "core" for s in M.TOOL_SPECS)


# ---------------------------------------------------------------------------
# wire-level list -> add -> state (success criteria 1 + 3)
# ---------------------------------------------------------------------------

def test_wire_level_list_add_state(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            # list — empty to start
            listed = await _call(client, "tasks_list", {"project": pid})
            assert listed["ok"] is True
            assert listed["project"] == pid
            assert listed["count"] == 0
            assert listed["tasks"] == []

            # add — a verifiable-goal task with predictions
            added = await _call(client, "tasks_add", {
                "project": pid,
                "title": "Wire an MCP smoke test",
                "complexity": "s",
                "success_criteria": ["integration test drives list->add->state"],
                "confidence": 3,
                "pre_mortem": "most likely failure: result serialization mismatch",
            })
            assert added["ok"] is True
            task = added["task"]
            task_id = task["id"]
            assert task_id.startswith("TEST-")
            # success_criteria + confidence survived into the persisted task
            preds = task["predictions"]
            assert preds["confidence"] == 3
            assert preds["success_criteria"]

            # list — now one task
            listed2 = await _call(client, "tasks_list", {"project": pid})
            assert listed2["count"] == 1
            assert listed2["tasks"][0]["id"] == task_id

            # get — full detail round-trips
            got = await _call(client, "tasks_get", {"project": pid, "task_id": task_id})
            assert got["ok"] is True
            assert got["task"]["id"] == task_id

            # state — open -> progress
            prog = await _call(client, "tasks_state", {
                "project": pid, "task_id": task_id, "new_state": "progress",
            })
            assert prog["ok"] is True
            assert prog["task_id"] == task_id
            assert prog["data"]["state"] == "progress"

            # state — progress -> done
            done = await _call(client, "tasks_state", {
                "project": pid, "task_id": task_id, "new_state": "done",
                "reflect_note": "shipped",
            })
            assert done["ok"] is True
            assert done["data"]["state"] == "done"

            # list open-only — the done task is gone from the active view
            open_only = await _call(client, "tasks_list", {"project": pid, "state": "open"})
            assert open_only["count"] == 0

            # get by short id — the done task is still resolvable
            done_num = task_id.split("-")[1]
            got_done = await _call(client, "tasks_get", {"project": pid, "task_id": done_num})
            assert got_done["ok"] is True
            assert got_done["task"]["state"] == "done"

    _run(scenario)


def test_wire_level_error_shapes(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            missing = await _call(client, "tasks_get", {"project": pid, "task_id": "999"})
            assert missing["ok"] is False
            assert missing["error"] == "not_found"

            bad_state = await _call(client, "tasks_state", {
                "project": pid, "task_id": "1", "new_state": "sideways",
            })
            assert bad_state["ok"] is False
            assert bad_state["error"] == "bad_state"

            added = await _call(client, "tasks_add", {"project": pid, "title": "x"})
            bad_tag = await _call(client, "tasks_state", {
                "project": pid, "task_id": added["task"]["id"], "new_state": "done",
                "surprise_tags": ["not_a_real_tag"],
            })
            assert bad_tag["ok"] is False
            assert bad_tag["error"] == "invalid_argument"

    _run(scenario)


def test_wire_level_research_roundtrip(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            empty = await _call(client, "research_list", {"project": pid})
            assert empty["ok"] is True
            assert empty["count"] == 0

            added = await _call(client, "research_add", {
                "project": pid,
                "title": "MCP transport choice",
                "research_type": "decision",
                "summary": "stdio for v1",
                "conclusion": "HTTP deferred",
            })
            assert added["ok"] is True
            assert added["research"]["id"]

            listed = await _call(client, "research_list", {"project": pid})
            assert listed["count"] == 1

    _run(scenario)


def test_wire_level_context_and_next(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            await _call(client, "tasks_add", {"project": pid, "title": "first"})

            nxt = await _call(client, "next", {"project": pid})
            assert nxt["ok"] is True
            assert nxt["task"] is not None
            assert nxt["task"]["title"] == "first"

            ctx = await _call(client, "context", {"project": pid})
            assert ctx["ok"] is True
            assert ctx["project"]["id"] == pid
            assert "open_count" in ctx
            assert ctx["open_count"] == 1

            missions = await _call(client, "mission_list", {"project": pid})
            assert missions["ok"] is True
            assert missions["count"] == 0

    _run(scenario)


# ---------------------------------------------------------------------------
# project resolution
# ---------------------------------------------------------------------------

def test_no_project_resolves_to_error(isolated_portfolio, monkeypatch, tmp_path):
    """With no explicit project and cwd outside any project, tools raise a
    friendly usage error (surfaced as an MCP tool error)."""
    from clawpm.context import CONTEXT_FILE

    # Ensure no `clawpm use` context and cwd is not inside a project.
    monkeypatch.setattr("clawpm.context.CONTEXT_FILE", tmp_path / ".no-such-context")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError):
        M.tasks_list()


# ---------------------------------------------------------------------------
# uncaught-exception -> JSON contract (CLAWP-068 review F11)
# ---------------------------------------------------------------------------

def test_wire_level_uncaught_exception_returns_json_contract(isolated_portfolio, monkeypatch, tmp_path):
    """An exception a tool doesn't explicitly catch (here: no project
    resolvable) still round-trips as {"ok": false, ...} over the wire, not
    the MCP SDK's default plain-text isError blob."""
    from mcp.shared.memory import create_connected_server_and_client_session

    monkeypatch.setattr("clawpm.context.CONTEXT_FILE", tmp_path / ".no-such-context")
    monkeypatch.chdir(tmp_path)

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()
            result = await client.call_tool("tasks_list", {})
            assert result.isError is not True
            payload = _payload(result)
            assert payload["ok"] is False
            assert payload["error"] == "invalid_argument"
            assert "project" in payload["message"].lower()

    _run(scenario)


def test_wire_level_bad_predict_duration_returns_bad_predictions(isolated_portfolio):
    """predict_duration raises click.BadParameter (not ValueError) inside
    parse_duration — confirms it's normalized to the documented
    bad_predictions shape rather than escaping uncaught (CLAWP-068 review F2)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "predict_duration": "asap",
            })
            assert added["ok"] is False
            assert added["error"] == "bad_predictions"

            edited_target = await _call(client, "tasks_add", {"project": pid, "title": "y"})
            edited = await _call(client, "tasks_edit", {
                "project": pid, "task_id": edited_target["task"]["id"],
                "predict_duration": "asap",
            })
            assert edited["ok"] is False
            assert edited["error"] == "bad_predictions"

    _run(scenario)


def test_wire_level_bad_predicted_by_returns_bad_predictions(isolated_portfolio):
    """predicted_by must match the CLI's --predicted-by vocabulary
    (agent|operator|operator-edited|retroactive) — an unvalidated string
    would pollute filled_by-bucketed calibration data (Codex review round 3)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "confidence": 3, "predicted_by": "robot-overlord",
            })
            assert added["ok"] is False
            assert added["error"] == "bad_predictions"

            valid = await _call(client, "tasks_add", {
                "project": pid, "title": "y", "confidence": 3, "predicted_by": "operator-edited",
            })
            assert valid["ok"] is True
            assert valid["task"]["predictions"]["filled_by"] == "operator-edited"

    _run(scenario)


# ---------------------------------------------------------------------------
# wire-level validation-error paths (CLAWP-068 review F12)
# ---------------------------------------------------------------------------

def test_wire_level_write_tool_validation_errors(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            bad_complexity = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "complexity": "xxl",
            })
            assert bad_complexity["ok"] is False
            assert bad_complexity["error"] == "bad_complexity"

            bad_delegability = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "delegability": "robot",
            })
            assert bad_delegability["ok"] is False
            assert bad_delegability["error"] == "bad_delegability"

            bad_status = await _call(client, "research_list", {
                "project": pid, "status": "not-a-status",
            })
            assert bad_status["ok"] is False
            assert bad_status["error"] == "bad_status"

    _run(scenario)


def test_wire_level_tasks_state_not_found(isolated_portfolio):
    """tasks_state on a nonexistent task id takes a different code path
    (services.tasks.transition's own not-found gate, "task_not_found") than
    tasks_get's "not_found" — exercised separately (CLAWP-068 review F12)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            result = await _call(client, "tasks_state", {
                "project": pid, "task_id": "TEST-999", "new_state": "progress",
            })
            assert result["ok"] is False
            assert result["error"] == "task_not_found"

    _run(scenario)


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------

def test_cli_mcp_command_registered():
    from clawpm.cli import main
    assert "mcp" in main.commands


def test_cli_mcp_help_runs():
    from click.testing import CliRunner
    from clawpm.cli import main

    result = CliRunner().invoke(main, ["mcp", "--help"])
    assert result.exit_code == 0
    assert "stdio" in result.output.lower()


def test_cli_mcp_import_error_shows_friendly_message(monkeypatch):
    """Regression test for CLAWP-068 review F1. The real failure mode is
    build_server() raising (its deferred `import mcp`), not
    `from clawpm.mcp_server import build_server` — that import always
    succeeds since mcp_server.py has no top-level `mcp` dependency. Simulate
    a missing extra by making build_server() raise
    ModuleNotFoundError(name="mcp"), matching what Python actually raises for
    `import mcp...` when the package is absent, and assert the CLI's guard
    now catches it (previously it only wrapped the import)."""
    from click.testing import CliRunner
    from clawpm.cli import main
    import clawpm.mcp_server as mcp_server_module

    def _boom(tools_tier):
        raise ModuleNotFoundError("No module named 'mcp'", name="mcp")

    monkeypatch.setattr(mcp_server_module, "build_server", _boom)

    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code == 1
    assert "pip install" in result.output.lower()
    assert "clawpm[mcp]" in result.output


def test_cli_mcp_unrelated_module_not_found_at_construction_propagates(monkeypatch):
    """Regression test for the antigravity-review tightening of F1: a
    ModuleNotFoundError for something OTHER than `mcp`, raised while
    building the server, must NOT be swallowed and misreported as a missing
    extra — it should propagate."""
    from click.testing import CliRunner
    from clawpm.cli import main
    import clawpm.mcp_server as mcp_server_module

    def _boom(tools_tier):
        raise ModuleNotFoundError("No module named 'some_unrelated_thing'", name="some_unrelated_thing")

    monkeypatch.setattr(mcp_server_module, "build_server", _boom)

    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code != 0
    assert "pip install 'clawpm[mcp]'" not in result.output
    assert isinstance(result.exception, ModuleNotFoundError)


def test_cli_mcp_module_not_found_during_run_propagates(monkeypatch):
    """Regression test for grok-4.5's round-3 finding: the guard must wrap
    ONLY construction (build_server()), not the blocking `.run()` call — a
    ModuleNotFoundError('mcp') raised during actual server operation (long
    after startup) must propagate as a real crash, not get misreported as
    "install the extra" the way it would if the try/except still spanned the
    whole blocking lifetime."""
    from click.testing import CliRunner
    from clawpm.cli import main
    import clawpm.mcp_server as mcp_server_module

    class _FakeServer:
        def run(self):
            raise ModuleNotFoundError("No module named 'mcp'", name="mcp")

    monkeypatch.setattr(mcp_server_module, "build_server", lambda tools_tier: _FakeServer())

    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code != 0
    assert "pip install 'clawpm[mcp]'" not in result.output
    assert isinstance(result.exception, ModuleNotFoundError)


# ---------------------------------------------------------------------------
# tasks_list default-view regression (CLAWP-068 review F3)
# ---------------------------------------------------------------------------

def test_wire_level_tasks_list_default_excludes_done(isolated_portfolio):
    """Omitting `state` must match the CLI's active-view default
    (open/progress/blocked), not an unfiltered scan that also walks the
    done/ directory."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {"project": pid, "title": "will finish"})
            task_id = added["task"]["id"]
            await _call(client, "tasks_state", {
                "project": pid, "task_id": task_id, "new_state": "progress",
            })
            await _call(client, "tasks_state", {
                "project": pid, "task_id": task_id, "new_state": "done",
            })

            default_view = await _call(client, "tasks_list", {"project": pid})
            assert default_view["count"] == 0
            assert default_view["tasks"] == []

            explicit_done = await _call(client, "tasks_list", {"project": pid, "state": "done"})
            assert explicit_done["count"] == 1
            assert explicit_done["tasks"][0]["id"] == task_id

    _run(scenario)


def test_wire_level_tasks_list_limit(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            for i in range(3):
                await _call(client, "tasks_add", {"project": pid, "title": f"t{i}"})

            limited = await _call(client, "tasks_list", {"project": pid, "limit": 2})
            assert limited["count"] == 2
            assert limited["total"] == 3
            assert len(limited["tasks"]) == 2

    _run(scenario)


def test_wire_level_tasks_list_negative_limit_ignored(isolated_portfolio):
    """A negative limit must not silently apply Python end-slicing
    (tasks[:-1] drops the last task) — mirrors the CLI's own
    `limit is not None and limit >= 0` guard (CLAWP-068 review, grok-4.5)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            await _call(client, "tasks_add", {"project": pid, "title": "t1"})
            await _call(client, "tasks_add", {"project": pid, "title": "t2"})

            result = await _call(client, "tasks_list", {"project": pid, "limit": -1})
            assert result["count"] == 2

    _run(scenario)


# ---------------------------------------------------------------------------
# predictions attribution + edit clear-vs-omit semantics (CLAWP-068 review,
# grok-4.5/4.6 convergent findings)
# ---------------------------------------------------------------------------

def test_wire_level_predictions_default_filled_by_agent(isolated_portfolio):
    """MCP-created predictions are attributed filled_by="agent" by default
    (the CLI's own default is "operator", since a human types CLI flags);
    predicted_by overrides it."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "confidence": 3,
            })
            assert added["task"]["predictions"]["filled_by"] == "agent"

            added2 = await _call(client, "tasks_add", {
                "project": pid, "title": "y", "confidence": 3,
                "predicted_by": "operator",
            })
            assert added2["task"]["predictions"]["filled_by"] == "operator"

    _run(scenario)


def test_wire_level_tasks_edit_empty_list_clears_scope(isolated_portfolio):
    """An explicit empty list for scope/out_of_scope/stop_conditions clears
    the field (matches edit_task's own contract); omitting the argument
    leaves it unchanged. `x if x else None` previously coerced [] to None,
    silently no-opping a clear request (CLAWP-068 review, grok-4.6)."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {
                "project": pid, "title": "x",
                "scope": ["a.py"], "out_of_scope": ["b.py"], "stop_conditions": ["blocked"],
            })
            task_id = added["task"]["id"]
            assert added["task"]["scope"] == ["a.py"]

            cleared = await _call(client, "tasks_edit", {
                "project": pid, "task_id": task_id,
                "scope": [], "out_of_scope": [], "stop_conditions": [],
            })
            assert cleared["ok"] is True
            assert cleared["task"]["scope"] == []
            assert cleared["task"]["out_of_scope"] == []
            assert cleared["task"]["stop_conditions"] == []

            unchanged = await _call(client, "tasks_edit", {
                "project": pid, "task_id": task_id, "title": "still x",
            })
            assert unchanged["task"]["scope"] == []

    _run(scenario)


# ---------------------------------------------------------------------------
# tasks_edit no_changes / conflicting_flags guards (CLAWP-068 review,
# grok-4.5 round 4)
# ---------------------------------------------------------------------------

def test_wire_level_tasks_edit_no_changes(isolated_portfolio):
    """tasks_edit with no mutable field supplied must be rejected, matching
    the CLI's no_changes guard — otherwise edit_task still rewrites the file
    and bumps `updated` for a request that changed nothing."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {"project": pid, "title": "x"})
            task_id = added["task"]["id"]

            result = await _call(client, "tasks_edit", {"project": pid, "task_id": task_id})
            assert result["ok"] is False
            assert result["error"] == "no_changes"

    _run(scenario)


def test_wire_level_tasks_edit_conflicting_flags(isolated_portfolio):
    """tags+clear_tags and parallel_group+clear_parallel_group must be
    rejected, matching the CLI's conflicting_flags guard — otherwise
    edit_task's clear-wins-silently ordering discards the supplied value
    with no signal that half the request was ignored."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {"project": pid, "title": "x"})
            task_id = added["task"]["id"]

            tags_conflict = await _call(client, "tasks_edit", {
                "project": pid, "task_id": task_id,
                "tags": ["a"], "clear_tags": True,
            })
            assert tags_conflict["ok"] is False
            assert tags_conflict["error"] == "conflicting_flags"

            pg_conflict = await _call(client, "tasks_edit", {
                "project": pid, "task_id": task_id,
                "parallel_group": 1, "clear_parallel_group": True,
            })
            assert pg_conflict["ok"] is False
            assert pg_conflict["error"] == "conflicting_flags"

    _run(scenario)


# ---------------------------------------------------------------------------
# context() negative log_limit clamp (CLAWP-068 review, grok-4.5 round 4)
# ---------------------------------------------------------------------------

def test_wire_level_context_negative_log_limit_clamped(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            result = await _call(client, "context", {"project": pid, "log_limit": -1})
            assert result["ok"] is True

    _run(scenario)


# ---------------------------------------------------------------------------
# mission_list status validation (CLAWP-068 review, grok-4.6)
# ---------------------------------------------------------------------------

def test_wire_level_mission_list_bad_status(isolated_portfolio):
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            result = await _call(client, "mission_list", {"project": pid, "status": "not-a-status"})
            assert result["ok"] is False
            assert result["error"] == "bad_status"

    _run(scenario)


# ---------------------------------------------------------------------------
# has_predictions truthiness — deliberately unchanged (CLAWP-068 review,
# grok-4.5 round 3 — see _build_predictions's NOTE for why)
# ---------------------------------------------------------------------------

def test_wire_level_predict_scope_alone_is_rejected_as_no_changes(isolated_portfolio):
    """`predict_scope: []` as the SOLE tasks_edit argument doesn't register
    as a predictions edit (matches the CLI's own `_has_predictions`
    truthiness check — see _build_predictions's NOTE for why switching to
    `is not None` would trade this for a worse bug, a silent full-wipe of
    every OTHER existing prediction field). Now that tasks_edit has a
    no_changes guard (grok-4.5 round 4), a call that registers nothing to
    change is rejected outright rather than silently succeeding with no
    effect — strictly better than the earlier no-op-but-ok:true behavior."""
    from mcp.shared.memory import create_connected_server_and_client_session

    pid = isolated_portfolio.project_id

    async def scenario():
        server = M.build_server("core")
        async with create_connected_server_and_client_session(server) as client:
            await client.initialize()

            added = await _call(client, "tasks_add", {
                "project": pid, "title": "x", "confidence": 3,
            })
            task_id = added["task"]["id"]
            assert added["task"]["predictions"]["confidence"] == 3

            edited = await _call(client, "tasks_edit", {
                "project": pid, "task_id": task_id, "predict_scope": [],
            })
            assert edited["ok"] is False
            assert edited["error"] == "no_changes"

            # Existing predictions are untouched by the rejected request.
            got = await _call(client, "tasks_get", {"project": pid, "task_id": task_id})
            assert got["task"]["predictions"]["confidence"] == 3

    _run(scenario)


# ---------------------------------------------------------------------------
# _catch_unhandled exception-code mapping (CLAWP-068 review, grok-4.5 round 3)
# ---------------------------------------------------------------------------

def test_catch_unhandled_maps_known_mutator_exceptions():
    """LockTimeout/FileNotFoundError/FileExistsError get their own error
    codes (matching services.tasks.transition's own convention) instead of
    falling into the generic internal_error bucket — a caller deciding
    whether to retry needs the distinction."""
    from clawpm.concurrency import LockTimeout

    def _raises(exc):
        def fn():
            raise exc
        return fn

    lock_result = M._catch_unhandled(_raises(LockTimeout("busy")))()
    assert lock_result == {"ok": False, "error": "lock_timeout", "message": "busy"}

    nf_result = M._catch_unhandled(_raises(FileNotFoundError("gone")))()
    assert nf_result == {"ok": False, "error": "not_found", "message": "gone"}

    exists_result = M._catch_unhandled(_raises(FileExistsError("dup")))()
    assert exists_result == {"ok": False, "error": "already_exists", "message": "dup"}

    other_result = M._catch_unhandled(_raises(RuntimeError("weird")))()
    assert other_result == {"ok": False, "error": "internal_error", "message": "weird"}
