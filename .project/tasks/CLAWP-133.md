---
baseline_ref: c402a17
created: '2026-10-04'
id: CLAWP-133
predictions:
  complexity: m
  confidence: 3
  duration_min: 120
  filled_by: agent
  pre_mortem: loosening the regex creates ambiguity in another id parser
  success_criteria:
  - taskless project 2-b mints <P>-000 then <P>-001 with inference returning <P>
priority: 5
updated: '2026-10-04'
---
# Digit-leading project ids get a task prefix that can never be re-inferred

## Problem

A project whose id starts with a digit (`2-b`, `2024`, `9-9`) gets a derived task prefix that also starts with a digit (`2B`, `2024`, `99`). `_PREFIX_NUM_RE` (`^([A-Z][A-Z0-9-]*?)-(\d+)...`) requires a leading letter. So `_infer_prefix_from_tasks` can never read the prefix back from the project's own files. The project looks taskless on every mint and is re-resolved from scratch each time. It could be handed a different prefix later, or have its prefix taken by a sibling.

This predates CLAWP-132. PR #70 (merged c402a17) corrected the docstrings that claimed the allocator only hands out re-inferable prefixes, and recorded this as a known gap. Found by antigravity and the generic pre-review on PR #70, 2026-10-04.

## Options (decide before building)

- Force a leading letter on derived candidates, e.g. prefix with `P` (`2-b` → `P2B`). Must stay stable and re-inferable, and must not collide.
- Loosen `_PREFIX_NUM_RE` to accept a leading digit. Check every parser of task ids (subtask split, explicit-id collision checks, emit_tree, doctor) for ambiguity.
- Refuse loudly at project creation or first mint, and require an explicit `task_prefix`.

Operator preference (memory `feedback-prefer-compat-over-hard-fail-clawpm`): existing projects must keep working; a loud error is acceptable for new-project-only edge cases.

## Acceptance criteria

- A test with taskless project `2-b`: first `add_task` mints `<P>-000`, `_infer_prefix_from_tasks` returns `<P>`, and the second mint is `<P>-001`.
- The same for `2024`.
- Full suite passes.


## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

