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
updated: '2026-10-07'
---
# Support --worktree dispatch for a project in a repository subdirectory (monorepo layout)



## Acceptance Criteria

- [x] `tasks dispatch --worktree` for a project at `<repo>/packages/foo` registers a session that resolves from `<wt>/packages/foo` and `<wt>/packages/foo/sub`; the dispatch target (marker, settings, agent cwd) is the project root inside the worktree.
- [x] An ID-based mutator run in that checkout mutates the worktree task file, never the main checkout.
- [x] A ledger record with no `project_prefix` still resolves as before (empty prefix); a malformed prefix (non-str, absolute, `..`) skips that event with an ERROR log and never raises.
- [x] A prefixed project and a sibling project in the same repo do not cross-match.
- [x] A `git worktree move`d prefixed worktree resolves via its marker, and teardown persists the new checkout root (marker dir minus the prefix); a path that does not end in the prefix aborts teardown loudly.
- [x] The `monorepo_worktree_unsupported` guard is gone; root-level projects behave and serialise exactly as before (no `project_prefix` key written).
- [x] `agent dispatch` uses the same layout (settings, subtask copy, cwd at the project root; session registered with the prefix).

## Notes

Accepted limitation: after a RELOCATION (`git worktree move`), a cwd at a sibling directory such as `<wt>/packages/bar` resolving project foo is not recovered, because the marker fallback is an ancestor lookup from the project root; non-relocated sessions resolve through the ledger and are unaffected.
