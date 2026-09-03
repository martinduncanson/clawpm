---
baseline_ref: 1f4316f
complexity: l
created: '2026-09-03'
id: CLAWP-117
predictions:
  approach: 'Treat worktree identity as first-class state rather than inferring it
    from a path: a stable worktree id written at dispatch and matched on resolution,
    so relocation is a lookup rather than a heuristic recovery. Scope the dispatch
    probe and create_worktree together from that identity.'
  complexity: l
  confidence: 2
  duration_min: 360
  filled_by: agent
  pre_mortem: 'It is re-attempted as another heuristic bolted onto path matching,
    and generates the same class of findings PR #55 accumulated (guard-by-guard patching
    of an ambiguous inference) instead of replacing the inference with recorded identity.'
  reference_tasks:
  - CLAWP-098
  success_criteria:
  - git worktree move of a dispatched worktree, followed by an ID-based mutator run
    from inside it, mutates the worktree task file and never the main checkout — covered
    by a test that fails without the change
  - Re-dispatching a task whose clawpm/<task> branch is already checked out elsewhere
    succeeds or fails with an actionable error; no silent branch-from-the-wrong-checkout
  - Dispatch probe and create_worktree resolve from the same checkout in every code
    path (asserted, not just reviewed)
  - Full test suite green and a code-quorum round closes with zero P1 findings on
    the new code
  unknowns: Whether relocation recovery is worth supporting at all versus detecting
    it and failing closed with clear operator guidance; whether the persisted-correction
    write can be made safe under concurrent dispatch without a lock.
priority: 5
tags:
- concurrency
- dispatch
updated: '2026-09-03'
---
# Worktree identity: relocation recovery + dispatch source-repo scoping (split from CLAWP-098)

# Worktree identity: relocation recovery + dispatch source-repo scoping

Split out of PR #55 (CLAWP-098) at round 8, operator-approved 2026-09-03.

## Why it was cut

PR #55's findings-per-round ran 5 → 8 → 7 — flat, not converging. Four of
round 8's seven findings traced to one feature, which had already produced
five findings across two rounds. It is a hard distributed-state problem
being solved inside a PR about something else.

## What was removed from PR #55

1. `sessions._rediscover_moved_session` and its call site at the tail of
   `find_session_for_cwd`. Recovered a session whose worktree had been
   relocated by `git worktree move`, by walking up from cwd to find the
   dispatch marker (which moves with the checkout) and matching its
   task/project back to a registered session — then PERSISTING the
   corrected path, because `teardown_dispatch_settings` removes the marker
   the recovery depends on while leaving the session active.
2. Session-scoped `_source_repo` in `cli/tasks.py` dispatch:
   `get_repo_path(config, project_id) or project.repo_path`, used for both
   the HEAD probe and `create_worktree`. Reverted to `project.repo_path`.

## Design input carried forward (round 7/8 review findings)

- Persistence is REQUIRED, not optional: a read path must write once, or
  the correction dies with the marker at teardown. Round 7 reversed round 6
  on exactly this point.
- Coalesce duplicate session records by path BEFORE the ambiguity guard —
  `register_session` appends a fresh record per dispatch, so a bare
  `len(matches) != 1` made recovery LESS likely the more a task had been
  dispatched.
- Round 8, unaddressed against the removed code: a failed moved-path update
  must not let resolution continue (P1); every coalesced session record
  must be moved, not just the last (P1).
- `_source_repo` driving `create_worktree` breaks re-dispatch of an
  existing task branch: `clawpm/<task>` is already checked out in the
  worktree being branched from (round 8, P2). Probing one checkout while
  branching from another is the mismatch that motivated the change — so
  the two halves cannot be separated, and both need designing together.

## Constraints

- Any recovery must not rebind while the recorded path is still a live
  directory — the session is legitimately active there.
- Cross-project isolation: a marker's project must match the project being
  resolved (see project memory `cross_project_isolation`).
- The walk runs during ORDINARY project resolution, so nothing on the path
  may raise out of `get_project_dir` — read-only commands share it.
  `read_dispatch_marker`'s shape guards stayed in PR #55 for this reason
  and are now tested directly.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

