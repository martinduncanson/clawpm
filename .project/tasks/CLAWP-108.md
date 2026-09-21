---
baseline_ref: 06a32b7
complexity: s
created: '2026-09-02'
id: CLAWP-108
predictions:
  approach: 'Merge-not-replace semantics: tasks edit should only overwrite prediction
    fields explicitly passed, preserving existing values for the rest (matching from_file''s
    lenient .get() pattern).'
  complexity: s
  confidence: 4
  duration_min: 45
  filled_by: agent
  pre_mortem: 'Risk: if predictions are stored as one opaque block rather than field-level,
    a true merge needs a schema change, not just a CLI fix - check models.py Predictions
    dataclass first'
  success_criteria:
  - tasks edit --hypothesis alone no longer nulls duration/complexity/confidence/pre_mortem/scope/filled_by
priority: 6
tags:
- cli-ergonomics
updated: '2026-09-02'
---
# tasks edit wholesale-replaces predictions block, silently nulling unrelated fields

Issue tracker entry (.agent/issues.jsonl, 2026-07-05T11:04:50Z, medium): editing only --hypothesis on a task nulls duration/complexity/confidence/pre-mortem/scope/filled_by. Silent, no warning - data-loss shape. That entry says 'Fold into CLAWP-096' but CLAWP-096's actual scope (glob-safe --predict-scope, combined duration units, prefix derivation) never picked this up. Filing separately since it's a distinct bug (predictions replace-semantics) from CLAWP-096's parse/derivation fixes.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

