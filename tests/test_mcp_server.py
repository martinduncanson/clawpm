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
    run_stdio() itself raising (via build_server()'s deferred `import mcp`),
    not `from clawpm.mcp_server import run_stdio` — that import always
    succeeds since mcp_server.py has no top-level `mcp` dependency. Simulate
    a missing extra by making run_stdio() raise ImportError, and assert the
    CLI's guard now catches it (previously it only wrapped the import)."""
    from click.testing import CliRunner
    from clawpm.cli import main
    import clawpm.mcp_server as mcp_server_module

    def _boom(tools_tier):
        raise ImportError("No module named 'mcp'")

    monkeypatch.setattr(mcp_server_module, "run_stdio", _boom)

    result = CliRunner().invoke(main, ["mcp"])
    assert result.exit_code == 1
    assert "pip install" in result.output.lower()
    assert "clawpm[mcp]" in result.output


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
