---
baseline_ref: 0b307f2
complexity: s
created: '2026-07-03'
id: CLAWP-080
predictions:
  confidence: 4
  duration_min: 120
  filled_by: agent
  success_criteria:
  - tasks list -s open does not read or YAML-parse files under done/ or blocked/
  - get_next_task benefits from the same skip
  - 'Benchmark note in the task: before/after wall time on a portfolio with 500+ done
    tasks (synthetic fixture acceptable)'
  - Full suite passes
priority: 5
scope:
- src/clawpm/tasks.py
---
# Scan performance: stop parsing state-excluded dirs in list_tasks

Audit 2026-07-03 (code-health): list_tasks (tasks.py:68) reads + YAML-parses EVERY task file per call and always scans done/ + blocked/ (tasks.py:86-90) even for --state open. get_next_task and reflect scans share the shape. With no done-task archival, done/ grows unboundedly and every tasks list / next pays O(n) file IO + YAML parse.

SPEC: derive the directory set from the state filter and skip excluded dirs entirely. Keep it simple - do NOT build an index/cache in this task (that is a later call if prefix-skip proves insufficient; see also the done-task archive task, which attacks the same cost from the other end).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

