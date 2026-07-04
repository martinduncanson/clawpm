---
baseline_ref: 5e9294e
created: '2026-07-04'
id: CLAWP-093
predictions:
  approach: 'local_review.py resolves the diff against ''origin'' (upstream malphas-gh)
    by default instead of ''fork'' (martinduncanson/clawpm, the actual PR base) when
    no explicit --pr/base is given, producing a multi-MB diff against a far-behind
    remote. Hit independently by two dispatched subagents (CLAWP-085, CLAWP-072) in
    this same campaign, both had to manually pass --pr <N> to work around it. Fix:
    detect the repo''s actual push/PR remote (or read gh''s configured base) rather
    than assuming ''origin'' is canonical.'
  complexity: s
  confidence: 4
  duration_min: 30
  filled_by: agent
  success_criteria:
  - local_review.py resolves the correct diff base automatically in a fork-primary
    repo without requiring --pr as a workaround
priority: 6
---
# local_review.py --engine grok defaults to wrong diff base in fork-primary repos



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

