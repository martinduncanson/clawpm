---
created: '2026-05-24'
id: CLAWP-030
predictions:
  complexity: m
  confidence: 3
  duration_min: 240
  filled_by: agent
  predicted_iterations: 2
priority: 5
---
# CodeGraph: reference-task scoring augmented with semantic symbol overlap

find_reference_tasks adds a semantic_overlap score component when codegraph is available. Resolve target task symbols ONCE via codegraph search on predicted scope; intersect vs each candidate symbol set. +1 per shared symbol capped at +4.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

