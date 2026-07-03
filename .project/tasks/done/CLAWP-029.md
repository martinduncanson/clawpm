---
created: '2026-05-24'
id: CLAWP-029
predictions:
  complexity: s
  confidence: 4
  duration_min: 60
  filled_by: agent
  predicted_iterations: 1
priority: 5
---
# CodeGraph: clawpm agent dispatch ensures .codegraph/ in worktree

When clawpm agent dispatch creates a per-task worktree, also run codegraph init plus codegraph index in it so the subagent has the index from turn one. Skip silently if codegraph not on PATH. Add --no-codegraph opt-out.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

