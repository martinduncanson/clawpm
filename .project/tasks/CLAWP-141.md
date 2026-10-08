---
baseline_ref: '9095538'
created: '2026-10-08'
id: CLAWP-141
predictions:
  complexity: s
  duration_min: 45
  files_changed: 2
  filled_by: agent
priority: 6
updated: '2026-10-08'
---
# get_project_prefix naive [:5] misaligned with allocator prefix (CLAWP-133 follow-up)

Pre-existing gap noted in PR #89 review. get_project_prefix truncates naively with [:5], which can disagree with the allocator-resolved prefix. Align it with the allocator, with a test over colliding and digit-leading project ids.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

