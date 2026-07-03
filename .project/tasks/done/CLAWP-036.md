---
created: '2026-05-25'
id: CLAWP-036
predictions:
  approach: 'Two-phase: PRESERVE — for each worktree (agent-a365ee7a, agent-a6fd096e,
    agent-a855fc96) and each local-only branch, check (a) diff vs merged main reveals
    unmerged changes, (b) any uncommitted work, (c) any notes/scratch files outside
    git. Only AFTER preservation check is done, execute git worktree remove + git
    branch -D + cleanup. Report findings before destructive ops.'
  complexity: s
  confidence: 4
  duration_min: 45
  filled_by: agent
  pitfalls: Worktree may contain uncommitted operator scratch work. Need to inspect
    status of each before destroying. Could find legitimately-merged-via-squash branches
    that look unmerged via git's perspective (squash loses parent ref).
  pre_mortem: 'Most likely failure: destroying a worktree/branch that contained partially-implemented
    work the operator wanted to resume. Mitigation: dry-run the inventory first, surface
    anything ambiguous to operator.'
  success_criteria:
  - 'Inventory report lists each worktree + local-only branch with: parent commit,
    divergence from current main, uncommitted files (if any), recommended action (delete
    / preserve / surface to operator)'
  - No worktree or branch is destroyed without an explicit recommendation in the inventory
  - 'Post-cleanup: 0 stale locked worktrees, 0 local-only branches that are demonstrably
    merged or abandoned'
priority: 5
---
# Housekeeping: preservation audit of stale worktrees + branches, then clean



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

