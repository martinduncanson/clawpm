---
baseline_ref: 01ac8ae
complexity: xl
created: '2026-06-26'
id: CLAWP-068
predictions:
  confidence: 3
  duration_min: 480
  filled_by: agent
priority: 5
---
# MCP server interface for clawpm (clawpm mcp, stdio)

GOAL: expose clawpm's core as an MCP server so ANY MCP host (Cursor, Windsurf, VS Code, Claude Code, Amazon Q) can drive clawpm task/research/mission management — not just the Claude Code skill. This is the single highest-leverage reach multiplier (claude-task-master's 27.7k stars come largely from being MCP-native in every editor; clawpm is currently skill-bound). Research: .project/research/2026-06-26_claude-task-master-agentbox-eval-for-clawpm.md.

ARCHITECTURE:
- New 'clawpm mcp' subcommand launching a stdio MCP server (official mcp Python SDK / FastMCP).
- Wrap the EXISTING core functions DIRECTLY (clawpm.tasks/research/mission/discovery + the CLAWP-056 emission API), NOT subprocess shell-outs — avoids the cp1252/spaced-path/UnicodeEncodeError class entirely and returns structured JSON natively (clawpm is already JSON-first).
- Respect CLAWPM_PORTFOLIO / project discovery exactly as the CLI does.

TOOL SURFACE (initial 'core' set, <=~12 to avoid context pollution):
- Read: tasks_list, tasks_get, context, next (get_next_task), research_list, mission_list.
- Write: tasks_add, tasks_state, tasks_edit, research_add.
- (Defer dispatch/agent tools to a later tier — they spawn work.)

TOOL-COUNT DISCIPLINE: gate exposed tools via an env like CLAWPM_MCP_TOOLS = core|standard|all (mirrors task-master's TASK_MASTER_TOOLS), default 'core', so a host's tool list stays lean.

REGISTRATION: document the per-project .mcp.json pattern (anti-pollution; load only when cd'd into a clawpm project), not a global mcpServers entry.

DECISIONS TO MAKE: stdio-only first (matches editors) vs also HTTP; FastMCP vs raw SDK; how to surface success_criteria/rubric + predictions in tool schemas; whether write tools require a confirm flag.

SUCCESS CRITERIA: (1) an MCP host can list/add/transition tasks end-to-end via the server; (2) core-mode tool count <=12; (3) an integration test drives the stdio server (list -> add -> state) and asserts JSON shapes; (4) zero subprocess shell-outs (direct core calls); (5) README + per-project .mcp.json snippet documented.

OUT OF SCOPE: dispatch/agent-spawning tools (later tier); HTTP transport (later); auth beyond local trust.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

