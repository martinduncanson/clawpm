---
baseline_ref: 35abdbd
complexity: m
created: '2026-10-14'
id: CLAWP-151
predictions:
  approach: Add a --task ID option to pr verify that records the verification result
    against the task record, reusing land's verification helpers and the shared done
    validator.
  complexity: m
  duration_min: 180
  files_changed: 4
  files_scope:
  - src/clawpm/pr_verify.py
  - src/clawpm/cli/
  - tests/
  filled_by: agent
  pitfalls: needs task-record writes (concurrency/lock), scope pinning, must not mutate
    before refusal
  prediction_id: a7f530938adc4ccc9e23fb2b863a2cf2
  success_criteria:
  - WHEN pr verify --task ID is run for a merged PR THEN the task record carries the
    verification and a refusal leaves task files byte-identical
priority: 6
updated: '2026-10-14'
---
# tasks land: pr verify --task ID (deferred follow-up of CLAWP-147-002)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

