---
created: '2026-05-22'
depends:
- CLAWP-016
id: CLAWP-017
predictions:
  approach: 'Subcommand reads stdin JSON (Claude Code hook input format), spawns fresh
    Claude Code sub-session via ''claude -p <judge-prompt>'' OR uses Anthropic SDK
    directly with managed-agents beta header if available, parses JSON response, returns
    hook-shaped {continue, stopReason, systemMessage}. Judge prompt adapted verbatim
    from Piebald agent-prompt-hook-condition-evaluator-stop.md. Default model: Haiku
    for cost (judge is small).'
  complexity: m
  confidence: 3
  duration_min: 240
  files_scope:
  - clawpm/commands/hook.py
  - clawpm/judges/stop_condition.py
  - tests/test_stop_hook.py
  filled_by: agent
  pitfalls: 'Subprocess Claude Code invocation latency could make Stop-hook feel sluggish;
    need timeout + graceful degradation. Judge model choice: Haiku may miss nuance
    on complex rubrics; allow override to Sonnet'
  pre_mortem: 'Most likely failure: subprocess overhead pushes Stop-hook latency over
    the user-perceptible threshold (>3s); subagents feel laggy. Mitigation: parallel
    invocation in background, cache rubric prompt, default Haiku'
  success_criteria:
  - JSON output matches Piebald shape exactly (ok, reason, optional impossible)
  - Returns continue=false when ok=false, allowing Claude to keep working
  - Returns continue=true + systemMessage when ok=true, allowing termination
  - 'Independent verification: judge does NOT trust subagent''s self-claim of impossibility;
    cross-checks transcript evidence'
  - 'Three adversarial tests pass: (a) genuine done, (b) subagent falsely claims done,
    (c) genuine impossibility'
priority: 5
---
# Stop-hook condition evaluator for clawpm subagents

Implement a Stop-hook judge that reads the subagent's transcript + the subtask's rubric, returns the official JSON shape {ok, reason} | {ok: false, impossible: true, reason}. Adopt Piebald's exact contract incl. the 'agent claiming impossible is evidence not proof' doctrine. Wire as a 'clawpm hook eval-stop' subcommand callable from .claude/settings.json Stop hooks. Subagent literally cannot terminate until criteria met or impossibility independently confirmed. Local emulation; does NOT require paid Managed Agents.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

