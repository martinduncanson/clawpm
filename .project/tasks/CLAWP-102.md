---
baseline_ref: e2a391a
created: '2026-07-10'
id: CLAWP-102
predictions:
  approach: 'CLAWP-088''s own added-scope note asked for this but it shipped without
    it: a CI step (or clawpm doctor --strict rule) that diffs README''s All-commands
    tables against clawpm introspect output, failing on a shipped command/flag with
    no README mention. Keep narrative sections hand-authored; only gate the mechanical
    reference tables. introspect (CLAWP-088) now exists and is exactly the ground
    truth needed -- this is a clean, small follow-up, not blocked on anything else.'
  complexity: s
  confidence: 4
  duration_min: 90
  filled_by: agent
  success_criteria:
  - CI (or doctor --strict) fails when a command/flag exists in introspect output
    but not in README's All-commands section
  - Passes cleanly against current README (post CLAWP-097)
priority: 6
updated: '2026-07-10'
---
# Wire doc-staleness CI check against introspect --json output



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

