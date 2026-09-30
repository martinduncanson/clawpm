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

**Operator policy decision (2026-09-27): refuse outright on collision** —
matches the auto-numbering path's existing behaviour; warn-and-proceed and
auto-suffix were rejected as options.

## Acceptance Criteria

- [x] `check_explicit_id_prefix_collision` derives the explicit id's prefix
  (`_PREFIX_NUM_RE`) and, under the portfolio lock, compares it against
  every OTHER project's real (explicit `task_prefix` or inferred-from-tasks)
  prefix — raising `ValueError` on a match.
- [x] An id matching THIS project's own established prefix, or a project's
  own first explicit-ID mint (no other project has claimed that prefix
  yet), is allowed through — not a false positive.
- [x] An explicit id that doesn't match the `PREFIX-NNN` shape skips
  validation (nothing to compare) rather than being rejected.
- [x] 5 new tests (`tests/test_clawp129_explicit_id_prefix_collision.py`)
  covering: explicit-prefix collision, inferred-prefix collision, own-prefix
  match, own-first-mint-unclaimed, malformed-id skip. Full suite green
  (1708 passed).

## Notes

Implementation lives in `src/clawpm/tasks.py`: `_prefix_from_explicit_id`
+ `check_explicit_id_prefix_collision`, called from `add_task`'s `else`
branch (the explicit-ID path) alongside the existing CLAWP-051 same-project
clobber guard. Reuses `resolve_existing_prefix` / `discover_projects` —
the same portfolio-scan primitives `assign_all_prefixes` already uses for
the auto-ID path — so the two paths can't disagree about what counts as a
"real" claim.

