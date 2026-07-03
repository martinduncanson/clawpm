---
created: '2026-05-22'
depends:
- CLAWP-017
id: CLAWP-018
predictions:
  approach: 'Template settings.local.json per dispatch with substitution of task_id.
    Generate via Jinja or simple .format(). ''clawpm tasks dispatch <id> [--worktree]''
    command produces the settings file and prints the subagent invocation command
    (Claude Code --settings flag if supported, else env var). Idempotent: re-running
    dispatch on same task overwrites cleanly.'
  complexity: m
  confidence: 3
  duration_min: 300
  files_scope:
  - clawpm/commands/dispatch.py
  - clawpm/templates/dispatch_settings.json.j2
  - tests/test_dispatch.py
  filled_by: agent
  pitfalls: Claude Code may not support --settings flag for ad-hoc per-invocation
    overrides; may need env var (CLAUDE_PROJECT_DIR) or local file convention. Hook
    propagation across nested subagent dispatches needs care to avoid infinite Stop-hook
    loops on parent agent.
  pre_mortem: 'Most likely failure: Claude Code''s settings layering means our per-dispatch
    settings get overridden by user''s global .claude/settings.json. Mitigation: verify
    settings precedence in docs first; if needed, use the project-local .claude/settings.local.json
    which has highest precedence.'
  success_criteria:
  - Dispatched subagent's transcript shows Stop-hook firing on attempted termination
  - PostToolUse hook writes work_log entries with correct task_id and files_changed
  - 'End-to-end smoke test: dispatch -> subagent completes subtask -> Stop-hook returns
    ok -> task state auto-transitions to done'
  - Settings file is per-dispatch (not global), cleaned up on done or after 24h via
    doctor
priority: 5
---
# Subagent dispatch via hooks: clawpm emits .claude/settings.local.json

When clawpm dispatches a subagent for a subtask, emit a per-dispatch .claude/settings.local.json (or pass via --settings) preloading: (1) Stop hook calling 'clawpm hook eval-stop --task <id>' for success-criteria enforcement; (2) PostToolUse hook calling 'clawpm log add --task <id> --action progress' for state capture; (3) SessionStart hook injecting task context. Subagent doesn't need to know about clawpm; integration by construction. Replaces ad-hoc subagent dispatch + manual state management.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

