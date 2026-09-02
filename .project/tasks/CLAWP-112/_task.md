---
baseline_ref: f776124
children:
- CLAWP-112-001
- CLAWP-112-002
- CLAWP-112-003
- CLAWP-112-004
- CLAWP-112-005
- CLAWP-112-006
created: '2026-09-02'
id: CLAWP-112
prd_ref: clawpm-research-prd-prd-clawpm-calibration-metrics-and-miss-
predictions:
  approach: 'Implement docs/design/calibration-metrics-spec.md sections 2.1-2.6 as
    five leaves: ledger + prediction_id (M1), honest actuals (M2), miss_category (M3),
    metrics in reflect summarize + scorecard (M4), nudges (M5), docs (M6). Pre-auth
    lanes are spec-only (2.7) and have no leaf. Sonnet executors, tests pin formulas
    against synthetic corpora.'
  complexity: l
  confidence: 3
  filled_by: agent
  pre_mortem: 'The formulas get implemented against wall-clock duration because active_min
    lands late; sequence M2 in group 1 so M4 can prefer it. Second risk: confidence
    proves to be noise against held - that is a valid finding, not a failure; report
    it, don''t tune the map to hide it.'
priority: 5
updated: '2026-09-02'
---
# Calibration layer: miss categories, real metrics, pre-registered predictions, honest actuals (calibration-metrics-spec 2026-09-02)

## Notes

