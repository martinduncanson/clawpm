---
complexity: m
created: '2026-06-10'
id: CLAWP-049
predictions:
  approach: 'Add a situation-to-command decision table to SKILL.md so the acting agent
    self-steers to the right power feature (decompose, batch, dispatch, leases, emit-rubric,
    confirm-close, subagent-judge, conflicts). Critically clarify the two dispatch
    modes: tasks dispatch writes .claude hooks for a SEPARATE spawned claude session
    (worktree workflow); for in-harness Agent-tool subagents the equivalent is success_criteria
    + the subagent-judge skill. Cover every switch with a when-to-use trigger, not
    just what-it-does.'
  confidence: 3
  duration_min: 180
  files_scope:
  - skills/clawpm/SKILL.md
  filled_by: agent
  success_criteria:
  - SKILL.md has a capability-map table mapping >=8 situations to the correct command/switch
  - the two dispatch modes (spawned-claude vs in-harness-subagent) are explicitly
    distinguished with when-to-use-each
priority: 5
---
# SKILL.md capability map: situation to command, + dispatch-mode clarification



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

