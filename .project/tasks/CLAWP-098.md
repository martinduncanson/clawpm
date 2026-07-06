---
baseline_ref: b1575ba
created: '2026-07-06'
id: CLAWP-098
predictions:
  approach: 'Register the dispatched worktree as a temporarily-scoped project during
    its lifetime (torn down with teardown-dispatch), OR prefer cwd-resolved .project/
    over portfolio-registry lookup when cwd is inside a worktree whose id matches
    the task''s project prefix. At minimum: loud warning + documented hand-edit-and-git-mv
    workaround in dispatch output and SKILL.md until the real fix lands.'
  complexity: m
  confidence: 4
  duration_min: 240
  filled_by: agent
  pitfalls: the portfolio registry is relied upon by many other commands too (tasks
    list, next, reflect) -- any fix must not break normal single-checkout usage; the
    temporarily-scoped-project approach needs careful teardown to avoid registry bloat
    from abandoned/crashed dispatches
  success_criteria:
  - clawpm tasks state <id> done run from inside a dispatched --worktree checkout
    mutates that worktree's own task file, not the main checkout's
  - A regression test proves the current bug (state-mutator run from a worktree currently
    corrupts the main checkout) and then proves the fix
  - SKILL.md / dispatch output documents the interim hand-edit-and-git-mv workaround
    if the full fix is deferred
priority: 2
updated: '2026-07-06'
---
# Worktree-dispatched ID-mutator commands silently corrupt the MAIN checkout's task file (portfolio-registry resolution bypasses cwd)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

