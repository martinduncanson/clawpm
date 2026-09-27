---
baseline_ref: 54c49b4
created: '2026-09-27'
id: CLAWP-128
predictions:
  approach: Extend _resolve_idempotency's traversal to also glob {state_dir}/{parent_id}-*/_task.md
    (directory-shaped children), mirroring how _existing_child_ordinals already handles
    this (tasks.py ~2245-2247) -- reference pattern, not a new mechanism
  complexity: m
  confidence: 3
  duration_min: 90
  filled_by: agent
  pre_mortem: Fixing only rejected/ and missing that done/blocked/archive share the
    identical blind spot, since the glob is flat-file-only regardless of which terminal
    state
  reference_tasks:
  - CLAWP-127
  success_criteria:
  - A previously-emitted child that is itself a directory task (has its own children,
    stored as <state-dir>/<child_id>/_task.md) is recognised by _resolve_idempotency
    after moving to done/blocked/rejected/archive, with a regression test using that
    exact shape
priority: 3
updated: '2026-09-27'
---
# emit-tree idempotency blind to a directory-shaped (has-its-own-children) rejected/done/blocked child



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

