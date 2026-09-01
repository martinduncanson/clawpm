---
baseline_ref: e2b4f24
complexity: s
created: '2026-08-31'
id: CLAWP-106
predictions:
  complexity: s
  confidence: 4
  duration_min: 45
  filled_by: agent
priority: 6
scope:
- src/clawpm/mcp_server.py
- src/clawpm/context.py
- README.md
updated: '2026-08-31'
---
# CLAWP-068 MCP server: tech-debt follow-ups from code-quorum review (PR #54)

Low-severity, non-blocking findings from the code-quorum adversarial review of PR #54 (CLAWP-068 MCP server), judged TECH-DEBT (not required before merge): (1) README documents --tools standard as widening the tool set but it's currently a no-op (all 10 specs tagged core) - reword to 'reserved for future tools'. (2) mcp_server.py's context() tool returns 'project' as a dict while every other tool returns it as a plain project-id string - rename the key (e.g. project_info) for surface consistency. (3) tasks_state omits meta_reflect/process_lesson params that services.tasks.transition accepts and the CLI exposes - docstring's CLI-parity claim is slightly overstated; add the two params. (4) ToolSpec.min_tier is a bare str with the tier invariant expressed nowhere in the type, and resolve_tier (soft-fallback) vs specs_for_tier (hard KeyError) disagree on failure mode for the same invariant - change to Literal['core','standard','all'] and align the two failure modes. (5) context.py's build_agent_context() docstring justifies function-local imports with a circular-import claim (tasks/links/worklog importing back into context.py) that is factually false (verified: no such imports exist) - hoist Task/TaskState onto the file's existing top-level models import and delete the false justification. (6) mcp_server.py's module docstring bolded claim 'Direct core calls, zero subprocess' overclaims since the context tool's build_agent_context() calls subprocess.run for git 3x - reword to drop 'zero subprocess' (the narrower 'nothing shells out to the clawpm CLI' claim one sentence later is the accurate one).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

