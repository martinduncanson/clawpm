---
baseline_ref: e44c6f8
created: '2026-09-27'
id: CLAWP-129
predictions:
  approach: Under the portfolio lock (now held unconditionally per CLAWP-116 for every
    add_task call), after resolving an explicit task_id's derived prefix, check it
    against assign_all_prefixes' real claims (the same used-set assign_task_prefix
    already computes) and apply whatever policy is chosen
  complexity: m
  confidence: 2
  duration_min: 120
  filled_by: agent
  pre_mortem: Treating this as a simple validation bolt-on without first deciding
    the POLICY question (refuse vs warn vs auto-suffix) - that is a design call for
    the operator, not a mechanical fix
  reference_tasks:
  - CLAWP-116
  success_criteria:
  - An explicit task_id create whose derived prefix collides with another project's
    real (explicit or inferred) prefix is either refused with an actionable error,
    or the collision is surfaced to the operator -- design the exact refusal/warn
    policy before implementing, since it changes observable behaviour for a previously-silent
    path
priority: 4
updated: '2026-09-27'
---
# explicit-ID task creates are never validated against the portfolio's real prefix claims

Note: minted as CLAWP-128 initially, then renamed to CLAWP-129 before commit
— this repo's own `clawpm/CLAWP-127` branch (open, unmerged PR #62 as of
2026-09-27) already used CLAWP-128 for a different follow-up
(emit-tree idempotency directory-shaped-child gap), and main's own
numbering scan can't see an unmerged branch's task files. Renamed here to
avoid a collision once that branch merges.



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

