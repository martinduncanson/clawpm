---
baseline_ref: fb0918f
complexity: m
created: '2026-10-15'
id: CLAWP-153
predictions:
  complexity: m
  confidence: 2
  duration_min: 180
  filled_by: agent
  pre_mortem: 'Most likely failure: transcripts do not carry reliable edit/test ordering
    metadata, so the measurement is noisy; mitigate by sampling and reporting uncertainty
    rather than building the check.'
  prediction_id: 4580aeb1a04442fca4775035f82d27a5
  success_criteria:
  - WHEN a sample of recorded Stop-hook transcripts is scanned THEN the share containing
    a passing test run older than the last edit is reported with n, and a build/no-build
    decision is recorded on this task
priority: 7
updated: '2026-10-15'
---
# Deterministic evidence-freshness check: last edit vs last passing test run in the Stop-hook transcript (measure prevalence first)

Follow-up from CLAWP-149 spike (PR #118): the Stop-hook transcript builder (src/clawpm/judges/stop_condition.py ~691-724) passes the raw transcript whole and does not filter by freshness; CLAWP-149 only added a prompt rule. First measure how often a Stop-hook transcript holds a passing run that predates the final edit. Only if prevalence is material, add a deterministic last-edit vs last-test-run check before the judge.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

