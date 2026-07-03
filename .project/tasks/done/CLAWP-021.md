---
created: '2026-05-22'
id: CLAWP-021
predictions:
  approach: 'Add parallel_group: int field to TaskFrontmatter. clawpm next --batch
    [N]: select tasks in lowest open group, assert pairwise scope disjoint via existing
    conflict-overlap heuristic generalised to k-way, emit manifest JSON. Topological
    sort: group N tasks must wait for group N-1 to be entirely done (state machine
    check). conflicts --batch wraps the same logic for explicit validation.'
  complexity: m
  confidence: 4
  duration_min: 240
  files_scope:
  - clawpm/models/task.py
  - clawpm/commands/next.py
  - clawpm/commands/conflicts.py
  - tests/test_batch.py
  filled_by: agent
  pitfalls: 'K-way scope-overlap is O(k^2) on the heuristic; fine for k<20. If groups
    grow larger, need optimisation. Also: tasks without parallel_group default to
    group 0; need clear semantics (sequential? always-dispatchable?)'
  pre_mortem: 'Most likely failure: ambiguity on default group for tasks without the
    field. Mitigation: explicit doctrine in SKILL.md — no group = sequential (group=0
    = first batch eligible; absent = excluded from --batch entirely).'
  success_criteria:
  - 'Tasks with parallel_group: 1 dispatchable as a batch; group 2 waits until all
    group 1 are done'
  - Batch with overlapping scope returns conflict error, not partial dispatch
  - clawpm next --batch emits a dispatch manifest consumable by CLAWP-NEW-3
  - Documentation in SKILL.md explains group semantics with worked example
priority: 5
---
# parallel_group: N for subagent dispatch + clawpm next --batch

Add parallel_group: N field to task frontmatter. 'clawpm next --batch' returns the next group whose scope sets are pairwise non-overlapping (assertion, not heuristic) — a dispatch manifest ready to hand to N parallel subagents. 'clawpm conflicts --batch <group>' validates pre-dispatch. Extends scope-aware dispatch from N=2 (pre-flight one task) to N=k (pre-flight a whole group). Pairs with CLAWP-NEW-3 dispatch via hooks: PreToolUse hook on dispatch tool validates group membership pre-flight, blocking conflicting dispatches with decision: block.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

