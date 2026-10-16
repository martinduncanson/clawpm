---
baseline_ref: 1338bc6
complexity: s
created: '2026-10-15'
id: CLAWP-156
predictions:
  complexity: s
  confidence: 3
  duration_min: 90
  filled_by: agent
  pre_mortem: 'Most likely failure: clearing in the shared state-change path changes
    the signature or behaviour of change_task_state for unrelated callers; mitigate
    by clearing in one narrow hook inside the existing transition and testing every
    state edge'
  prediction_id: 6094f49c65f94703b5a0f1eb1fc72a76
  success_criteria:
  - WHEN a task with a recorded question is moved open then blocked via tasks state
    (not the block shortcut) THEN tasks list --needs-human does not show the stale
    question
priority: 6
updated: '2026-10-16'
---
# CLAWP-154 follow-up: close the tasks state open->blocked stale-question bypass

Source: PR #122 (CLAWP-154) gate r1 and author doubt. block and unblock shortcuts clear a stale decision record, but a direct tasks state <id> open followed by tasks state <id> blocked bypasses them. Find the single shared state-change path and clear the structured fields on leaving blocked. Do not change change_task_state signature.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

