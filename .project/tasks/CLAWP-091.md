---
baseline_ref: 8606b04
created: '2026-07-04'
id: CLAWP-091
predictions:
  approach: Guard mutation sites (edit_task etc.) against non-dict parsed frontmatter,
    matching the pre-existing safe_load(...) or {} exposure surfaced during CLAWP-079
  complexity: s
  confidence: 3
  duration_min: 60
  filled_by: agent
  success_criteria:
  - Mutation sites raise a friendly error (not raw TypeError) when frontmatter parses
    to a non-dict
priority: 6
---
# Non-dict frontmatter can TypeError at mutation sites



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

