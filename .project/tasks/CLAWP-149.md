---
baseline_ref: 00fc6fd
created: '2026-10-14'
id: CLAWP-149
predictions:
  complexity: s
  confidence: 3
  duration_min: 90
  filled_by: agent
  pre_mortem: Prompt-only change may not alter judge behaviour; the spike decides
    whether a deterministic check is needed.
  prediction_id: c352879686154589a7a121b018fddc95
  success_criteria:
  - comparator: substring present
    criterion: WHEN grading instructions and refuter prompt are rendered THEN both
      contain the freshness rule
    gradeable_signal: pytest asserts on rendered text
priority: 6
scope:
- src/clawpm/rubric.py
- src/clawpm/judges/stop_condition.py
updated: '2026-10-14'
---
# Rubric evidence freshness: a gradeable signal only counts if produced after the last edit (prompt line + spike)

From the Bello audit (.project/research/2026-10-14_makson179-bello-audit-review-prompts-and-handoff-fields.md, items 4 and spike). Add to _grading_instructions and the refuter EVIDENCE lens: a check counts only if it ran after the last relevant edit; lint/build/type checks are hygiene, not behavioural evidence. Test the rendered text. Then spike: measure how often a Stop-hook transcript holds a passing run that predates the final edit before building a deterministic check. Unverified: whether the transcript builder already filters by freshness.

## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

