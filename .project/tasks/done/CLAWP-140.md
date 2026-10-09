---
baseline_ref: '9095538'
created: '2026-10-08'
id: CLAWP-140
predictions:
  complexity: s
  duration_min: 45
  files_changed: 2
  filled_by: agent
  pitfalls: ids must be tested in the shapes the allocator really mints
priority: 5
updated: '2026-10-09'
---
# expand_task_id regex mis-reads digit-leading / multi-hyphen full ids (CLAWP-133 follow-up)

Pre-existing gap noted in PR #89 review. context.py expand_task_id uses ^[A-Z]+-\d+, which mis-reads full ids whose prefix is digit-leading or contains hyphens (P2-B-001, 2024-001). Fix with a test using realistic auto-minted ids from the allocator.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

