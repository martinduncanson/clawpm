---
created: '2026-05-24'
id: CLAWP-031
predictions:
  complexity: s
  confidence: 4
  duration_min: 60
  filled_by: agent
  predicted_iterations: 1
priority: 5
---
# CodeGraph: doctor advisory for code-bearing projects without .codegraph/

Add a doctor check: for each project with repo_path AND code-language files greater than 50, if .codegraph/ does not exist, surface codegraph_not_initialized soft advisory. Operator-judgment class — not auto-applyable by doctor --apply.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

