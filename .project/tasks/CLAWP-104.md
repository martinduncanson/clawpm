---
baseline_ref: b675a5e
created: '2026-07-31'
id: CLAWP-104
predictions:
  approach: Audit first, propose mapping doc, then implement taxonomy field + summarize
    metrics; ledger migration only if cheap
  complexity: l
  confidence: 2
  duration_min: 240
  filled_by: operator-edited
  pre_mortem: 'If this fails: clawpm Phase 2 architecture diverges enough from the
    spec''s ledger model that retrofitting costs more than the calibration signal
    is worth - in which case ship the metrics on the existing event shape'
  unknowns: Whether existing reflection JSONLs have enough resolved predictions to
    compute meaningful baselines
priority: 5
updated: '2026-07-31'
---
# Audit self-improvement spec (Q-agent) for clawpm calibration implementation

Source: F:/Git/.harness-configs/distilled/self-improvement-spec.md (2026-04-18, operator-directed universal 8-step predict-measure-diverge-reflect-metareflect-patch loop). Audit clawpm reflection layer (Phase 1.5/1.6 predictions, surprise taxonomy, reflect void, Phase 2 stubs) against the spec and propose/implement: (1) miss-category taxonomy (wrong_signal / wrong_weight / missed_variable / operator_override / noise / structural) as a first-class field alongside or replacing surprise tags - forces root-cause classification, includes the noise-is-valid rule and repeat-category-triggers-metareflection rule; (2) calibration metric set for reflect summarize (Brier, MAE, overconfidence ratio = hit-rate of 0.9-conf / 0.9, systematic bias, learning velocity = Brier slope over 30/90/365d, miss-category distribution); (3) two-line append-only ledger pattern (prediction line + outcome line sharing prediction_id) vs current single-event reflections - assess migration; (4) pre-authorisation lanes concept (quantified auto-apply thresholds per domain) as a future clawpm-policy feature - spec only. Cross-links: cognition-layer COGNI-007 (calibration engine resurrection - this spec is its design doc) and gbrain takes_calibration (dedupe metric definitions so clawpm and gbrain compute calibration identically).

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

