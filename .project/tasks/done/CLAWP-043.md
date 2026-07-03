---
complexity: s
created: '2026-06-05'
id: CLAWP-043
predictions:
  approach: Make prior_reason exposure optional on build_refutation_prompt, default
    blind; thread refuter_sees_prior (env CLAWPM_REFUTER_SEES_PRIOR) through evaluate_stop_condition_confirmed.
    Removes the mutual-softening anchor the LinkedIn adversarial-review piece warns
    against.
  confidence: 4
  duration_min: 120
  files_changed: 2
  files_scope:
  - src/clawpm/judges/stop_condition.py
  - tests/test_confirm_close.py
  filled_by: agent
  hypothesis: If the refuter no longer sees the base judge's passing rationale, it
    stops anchoring toward agreement and the close gate strengthens (bias-to-refute
    preserved).
  pre_mortem: 'Most likely failure: a refuter that needs prior_reason to disambiguate
    an ambiguous criterion now refutes on a different reading, raising false-block
    churn.'
  reference_tasks:
  - CLAWP-041
  success_criteria:
  - build_refutation_prompt omits the prior-reason line when include_prior_reason=False
    (default)
  - evaluate_stop_condition_confirmed defaults to blind refuter; CLAWPM_REFUTER_SEES_PRIOR=1
    restores the legacy anchored prompt
  - new tests assert both prompt modes; full test suite green
priority: 5
---
# Blind the confirm-close refuter to the base judge's prior_reason (anchoring fix)



## Acceptance Criteria

- [ ] (Add criteria here)

## Notes

