---
baseline_ref: af521fd
created: '2026-09-17'
id: CLAWP-119
predictions:
  complexity: s
  duration_min: 30
  filled_by: agent
  hypothesis: 'Filing only -- the code change already shipped in commit 6f176f0 on
    the clawp096-cli-ergonomics branch (PR #57).'
  success_criteria:
  - 'Task filed and linked to commit 6f176f0 / PR #57 for provenance; no code change
    required'
priority: 5
updated: '2026-10-07'
---
# Remove digest-fallback prefix synthesis from assign_task_prefix

assign_task_prefix previously synthesised a digest-derived prefix when no explicit task_prefix was set and the naive prefix collided. It produced a finding in 5 consecutive PR #57 review rounds (6-10) because its length budget can't be tuned correct: emit_tree mints child ids recursively with no depth cap, so any fixed suffix reserve is a wall at some depth. Fixed in commit 6f176f0 (PR #57, CLAWP-096 branch) by removing the digest arm entirely -- assign_task_prefix now raises with an actionable message (set an explicit task_prefix) instead of synthesising one. This task exists to give that commit a task reference; it was cited in the commit message and test comments before being filed.

## Acceptance Criteria

- [x] Change is on main: PR #57 squash-merged as 9ae813a (ancestor of fork/main, verified 2026-10-07 with
  `git merge-base --is-ancestor`); the branch commit 6f176f0 is not itself an ancestor because of the squash.
- [x] `assign_task_prefix` (src/clawpm/tasks.py) has no digest-fallback arm and raises with the explicit-`task_prefix` remedy.
- [x] Covered by tests citing CLAWP-119 (tests/test_task_id_allocation.py, tests/test_phase16.py).

## Notes

Closed 2026-10-07 as filing-only: no code change in this task.

