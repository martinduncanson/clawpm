---
created: '2026-05-24'
id: CLAWP-027
predictions:
  complexity: m
  confidence: 3
  duration_min: 180
  filled_by: agent
  predicted_iterations: 2
priority: 5
---
# CodeGraph: auto-populate --predict-scope from NLP task description

When tasks add gets -t/-b text without --predict-scope AND .codegraph/ exists, run codegraph search/context against title+body, suggest files_scope globs. Surface as suggested_scope in the response.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

