---
baseline_ref: 8606b04
created: '2026-07-04'
id: CLAWP-092
predictions:
  approach: Two parallel worktrees (CLAWP-078, CLAWP-079) independently computed the
    same next-free task id (CLAWP-089) when each filed a follow-up task, since separate
    worktree working directories can't see each other's uncommitted task files. Relevant
    input for CLAWP-071 (transaction integrity) scope, or a note that next-id allocation
    needs a shared-lock/counter mechanism across worktrees, not just within one checkout.
  complexity: s
  confidence: 4
  duration_min: 30
  filled_by: agent
  success_criteria:
  - Decision recorded on whether next-id allocation needs cross-worktree coordination
    (e.g. lease-style id reservation) or whether this is accepted as a rare collision
    caught by review
priority: 6
---
# Observation: cross-worktree next-task-id race (dispatch campaign 2026-07-03)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

