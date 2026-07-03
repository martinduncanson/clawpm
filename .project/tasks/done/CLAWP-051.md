---
complexity: l
created: '2026-06-10'
id: CLAWP-051
predictions:
  approach: 'Make two agent sessions in the same repo/task-tree collision-safe. Today
    JSONL appends are locked (concurrency.py) but task-ID allocation (scan+create)
    and task-state transitions (file edit/move) are racy. Add a critical-section lock
    (dedicated .lock file, reuse the fcntl/msvcrt pattern generalised beyond append)
    around: (1) allocate-and-create in add_task, (2) state transition file moves.
    Builds on CLAWP-048 unique-prefix. Document the supported multi-session model
    (branch + scope-claim + conflicts pre-flight + locks).'
  confidence: 2
  duration_min: 1440
  files_scope:
  - src/clawpm/tasks.py
  - src/clawpm/concurrency.py
  filled_by: agent
  pre_mortem: 'Most likely failure: lock granularity wrong - too coarse (serialises
    all clawpm ops, kills parallelism) or too fine (misses the scan+create TOCTOU
    window).'
  reference_tasks:
  - CLAWP-048
  - CLAWP-032
  success_criteria:
  - concurrent add_task from two processes never allocate the same ID (test with real
    subprocess/thread contention)
  - concurrent state transition on the same task is serialised, not last-write-wins;
    supported multi-session model documented in SKILL.md
priority: 5
---
# Multi-session concurrency: lock ID allocation + state transitions for two-sessions-same-tree safety



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

