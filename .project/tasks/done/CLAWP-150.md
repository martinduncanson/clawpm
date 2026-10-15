---
baseline_ref: 35abdbd
complexity: s
created: '2026-10-14'
id: CLAWP-150
predictions:
  approach: Route the by_complexity/by_confidence/by_agent_profile median_ratio slices
    and the reflect suggest deflator through the single calibration._bias function
    so even-n buckets use the geometric midpoint; keep n>=5/n>=20 fall-back rules.
  complexity: s
  duration_min: 90
  files_changed: 4
  files_scope:
  - src/clawpm/reflect.py
  - src/clawpm/calibration.py
  - tests/
  filled_by: agent
  pitfalls: reflect suggest consumers may depend on arithmetic medians; golden tests
    need recomputing
  prediction_id: 5bf907ec8a3f427d8610489b1cd08aa7
  success_criteria:
  - WHEN a bucket has equal groups at ratios 0.25 and 1 THEN summarize median_ratio
    and reflect suggest report 0.5 not 0.625
priority: 6
resolution: 'Legacy median_ratio slices and reflect suggest use the geometric median
  via calibration._median_factor (PR #115)'
resolved_at: '2026-10-15'
updated: '2026-10-15'
---
# Migrate legacy median_ratio slices and reflect suggest to exp(median(ln r)) (spec 2.3 bias)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

