---
baseline_ref: 1f4316f
complexity: m
created: '2026-09-03'
id: CLAWP-118
predictions:
  approach: Carry the project's repo-relative prefix in the session record so the
    repo root and the project root are both recoverable, then decide the agent cwd
    and settings location against that single source rather than inferring either
    from the worktree path.
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pre_mortem: Only one of the three coupled decisions gets made - session record,
    agent cwd, settings location - and the layout half-works again, which is how it
    produced a silent fall-through to the main checkout the first time.
  reference_tasks:
  - CLAWP-098
  success_criteria:
  - tasks dispatch --worktree for a project at a repo subdirectory registers a session
    that session-scoped resolution actually resolves; an ID-based mutator run inside
    that checkout mutates the worktree task file, never the main checkout
  - The monorepo_worktree_unsupported guard in cli/tasks.py is removed and its tests
    replaced by positive coverage of the working path
  - Existing session records with no prefix keep resolving unchanged (no migration
    regression)
  unknowns: Whether the agent should run at the repo root (sibling packages visible)
    or the project root (resolution matches non-worktree dispatch); whether existing
    session records need a migration or can default to an empty prefix.
priority: 5
tags:
- dispatch
updated: '2026-09-03'
---
# Support --worktree dispatch for a project in a repository subdirectory (monorepo layout)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

