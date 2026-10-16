---
baseline_ref: 1338bc6
complexity: m
created: '2026-10-15'
id: CLAWP-158
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: touching ten writers at once with no shared helper;
    mitigate by one atomic_write helper used by all, with a collision test per site'
  prediction_id: b9244190eeab4c179e73e57312dc7770
  success_criteria:
  - WHEN two writers rewrite different task or mission files in one directory concurrently
    THEN neither loses its temp file and no leftover .tmp remains
priority: 6
updated: '2026-10-16'
---
# Atomic-write hygiene: replace shared with_suffix(.tmp) writers with unique temp files

Source: PR #122 gate r1 P1 (fixed there for the decision writers only). Pre-existing writers using with_suffix(.tmp) or an id.tmp name: src/clawpm/tasks.py near lines 957, 1018 and 3685, and src/clawpm/mission.py near lines 337, 462 and 547. Extract one atomic_write helper (mkstemp in the same directory, cleanup on every failure path, atomic replace) and migrate them. Re-grep for the pattern first; line numbers are from 2026-10-15.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

