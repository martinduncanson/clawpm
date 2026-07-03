---
baseline_ref: 84a3869
complexity: s
created: '2026-06-12'
id: CLAWP-063
predictions:
  complexity: s
  confidence: 4
  duration_min: 60
  filled_by: agent
  reference_tasks:
  - CLAWP-055
  success_criteria:
  - an error-class drift skip surfaces a drift-not-checked warning at dispatch; expected
    skips (no-scope/no-baseline/ts/non-git) stay silent — tests distinguish the two
    classes
priority: 6
---
# Surface drift-not-checked marker when CLAWP-055 drift gate skips on git ERROR (fail-open not fail-silent)

CLAWP-055 drift gate fails OPEN (skips + proceeds) on subprocess/git error — correct for availability but currently SILENT, so the operator cannot tell a clean check from one that did not run. Per fail-open-not-fail-silent, emit a drift-not-checked marker ONLY for the ERROR-class skip (subprocess failure, unverifiable/force-pushed ref). The legitimate expected skips (no scope, no baseline, ts: marker, non-git) stay silent — do not warn or it becomes noise. Touches baseline.py (distinguish error-skip from expected-skip) + cli.py dispatch gate.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

